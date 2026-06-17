"""Ingest one flight's review queue into Postgres + chip storage.

Consumes the active-learning pipeline outputs (see ingest/contract.py):
  review gpkg   build_abstain_review_polygons() -- one row per promoted container
  ortho         multiband ortho, for rendering chips
  superpixel    uint32 superpixel-id raster (pixel -> container)
  softmax       optional per-class probability raster (for model_probs)

For each review container it renders the configured chip views around the
container (plus context padding), uploads them, computes the model's mean
probabilities, scores serving priority, and inserts a `containers` row.

Run:
    python -m ingest.ingest_flight --config flights/example.yaml

Rounds: each retrain is a new round. Set `round` in the config (or pass
--round N). Ingesting a round replaces just that round's containers and points
the flight's queue at it; superpixels that already carry a gold verdict are
skipped (settled, never re-asked). Labels/verdicts key on the superpixel, so
none of this disturbs answers already collected.

Heavy geo deps (rasterio/geopandas/matplotlib) are imported lazily so the rest
of the package stays importable without them.
"""
from __future__ import annotations

import argparse
import os
import tempfile

import yaml

from app.constants import DEFAULT_SCHEME, pair_priority
from app.db import SessionLocal
from app.models import Container, Flight, Project, ResolvedLabel
from app.storage import put_chip

from .contract import REQUIRED_REVIEW_COLUMNS, FlightInputs, ViewSpec
from .render import render_views


def load_inputs(config_path: str) -> FlightInputs:
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    raw["views"] = [ViewSpec(**v) for v in raw.get("views", [])]
    return FlightInputs(**raw)


def _mean_probs(softmax_path: str, superpixel_path: str, superpixel_id: int):
    """Mean per-class probability over one container's pixels: {class_id: prob}.
    Assumes softmax band i (1-based) holds class id i-1."""
    import numpy as np
    import rasterio

    with rasterio.open(superpixel_path) as sp:
        sp_arr = sp.read(1)
    mask = sp_arr == superpixel_id
    if not mask.any():
        return None
    with rasterio.open(softmax_path) as sm:
        return {
            b - 1: float(sm.read(b)[mask].mean()) for b in range(1, sm.count + 1)
        }


def _i(v):
    return None if v is None else int(v)


def _f(v):
    return None if v is None else float(v)


def _srid(crs: str) -> int:
    return int(crs.split(":")[-1])


def _resolve_scheme(classes: dict | None) -> dict:
    """Normalize a flight's class scheme; fall back to the synthetic default.
    YAML/JSON makes dict keys strings, so coerce the `names` keys back to int ids
    and the damage list to ints. No class id is assumed -- whatever the flight
    declares is what we store and serve by."""
    scheme = classes or DEFAULT_SCHEME
    names = {int(k): v for k, v in scheme.get("names", {}).items()}
    return {
        "names": names,
        "damage": [int(c) for c in scheme.get("damage", [])],
        "ignore_index": int(scheme.get("ignore_index", 255)),
    }


def ingest(config_path: str, round_override: int | None = None) -> None:
    import geopandas as gpd
    import rasterio

    inp = load_inputs(config_path)
    if round_override is not None:
        inp.round = round_override
    gdf = gpd.read_file(inp.review_gpkg)
    missing = [
        c for c in REQUIRED_REVIEW_COLUMNS if c != "geometry" and c not in gdf.columns
    ]
    if missing:
        raise ValueError(f"review gpkg is missing required columns: {missing}")

    session = SessionLocal()
    try:
        scheme = _resolve_scheme(inp.classes)
        rnd = int(inp.round)

        project = None
        if inp.project:
            project = session.query(Project).filter_by(name=inp.project).one_or_none()
            if project is None:
                project = Project(name=inp.project)
                session.add(project)
                session.flush()

        flight = session.query(Flight).filter_by(name=inp.name).one_or_none()
        if flight is None:
            flight = Flight(
                name=inp.name,
                project_id=project.id if project else None,
                crs=inp.crs,
                gsd_cm=inp.gsd_cm,
                ortho_path=inp.ortho_path,
                superpixel_path=inp.superpixel_path,
                abstain_path=inp.abstain_path,
                class_scheme=scheme,
                active_round=rnd,
            )
            session.add(flight)
            session.flush()
        else:
            if project is not None:
                flight.project_id = project.id
            # Re-ingesting this round (or advancing to a new one): clear just this
            # round's containers and serve it. Labels/verdicts are keyed to the
            # superpixel, so this never touches answers.
            session.query(Container).filter_by(flight_id=flight.id, round=rnd).delete()
            flight.active_round = rnd

        # Superpixels already settled by a gold verdict are done -- don't bother
        # rendering chips or queuing questions for them in this (or any) round.
        settled = {
            sp
            for (sp,) in session.query(ResolvedLabel.superpixel_id)
            .filter(
                ResolvedLabel.flight_id == flight.id,
                ResolvedLabel.class_id.isnot(None),
            )
            .all()
        }

        # serve by whatever THIS flight calls damage (existing flight wins).
        damage = set((flight.class_scheme or scheme).get("damage", []))
        # context padding in map units, from the ortho's pixel size (once).
        with rasterio.open(inp.ortho_path) as o:
            pad = inp.context_pad_px * abs(o.res[0])

        srid = _srid(inp.crs)
        n_ingested = 0
        n_skipped = 0
        for _, row in gdf.iterrows():
            sp_id = int(row["superpixel_id"])
            if sp_id in settled:
                n_skipped += 1
                continue
            abstain_frac = float(row.get("abstain_frac") or 0.0)
            is_diffuse = bool(row.get("is_diffuse"))
            class_a = None if is_diffuse else _i(row.get("class_a"))
            class_b = None if is_diffuse else _i(row.get("class_b"))

            minx, miny, maxx, maxy = row.geometry.bounds
            with tempfile.TemporaryDirectory() as tmp:
                chip_keys = render_views(
                    inp.views,
                    inp.ortho_path,
                    (minx - pad, miny - pad, maxx + pad, maxy + pad),
                    tmp,
                    f"{inp.name}/r{rnd}/sp_{sp_id}",
                    put_chip,
                )

            probs = (
                _mean_probs(inp.softmax_path, inp.superpixel_path, sp_id)
                if inp.softmax_path
                else None
            )

            session.add(
                Container(
                    flight_id=flight.id,
                    round=rnd,
                    superpixel_id=sp_id,
                    geom=f"SRID={srid};{row.geometry.wkt}",
                    n_pixels=_i(row.get("n_pixels")) or 0,
                    abstain_frac=abstain_frac,
                    pair_purity=_f(row.get("pair_purity")),
                    diffuse_frac=_f(row.get("diffuse_frac")),
                    pair_code=_i(row.get("pair_code")),
                    class_a=class_a,
                    class_b=class_b,
                    is_diffuse=is_diffuse,
                    model_probs=probs,
                    chip_keys=chip_keys,
                    priority=pair_priority(class_a, class_b, abstain_frac, damage),
                    replication_target=inp.replication_target,
                )
            )
            n_ingested += 1
        session.commit()
        where = f"project '{inp.project}' / " if inp.project else ""
        print(
            f"Ingested {n_ingested} containers for {where}flight '{inp.name}' "
            f"round {rnd} (skipped {n_skipped} already-settled superpixels)."
        )
    finally:
        session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="YAML flight config (see flights/)")
    ap.add_argument(
        "--round",
        type=int,
        default=None,
        help="active-learning round number (overrides the config's `round`). "
        "Re-running a round replaces just that round's containers.",
    )
    args = ap.parse_args()
    ingest(args.config, round_override=args.round)

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


_DIFFUSE_CODE = 100  # matches abstain.py


def _pair_codes(n_classes):
    """code (1..C(n,2)) -> (a, b), a < b. Mirrors abstain.py._pair_codes."""
    import itertools

    return {k + 1: pair for k, pair in enumerate(itertools.combinations(range(n_classes), 2))}


def _load_stat_rasters(softmax_path, abstain_path, superpixel_path):
    """Read the rasters needed for per-superpixel stats ONCE (not per superpixel).
    Returns (softmax (C,H,W), abstain (H,W)|None, superpixel (H,W), valid (H,W),
    pred (H,W)), or None when there's no softmax."""
    import numpy as np
    import rasterio

    if not softmax_path:
        return None
    with rasterio.open(superpixel_path) as sp:
        sp_arr = sp.read(1)
    with rasterio.open(softmax_path) as sm:
        sm_arr = sm.read().astype("float32")  # (C, H, W)
    ab_arr = None
    if abstain_path:
        with rasterio.open(abstain_path) as ab:
            ab_arr = ab.read(1)
    valid = np.isfinite(sm_arr).all(axis=0) & (sm_arr.sum(axis=0) > 0.5)
    pred = np.argmax(sm_arr, axis=0)  # confident class per pixel
    return sm_arr, ab_arr, sp_arr, valid, pred


def _pixel_stats(rasters, sp_id):
    """(model_probs, composition) for one superpixel, from the once-loaded
    rasters. model_probs = mean per-class softmax over the whole superpixel.
    composition = the confident-vs-abstain decomposition the labeler sees:
    total pixels, confident-class counts, and the abstain "questions" (contested
    pair counts) + diffuse count -- the honest breakdown, not a blended mean."""
    import numpy as np

    sm_arr, ab_arr, sp_arr, valid, pred = rasters
    C = sm_arr.shape[0]
    mask = sp_arr == sp_id
    if not mask.any():
        return None, None

    probs = {k: float(sm_arr[k][mask].mean()) for k in range(C)}
    if ab_arr is None:
        return probs, None

    codes = ab_arr[mask]
    vmask = valid[mask]
    confident = (codes == 0) & vmask          # confident & real (not nodata)
    diffuse = codes == _DIFFUSE_CODE
    pair = (codes >= 1) & (codes < _DIFFUSE_CODE)

    conf_counts = np.bincount(pred[mask][confident], minlength=C)
    confident_list = sorted(
        ({"c": int(k), "n": int(v)} for k, v in enumerate(conf_counts) if v),
        key=lambda x: -x["n"],
    )

    code_to_pair = _pair_codes(C)
    qcounts = np.bincount(codes[pair], minlength=_DIFFUSE_CODE + 1)
    questions = sorted(
        (
            {"a": int(code_to_pair[code][0]), "b": int(code_to_pair[code][1]),
             "n": int(qcounts[code])}
            for code in range(1, _DIFFUSE_CODE)
            if qcounts[code] and code in code_to_pair
        ),
        key=lambda x: -x["n"],
    )

    composition = {
        "n": int(mask.sum()),
        "n_confident": int(confident.sum()),
        "n_abstain": int(pair.sum()),
        "n_diffuse": int(diffuse.sum()),
        "confident": confident_list,
        "questions": questions,
    }
    return probs, composition


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

    from ingest.preflight import check_inputs
    problems = check_inputs(inp)
    if problems:
        raise FileNotFoundError(
            "preflight failed -- fix these before ingest (see RUNBOOK.md):\n  "
            + "\n  ".join(problems)
        )

    selection_params = None
    if inp.selection_params_path:
        import json
        with open(inp.selection_params_path) as f:
            selection_params = json.load(f)

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
                selection_params=selection_params,
                active_round=rnd,
            )
            session.add(flight)
            session.flush()
        else:
            if project is not None:
                flight.project_id = project.id
            # The ingest is the airlock that defines the canonical grid + sources,
            # so refresh the raster paths on the flight even when it already exists
            # (e.g. created by seed.py without them). Verdicts/export read these.
            flight.superpixel_path = inp.superpixel_path
            flight.ortho_path = inp.ortho_path
            flight.abstain_path = inp.abstain_path
            # refresh the queue-explanation only when this ingest supplied one,
            # so a re-ingest without the JSON keeps the prior panel.
            if selection_params is not None:
                flight.selection_params = selection_params
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
                (ResolvedLabel.class_id.isnot(None)) | (ResolvedLabel.out_of_scope.is_(True)),
            )
            .all()
        }

        # serve by whatever THIS flight calls damage (existing flight wins).
        damage = set((flight.class_scheme or scheme).get("damage", []))
        # fixed chip window (map units), centered on each container; clamp to ortho.
        half = inp.window_m / 2.0
        with rasterio.open(inp.ortho_path) as o:
            ob = o.bounds

        srid = _srid(inp.crs)
        # Read the softmax/abstain/superpixel rasters ONCE for per-superpixel
        # stats (mean probs + the confident/abstain composition).
        stat_rasters = _load_stat_rasters(
            inp.softmax_path, inp.abstain_path, inp.superpixel_path
        )
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

            c = row.geometry.centroid
            wb = (max(ob.left, c.x - half), max(ob.bottom, c.y - half),
                  min(ob.right, c.x + half), min(ob.top, c.y + half))
            with tempfile.TemporaryDirectory() as tmp:
                chip_keys = render_views(
                    inp.views,
                    inp.ortho_path,
                    wb,
                    tmp,
                    f"{inp.name}/r{rnd}/sp_{sp_id}",
                    put_chip,
                    outline_path=inp.superpixel_path,
                    outline_id=sp_id,
                )

            probs, composition = (
                _pixel_stats(stat_rasters, sp_id) if stat_rasters else (None, None)
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
                    composition=composition,
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

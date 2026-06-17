"""Seed a demo flight with placeholder chips, so you can click through the UI
before any real ingest. The chips are generated colored tiles, NOT real imagery
-- just enough to exercise the labeling flow end to end.

Run (with the DB up and migrated):
    python seed.py            create/refresh the demo flight
    python seed.py --wipe     delete it first
"""
from __future__ import annotations

import argparse
import os
import tempfile

from app.constants import DEFAULT_SCHEME, pair_priority
from app.db import SessionLocal
from app.models import Container, Exemplar, Flight, Project
from app.storage import put_chip

PROJECT = "Wellfleet"
FLIGHT = "demo_synthetic"
VIEWS = ["truecolor", "cir", "geomorphic"]
# a spread of contested pairs to show different question types
PAIRS = [(2, 4), (1, 3), (3, 4), (3, 5), (1, 2)]
VIEW_TINT = {"truecolor": "#5b7553", "cir": "#7a3b3b", "geomorphic": "#3f5870"}
CLASS_TINT = {0: "#6b7280", 1: "#4e8a6b", 2: "#b08948",
              3: "#c2632f", 4: "#9c3f6d", 5: "#7b5ea7"}


def _tile(text: str, hexcolor: str, path: str) -> None:
    """Render a flat colored tile with a label -- a stand-in for a real chip."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(3, 3), dpi=100)
    fig.patch.set_facecolor(hexcolor)
    ax.set_facecolor(hexcolor)
    ax.text(0.5, 0.5, text, ha="center", va="center", color="white",
            fontsize=12, wrap=True, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.savefig(path, facecolor=hexcolor, bbox_inches="tight", pad_inches=0)
    plt.close(fig)


def _chips(prefix: str, label: str, tint_by_view) -> dict:
    keys = {}
    with tempfile.TemporaryDirectory() as tmp:
        for v in VIEWS:
            local = os.path.join(tmp, f"{v}.png")
            _tile(f"{label}\n{v}", tint_by_view(v), local)
            keys[v] = put_chip(local, f"{prefix}/{v}.png")
    return keys


def seed(wipe: bool = False) -> None:
    names = DEFAULT_SCHEME["names"]
    damage = DEFAULT_SCHEME["damage"]
    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(name=PROJECT).one_or_none()
        if project is None:
            project = Project(name=PROJECT, notes="Demo project for the synthetic flight.")
            db.add(project)
            db.flush()

        flight = db.query(Flight).filter_by(name=FLIGHT).one_or_none()
        if flight is not None and wipe:
            db.delete(flight)
            db.commit()
            flight = None
        if flight is None:
            flight = Flight(
                name=FLIGHT,
                project_id=project.id,
                crs="EPSG:26919",
                gsd_cm=1.0,
                class_scheme=DEFAULT_SCHEME,
                active_round=1,
            )
            db.add(flight)
            db.flush()
        else:
            flight.project_id = project.id
            db.query(Container).filter_by(flight_id=flight.id).delete()
            db.query(Exemplar).filter_by(flight_id=flight.id).delete()

        # containers: a few per contested pair, with plausible stats (round 1)
        sp = 1000
        for a, b in PAIRS:
            for i in range(3):
                sp += 1
                frac = round(0.3 + 0.1 * i, 2)
                pa = round(0.5 - 0.03 * i, 3)
                chips = _chips(f"{FLIGHT}/r1/sp_{sp}", f"container {sp}",
                               lambda v: VIEW_TINT[v])
                db.add(Container(
                    flight_id=flight.id, round=1, superpixel_id=sp,
                    n_pixels=900, abstain_frac=frac, pair_purity=round(0.7 + 0.05 * i, 2),
                    diffuse_frac=0.05, pair_code=0, class_a=a, class_b=b, is_diffuse=False,
                    model_probs={str(a): pa, str(b): round(1 - pa - 0.05, 3)},
                    chip_keys=chips,
                    priority=pair_priority(a, b, frac, damage),
                    replication_target=1,
                ))

        # exemplars: a few per class, tinted by class
        for cid, name in names.items():
            for k in range(3):
                chips = _chips(f"{FLIGHT}/exemplar/c{cid}/{k}", f"{name}",
                               lambda v, c=cid: CLASS_TINT[c])
                db.add(Exemplar(flight_id=flight.id, class_id=cid, source_fid=k,
                                chip_keys=chips))

        db.commit()
        n_c = db.query(Container).filter_by(flight_id=flight.id).count()
        n_e = db.query(Exemplar).filter_by(flight_id=flight.id).count()
        print(f"seeded flight '{FLIGHT}': {n_c} containers, {n_e} exemplars.")
        print("start the app (uvicorn app.main:app --reload) and open http://localhost:8000")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wipe", action="store_true", help="delete the demo flight first")
    seed(wipe=ap.parse_args().wipe)

"""Preflight: verify every file a flight config references is on disk *before*
ingest touches it, so a missing or misnamed file fails with a clear list instead
of a deep rasterio/geopandas error partway through a run.

    python -m ingest.preflight --config flights/synthetic.yaml

ingest_flight calls check_inputs() at the top of ingest(), so a bad config stops
before any work is done. Paths are resolved relative to the current directory --
run from the repo root, same as ingest.
"""
from __future__ import annotations

import argparse
import os

from ingest.ingest_flight import load_inputs


def check_inputs(inp) -> list[str]:
    """Return human-readable problems for an in-memory FlightInputs (empty == ok)."""
    problems: list[str] = []
    required = [
        ("review_gpkg", inp.review_gpkg),
        ("ortho_path", inp.ortho_path),
        ("superpixel_path", inp.superpixel_path),
    ]
    optional = [
        ("abstain_path", inp.abstain_path),
        ("softmax_path", inp.softmax_path),
        ("selection_params_path", inp.selection_params_path),
        ("labeled_polygons", inp.labeled_polygons),
    ]
    for name, path in required:
        if not path:
            problems.append(f"{name}: required but not set in config")
        elif not os.path.exists(path):
            problems.append(f"{name}: file not found -> {path}")
    for name, path in optional:
        if path and not os.path.exists(path):
            problems.append(f"{name}: set in config but file not found -> {path}")
    for v in inp.views:
        sp = getattr(v, "source_path", None)
        if sp and not os.path.exists(sp):
            problems.append(f"view '{v.name}': source_path not found -> {sp}")
    return problems


def check_config(config_path: str) -> list[str]:
    """Load a flight YAML and return any file problems."""
    return check_inputs(load_inputs(config_path))


def _report(config_path: str) -> int:
    inp = load_inputs(config_path)
    rows: list[tuple[str, str, str]] = []

    def add(name, path, optional=False):
        if not path:
            rows.append((name, "(unset)", "skip" if optional else "MISSING"))
        else:
            rows.append((name, path, "ok" if os.path.exists(path) else "MISSING"))

    add("review_gpkg", inp.review_gpkg)
    add("ortho_path", inp.ortho_path)
    add("superpixel_path", inp.superpixel_path)
    add("abstain_path", inp.abstain_path, optional=True)
    add("softmax_path", inp.softmax_path, optional=True)
    add("selection_params_path", inp.selection_params_path, optional=True)
    add("labeled_polygons", inp.labeled_polygons, optional=True)
    for v in inp.views:
        if getattr(v, "source_path", None):
            add(f"view:{v.name}", v.source_path, optional=True)

    w = max(len(n) for n, _, _ in rows)
    flag = {"ok": "OK  ", "MISSING": "!!  ", "skip": "--  "}
    for name, path, status in rows:
        print(f"  {flag[status]}{name.ljust(w)}  {path}")

    missing = [n for n, _, s in rows if s == "MISSING"]
    print()
    if missing:
        print(f"MISSING {len(missing)}: {', '.join(missing)}")
        print("Bring these in from Drive (and rename to the canonical names) "
              "before ingest -- see RUNBOOK.md.")
        return 1
    print("All referenced files present. Ready to ingest.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Check a flight config's files exist.")
    ap.add_argument("--config", required=True)
    raise SystemExit(_report(ap.parse_args().config))

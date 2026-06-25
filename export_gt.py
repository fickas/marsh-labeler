"""export_gt.py -- append human SP verdicts to a provenance-tagged GT polygon
layer, reconciled against existing higher-authority ground truth.

Each settled superpixel becomes one ground-truth polygon, tagged source='sp_label'.
Before it's added, it is reconciled against the GT already in the layer:

  - SP poly is CLIPPED out of any higher-authority polygon it overlaps (hand-drawn
    'pi_digitized', 'field_survey'). The originals are NEVER edited -- only the
    incoming SP poly loses the overlap. If it's fully covered, it's dropped.
  - Where the overlap is with a DIFFERENT class, the disagreement is written to a
    separate conflicts layer (geometry of the overlap + both classes + who won) so
    you can open it in QGIS and adjudicate. Same-class overlap is silent redundancy.

Authority order is one editable dict (AUTHORITY); the higher source wins, which is
where "hand-drawn polys win" lives. Re-running refreshes only THIS flight's
sp_label rows and conflicts; PI/field rows and other flights are untouched.

Polygons are vectorized from the flight's canonical superpixels.tif so geometry
matches the segmentation the verdicts were keyed to. Once written, a GT polygon is
just geometry + class + provenance -- independent of superpixels.tif.

Usage (repo root, venv):
    python export_gt.py --flight demo_synthetic
    python export_gt.py --flight demo_synthetic --out data/synthetic/gt_labeled.gpkg

GT columns:        geometry, class_id, class_name, source, flight, superpixel_id,
                   labeler, method, created_at, clipped
conflict columns:  geometry, sp_class_id, sp_class_name, gt_class_id, gt_class_name,
                   gt_source, flight, superpixel_id, winner, overlap_frac
"""
from __future__ import annotations

import argparse
import os

SP_SOURCE = "sp_label"
LAYER = "ground_truth"
CONFLICT_LAYER = "conflicts"

# Higher number = more trusted. Hand-drawn (pi_digitized) and field_survey outrank
# app SP labels, so an SP poly is clipped where it overlaps them and a class
# disagreement is logged. Edit this one dict to change the precedence everywhere.
AUTHORITY = {"field_survey": 3, "pi_digitized": 2, "sp_label": 1}
SP_RANK = AUTHORITY[SP_SOURCE]

OVERLAP_FRAC = 0.05   # min (overlap area / SP area) to log a conflict
MIN_AREA = 1e-6       # drop clipped slivers smaller than this

PALETTE = {
    0: ("other",            "150,150,150,255"),
    1: ("healthy_bank",     "60,150,90,255"),
    2: ("eroding_non_crab", "200,170,90,255"),
    3: ("crab_edge",        "230,140,60,255"),
    4: ("crab_platform",    "200,70,110,255"),
    5: ("collapsed",        "140,40,70,255"),
}

_CONFLICT_COLS = ["geometry", "sp_class_id", "sp_class_name", "gt_class_id",
                  "gt_class_name", "gt_source", "flight", "superpixel_id",
                  "winner", "overlap_frac"]


def verdict_polygons(sp_arr, transform, verdict_ids):
    """One dissolved polygon per verdicted superpixel id. Pure -> testable."""
    import numpy as np
    from rasterio.features import shapes
    from shapely.geometry import shape
    from shapely.ops import unary_union

    want = {int(i) for i in verdict_ids}
    mask = np.isin(sp_arr, list(want))
    parts: dict[int, list] = {}
    for geom, val in shapes(sp_arr.astype("int32"), mask=mask, transform=transform):
        sid = int(val)
        if sid in want:
            parts.setdefault(sid, []).append(shape(geom))
    return {sid: (unary_union(gs) if len(gs) > 1 else gs[0]) for sid, gs in parts.items()}


def reconcile(new_gdf, higher_gdf):
    """Clip each new SP poly out of any higher-authority GT it overlaps and log
    class disagreements. Returns (kept_gdf, conflicts_gdf). Pure -> testable.

    higher_gdf carries: geometry, class_id, class_name, source (rows that outrank
    sp_label). The incoming SP polys are clipped; higher_gdf is never modified.
    """
    import geopandas as gpd
    from shapely.ops import unary_union

    crs = new_gdf.crs
    if higher_gdf is None or len(higher_gdf) == 0:
        out = new_gdf.copy()
        out["clipped"] = False
        return out, gpd.GeoDataFrame(columns=_CONFLICT_COLS, geometry="geometry", crs=crs)

    sidx = higher_gdf.sindex
    kept_rows, conflicts = [], []
    for _, sp in new_gdf.iterrows():
        geom = sp.geometry
        clip_geoms = []
        was_clipped = False
        for ci in sidx.query(geom, predicate="intersects"):
            h = higher_gdf.iloc[int(ci)]
            inter = geom.intersection(h.geometry)
            if inter.is_empty or inter.area <= 0:
                continue
            clip_geoms.append(h.geometry)
            was_clipped = True
            frac = inter.area / geom.area if geom.area > 0 else 0.0
            if int(h["class_id"]) != int(sp["class_id"]) and frac >= OVERLAP_FRAC:
                conflicts.append({
                    "geometry": inter,
                    "sp_class_id": int(sp["class_id"]),
                    "sp_class_name": sp["class_name"],
                    "gt_class_id": int(h["class_id"]),
                    "gt_class_name": h.get("class_name"),
                    "gt_source": h["source"],
                    "flight": sp.get("flight"),
                    "superpixel_id": int(sp["superpixel_id"]),
                    "winner": h["source"],          # higher authority wins
                    "overlap_frac": round(float(frac), 3),
                })
        if clip_geoms:
            geom = geom.difference(unary_union(clip_geoms))
        if geom.is_empty or geom.area < MIN_AREA:
            continue                                 # fully covered -> drop
        row = sp.to_dict()
        row["geometry"] = geom
        row["clipped"] = was_clipped
        kept_rows.append(row)

    kept = (gpd.GeoDataFrame(kept_rows, geometry="geometry", crs=crs) if kept_rows
            else gpd.GeoDataFrame(columns=list(new_gdf.columns) + ["clipped"],
                                  geometry="geometry", crs=crs))
    conf = (gpd.GeoDataFrame(conflicts, geometry="geometry", crs=crs) if conflicts
            else gpd.GeoDataFrame(columns=_CONFLICT_COLS, geometry="geometry", crs=crs))
    return kept, conf


def write_qml(path):
    """Best-effort categorized QGIS style on class_id (adjust if your QGIS balks)."""
    import xml.sax.saxutils as sx

    cats, syms = [], []
    for cid, (name, rgba) in PALETTE.items():
        cats.append(f'<category render="true" value="{cid}" symbol="{cid}" label="{sx.escape(name)}"/>')
        syms.append(
            f'<symbol type="fill" name="{cid}" alpha="1" clip_to_extent="1" force_rhr="0">'
            f'<layer class="SimpleFill" enabled="1" locked="0" pass="0">'
            f'<prop k="color" v="{rgba}"/><prop k="style" v="solid"/>'
            f'<prop k="outline_color" v="35,35,35,255"/><prop k="outline_style" v="solid"/>'
            f'<prop k="outline_width" v="0.1"/></layer></symbol>'
        )
    qml = (
        '<!DOCTYPE qgis>\n<qgis styleCategories="Symbology" version="3.28">\n'
        '  <renderer-v2 type="categorizedSymbol" attr="class_id" forceraster="0" enableorderby="0">\n'
        '    <categories>\n      ' + "\n      ".join(cats) + '\n    </categories>\n'
        '    <symbols>\n      ' + "\n      ".join(syms) + '\n    </symbols>\n'
        '  </renderer-v2>\n</qgis>\n'
    )
    with open(path, "w") as f:
        f.write(qml)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flight", required=True, help="flight name")
    ap.add_argument("--out", default="data/synthetic/gt_labeled.gpkg")
    ap.add_argument(
        "--init", action="store_true",
        help="allow creating a NEW ground-truth file when --out doesn't exist. "
             "Without this, export refuses to write to a missing file, so you "
             "can't accidentally produce an island GT (only sp_label polys, no "
             "PI polygons to reconcile against) and clobber the canonical layer "
             "when you sync it back to Drive. Bring the canonical gt_labeled.gpkg "
             "in first, or pass --init if this really is a fresh start.",
    )
    args = ap.parse_args()

    if not os.path.exists(args.out) and not args.init:
        raise SystemExit(
            f"{args.out} does not exist.\n"
            "export_gt reconciles INTO the existing ground-truth layer, so writing "
            "to a missing file would create an island GT with only this flight's "
            "verdicts and no PI polygons -- syncing that to Drive would clobber the "
            "canonical layer.\n"
            "  - bring the canonical gt_labeled.gpkg into that path first, OR\n"
            "  - pass --init if you genuinely want to start a new GT file here.\n"
            "(see RUNBOOK.md)"
        )

    import geopandas as gpd
    import pandas as pd
    import rasterio
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Flight, Label, ResolvedLabel, User

    conflicts_path = os.path.splitext(args.out)[0] + "_conflicts.gpkg"
    db = SessionLocal()
    try:
        flight = db.scalar(select(Flight).where(Flight.name == args.flight))
        if flight is None:
            raise SystemExit(f"no flight named {args.flight!r}")
        names = {int(k): v for k, v in (flight.class_scheme or {}).get("names", {}).items()}

        verdicts = db.execute(
            select(ResolvedLabel.superpixel_id, ResolvedLabel.class_id,
                   ResolvedLabel.method, ResolvedLabel.created_at)
            .where(ResolvedLabel.flight_id == flight.id, ResolvedLabel.class_id.is_not(None))
        ).all()
        if not verdicts:
            raise SystemExit(f"no settled class verdicts for flight {args.flight!r} yet")

        lab = db.execute(
            select(Label.superpixel_id, User.email)
            .join(User, User.id == Label.user_id)
            .where(Label.flight_id == flight.id, Label.action == "label")
        ).all()
        labelers: dict[int, set] = {}
        for sid, email in lab:
            labelers.setdefault(sid, set()).add(email)

        with rasterio.open(flight.superpixel_path) as src:
            sp_arr = src.read(1)
            transform = src.transform
            crs = src.crs

        polys = verdict_polygons(sp_arr, transform, [v.superpixel_id for v in verdicts])
        rows = []
        for v in verdicts:
            g = polys.get(v.superpixel_id)
            if g is None:
                print(f"  warn: superpixel {v.superpixel_id} not in {flight.superpixel_path}; skipped")
                continue
            rows.append({
                "geometry": g,
                "class_id": int(v.class_id),
                "class_name": names.get(int(v.class_id), f"class {v.class_id}"),
                "source": SP_SOURCE,
                "flight": args.flight,
                "superpixel_id": int(v.superpixel_id),
                "labeler": ",".join(sorted(labelers.get(v.superpixel_id, []))) or None,
                "method": v.method,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            })
        new = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)

        # Reconcile against higher-authority GT already in the layer.
        keep = None
        if os.path.exists(args.out):
            old = gpd.read_file(args.out, layer=LAYER)
            if "source" in old.columns and "flight" in old.columns:
                keep = old[~((old["source"] == SP_SOURCE) & (old["flight"] == args.flight))]
                higher = keep[keep["source"].map(lambda s: AUTHORITY.get(s, 0) > SP_RANK)]
            else:
                keep, higher = old, old.iloc[0:0]      # foreign schema -> don't reconcile
        else:
            higher = new.iloc[0:0]

        new_kept, conflicts = reconcile(new, higher if len(higher) else None)

        out = (gpd.GeoDataFrame(pd.concat([keep, new_kept], ignore_index=True),
                                geometry="geometry", crs=new.crs)
               if keep is not None else new_kept)
        out.to_file(args.out, layer=LAYER, driver="GPKG")
        write_qml(os.path.splitext(args.out)[0] + ".qml")

        # Refresh this flight's conflict rows.
        if os.path.exists(conflicts_path):
            oldc = gpd.read_file(conflicts_path, layer=CONFLICT_LAYER)
            oldc = oldc[oldc["flight"] != args.flight] if "flight" in oldc.columns else oldc
            allc = gpd.GeoDataFrame(pd.concat([oldc, conflicts], ignore_index=True),
                                    geometry="geometry", crs=conflicts.crs)
        else:
            allc = conflicts
        if len(allc):
            allc.to_file(conflicts_path, layer=CONFLICT_LAYER, driver="GPKG")

        dropped = len(new) - len(new_kept)
        print(f"GT: kept {len(new_kept)} sp_label polys "
              f"({dropped} dropped as covered), {len(out)} total -> {args.out}")
        print(f"conflicts this flight: {len(conflicts)}" +
              (f" -> {conflicts_path}" if len(allc) else " (none)"))
        if len(out):
            print(out.groupby(["source", "class_name"]).size().to_string())
    finally:
        db.close()


if __name__ == "__main__":
    main()

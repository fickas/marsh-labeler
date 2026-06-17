"""Ingest reference exemplars: a few labeled-polygon chips per class, shown to
labelers as a calibration gallery.

Reuses the flight's config (ortho + view specs) and the shared chip renderer, so
exemplars look exactly like the container chips. For each class it picks the
largest N polygons (clearest, most typical) -- or a curated list -- and renders a
container-scale crop sampled from each polygon's interior (a point guaranteed to
be inside, so the crop isn't a mixed boundary).

Run (after ingest_flight has created the flight):
    python -m ingest.ingest_exemplars --config flights/example.yaml
    python -m ingest.ingest_exemplars --config flights/example.yaml --replace
"""
from __future__ import annotations

import argparse
import tempfile

from app.db import SessionLocal
from app.models import Exemplar, Flight
from app.storage import put_chip

from .ingest_flight import _srid, load_inputs
from .render import render_views


def ingest_exemplars(config_path: str, replace: bool = False) -> None:
    import geopandas as gpd
    import rasterio
    from shapely.geometry import box

    inp = load_inputs(config_path)
    if not inp.labeled_polygons:
        raise ValueError("config has no 'labeled_polygons'; nothing to build exemplars from")

    gdf = gpd.read_file(inp.labeled_polygons)
    field = inp.label_class_field
    if field not in gdf.columns:
        raise ValueError(f"label_class_field {field!r} not in {list(gdf.columns)}")

    with rasterio.open(inp.ortho_path) as o:
        half = (inp.exemplar_crop_px / 2.0) * abs(o.res[0])  # half-window in map units
    srid = _srid(inp.crs)
    curated = inp.exemplar_curated or {}

    session = SessionLocal()
    try:
        flight = session.query(Flight).filter_by(name=inp.name).one_or_none()
        if flight is None:
            raise ValueError(
                f"flight {inp.name!r} not found; run ingest_flight first."
            )
        if replace:
            session.query(Exemplar).filter_by(flight_id=flight.id).delete()

        gdf["_area"] = gdf.geometry.area
        n = 0
        for raw_class, grp in gdf.groupby(field):
            class_id = int(raw_class)
            picks = curated.get(class_id, curated.get(str(class_id)))
            if picks:
                chosen = grp[grp.index.isin(picks)]
            else:
                chosen = grp.sort_values("_area", ascending=False).head(inp.examples_per_class)

            for fid, row in chosen.iterrows():
                pt = row.geometry.representative_point()   # guaranteed inside the polygon
                window = (pt.x - half, pt.y - half, pt.x + half, pt.y + half)
                with tempfile.TemporaryDirectory() as tmp:
                    chip_keys = render_views(
                        inp.views,
                        inp.ortho_path,
                        window,
                        tmp,
                        f"{inp.name}/exemplar/c{class_id}/{fid}",
                        put_chip,
                    )
                session.add(
                    Exemplar(
                        flight_id=flight.id,
                        class_id=class_id,
                        source_fid=int(fid),
                        chip_keys=chip_keys,
                        geom=f"SRID={srid};{box(*window).wkt}",
                    )
                )
                n += 1
        session.commit()
        print(f"ingested {n} exemplars across {gdf[field].nunique()} classes for '{inp.name}'.")
    finally:
        session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="YAML flight config (see flights/)")
    ap.add_argument(
        "--replace", action="store_true", help="clear this flight's exemplars first"
    )
    args = ap.parse_args()
    ingest_exemplars(args.config, replace=args.replace)

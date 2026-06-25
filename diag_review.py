"""diag_review.py -- localize a mismatch between the review gpkg and the rasters.

For each review row it compares what the gpkg recorded (n_pixels, n_pair) against
what superpixels.tif + abstain.tif actually contain for that superpixel_id, and
checks the gpkg polygon actually sits on that superpixel. Run from the repo root:

    python diag_review.py

Reads data/synthetic/{superpixels.tif,abstain.tif,abstain_review.gpkg}.

How to read the output:
  - grids differ (shape/transform)         -> rasters aren't co-registered.
  - N_MISMATCH (gpkg n_pixels != raster n)  -> superpixel_id doesn't index the
                                               same patch the gpkg was built from
                                               (the id mapping is wrong).
  - ABSTAIN_MISMATCH (gpkg had pairs,
        raster has none)                    -> abstain.tif is a different run than
                                               the gpkg was built from.
  - OFF_SUPERPIXEL (polygon centroid not
        inside sp_arr==id)                  -> geometry and id disagree.
"""
import itertools

import geopandas as gpd
import numpy as np
import rasterio

DIFFUSE = 100
SP = "data/synthetic/superpixels.tif"
AB = "data/synthetic/abstain.tif"
GP = "data/synthetic/abstain_review.gpkg"


def main():
    sp = rasterio.open(SP)
    ab = rasterio.open(AB)
    sp_arr = sp.read(1)
    ab_arr = ab.read(1)

    print(f"superpixels: shape={sp_arr.shape} transform={tuple(round(x,4) for x in sp.transform[:6])}")
    print(f"abstain    : shape={ab_arr.shape} transform={tuple(round(x,4) for x in ab.transform[:6])}")
    same_grid = sp_arr.shape == ab_arr.shape and sp.transform == ab.transform
    print(f"same grid? {same_grid}")
    if sp_arr.shape != ab_arr.shape:
        print("!! shapes differ -- cannot index abstain by superpixel mask; stop here.")
        return

    g = gpd.read_file(GP)
    print(f"\n{len(g)} review rows\n")
    mism_n = mism_ab = off_sp = 0
    for _, r in g.iterrows():
        spid = int(r["superpixel_id"])
        mask = sp_arr == spid
        n = int(mask.sum())
        codes = ab_arr[mask]
        n_pair = int(((codes >= 1) & (codes < DIFFUSE)).sum())
        n_diff = int((codes == DIFFUSE).sum())
        gn = int(r.get("n_pixels") or 0)
        gpair = int(r.get("n_pair") or 0)

        flags = []
        if n != gn:
            mism_n += 1
            flags.append(f"N_MISMATCH(gpkg={gn} raster={n})")
        if gpair > 0 and n_pair == 0:
            mism_ab += 1
            flags.append(f"ABSTAIN_MISMATCH(gpkg_pair={gpair} raster_pair=0)")
        # does the polygon actually sit on this superpixel?
        try:
            cx, cy = r.geometry.centroid.x, r.geometry.centroid.y
            row, col = sp.index(cx, cy)
            if not (0 <= row < sp_arr.shape[0] and 0 <= col < sp_arr.shape[1] and sp_arr[row, col] == spid):
                off_sp += 1
                flags.append(f"OFF_SUPERPIXEL(centroid sits on sp={sp_arr[row, col] if 0 <= row < sp_arr.shape[0] and 0 <= col < sp_arr.shape[1] else 'oob'})")
        except Exception as e:
            flags.append(f"centroid-check-failed:{e}")

        pair = r.get("contested_pair", "")
        print(f"sp {spid:>7} [{pair}] gpkg n={gn} pair={gpair} | raster n={n} pair={n_pair} diff={n_diff}"
              + ("  <<< " + " ".join(flags) if flags else ""))

    print(f"\npixel-count mismatches : {mism_n}/{len(g)}")
    print(f"abstain mismatches     : {mism_ab}/{len(g)}")
    print(f"polygon-off-superpixel : {off_sp}/{len(g)}")


if __name__ == "__main__":
    main()

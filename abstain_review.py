"""
abstain_review.py -- intersect the abstain bucket with the superpixel containers
and promote high-abstain containers into a tagged review layer for QGIS.

For every container (superpixel id > 0) it computes, from the per-pixel abstain
codes:
  n_pixels      container size
  n_pair        pixels flagged as a two-way tie
  n_diffuse     pixels flagged diffuse (DIFFUSE_CODE)
  abstain_frac  n_pair / n_pixels         <- the promotion metric
  contested_pair  the container's DOMINANT pair (mode over its pair pixels)
  pair_purity   share of pair pixels that are the dominant pair (low => mixed)

Promotion is driven by PAIR abstentions only (diffuse is tracked but does not
promote). Output: a GeoPackage of promoted container polygons, tagged with the
contested pair and sorted into a work queue.

Nothing here assumes a fixed class count or fixed class meanings: the number of
classes is inferred from the abstain legend (falling back to the softmax-derived
pair codes), and the pair-code range and histogram radix are derived from it.
The class scheme can change between flights without touching this code.

The output columns are the ingestion contract for the labeling app
(ingest/contract.REQUIRED_REVIEW_COLUMNS): besides geometry, superpixel_id,
n_pixels, abstain_frac, pair_purity, diffuse_frac, pair_code, class_a, class_b,
is_diffuse. class_a/class_b are the integer class ids of the dominant contested
pair (lower id first), recovered from the pair code with the same enumeration
build_abstain_raster used.

Requires: rasterio, numpy, geopandas, shapely.
"""

import os
import json
import math
import itertools
import numpy as np
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape
import geopandas as gpd

# Must match abstain.DIFFUSE_CODE. Pair codes run 1..C(n,2); this sentinel must
# stay above the largest pair code (asserted at run time).
DIFFUSE_CODE = 100

_SCHEMA = ["superpixel_id", "container_id", "n_pixels", "n_pair", "n_diffuse",
           "abstain_frac", "diffuse_frac", "pair_purity",
           "pair_code", "class_a", "class_b", "is_diffuse", "contested_pair"]


def _code_to_pair(n_classes):
    """Invert build_abstain_raster's pair-code enumeration: code -> (a, b).

    Mirrors abstain._pair_codes exactly: unordered pairs in
    itertools.combinations(range(n_classes), 2) order, code = k + 1. Returns
    integer class ids with a < b.
    """
    pairs = list(itertools.combinations(range(n_classes), 2))
    return {k + 1: pair for k, pair in enumerate(pairs)}


def _infer_n_classes(legend):
    """Recover the class count from the legend: it lists C(n,2) pair entries plus
    the diffuse entry, so n = (1 + sqrt(1 + 8 * n_pairs)) / 2. Returns None if the
    legend is empty or its pair count isn't a valid C(n,2)."""
    n_pairs = sum(1 for k in legend if k != str(DIFFUSE_CODE))
    if n_pairs == 0:
        return None
    n = (1 + math.isqrt(1 + 8 * n_pairs)) // 2
    return n if n * (n - 1) // 2 == n_pairs else None


def build_abstain_review_polygons(
    superpixel_path,
    abstain_path,
    out_gpkg,
    min_abstain_frac=0.30,
    min_pair_pixels=0,
    legend_path=None,
    layer="abstain_review",
    n_classes=None,
):
    """Promote high-abstain containers to a tagged review GeoPackage.

    min_abstain_frac : promote a container when its pair-abstain fraction >= this.
    min_pair_pixels  : also require at least this many pair pixels (guards tiny
                       containers tripping the fraction on a couple of pixels).
    legend_path      : pair-code -> [classA, classB] JSON; defaults to the
                       '<abstain>_legend.json' written by build_abstain_raster.
    n_classes        : class count of the Model 1 scheme. Default None infers it
                       from the legend, so a changing class scheme needs no code
                       change. Pass an int only to override.

    Returns the GeoDataFrame (also written to out_gpkg). Empty if none promoted.
    """
    if legend_path is None:
        legend_path = os.path.splitext(abstain_path)[0] + "_legend.json"
    legend = json.load(open(legend_path)) if os.path.exists(legend_path) else {}

    if n_classes is None:
        n_classes = _infer_n_classes(legend)
        if n_classes is None:
            raise ValueError(
                "could not infer n_classes from the legend; pass n_classes "
                "explicitly (no usable legend at "
                f"{legend_path!r})."
            )

    code_to_pair = _code_to_pair(n_classes)
    n_pairs = len(code_to_pair)          # C(n_classes, 2)
    radix = n_pairs + 1                  # codes occupy 0..n_pairs
    if n_pairs >= DIFFUSE_CODE:
        raise ValueError(
            f"{n_classes} classes -> {n_pairs} pair codes collides with "
            f"DIFFUSE_CODE={DIFFUSE_CODE}; bump DIFFUSE_CODE in abstain.py and here."
        )

    with rasterio.open(superpixel_path) as ssrc:
        seg = ssrc.read(1).astype(np.int64)
        crs, transform = ssrc.crs, ssrc.transform
        sshape = (ssrc.height, ssrc.width)
    with rasterio.open(abstain_path) as asrc:
        ab = asrc.read(1)
        ashape = (asrc.height, asrc.width)
    if sshape != ashape:
        raise ValueError(f"grid mismatch: superpixels {sshape} vs abstain {ashape}")

    n_ids = int(seg.max())
    if n_ids == 0:
        return _empty(crs)

    valid = seg > 0
    size = np.bincount(seg[valid].ravel(), minlength=n_ids + 1)

    is_pair = (ab >= 1) & (ab <= n_pairs) & valid
    is_diffuse = (ab == DIFFUSE_CODE) & valid
    pair_count = np.bincount(seg[is_pair].ravel(), minlength=n_ids + 1)
    diffuse_count = np.bincount(seg[is_diffuse].ravel(), minlength=n_ids + 1)

    # Dominant pair code per container via a (container_id, code) histogram.
    cid = seg[is_pair].ravel()
    code = ab[is_pair].ravel().astype(np.int64)
    hist = np.bincount(cid * radix + code,
                       minlength=(n_ids + 1) * radix).reshape(n_ids + 1, radix)
    dom_code = hist.argmax(axis=1)
    dom_count = hist.max(axis=1)

    with np.errstate(divide="ignore", invalid="ignore"):
        abstain_frac = np.where(size > 0, pair_count / size, 0.0)
        diffuse_frac = np.where(size > 0, diffuse_count / size, 0.0)
        pair_purity = np.where(pair_count > 0, dom_count / pair_count, 0.0)

    promote = (abstain_frac >= min_abstain_frac) & (pair_count >= max(1, min_pair_pixels))
    if not promote[1:].any():
        print(f"no containers promoted at min_abstain_frac={min_abstain_frac}")
        return _empty(crs)

    promoted_mask = promote[seg]                 # per-pixel; promote[0] is False
    seg_i32 = seg.astype(np.int32)

    feats = []
    for geom, cid_f in shapes(seg_i32, mask=promoted_mask,
                              transform=transform, connectivity=4):
        c = int(cid_f)
        if c == 0:
            continue
        dc = int(dom_code[c])
        names = [n for n in legend.get(str(dc), [f"code_{dc}"]) if n]
        a_id, b_id = code_to_pair.get(dc, (None, None))
        feats.append({
            "superpixel_id": c,
            "container_id": c,            # kept for backward compatibility
            "n_pixels": int(size[c]),
            "n_pair": int(pair_count[c]),
            "n_diffuse": int(diffuse_count[c]),
            "abstain_frac": round(float(abstain_frac[c]), 3),
            "diffuse_frac": round(float(diffuse_frac[c]), 3),
            "pair_purity": round(float(pair_purity[c]), 3),
            "pair_code": dc,
            "class_a": None if a_id is None else int(a_id),
            "class_b": None if b_id is None else int(b_id),
            # this builder promotes pair containers only, so a clean pair always
            # exists; the column is here for the contract / future diffuse promotion.
            "is_diffuse": False,
            "contested_pair": "|".join(names),
            "geometry": shape(geom),
        })

    gdf = gpd.GeoDataFrame(feats, crs=crs)
    gdf = gdf.sort_values(["contested_pair", "abstain_frac"],
                          ascending=[True, False]).reset_index(drop=True)

    if os.path.exists(out_gpkg):
        os.remove(out_gpkg)
    os.makedirs(os.path.dirname(out_gpkg) or ".", exist_ok=True)
    gdf.to_file(out_gpkg, layer=layer, driver="GPKG")

    print(f"promoted {gdf['superpixel_id'].nunique()} containers -> {out_gpkg}")
    print(gdf.groupby("contested_pair")["abstain_frac"].agg(["count", "mean"]).round(3))
    return gdf


def _empty(crs):
    return gpd.GeoDataFrame(columns=_SCHEMA, geometry=[], crs=crs)

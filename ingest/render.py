"""Shared chip rendering.

Turns a geographic window of the ortho (or a derived band source) into PNG
chips, one per view spec, and stores them. Used by both container ingest and
exemplar ingest so the two render identically -- same stretch, same views, same
look -- which is what makes the exemplar gallery a fair comparison.

The caller passes an explicit window in map units (it already includes whatever
context padding it wants); this module just renders that window against each
view's source. Rendering the same map-unit window against each source's own
transform keeps views aligned even when a derived band sits on a different grid.

STRETCH IS GLOBAL, NOT PER-CHIP. The contrast stretch for a band is computed
once over the whole source raster and reused for every chip and every exemplar.
A per-chip stretch would blow up the within-crop sensor noise of a homogeneous
superpixel into full-range rainbow speckle, and -- worse -- would put each chip
on its own scale, so a container and its class exemplars wouldn't be visually
comparable even when they show the same material. The global bounds are cached
in-process and in a `<source>.stretch.json` sidecar so the separate container
and exemplar ingest runs agree on one stretch.
"""
from __future__ import annotations

import json
import os

# abspath(source) -> {band_index: (lo, hi)}
_BOUNDS_CACHE: dict[str, dict[int, tuple]] = {}


def _sidecar_path(src_path: str) -> str:
    return os.path.abspath(src_path) + ".stretch.json"


def source_bounds(src, src_path, bands, lo: float = 2, hi: float = 98,
                  max_dim: int = 2048):
    """Per-band global stretch bounds for `src_path`: {band: (p_lo, p_hi)}.

    Computed from a decimated read of the full raster (so it's cheap even on big
    orthos) over finite, non-nodata pixels. Cached in-process and to a sidecar
    JSON next to the source, so container ingest and exemplar ingest -- separate
    processes -- reuse the exact same stretch.
    """
    import numpy as np

    key = os.path.abspath(src_path)
    cache = _BOUNDS_CACHE.get(key)
    if cache is None:
        sidecar = _sidecar_path(src_path)
        if os.path.exists(sidecar):
            try:
                cache = {int(k): tuple(v) for k, v in json.load(open(sidecar)).items()}
            except (OSError, ValueError):
                cache = {}
        else:
            cache = {}

    missing = [b for b in bands if b not in cache]
    if missing:
        h, w = src.height, src.width
        scale = max(1, int(max(h, w) / max_dim))
        out_h, out_w = max(1, h // scale), max(1, w // scale)
        nodata = src.nodata
        for b in missing:
            arr = src.read(b, out_shape=(out_h, out_w)).astype("float32")
            m = np.isfinite(arr)
            if nodata is not None:
                m &= arr != nodata
            vals = arr[m]
            if vals.size == 0:
                cache[b] = (0.0, 1.0)
            else:
                p_lo, p_hi = np.percentile(vals, [lo, hi])
                if p_hi <= p_lo:
                    p_hi = float(p_lo) + 1e-6
                cache[b] = (float(p_lo), float(p_hi))
        try:
            json.dump({str(k): list(v) for k, v in cache.items()},
                      open(_sidecar_path(src_path), "w"))
        except OSError:
            pass

    _BOUNDS_CACHE[key] = cache
    return cache


def apply_stretch(arr, lo: float, hi: float):
    """Map [lo, hi] -> [0, 1] with clipping, using fixed (global) bounds."""
    import numpy as np

    if hi <= lo:
        hi = lo + 1e-6
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)


def render_views(views, default_ortho_path, window_bounds, tmpdir, key_prefix, put_chip,
                 outline_path=None, outline_id=None):
    """Render each ViewSpec over `window_bounds` (minx, miny, maxx, maxy in map
    units) and store it. Returns {view_name: public_url}.

    views              : list of ingest.contract.ViewSpec
    default_ortho_path : source for views without their own source_path
    put_chip           : storage function (local_path, key) -> url
    outline_path       : optional superpixel-id raster; if given with outline_id,
                         the boundary of that superpixel is drawn onto every view,
                         so the labeler sees which unit the question is about.
    outline_id         : the superpixel id to outline.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    minx, miny, maxx, maxy = window_bounds
    urls: dict[str, str] = {}
    for view in views:
        src_path = view.source_path or default_ortho_path
        with rasterio.open(src_path) as src:
            window = from_bounds(minx, miny, maxx, maxy, src.transform)
            bands = list(view.bands) if view.bands else [1]
            bounds = source_bounds(src, src_path, bands)
            if view.bands:  # RGB composite, global stretch per band
                channels = []
                for b in view.bands:
                    lo, hi = bounds[b]
                    channels.append(
                        apply_stretch(src.read(b, window=window).astype("float32"), lo, hi)
                    )
                img = np.dstack(channels)
                cmap = None
            else:  # single-band derived view
                lo, hi = bounds[1]
                img = apply_stretch(src.read(1, window=window).astype("float32"), lo, hi)
                cmap = view.cmap

        fig, ax = plt.subplots(figsize=(3, 3), dpi=100)
        ax.imshow(img, cmap=cmap)
        if outline_path is not None and outline_id is not None:
            ih, iw = img.shape[0], img.shape[1]
            with rasterio.open(outline_path) as sp:
                sp_win = from_bounds(minx, miny, maxx, maxy, sp.transform)
                sp_arr = sp.read(1, window=sp_win, out_shape=(ih, iw),
                                 resampling=Resampling.nearest)
            mask = sp_arr == outline_id
            if mask.any():
                ax.contour(mask.astype(float), levels=[0.5],
                           colors=["#00e5ff"], linewidths=1.2)
        ax.axis("off")
        local = os.path.join(tmpdir, f"{view.name}.png")
        fig.savefig(local, bbox_inches="tight", pad_inches=0)
        plt.close(fig)
        urls[view.name] = put_chip(local, f"{key_prefix}/{view.name}.png")
    return urls

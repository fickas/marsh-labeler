"""Shared chip rendering.

Turns a geographic window of the ortho (or a derived band source) into PNG
chips, one per view spec, and stores them. Used by both container ingest and
exemplar ingest so the two render identically -- same stretch, same views, same
look -- which is what makes the exemplar gallery a fair comparison.

The caller passes an explicit window in map units (it already includes whatever
context padding it wants); this module just renders that window against each
view's source. Rendering the same map-unit window against each source's own
transform keeps views aligned even when a derived band sits on a different grid.
"""
from __future__ import annotations

import os


def percentile_stretch(arr, lo: float = 2, hi: float = 98):
    """Stretch a band to [0, 1] on its 2nd/98th percentiles (drops outliers)."""
    import numpy as np

    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return arr
    p_lo, p_hi = np.percentile(finite, [lo, hi])
    if p_hi <= p_lo:
        p_hi = p_lo + 1e-6
    return np.clip((arr - p_lo) / (p_hi - p_lo), 0.0, 1.0)


def render_views(views, default_ortho_path, window_bounds, tmpdir, key_prefix, put_chip):
    """Render each ViewSpec over `window_bounds` (minx, miny, maxx, maxy in map
    units) and store it. Returns {view_name: public_url}.

    views            : list of ingest.contract.ViewSpec
    default_ortho_path : source for views without their own source_path
    put_chip         : storage function (local_path, key) -> url
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds

    minx, miny, maxx, maxy = window_bounds
    urls: dict[str, str] = {}
    for view in views:
        src_path = view.source_path or default_ortho_path
        with rasterio.open(src_path) as src:
            window = from_bounds(minx, miny, maxx, maxy, src.transform)
            if view.bands:  # RGB composite
                channels = [
                    percentile_stretch(src.read(b, window=window).astype("float32"))
                    for b in view.bands
                ]
                img = np.dstack(channels)
                cmap = None
            else:  # single-band derived view
                img = percentile_stretch(src.read(1, window=window).astype("float32"))
                cmap = view.cmap

        fig, ax = plt.subplots(figsize=(3, 3), dpi=100)
        ax.imshow(img, cmap=cmap)
        ax.axis("off")
        local = os.path.join(tmpdir, f"{view.name}.png")
        fig.savefig(local, bbox_inches="tight", pad_inches=0)
        plt.close(fig)
        urls[view.name] = put_chip(local, f"{key_prefix}/{view.name}.png")
    return urls

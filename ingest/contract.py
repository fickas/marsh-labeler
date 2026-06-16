"""The ingestion contract.

These dataclasses are the seam between the active-learning pipeline (which
produces rasters and the review GeoPackage) and the labeling app's database.
`FlightInputs` is what one YAML config in flights/ deserializes into.

REQUIRED_REVIEW_COLUMNS is what the script expects to find on the review
GeoPackage produced by build_abstain_review_polygons(). Two of these
(`superpixel_id` and the split `class_a`/`class_b`) are a small addition to the
current abstain_review output -- see README "Integration seam".
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ViewSpec:
    """One renderable chip view.

    Either an RGB composite from the ortho (`bands` = three 1-based indices) or a
    single-band derived view (`source_path` + optional matplotlib `cmap`).
    """

    name: str
    bands: tuple[int, int, int] | None = None
    source_path: str | None = None
    cmap: str | None = None


@dataclass
class FlightInputs:
    name: str
    crs: str
    review_gpkg: str          # build_abstain_review_polygons() output
    ortho_path: str           # multiband ortho, for chip rendering
    superpixel_path: str      # uint32 superpixel-id raster
    abstain_path: str | None = None
    softmax_path: str | None = None   # optional; enables per-container model_probs
    gsd_cm: float | None = None
    # per-flight class scheme: {"names": {id: name}, "damage": [ids],
    # "ignore_index": int}. None falls back to constants.DEFAULT_SCHEME (synthetic).
    classes: dict | None = None
    views: list[ViewSpec] = field(default_factory=list)
    context_pad_px: int = 64  # surrounding context rendered around each container
    replication_target: int = 1


# Columns the review GeoPackage must carry (besides `geometry`).
REQUIRED_REVIEW_COLUMNS = (
    "superpixel_id",
    "geometry",
    "n_pixels",
    "abstain_frac",
    "pair_purity",
    "diffuse_frac",
    "pair_code",
    "class_a",
    "class_b",
    "is_diffuse",
)

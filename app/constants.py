"""Class scheme handling and queue priority.

The class scheme (how many classes there are and what each means) is DATA, not
code: it is stored per flight in flights.class_scheme and will change when real
data arrives. Nothing in the app's logic assumes a fixed class count or fixed
class meanings -- `pair_priority` takes the damage set as an argument, and the
API/UI read names from the flight's scheme.

DEFAULT_SCHEME below is only a convenience fallback for the current synthetic
flight when a flight config doesn't supply its own scheme. Do not rely on it in
logic; pass the flight's scheme through.
"""
from __future__ import annotations

DEFAULT_IGNORE_INDEX = 255

# Fallback ONLY for the current synthetic data. Real flights supply their own.
DEFAULT_SCHEME = {
    "names": {
        0: "other",
        1: "healthy_bank",
        2: "eroding_non_crab",
        3: "crab_edge",
        4: "crab_platform",
        5: "collapsed",
    },
    "damage": [3, 4, 5],          # classes that count toward the AREA metric
    "ignore_index": DEFAULT_IGNORE_INDEX,
}


def pair_priority(
    class_a: int | None,
    class_b: int | None,
    abstain_frac: float,
    damage_classes,
) -> float:
    """Serving weight for a review container, given this flight's damage set.

    A tie that crosses the damage boundary (exactly one class is a damage class)
    can flip a pixel into or out of the AREA total, so it ranks highest. A
    within-damage tie reshuffles among damage classes without changing the total.
    A non-damage tie ranks lowest, and diffuse (no clean pair) is lowest of all.
    `damage_classes` is whatever this flight calls damage -- no class id is
    assumed here.
    """
    damage = set(damage_classes or ())
    if class_a is None or class_b is None:  # diffuse
        return 0.1 * abstain_frac
    a_dmg = class_a in damage
    b_dmg = class_b in damage
    if a_dmg ^ b_dmg:        # boundary-crossing
        return 3.0 * abstain_frac
    if a_dmg and b_dmg:      # within-damage
        return 2.0 * abstain_frac
    return 1.0 * abstain_frac  # both non-damage

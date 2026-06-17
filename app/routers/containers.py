"""Container serving endpoint.

GET /flights/{flight_id}/next-container?user_id=...

Returns the highest-priority review container in a flight that:
  - is not done/skipped and has not been resolved,
  - still needs labels (fewer real `label` answers than its replication_target),
  - and this user has not already answered.

The payload is one labeling screen: the contested pair as ids and names (via
the flight's class_scheme), the chip URL per view, the model's per-class
probabilities, and the exemplar gallery with the contested classes first. When
the queue is exhausted it returns {"done": true, "flight_id": ...}.
"""
from __future__ import annotations

from typing import Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Container, Exemplar, Flight, Label, ResolvedLabel

router = APIRouter(tags=["containers"])


def get_db():
    """Request-scoped session. Move to app/db.py if other routers want it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- response schema -------------------------------------------------------

class ChipView(BaseModel):
    view: str
    url: str


class ExemplarOut(BaseModel):
    class_id: int
    class_name: str
    contested: bool          # one of this container's two contested classes
    chips: dict[str, str]    # view -> url, same views as the container


class NextContainerOut(BaseModel):
    container_id: int
    superpixel_id: int
    is_diffuse: bool
    class_a: int | None
    class_b: int | None
    class_a_name: str | None
    class_b_name: str | None
    priority: float
    abstain_frac: float
    model_probs: dict | None
    chips: list[ChipView]
    exemplars: list[ExemplarOut]


class QueueEmptyOut(BaseModel):
    done: bool = True
    flight_id: int


# ---- helpers ---------------------------------------------------------------

def _scheme_name(class_scheme, class_id: int | None) -> str | None:
    """Resolve a class id to its name via flight.class_scheme.

    Scheme shape: {"names": {id: name}, "damage": [...], "ignore_index": int}.
    JSONB keys come back as strings, so look up str(class_id) first.
    """
    if class_id is None or not isinstance(class_scheme, dict):
        return None
    names = class_scheme.get("names")
    if not isinstance(names, dict):
        return None
    return names.get(str(class_id)) or names.get(class_id)


# ---- endpoint --------------------------------------------------------------

@router.get(
    "/flights/{flight_id}/next-container",
    response_model=Union[NextContainerOut, QueueEmptyOut],
)
def next_container(
    flight_id: int,
    user_id: int,
    db: Session = Depends(get_db),
):
    flight = db.get(Flight, flight_id)
    if flight is None:
        raise HTTPException(status_code=404, detail="flight not found")

    # real answers per candidate container (skips/splits don't count toward
    # replication; correlated scalar subquery so it's per-row).
    label_count = (
        select(func.count())
        .select_from(Label)
        .where(Label.container_id == Container.id)
        .where(Label.action == "label")
        .scalar_subquery()
    )
    # has this user already answered the container (any action)?
    user_answered = (
        select(Label.container_id)
        .where(Label.container_id == Container.id)
        .where(Label.user_id == user_id)
        .exists()
    )
    # already resolved to a gold label?
    resolved = (
        select(ResolvedLabel.container_id)
        .where(ResolvedLabel.container_id == Container.id)
        .exists()
    )

    stmt = (
        select(Container)
        .where(Container.flight_id == flight_id)
        .where(Container.status.notin_(("done", "skipped")))
        .where(label_count < Container.replication_target)
        .where(~user_answered)
        .where(~resolved)
        .order_by(Container.priority.desc(), Container.superpixel_id.asc())
        .limit(1)
    )
    container = db.execute(stmt).scalars().first()

    if container is None:
        return QueueEmptyOut(flight_id=flight_id)

    # chip_keys already holds public URLs ({view: url}); no prefixing.
    chips = [
        ChipView(view=view, url=url)
        for view, url in (container.chip_keys or {}).items()
    ]

    # exemplar gallery, contested classes first
    contested = {c for c in (container.class_a, container.class_b) if c is not None}
    ex_rows = (
        db.execute(select(Exemplar).where(Exemplar.flight_id == flight_id))
        .scalars()
        .all()
    )
    exemplars = sorted(
        (
            ExemplarOut(
                class_id=e.class_id,
                class_name=_scheme_name(flight.class_scheme, e.class_id) or str(e.class_id),
                contested=e.class_id in contested,
                chips=e.chip_keys or {},
            )
            for e in ex_rows
        ),
        key=lambda x: (not x.contested, x.class_id),
    )

    return NextContainerOut(
        container_id=container.id,
        superpixel_id=container.superpixel_id,
        is_diffuse=container.is_diffuse,
        class_a=container.class_a,
        class_b=container.class_b,
        class_a_name=_scheme_name(flight.class_scheme, container.class_a),
        class_b_name=_scheme_name(flight.class_scheme, container.class_b),
        priority=container.priority,
        abstain_frac=container.abstain_frac,
        model_probs=container.model_probs,
        chips=chips,
        exemplars=exemplars,
    )

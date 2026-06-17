"""Labeling API.

Endpoints the front end uses:
  GET  /api/projects                     projects, each with its flights + progress
  GET  /api/projects/{id}                one project + flights + progress
  GET  /api/flights                      flat flight list + class schemes
  GET  /api/flights/{id}/next?user=...   next container for this user in the
                                         flight's ACTIVE round, with the contested
                                         pair, model_probs, chips, and the two
                                         classes' exemplars; or {done}
  POST /api/labels                       record/replace this user's answer

"Next" serves the active round only, skips superpixels this user already answered,
skips superpixels that already have a gold verdict (settled -> never re-asked),
and honors replication_target (single-coverage drops out once answered; replicated
stays open until it hits the target).

Answers are stored against the superpixel, not the container, so they survive the
container churn of the next retrain. The POST still takes a container_id (that is
what the UI has on screen); the server reads the flight/superpixel/round/pair off
that container and writes the durable label by superpixel.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from .constants import DEFAULT_SCHEME
from .db import SessionLocal
from .models import (
    ACTIONS,
    Container,
    Exemplar,
    Flight,
    Label,
    Project,
    ResolvedLabel,
    User,
)

router = APIRouter(prefix="/api")


def _scheme(flight: Flight):
    s = flight.class_scheme or DEFAULT_SCHEME
    names = {int(k): v for k, v in s.get("names", {}).items()}
    damage = {int(c) for c in s.get("damage", [])}
    return names, damage


def _get_or_create_user(db, email: str) -> User:
    u = db.scalar(select(User).where(User.email == email))
    if u is None:
        u = User(email=email, name=email.split("@")[0])
        db.add(u)
        db.commit()
        db.refresh(u)
    return u


def _flight_progress(db, flight_id: int, active_round: int) -> dict:
    """Round-aware progress: how many of this round's questions are open vs how
    many superpixels are permanently settled (gold verdicts) for the flight."""
    open_q = db.scalar(
        select(func.count())
        .select_from(Container)
        .where(Container.flight_id == flight_id, Container.round == active_round)
    )
    settled = db.scalar(
        select(func.count())
        .select_from(ResolvedLabel)
        .where(
            ResolvedLabel.flight_id == flight_id,
            ResolvedLabel.class_id.is_not(None),
        )
    )
    return {"round": active_round, "open_questions": open_q or 0, "settled": settled or 0}


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #
def _flight_brief(db, f: Flight) -> dict:
    p = _flight_progress(db, f.id, f.active_round)
    return {
        "id": f.id,
        "name": f.name,
        "gsd_cm": f.gsd_cm,
        "active_round": f.active_round,
        "class_scheme": f.class_scheme,
        "progress": p,
    }


@router.get("/projects")
def list_projects():
    db = SessionLocal()
    try:
        out = []
        projects = db.scalars(select(Project).order_by(Project.name)).all()
        for p in projects:
            flights = db.scalars(
                select(Flight)
                .where(Flight.project_id == p.id)
                .order_by(Flight.gsd_cm, Flight.name)
            ).all()
            out.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "notes": p.notes,
                    "flights": [_flight_brief(db, f) for f in flights],
                }
            )
        # flights not yet assigned to a project show up under a synthetic bucket
        # so nothing silently disappears from the picker.
        orphans = db.scalars(
            select(Flight).where(Flight.project_id.is_(None)).order_by(Flight.name)
        ).all()
        if orphans:
            out.append(
                {
                    "id": None,
                    "name": "Unassigned",
                    "notes": None,
                    "flights": [_flight_brief(db, f) for f in orphans],
                }
            )
        return out
    finally:
        db.close()


@router.get("/projects/{project_id}")
def get_project(project_id: int):
    db = SessionLocal()
    try:
        p = db.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "project not found")
        flights = db.scalars(
            select(Flight)
            .where(Flight.project_id == p.id)
            .order_by(Flight.gsd_cm, Flight.name)
        ).all()
        return {
            "id": p.id,
            "name": p.name,
            "notes": p.notes,
            "flights": [_flight_brief(db, f) for f in flights],
        }
    finally:
        db.close()


@router.get("/flights")
def list_flights():
    db = SessionLocal()
    try:
        flights = db.scalars(select(Flight).order_by(Flight.name)).all()
        return [
            {
                "id": f.id,
                "name": f.name,
                "project_id": f.project_id,
                "active_round": f.active_round,
                "class_scheme": f.class_scheme,
            }
            for f in flights
        ]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# labeling queue
# --------------------------------------------------------------------------- #
@router.get("/flights/{flight_id}/next")
def next_container(flight_id: int, user: str = Query(..., description="labeler email")):
    db = SessionLocal()
    try:
        flight = db.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(404, "flight not found")
        u = _get_or_create_user(db, user)
        names, damage = _scheme(flight)
        rnd = flight.active_round

        # superpixels this user has already answered (any action) -- skip them.
        answered_by_me = select(Label.superpixel_id).where(
            Label.flight_id == flight_id, Label.user_id == u.id
        )
        # superpixels with a gold verdict are settled permanently -- skip them.
        settled = select(ResolvedLabel.superpixel_id).where(
            ResolvedLabel.flight_id == flight_id,
            ResolvedLabel.class_id.is_not(None),
        )
        # how many "label" answers each superpixel already has, for replication.
        label_counts = (
            select(Label.superpixel_id, func.count().label("n"))
            .where(Label.flight_id == flight_id, Label.action == "label")
            .group_by(Label.superpixel_id)
            .subquery()
        )

        c = db.scalar(
            select(Container)
            .outerjoin(
                label_counts, label_counts.c.superpixel_id == Container.superpixel_id
            )
            .where(
                Container.flight_id == flight_id,
                Container.round == rnd,
                Container.superpixel_id.not_in(answered_by_me),
                Container.superpixel_id.not_in(settled),
                func.coalesce(label_counts.c.n, 0) < Container.replication_target,
            )
            .order_by(Container.priority.desc(), Container.id)
            .limit(1)
        )

        mine = db.scalar(
            select(func.count())
            .select_from(Label)
            .where(Label.flight_id == flight_id, Label.user_id == u.id)
        )
        progress = {"labeled": mine or 0, **_flight_progress(db, flight_id, rnd)}

        if c is None:
            return {"done": True, "progress": progress}

        exemplars = {}
        for cid in (c.class_a, c.class_b):
            if cid is None:
                continue
            rows = db.scalars(
                select(Exemplar)
                .where(Exemplar.flight_id == flight_id, Exemplar.class_id == cid)
                .order_by(Exemplar.id)
            ).all()
            exemplars[str(cid)] = [{"id": e.id, "chip_keys": e.chip_keys} for e in rows]

        return {
            "done": False,
            "progress": progress,
            "container": {
                "id": c.id,
                "round": c.round,
                "superpixel_id": c.superpixel_id,
                "abstain_frac": c.abstain_frac,
                "pair_purity": c.pair_purity,
                "is_diffuse": c.is_diffuse,
                "class_a": c.class_a,
                "class_b": c.class_b,
                "class_a_name": names.get(c.class_a),
                "class_b_name": names.get(c.class_b),
                "model_probs": c.model_probs,
                "chip_keys": c.chip_keys,
            },
            "exemplars": exemplars,
            "scheme": {
                "names": {str(k): v for k, v in names.items()},
                "damage": sorted(damage),
            },
        }
    finally:
        db.close()


def _recompute_verdict(db, flight_id: int, superpixel_id: int, replication_target: int):
    """Roll this superpixel's raw labels up into a gold verdict.

    Only "label" actions with a class count. Single coverage: one label settles
    it. Replicated: needs >= target agreeing labels. Disagreement (or a previously
    settled spot whose support went away) clears the verdict back to unresolved,
    unless a human adjudicated it. A verdict here is what retires the superpixel
    from future rounds, so it is deliberately conservative.
    """
    rows = db.scalars(
        select(Label).where(
            Label.flight_id == flight_id,
            Label.superpixel_id == superpixel_id,
            Label.action == "label",
            Label.class_id.is_not(None),
        )
    ).all()
    rl = db.get(ResolvedLabel, (flight_id, superpixel_id))
    if rl is not None and rl.method == "adjudicated":
        return  # a human override stands; don't auto-touch it

    classes = {r.class_id for r in rows}
    settled = len(classes) == 1 and len(rows) >= max(1, replication_target)
    if settled:
        cid = next(iter(classes))
        method = "single" if replication_target <= 1 else "majority"
        if rl is None:
            db.add(
                ResolvedLabel(
                    flight_id=flight_id,
                    superpixel_id=superpixel_id,
                    class_id=cid,
                    method=method,
                )
            )
        else:
            rl.class_id, rl.method = cid, method
    elif rl is not None:
        rl.class_id = None  # support insufficient/conflicting -> unresolved


@router.get("/flights/{flight_id}/stats")
def flight_stats(flight_id: int):
    """Everything the progress page needs for one flight: round, the settled-vs-
    open split, the verdict breakdown by class, and what each labeler has done."""
    db = SessionLocal()
    try:
        flight = db.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(404, "flight not found")
        names, _ = _scheme(flight)
        rnd = flight.active_round
        prog = _flight_progress(db, flight_id, rnd)

        vrows = db.execute(
            select(ResolvedLabel.class_id, func.count())
            .where(
                ResolvedLabel.flight_id == flight_id,
                ResolvedLabel.class_id.is_not(None),
            )
            .group_by(ResolvedLabel.class_id)
        ).all()
        verdicts = sorted(
            (
                {"class_id": cid, "name": names.get(cid, f"class {cid}"), "count": n}
                for cid, n in vrows
            ),
            key=lambda r: -r["count"],
        )

        arows = db.execute(
            select(User.email, Label.action, func.count())
            .join(User, User.id == Label.user_id)
            .where(Label.flight_id == flight_id)
            .group_by(User.email, Label.action)
        ).all()
        by_user: dict[str, dict] = {}
        for email, action, n in arows:
            d = by_user.setdefault(
                email, {"label": 0, "skip": 0, "split": 0, "other": 0, "total": 0}
            )
            d[action] = d.get(action, 0) + n
            d["total"] += n
        labelers = [
            {"email": e, **d}
            for e, d in sorted(by_user.items(), key=lambda kv: -kv[1]["total"])
        ]

        return {
            "flight": {
                "id": flight.id,
                "name": flight.name,
                "gsd_cm": flight.gsd_cm,
                "project_id": flight.project_id,
            },
            "round": rnd,
            "open_questions": prog["open_questions"],
            "settled": prog["settled"],
            "scheme": {"names": {str(k): v for k, v in names.items()}},
            "verdicts": verdicts,
            "labelers": labelers,
        }
    finally:
        db.close()


class LabelIn(BaseModel):
    container_id: int
    user: str
    action: str = "label"
    class_id: int | None = None
    confidence: int | None = None
    notes: str | None = None


@router.post("/labels")
def submit_label(body: LabelIn):
    if body.action not in ACTIONS:
        raise HTTPException(422, f"action must be one of {ACTIONS}")
    if body.action == "label" and body.class_id is None:
        raise HTTPException(422, "class_id is required when action is 'label'")

    db = SessionLocal()
    try:
        c = db.get(Container, body.container_id)
        if c is None:
            raise HTTPException(404, "container not found")
        u = _get_or_create_user(db, body.user)

        existing = db.scalar(
            select(Label).where(
                Label.flight_id == c.flight_id,
                Label.superpixel_id == c.superpixel_id,
                Label.user_id == u.id,
            )
        )
        if existing is not None:
            existing.action = body.action
            existing.class_id = body.class_id
            existing.confidence = body.confidence
            existing.notes = body.notes
            existing.round = c.round
            existing.pair_code = c.pair_code
            existing.class_a = c.class_a
            existing.class_b = c.class_b
        else:
            db.add(
                Label(
                    flight_id=c.flight_id,
                    superpixel_id=c.superpixel_id,
                    user_id=u.id,
                    action=body.action,
                    class_id=body.class_id,
                    confidence=body.confidence,
                    notes=body.notes,
                    round=c.round,
                    pair_code=c.pair_code,
                    class_a=c.class_a,
                    class_b=c.class_b,
                )
            )
        db.flush()
        _recompute_verdict(db, c.flight_id, c.superpixel_id, c.replication_target)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

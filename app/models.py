"""SQLAlchemy 2.0 models for the superpixel labeling app.

Hierarchy (top to bottom):
  projects          - one salt marsh / study site (e.g. "Wellfleet")
  flights           - one drone flight under a project; owns its own class scheme
                      and is the unit a labeler works on (a "task")
  containers        - one superpixel review item FOR ONE ROUND (the question).
                      EPHEMERAL: regenerated every retrain; safe to replace.

Durable layer, keyed to the superpixel rather than to any round's container:
  users             - the labelers
  labels            - one labeler's answer about a superpixel (the audit trail),
                      stamped with the round + contested pair it was given under
  resolved_labels   - the gold verdict per (flight, superpixel). This is the
                      training fact; it survives every retrain and, once set,
                      retires that superpixel from all future rounds.

The split is the point: a container is a disposable per-round QUESTION; a label/
verdict is a durable ANSWER about a place on the ground. Retraining throws away
the old round's containers and ingests new ones, and the answers persist because
they never pointed at a container in the first place.

Allowed-value sets are enforced with CHECK constraints over plain strings,
deliberately avoiding native PG enum types (which are painful under Alembic).
"""
from __future__ import annotations

import datetime as dt

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# All Wellfleet data lives in NAD83 / UTM 19N. Geometries are stored in this CRS
# so areas come out in meters. Change if you ever add another UTM zone.
SRID = 26919

ROLES = ("labeler", "admin", "ecologist")
ACTIONS = ("label", "skip", "split", "out_of_scope")
STATUSES = ("pending", "in_progress", "done", "skipped")
RESOLVE_METHODS = ("single", "majority", "adjudicated")


def _in_sql(values: tuple[str, ...]) -> str:
    """Render a Python tuple of strings as a SQL IN-list, e.g. ('a','b')."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    pass


class Project(Base):
    """A study site that groups one or more flights. Simple project = one flight
    (e.g. the 1cm); complex project = two flights (1cm + 4cm) of the same marsh,
    flown about the same day, each with its own scheme and its own queue."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    flights: Mapped[list["Flight"]] = relationship(
        back_populates="project", order_by="Flight.gsd_cm"
    )


class Flight(Base):
    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    crs: Mapped[str] = mapped_column(String, default=f"EPSG:{SRID}")
    gsd_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ortho_path: Mapped[str | None] = mapped_column(String, nullable=True)
    superpixel_path: Mapped[str | None] = mapped_column(String, nullable=True)
    abstain_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # the class scheme for THIS flight: {"names": {id: name}, "damage": [ids],
    # "ignore_index": int}. Class meanings/counts live here, not in code. Stays
    # per-flight on purpose: a project's 1cm and 4cm flights can use different
    # models with different schemes.
    class_scheme = mapped_column(JSONB, nullable=True)
    # how the labeler queue was built for THIS flight: the abstain rule values
    # (min_margin, mass_cutoff, min_abstain_frac, window_m) plus one real example
    # pixel, captured by the production notebook. Drives the "Why this tile?" panel
    # so the explanation tracks the actual run instead of drifting in the UI.
    selection_params = mapped_column(JSONB, nullable=True)
    # which round's containers the labeler queue currently serves. Bumped by each
    # retrain's ingest; old rounds' containers may linger for history but are not
    # served.
    active_round: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project | None"] = relationship(back_populates="flights")
    containers: Mapped[list["Container"]] = relationship(
        back_populates="flight", cascade="all, delete-orphan"
    )
    exemplars: Mapped[list["Exemplar"]] = relationship(
        back_populates="flight", cascade="all, delete-orphan"
    )


class Container(Base):
    """One superpixel review item for ONE round -- the question put to a labeler.

    Ephemeral: a retrain produces a fresh abstain set, which is ingested as a new
    round of containers; the previous round's containers can be dropped. Nothing
    durable hangs off a container (labels/verdicts key on the superpixel), so
    replacing a round never touches answers.
    """

    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), index=True
    )
    # which round (model generation) raised this question.
    round: Mapped[int] = mapped_column(Integer, default=1, index=True)
    # the superpixel's DN within this flight's superpixel-id raster; this is the
    # key that lets verdicts rasterize back onto the training mask, and the key
    # that ties a round's question to the durable answer.
    superpixel_id: Mapped[int] = mapped_column(Integer, index=True)
    geom = mapped_column(Geometry("POLYGON", srid=SRID), nullable=True)

    n_pixels: Mapped[int] = mapped_column(Integer, default=0)
    abstain_frac: Mapped[float] = mapped_column(Float, default=0.0)
    pair_purity: Mapped[float | None] = mapped_column(Float, nullable=True)
    diffuse_frac: Mapped[float | None] = mapped_column(Float, nullable=True)

    # contested pair (from abstain.py). class_a/class_b are the two tied classes
    # (a < b); null when the container is diffuse rather than a clean two-way tie.
    pair_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_a: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_b: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_diffuse: Mapped[bool] = mapped_column(Boolean, default=False)

    # mean per-class softmax over the container's pixels: {class_id: prob}.
    model_probs = mapped_column(JSONB, nullable=True)
    # rendered chip views: {"truecolor": url, "cir": url, "geomorphic": url, ...}
    chip_keys = mapped_column(JSONB, nullable=True)
    # per-superpixel decomposition the labeler sees: total pixels, the
    # confident-class breakdown, and the abstain "questions" (contested-pair
    # counts) + diffuse count. Computed at ingest from the abstain + softmax
    # rasters (see ingest._pixel_stats) -- the honest breakdown behind the mean.
    composition = mapped_column(JSONB, nullable=True)

    # serving order: higher = labeled sooner (see app.constants.pair_priority).
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    # how many independent labels we want (1 = single coverage; >1 = agreement).
    replication_target: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    flight: Mapped["Flight"] = relationship(back_populates="containers")

    __table_args__ = (
        UniqueConstraint(
            "flight_id", "round", "superpixel_id", name="uq_container_flight_round_sp"
        ),
        CheckConstraint(f"status in {_in_sql(STATUSES)}", name="ck_container_status"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, default="labeler")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    labels: Mapped[list["Label"]] = relationship(back_populates="user")

    __table_args__ = (CheckConstraint(f"role in {_in_sql(ROLES)}", name="ck_user_role"),)


class Label(Base):
    """One labeler's answer about one superpixel -- the durable audit trail.

    Keyed to (flight, superpixel, user), NOT to a container: the answer is a fact
    about a place on the ground and must outlive the round-specific container that
    happened to ask the question. The round and contested pair the answer was
    given under are stamped here (denormalized) so the trail stays interpretable
    even after that round's containers are gone.
    """

    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), index=True
    )
    superpixel_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String, default="label")
    class_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # null: skip/split

    # audit context: the round + contested pair this answer was given under.
    round: Mapped[int] = mapped_column(Integer, default=1)
    pair_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_a: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_b: Mapped[int | None] = mapped_column(Integer, nullable=True)

    confidence: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # optional 1-5
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="labels")

    __table_args__ = (
        UniqueConstraint(
            "flight_id", "superpixel_id", "user_id", name="uq_label_flight_sp_user"
        ),
        CheckConstraint(f"action in {_in_sql(ACTIONS)}", name="ck_label_action"),
    )


class ResolvedLabel(Base):
    """The gold verdict per (flight, superpixel) -- the durable training fact.

    This is the layer that rasterizes back onto the superpixel raster to build a
    training mask, and the layer the queue checks to retire settled superpixels.
    A superpixel is RETIRED (never re-asked, in any round, for anyone) once it is
    either resolved to a class (class_id set) OR marked out of scope. A "skip"
    never produces a verdict, so genuinely-skipped superpixels can resurface in a
    later round. out_of_scope rows carry class_id=None: they're excluded from GT
    and training, not labeled as a class.
    """

    __tablename__ = "resolved_labels"

    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), primary_key=True
    )
    superpixel_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # null = unresolved
    # True = labeler marked this superpixel out of scope (water, mudflat, image
    # edge, junk). Retires it for everyone, but it is NOT a class: kept out of GT
    # and training. Mutually exclusive with class_id in practice.
    out_of_scope: Mapped[bool] = mapped_column(Boolean, default=False)
    method: Mapped[str] = mapped_column(String, default="single")
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    is_gold: Mapped[bool] = mapped_column(Boolean, default=False)
    # When the verdict first came into existence. Stable -- no onupdate -- so it
    # is the correct snapshot key for "training set as of T". updated_at moves
    # whenever a verdict is re-touched (e.g. adjudication), so it must NOT be
    # used to decide which verdicts existed at a past point in time.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"method in {_in_sql(RESOLVE_METHODS)}", name="ck_resolved_method"),
    )


class Exemplar(Base):
    """A reference chip for one class, rendered from a labeled polygon and shown
    to labelers as a calibration gallery ("this is what crab_edge looks like").

    Tied to a flight and rendered with that flight's view specs, so the gallery
    matches the chips the labeler is judging. class_id is interpreted through the
    flight's class_scheme like every other class id -- nothing fixed here.
    """

    __tablename__ = "exemplars"

    id: Mapped[int] = mapped_column(primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), index=True
    )
    class_id: Mapped[int] = mapped_column(Integer, index=True)
    source_fid: Mapped[int | None] = mapped_column(Integer, nullable=True)  # labeled polygon
    chip_keys = mapped_column(JSONB, nullable=True)   # {view: url}, same views as containers
    geom = mapped_column(Geometry("POLYGON", srid=SRID), nullable=True)  # the sampled window
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    flight: Mapped["Flight"] = relationship(back_populates="exemplars")

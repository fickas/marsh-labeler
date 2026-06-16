"""SQLAlchemy 2.0 models for the superpixel labeling app.

Five tables:
  flights          - one drone flight / dataset
  containers        - one superpixel review item (the unit of work)
  users             - the labelers
  labels            - raw per-(container, user) answers  (the audit trail)
  resolved_labels   - one resolved/gold label per container (feeds retraining)

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
ACTIONS = ("label", "skip", "split", "other")
STATUSES = ("pending", "in_progress", "done", "skipped")
RESOLVE_METHODS = ("single", "majority", "adjudicated")


def _in_sql(values: tuple[str, ...]) -> str:
    """Render a Python tuple of strings as a SQL IN-list, e.g. ('a','b')."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    pass


class Flight(Base):
    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    crs: Mapped[str] = mapped_column(String, default=f"EPSG:{SRID}")
    gsd_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ortho_path: Mapped[str | None] = mapped_column(String, nullable=True)
    superpixel_path: Mapped[str | None] = mapped_column(String, nullable=True)
    abstain_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # the class scheme for THIS flight: {"names": {id: name}, "damage": [ids],
    # "ignore_index": int}. Class meanings/counts live here, not in code.
    class_scheme = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    containers: Mapped[list["Container"]] = relationship(
        back_populates="flight", cascade="all, delete-orphan"
    )


class Container(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), index=True
    )
    # the superpixel's DN within this flight's superpixel-id raster; this is the
    # key that lets resolved labels rasterize back onto the training mask.
    superpixel_id: Mapped[int] = mapped_column(Integer)
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
    # powers the "model says 48% edge / 45% platform" assist.
    model_probs = mapped_column(JSONB, nullable=True)
    # rendered chip views: {"truecolor": url, "cir": url, "geomorphic": url, ...}
    chip_keys = mapped_column(JSONB, nullable=True)

    # serving order: higher = labeled sooner (see app.constants.pair_priority).
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    # how many independent labels we want (1 = single coverage; >1 = agreement).
    replication_target: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    flight: Mapped["Flight"] = relationship(back_populates="containers")
    labels: Mapped[list["Label"]] = relationship(
        back_populates="container", cascade="all, delete-orphan"
    )
    resolved: Mapped["ResolvedLabel | None"] = relationship(
        back_populates="container", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("flight_id", "superpixel_id", name="uq_container_flight_sp"),
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
    """One labeler's answer for one container. Unique per (container, user), so a
    person gives a single (updatable) answer and we can compare across people."""

    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    container_id: Mapped[int] = mapped_column(
        ForeignKey("containers.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String, default="label")
    class_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # null: skip/split
    confidence: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # optional 1-5
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    container: Mapped["Container"] = relationship(back_populates="labels")
    user: Mapped["User"] = relationship(back_populates="labels")

    __table_args__ = (
        UniqueConstraint("container_id", "user_id", name="uq_label_container_user"),
        CheckConstraint(f"action in {_in_sql(ACTIONS)}", name="ck_label_action"),
    )


class ResolvedLabel(Base):
    """The gold label per container (majority / single / adjudicated). This is the
    layer that rasterizes back to a training mask; raw `labels` stay as the trail."""

    __tablename__ = "resolved_labels"

    container_id: Mapped[int] = mapped_column(
        ForeignKey("containers.id", ondelete="CASCADE"), primary_key=True
    )
    class_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # null = unresolved
    method: Mapped[str] = mapped_column(String, default="single")
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    is_gold: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    container: Mapped["Container"] = relationship(back_populates="resolved")

    __table_args__ = (
        CheckConstraint(f"method in {_in_sql(RESOLVE_METHODS)}", name="ck_resolved_method"),
    )

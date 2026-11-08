import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# JSONB on PostgreSQL (indexable, canonical), plain JSON elsewhere (SQLite in offline tests).
JsonType = JSON().with_variant(JSONB(), "postgresql")

# Research run lifecycle: queued -> running -> completed | failed
#   running -> queued   (worker died; lease expired; attempts remain)
RUN_STATUSES = ("queued", "running", "completed", "failed")

# Opportunity lifecycle. Only `pending_approval` / `revision_requested` accept a decision.
PENDING = "pending_approval"
BELOW_THRESHOLD = "below_threshold"
APPROVED = "approved"
REJECTED = "rejected"
REVISION_REQUESTED = "revision_requested"
DECISIONS = (APPROVED, REJECTED, REVISION_REQUESTED)
DECIDABLE_STATUSES = (PENDING, REVISION_REQUESTED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    # Research domain: "apps" (utility app ideas) or "games" (2D game concepts). Added after the first
    # release: init_db() adds the column to existing databases (see core/database.py).
    domain: Mapped[str] = mapped_column(String(16), default="apps", server_default="apps")
    brief: Mapped[str] = mapped_column(Text)
    regions: Mapped[list] = mapped_column(JsonType, default=list)
    categories: Mapped[list] = mapped_column(JsonType, default=list)
    max_opportunities: Mapped[int] = mapped_column(Integer, default=5)
    requested_by: Mapped[str] = mapped_column(String(64), default="owner")

    # Job bookkeeping. PostgreSQL is the source of truth; Redis only delivers the wake-up.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_ledger: Mapped[dict | None] = mapped_column(JsonType, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(16), default="apps", server_default="apps")
    # Domain-specific descriptive fields (games: genre, core_loop, twist, ad_moments, prototype_scope, hook_clip).
    details: Mapped[dict] = mapped_column(JsonType, default=dict, server_default=text("'{}'"))
    name: Mapped[str] = mapped_column(String(180))
    elevator_pitch: Mapped[str] = mapped_column(Text)
    target_audience: Mapped[str] = mapped_column(Text)
    problem: Mapped[str] = mapped_column(Text)
    proposed_solution: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100), index=True)
    target_regions: Mapped[list] = mapped_column(JsonType, default=list)
    competitors: Mapped[list] = mapped_column(JsonType, default=list)
    evidence: Mapped[list] = mapped_column(JsonType, default=list)
    monetization_hypothesis: Mapped[str] = mapped_column(Text)
    ad_suitability: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks: Mapped[list] = mapped_column(JsonType, default=list)
    assumptions: Mapped[list] = mapped_column(JsonType, default=list)
    validation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Deterministic scoring output (see app/research/scoring.py)
    total_score: Mapped[float] = mapped_column(Float, default=0, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    confidence_label: Mapped[str] = mapped_column(String(16), default="low")
    score_breakdown: Mapped[dict] = mapped_column(JsonType, default=dict)
    scoring_version: Mapped[str] = mapped_column(String(16), default="")
    warnings: Mapped[list] = mapped_column(JsonType, default=list)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    decision_status: Mapped[str] = mapped_column(String(32), default=PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped["ResearchRun"] = relationship(back_populates="opportunities")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="opportunity")


class Approval(Base):
    """Append-only decision history (enforced by a trigger on PostgreSQL)."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # RESTRICT, not CASCADE: an opportunity with decision history must not be deletable.
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id", ondelete="RESTRICT"), index=True)
    decision: Mapped[str] = mapped_column(String(32))
    previous_status: Mapped[str] = mapped_column(String(32))
    reviewer: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_at_decision: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    opportunity: Mapped["Opportunity"] = relationship(back_populates="approvals")

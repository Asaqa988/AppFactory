"""API response models (kept separate from ORM models so the wire format is explicit)."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunCreated(_Out):
    run_id: str
    status: str


class RunSummary(_Out):
    id: str
    status: str
    domain: str
    brief: str
    requested_by: str
    attempts: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None


class RunDetail(RunSummary):
    regions: list
    categories: list
    max_opportunities: int
    model: str | None
    summary: str | None = None
    opportunity_ids: list[str] = []
    source_count: int = 0


class OpportunityOut(_Out):
    id: str
    run_id: str
    domain: str
    details: dict
    name: str
    elevator_pitch: str
    target_audience: str
    problem: str
    proposed_solution: str
    category: str
    target_regions: list
    competitors: list
    evidence: list
    monetization_hypothesis: str
    ad_suitability: str | None
    risks: list
    assumptions: list
    validation_notes: str | None
    total_score: float
    confidence: float
    confidence_label: str
    score_breakdown: dict
    scoring_version: str
    warnings: list
    recommended: bool
    decision_status: str
    created_at: datetime


class OpportunitySummary(_Out):
    id: str
    run_id: str
    domain: str
    name: str
    category: str
    total_score: float
    confidence_label: str
    recommended: bool
    decision_status: str
    created_at: datetime


class ApprovalOut(_Out):
    id: str
    opportunity_id: str
    decision: str
    previous_status: str
    reviewer: str
    notes: str | None
    score_at_decision: float
    created_at: datetime


class DecisionOut(_Out):
    opportunity_id: str
    decision_status: str
    approval_id: str
    reviewer: str

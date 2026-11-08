"""Pydantic schemas.

Three layers, deliberately separate:
  * API request models           (ResearchRequest, DecisionRequest)
  * LLM-facing "draft" models    (*Draft) - what agents are allowed to produce. Unknown fields the
                                  model invents (e.g. "estimated_downloads") are ignored, never stored.
  * Stored models                (EvidenceItem, Competitor, ScoredOpportunity) - produced by the
                                  Python validation step, which adds verification flags and scores.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.research.ledger import normalize_url

Domain = Literal["apps", "games"]

DimensionName = Literal[
    "unmet_user_need", "demand_evidence", "monetization_fit",
    "retention_potential", "competition_gap", "build_feasibility",
]
# Evidence may be tagged with any dimension of either domain; the scorer ignores tags that are not part of
# the run's domain (`build_feasibility` is shared).
EvidenceDimension = Literal[
    "unmet_user_need", "demand_evidence", "monetization_fit",
    "retention_potential", "competition_gap", "build_feasibility",
    "gameplay_gap", "genre_demand", "shareability", "retention_loop", "ad_fit", "distribution_path",
    "general",
]


def _clean_list(values: list[str], max_items: int, max_len: int) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = " ".join(str(value).split())[:max_len]
        if text and text.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(text)
    return cleaned[:max_items]


# ----------------------------- API requests -----------------------------

class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(min_length=20, max_length=5000)
    regions: list[str] = Field(default_factory=lambda: ["Global"], max_length=20)
    categories: list[str] = Field(default_factory=list, max_length=20)
    max_opportunities: int = Field(default=5, ge=1, le=10)
    # "apps": utility/consumer app ideas. "games": 2D game concepts (different rubric, agents and tools).
    domain: Domain = "apps"

    @field_validator("regions", "categories")
    @classmethod
    def _tidy(cls, value: list[str]) -> list[str]:
        return _clean_list(value, max_items=20, max_len=80)

    @field_validator("regions")
    @classmethod
    def _regions_not_empty(cls, value: list[str]) -> list[str]:
        return value or ["Global"]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected", "revision_requested"]
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("notes")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


# ----------------------------- LLM-facing drafts -----------------------------

class DimensionScores(BaseModel):
    """Agent-proposed 0-100 scores. Opinions, not data: the scoring engine caps them by evidence."""

    unmet_user_need: float = Field(ge=0, le=100)
    demand_evidence: float = Field(ge=0, le=100)
    monetization_fit: float = Field(ge=0, le=100)
    retention_potential: float = Field(ge=0, le=100)
    competition_gap: float = Field(ge=0, le=100)
    build_feasibility: float = Field(ge=0, le=100)


class EvidenceDraft(BaseModel):
    claim: str = Field(description="One factual statement supported by the source.", min_length=5, max_length=600)
    url: str | None = Field(
        default=None,
        description="Exact URL returned by a search tool. Required for observed/estimate; omit for assumptions.",
    )
    source_name: str | None = Field(default=None, max_length=200)
    dimension: EvidenceDimension = Field(
        default="general", description="The scoring dimension this evidence supports."
    )
    evidence_type: Literal["observed", "estimate", "assumption"] = Field(
        default="observed",
        description="observed = stated in the source; estimate = your inference from a cited source; "
        "assumption = unsupported by any source.",
    )
    published_at: str | None = Field(default=None, max_length=40, description="Publication date if visible, else null.")


class CompetitorDraft(BaseModel):
    """No rating/download fields on purpose: ratings are attached from Apple API data only."""

    name: str = Field(max_length=200)
    url: str | None = Field(default=None, description="Store listing or product page URL from a search tool.")
    platform: Literal["ios", "android", "web", "unknown"] = "unknown"
    positioning: str | None = Field(default=None, max_length=500)
    gap: str | None = Field(default=None, max_length=500, description="Observed weakness or missing capability.")


class OpportunityDraft(BaseModel):
    name: str = Field(max_length=180)
    elevator_pitch: str
    target_audience: str
    problem: str
    proposed_solution: str
    category: str = Field(max_length=100)
    target_regions: list[str] = Field(default_factory=list)
    competitors: list[CompetitorDraft] = Field(default_factory=list)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    monetization_hypothesis: str
    ad_suitability: str = Field(default="", description="Where ads fit naturally and the policy risks. No eCPM figures.")
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    validation_notes: str | None = None
    dimension_scores: DimensionScores
    score_rationale: str = ""


class SynthesisOutput(BaseModel):
    research_summary: str
    opportunities: list[OpportunityDraft]


class GameDimensionScores(BaseModel):
    """Agent-proposed 0-100 scores for a game concept. Opinions, capped by evidence like DimensionScores."""

    gameplay_gap: float = Field(ge=0, le=100, description="How different the core mechanic is from named competitors.")
    genre_demand: float = Field(ge=0, le=100, description="Observed traction of the genre (charts, trends, communities).")
    shareability: float = Field(ge=0, le=100, description="Would a 10-15 second clip of it make a stranger want to play?")
    retention_loop: float = Field(ge=0, le=100, description="Is there a reason to come back tomorrow?")
    ad_fit: float = Field(ge=0, le=100, description="Natural rewarded/interstitial ad moments that do not annoy players.")
    distribution_path: float = Field(ge=0, le=100, description="Plausible way to reach players without paid acquisition.")
    build_feasibility: float = Field(ge=0, le=100, description="Scope a single developer can ship in Godot 2D.")


class GameOpportunityDraft(OpportunityDraft):
    """A game concept. `problem` = what players are missing or put up with; `proposed_solution` = the concept."""

    dimension_scores: GameDimensionScores  # type: ignore[assignment]
    genre: str = Field(max_length=100, description="Primary genre, e.g. 'merge puzzle' or 'word puzzle'.")
    core_loop: str = Field(description="What the player does every few seconds, in one or two sentences.")
    twist: str = Field(
        description="What is different from the named competitors. Describe the difference; never claim that "
        "nothing similar exists (store searches only sample the market)."
    )
    ad_moments: list[str] = Field(default_factory=list, description="Natural rewarded/interstitial placements. No eCPM.")
    prototype_scope: str = Field(default="", description="What a one-person Godot 2D prototype would contain.")
    hook_clip: str = Field(default="", description="The 10-15 second clip that would make someone want to play.")


class GameSynthesisOutput(BaseModel):
    research_summary: str
    opportunities: list[GameOpportunityDraft]


# ----------------------------- Stored / validated models -----------------------------

class EvidenceItem(BaseModel):
    claim: str
    url: str | None = None
    source_name: str | None = None
    dimension: EvidenceDimension = "general"
    # observed | estimate | assumption | unverified (cited URL never returned by a tool this run)
    evidence_type: Literal["observed", "estimate", "assumption", "unverified"] = "observed"
    published_at: str | None = None
    retrieved_at: str | None = None
    url_verified: bool = False

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        return value if value is None or normalize_url(value) else None


class Competitor(BaseModel):
    name: str
    url: str | None = None
    platform: str = "unknown"
    positioning: str | None = None
    gap: str | None = None
    # Populated only from Apple Search API data recorded in the evidence ledger.
    rating: float | None = None
    rating_count: int | None = None
    price: str | None = None
    rating_source: str | None = None
    rating_retrieved_at: str | None = None


class ScoredOpportunity(BaseModel):
    name: str
    elevator_pitch: str
    target_audience: str
    problem: str
    proposed_solution: str
    category: str
    target_regions: list[str]
    competitors: list[Competitor]
    evidence: list[EvidenceItem]
    monetization_hypothesis: str
    ad_suitability: str
    risks: list[str]
    assumptions: list[str]
    validation_notes: str | None
    total_score: float
    confidence: float
    confidence_label: str
    score_breakdown: dict
    scoring_version: str
    warnings: list[str]
    recommended: bool
    domain: str = "apps"
    # Domain-specific descriptive fields (games: genre, core_loop, twist, ad_moments, prototype_scope, hook_clip).
    details: dict = Field(default_factory=dict)


class ResearchResult(BaseModel):
    research_summary: str
    opportunities: list[ScoredOpportunity]

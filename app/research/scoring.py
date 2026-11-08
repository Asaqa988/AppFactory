"""Deterministic opportunity scoring. No LLM calls, no network, no randomness.

Agents propose a 0-100 score per dimension. Those numbers are *opinions*, not market data, so they
are never used as-is: each dimension score is capped by how much verified, observed evidence backs
it. An opportunity with no sourced evidence cannot clear the recommendation threshold no matter what
the agents claim, and its confidence is reported as low.
"""
from dataclasses import dataclass, field
from urllib.parse import urlparse

SCORING_VERSION = "1.0"

WEIGHTS: dict[str, float] = {
    "unmet_user_need": 0.20,
    "demand_evidence": 0.20,
    "monetization_fit": 0.20,
    "retention_potential": 0.15,
    "competition_gap": 0.15,
    "build_feasibility": 0.10,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

# Games domain. These weights are the owner's heuristic, not calibrated against any outcome data: a new
# game usually fails on discovery and on whether a 10-second clip makes anyone want to play it, so those
# two (shareability, distribution_path) carry weight that "unmet need" carries for utility apps.
# `build_feasibility` is shared with the apps domain (same key, different weight).
GAME_WEIGHTS: dict[str, float] = {
    "gameplay_gap": 0.20,
    "genre_demand": 0.20,
    "shareability": 0.15,
    "retention_loop": 0.15,
    "ad_fit": 0.15,
    "distribution_path": 0.10,
    "build_feasibility": 0.05,
}
GAME_SCORING_VERSION = "games-1.0"
assert abs(sum(GAME_WEIGHTS.values()) - 1.0) < 1e-9

DOMAIN_WEIGHTS: dict[str, dict[str, float]] = {"apps": WEIGHTS, "games": GAME_WEIGHTS}
DOMAIN_VERSIONS: dict[str, str] = {"apps": SCORING_VERSION, "games": GAME_SCORING_VERSION}

RECOMMEND_THRESHOLD = 70.0

# Evidence strength -> ceiling on the dimension score.
# "Effective sources" = distinct hostnames of verified observed evidence, plus half a point per
# distinct hostname of verified estimates (an estimate is a reasoned inference that cites a source).
# Assumptions and evidence whose URL was not returned by a tool during the run count for nothing.
_CAP_TABLE = (  # (minimum effective sources, ceiling)
    (3.0, 100.0),
    (2.0, 85.0),
    (1.0, 65.0),
    (0.5, 50.0),
    (0.0, 40.0),
)
FULL_CONFIDENCE_SOURCES = 3.0


@dataclass(frozen=True)
class EvidenceRef:
    """The minimum the scorer needs to know about one evidence item."""

    dimension: str  # a key of the domain's weights, or "general"
    evidence_type: str  # observed | estimate | assumption
    url: str | None
    url_verified: bool


@dataclass
class DimensionResult:
    agent_score: float
    capped_score: float
    effective_sources: float
    confidence: float
    capped: bool

    def as_dict(self) -> dict:
        return {
            "agent_score": self.agent_score,
            "capped_score": self.capped_score,
            "effective_sources": self.effective_sources,
            "confidence": self.confidence,
            "capped": self.capped,
        }


@dataclass
class ScoreResult:
    total_score: float
    confidence: float
    confidence_label: str
    recommended: bool
    dimensions: dict[str, DimensionResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def breakdown(self) -> dict:
        return {k: v.as_dict() for k, v in self.dimensions.items()}


def _clamp(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(100.0, number))


def _host(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def effective_sources(evidence: list[EvidenceRef], dimension: str) -> float:
    observed: set[str] = set()
    estimated: set[str] = set()
    for item in evidence:
        if item.dimension != dimension or not item.url_verified:
            continue
        host = _host(item.url)
        if host is None:
            continue
        if item.evidence_type == "observed":
            observed.add(host)
        elif item.evidence_type == "estimate":
            estimated.add(host)
    return len(observed) + 0.5 * len(estimated - observed)


def evidence_cap(sources: float) -> float:
    for minimum, ceiling in _CAP_TABLE:
        if sources >= minimum:
            return ceiling
    return _CAP_TABLE[-1][1]


def confidence_label(confidence: float) -> str:
    if confidence >= 0.67:
        return "high"
    if confidence >= 0.34:
        return "medium"
    return "low"


def score_opportunity(
    dimension_scores: dict,
    evidence: list[EvidenceRef],
    threshold: float = RECOMMEND_THRESHOLD,
    weights: dict[str, float] | None = None,
) -> ScoreResult:
    dimensions: dict[str, DimensionResult] = {}
    warnings: list[str] = []
    total = 0.0
    confidence = 0.0

    for name, weight in (weights or WEIGHTS).items():
        if name not in dimension_scores:
            warnings.append(f"{name}: no score provided; treated as 0")
        agent_score = _clamp(dimension_scores.get(name, 0))
        sources = effective_sources(evidence, name)
        ceiling = evidence_cap(sources)
        capped_score = min(agent_score, ceiling)
        dim_confidence = min(1.0, sources / FULL_CONFIDENCE_SOURCES)
        if capped_score < agent_score:
            warnings.append(
                f"{name}: agent score {agent_score:g} capped to {capped_score:g} "
                f"({sources:g} verified evidence source(s))"
            )
        dimensions[name] = DimensionResult(
            agent_score=agent_score,
            capped_score=capped_score,
            effective_sources=sources,
            confidence=round(dim_confidence, 4),
            capped=capped_score < agent_score,
        )
        total += capped_score * weight
        confidence += dim_confidence * weight

    total = round(total, 2)
    confidence = round(confidence, 4)
    return ScoreResult(
        total_score=total,
        confidence=confidence,
        confidence_label=confidence_label(confidence),
        recommended=total >= threshold,
        dimensions=dimensions,
        warnings=warnings,
    )

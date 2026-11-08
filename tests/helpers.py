from app.models.entities import PENDING, Opportunity, ResearchRun
from app.research.ledger import EvidenceLedger
from app.research.scoring import EvidenceRef
from app.core.database import SessionLocal
from app.schemas.research import OpportunityDraft, SynthesisOutput

DIMENSIONS = ["unmet_user_need", "demand_evidence", "monetization_fit",
              "retention_potential", "competition_gap", "build_feasibility"]


def refs(per_dimension: int, evidence_type="observed", verified=True) -> list[EvidenceRef]:
    """`per_dimension` distinct-host evidence items for every dimension."""
    return [
        EvidenceRef(dim, evidence_type, f"https://site{i}.example.org/{dim}", verified)
        for dim in DIMENSIONS for i in range(per_dimension)
    ]


def ledger_with(*urls: str) -> EvidenceLedger:
    ledger = EvidenceLedger()
    for url in urls:
        ledger.record_result("web_search", url, "title", "2026-01-02", "snippet")
    ledger.record_call("web_search", "q", ok=True, result_count=len(urls))
    return ledger


def draft(name="Offline Habit Tracker", scores=90, evidence=None, competitors=None, **extra) -> dict:
    data = {
        "name": name, "elevator_pitch": "pitch", "target_audience": "students", "problem": "problem",
        "proposed_solution": "solution", "category": "productivity", "target_regions": ["US"],
        "competitors": competitors or [], "evidence": evidence or [],
        "monetization_hypothesis": "banner ads between sessions", "ad_suitability": "good",
        "risks": ["crowded"], "assumptions": [], "validation_notes": None,
        "dimension_scores": {d: scores for d in DIMENSIONS}, "score_rationale": "because",
    }
    data.update(extra)
    return data


def evidence_for_all(urls: list[str], evidence_type="observed") -> list[dict]:
    return [
        {"claim": f"claim {dim} {i}", "url": url, "dimension": dim, "evidence_type": evidence_type}
        for dim in DIMENSIONS for i, url in enumerate(urls)
    ]


def synthesis(*drafts: dict, summary="summary") -> SynthesisOutput:
    return SynthesisOutput(research_summary=summary, opportunities=[OpportunityDraft(**d) for d in drafts])


async def seed_run(status="queued", **overrides) -> str:
    async with SessionLocal() as session:
        run = ResearchRun(
            brief="Find underserved consumer app opportunities for a small team.",
            regions=["US"], categories=["productivity"], max_opportunities=5, status=status, **overrides,
        )
        session.add(run)
        await session.commit()
        return run.id


async def seed_opportunity(run_id: str, score=82.0, status=PENDING, name="Seeded idea") -> str:
    async with SessionLocal() as session:
        opp = Opportunity(
            run_id=run_id, name=name, elevator_pitch="p", target_audience="a", problem="pr",
            proposed_solution="s", category="productivity", monetization_hypothesis="m",
            total_score=score, recommended=score >= 70, decision_status=status,
        )
        session.add(opp)
        await session.commit()
        return opp.id


# ----------------------------------------------------------------- games domain

GAME_DIMENSIONS = ["gameplay_gap", "genre_demand", "shareability", "retention_loop",
                   "ad_fit", "distribution_path", "build_feasibility"]


def game_draft(name="Gravity Merge", scores=90, evidence=None, competitors=None, **extra) -> dict:
    data = draft(name=name, scores=scores, evidence=evidence, competitors=competitors)
    data.update({
        "dimension_scores": {d: scores for d in GAME_DIMENSIONS},
        "genre": "merge puzzle", "core_loop": "Tilt the phone so same-tier balls roll together and merge.",
        "twist": "Gravity direction is player-controlled; the closest listed games drop pieces from the top only.",
        "ad_moments": ["rewarded revive after a full board"], "prototype_scope": "one board, five ball tiers",
        "hook_clip": "four merges in a row from one tilt",
    })
    data.update(extra)
    return data


def game_evidence_for_all(urls: list[str], evidence_type="observed") -> list[dict]:
    return [
        {"claim": f"claim {dim} {i}", "url": url, "dimension": dim, "evidence_type": evidence_type}
        for dim in GAME_DIMENSIONS for i, url in enumerate(urls)
    ]


def game_synthesis(*drafts: dict, summary="summary"):
    from app.schemas.research import GameOpportunityDraft, GameSynthesisOutput

    return GameSynthesisOutput(research_summary=summary, opportunities=[GameOpportunityDraft(**d) for d in drafts])

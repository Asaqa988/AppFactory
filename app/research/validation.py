"""Turns LLM output (SynthesisOutput) into verified, scored opportunities. Pure Python, no I/O.

Rules enforced here, regardless of what the model wrote:
  * An evidence URL counts only if a tool returned it during this run (ledger lookup).
    Otherwise the item is kept but relabelled `unverified` and contributes nothing to the score.
  * Competitor ratings/prices come only from recorded Apple Search API responses. Anything the
    model wrote about ratings is discarded because the draft schema has no such field.
  * A competitor URL no tool returned is removed.
  * Scores come from app.research.scoring, never from the model.
"""
from app.research.ledger import EvidenceLedger
from app.research.scoring import (
    DOMAIN_VERSIONS, DOMAIN_WEIGHTS, RECOMMEND_THRESHOLD, EvidenceRef, score_opportunity,
)
from app.schemas.research import (
    Competitor, CompetitorDraft, EvidenceDraft, EvidenceItem, GameSynthesisOutput, OpportunityDraft,
    ResearchResult, ScoredOpportunity, SynthesisOutput,
)

# Descriptive game fields carried through to storage (see GameOpportunityDraft).
GAME_DETAIL_FIELDS = ("genre", "core_loop", "twist", "ad_moments", "prototype_scope", "hook_clip")
GAME_NOVELTY_WARNING = (
    "novelty is judged against a sample of Apple App Store results only; Google Play, Steam and itch.io "
    "were not searched, so 'different from existing games' is unverified"
)


def verify_evidence(draft: EvidenceDraft, ledger: EvidenceLedger, warnings: list[str]) -> EvidenceItem:
    base = dict(
        claim=draft.claim, source_name=draft.source_name, dimension=draft.dimension,
        published_at=draft.published_at,
    )
    if draft.evidence_type == "assumption":
        return EvidenceItem(**base, url=draft.url, evidence_type="assumption")

    entry = ledger.lookup(draft.url)
    if entry is None:
        reason = "no URL" if not draft.url else f"URL not returned by any research tool: {draft.url}"
        warnings.append(f"evidence downgraded to unverified ({reason}): {draft.claim[:80]}")
        return EvidenceItem(**base, url=draft.url, evidence_type="unverified")

    base["published_at"] = draft.published_at or entry.get("published_date")
    return EvidenceItem(
        **base, url=entry["url"], evidence_type=draft.evidence_type,
        retrieved_at=entry["retrieved_at"], url_verified=True,
    )


def _dedupe_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[tuple] = set()
    unique = []
    for item in items:
        key = (item.url or "", item.dimension, item.claim.strip().lower())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def attach_competitor(draft: CompetitorDraft, ledger: EvidenceLedger, warnings: list[str]) -> Competitor:
    record = ledger.lookup_apple(draft.url)
    if record is None:  # fall back to an exact name match against Apple results from this run
        wanted = draft.name.strip().lower()
        record = next((r for r in ledger.apple.values() if (r.get("name") or "").strip().lower() == wanted), None)

    url = draft.url
    if record is not None:
        url = record["url"]
    elif ledger.lookup(draft.url) is None:
        if draft.url:
            warnings.append(f"competitor '{draft.name}': URL not returned by any tool; removed")
        url = None

    competitor = Competitor(
        name=draft.name, url=url, platform="ios" if record else draft.platform,
        positioning=draft.positioning, gap=draft.gap,
    )
    if record is not None:
        competitor.rating = record.get("rating")
        competitor.rating_count = record.get("rating_count")
        competitor.price = record.get("price")
        competitor.rating_source = "Apple Search API"
        competitor.rating_retrieved_at = record.get("retrieved_at")
    return competitor


def finalize_opportunity(
    draft: OpportunityDraft, ledger: EvidenceLedger, default_regions: list[str],
    threshold: float = RECOMMEND_THRESHOLD, domain: str = "apps",
) -> ScoredOpportunity:
    warnings: list[str] = []
    evidence = _dedupe_evidence([verify_evidence(e, ledger, warnings) for e in draft.evidence])

    competitors: list[Competitor] = []
    names: set[str] = set()
    for c in draft.competitors:
        if c.name.strip().lower() not in names:
            names.add(c.name.strip().lower())
            competitors.append(attach_competitor(c, ledger, warnings))

    refs = [EvidenceRef(e.dimension, e.evidence_type, e.url, e.url_verified) for e in evidence]
    result = score_opportunity(draft.dimension_scores.model_dump(), refs, threshold, DOMAIN_WEIGHTS[domain])
    warnings.extend(result.warnings)
    if not any(e.url_verified for e in evidence):
        warnings.append("no verified evidence at all; score is capped and confidence is low")
    if ledger.dropped:
        warnings.append(
            f"{ledger.dropped} research result(s) could not be recorded (ledger cap); evidence that cites "
            "them may be wrongly marked unverified, so this score can be too low"
        )
    details: dict = {}
    if domain == "games":
        details = {name: getattr(draft, name, None) for name in GAME_DETAIL_FIELDS}
        warnings.append(GAME_NOVELTY_WARNING)

    return ScoredOpportunity(
        name=draft.name.strip(), elevator_pitch=draft.elevator_pitch, target_audience=draft.target_audience,
        problem=draft.problem, proposed_solution=draft.proposed_solution, category=draft.category.strip(),
        target_regions=draft.target_regions or default_regions,
        competitors=competitors, evidence=evidence,
        monetization_hypothesis=draft.monetization_hypothesis, ad_suitability=draft.ad_suitability,
        risks=draft.risks, assumptions=draft.assumptions, validation_notes=draft.validation_notes,
        total_score=result.total_score, confidence=result.confidence, confidence_label=result.confidence_label,
        score_breakdown=result.breakdown(), scoring_version=DOMAIN_VERSIONS[domain],
        warnings=warnings, recommended=result.recommended, domain=domain, details=details,
    )


def finalize(
    synthesis: SynthesisOutput | GameSynthesisOutput, ledger: EvidenceLedger, *, max_opportunities: int,
    default_regions: list[str], threshold: float = RECOMMEND_THRESHOLD, domain: str = "apps",
) -> ResearchResult:
    scored = [finalize_opportunity(o, ledger, default_regions, threshold, domain) for o in synthesis.opportunities]
    scored.sort(key=lambda o: (o.total_score, o.confidence), reverse=True)
    return ResearchResult(research_summary=synthesis.research_summary, opportunities=scored[:max_opportunities])

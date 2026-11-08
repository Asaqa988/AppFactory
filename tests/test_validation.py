from app.research.ledger import EvidenceLedger
from app.research.validation import finalize
from tests.helpers import draft, evidence_for_all, ledger_with, synthesis

URLS = ["https://news.example.org/a", "https://forum.example.net/b", "https://blog.example.com/c"]


def run(syn, ledger, **kw):
    return finalize(syn, ledger, max_opportunities=kw.pop("max_opportunities", 5), default_regions=["Global"], **kw)


def test_fully_sourced_opportunity_is_recommended_and_verified():
    result = run(synthesis(draft(scores=90, evidence=evidence_for_all(URLS))), ledger_with(*URLS))
    opp = result.opportunities[0]
    assert opp.total_score == 90.0
    assert opp.recommended is True
    assert opp.confidence_label == "high"
    assert all(e.url_verified and e.retrieved_at for e in opp.evidence)
    assert opp.evidence[0].published_at == "2026-01-02"  # filled from the tool result


def test_fabricated_urls_are_downgraded_and_cannot_reach_threshold():
    fake = ["https://invented.example/1", "https://invented.example/2", "https://invented.example/3"]
    result = run(synthesis(draft(scores=100, evidence=evidence_for_all(fake))), ledger_with(*URLS))
    opp = result.opportunities[0]
    assert opp.recommended is False
    assert opp.total_score == 40.0
    assert {e.evidence_type for e in opp.evidence} == {"unverified"}
    assert not any(e.url_verified for e in opp.evidence)
    assert any("not returned by any research tool" in w for w in opp.warnings)
    assert any("no verified evidence at all" in w for w in opp.warnings)


def test_url_match_is_normalisation_tolerant():
    ledger = ledger_with("https://www.news.example.org/a/")
    ev = [{"claim": "something happened here", "url": "https://news.example.org/a?utm_source=tw", "dimension": "demand_evidence"}]
    opp = run(synthesis(draft(evidence=ev)), ledger).opportunities[0]
    assert opp.evidence[0].url_verified


def test_observed_evidence_without_url_is_unverified():
    ev = [{"claim": "something happened here", "dimension": "demand_evidence", "evidence_type": "observed"}]
    opp = run(synthesis(draft(evidence=ev)), ledger_with(*URLS)).opportunities[0]
    assert opp.evidence[0].evidence_type == "unverified"


def test_assumptions_are_kept_but_never_verified():
    ev = [{"claim": "users probably want this", "dimension": "unmet_user_need", "evidence_type": "assumption"}]
    opp = run(synthesis(draft(evidence=ev)), ledger_with(*URLS)).opportunities[0]
    assert opp.evidence[0].evidence_type == "assumption" and not opp.evidence[0].url_verified


def test_duplicate_evidence_is_collapsed():
    ev = evidence_for_all(URLS[:1]) * 2
    opp = run(synthesis(draft(evidence=ev)), ledger_with(*URLS)).opportunities[0]
    assert len(opp.evidence) == 6


def test_competitor_ratings_come_only_from_apple_ledger():
    ledger = ledger_with(*URLS)
    ledger.record_apple({"name": "Rival App", "url": "https://apps.apple.com/us/app/rival/id42?uo=4",
                         "rating": 4.1, "rating_count": 1234, "price": "Free", "developer": "Rival Inc"})
    competitors = [
        {"name": "Rival App", "url": "https://apps.apple.com/us/app/rival/id42", "gap": "no offline mode"},
        {"name": "Ghost App", "url": "https://play.google.com/store/apps/details?id=ghost", "gap": "x"},
    ]
    opp = run(synthesis(draft(competitors=competitors)), ledger).opportunities[0]
    rival, ghost = opp.competitors
    assert (rival.rating, rival.rating_count, rival.price) == (4.1, 1234, "Free")
    assert rival.rating_source == "Apple Search API" and rival.rating_retrieved_at
    assert ghost.rating is None and ghost.rating_count is None
    assert ghost.url is None  # URL no tool returned was removed
    assert any("Ghost App" in w for w in opp.warnings)


def test_competitor_name_fallback_matches_apple_record():
    ledger = ledger_with(*URLS)
    ledger.record_apple({"name": "Rival App", "url": "https://apps.apple.com/us/app/rival/id42", "rating": 3.5, "rating_count": 10, "price": "Free"})
    opp = run(synthesis(draft(competitors=[{"name": "rival app"}])), ledger).opportunities[0]
    assert opp.competitors[0].rating == 3.5


def test_competitor_listed_by_search_keeps_url_but_has_no_rating():
    ledger = ledger_with("https://play.google.com/store/apps/details?id=real")
    opp = run(synthesis(draft(competitors=[{"name": "Real", "url": "https://play.google.com/store/apps/details?id=real"}])), ledger).opportunities[0]
    assert opp.competitors[0].url and opp.competitors[0].rating is None


def test_results_are_ranked_and_truncated():
    drafts = [draft(name=f"idea {s}", scores=s, evidence=evidence_for_all(URLS)) for s in (60, 95, 80)]
    result = run(synthesis(*drafts), ledger_with(*URLS), max_opportunities=2)
    assert [o.name for o in result.opportunities] == ["idea 95", "idea 80"]


def test_regions_default_when_model_gives_none():
    opp = run(synthesis(draft(target_regions=[])), ledger_with(*URLS)).opportunities[0]
    assert opp.target_regions == ["Global"]


def test_threshold_is_configurable():
    syn = synthesis(draft(scores=80, evidence=evidence_for_all(URLS)))
    assert run(syn, ledger_with(*URLS), threshold=85).opportunities[0].recommended is False


def test_empty_ledger_never_verifies():
    opp = run(synthesis(draft(scores=100, evidence=evidence_for_all(URLS))), EvidenceLedger()).opportunities[0]
    assert opp.recommended is False and opp.total_score == 40.0

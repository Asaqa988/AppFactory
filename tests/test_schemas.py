import pytest
from pydantic import ValidationError

from app.research.ledger import normalize_url
from app.schemas.research import (
    DecisionRequest, DimensionScores, EvidenceItem, OpportunityDraft, ResearchRequest, SynthesisOutput,
)
from tests.helpers import draft


def test_request_defaults_and_normalisation():
    req = ResearchRequest(brief="Find underserved consumer apps for a small team.", regions=[], categories=[" Fitness ", "fitness", "Finance"])
    assert req.regions == ["Global"]
    assert req.categories == ["Fitness", "Finance"]
    assert req.max_opportunities == 5


@pytest.mark.parametrize("payload", [
    {"brief": "too short"},
    {"brief": "x" * 5001},
    {"brief": "Find underserved consumer apps.", "max_opportunities": 11},
    {"brief": "Find underserved consumer apps.", "max_opportunities": 0},
    {"brief": "Find underserved consumer apps.", "surprise": True},
])
def test_request_rejects_bad_input(payload):
    with pytest.raises(ValidationError):
        ResearchRequest(**payload)


def test_decision_values_are_restricted():
    assert DecisionRequest(decision="approved").notes is None
    assert DecisionRequest(decision="rejected", notes="  ").notes is None
    with pytest.raises(ValidationError):
        DecisionRequest(decision="auto_approved")
    with pytest.raises(ValidationError):
        DecisionRequest(decision="approved", reviewer="mallory")  # reviewer comes from the API key only


@pytest.mark.parametrize("value", [-1, 101])
def test_dimension_scores_must_be_0_to_100(value):
    ok = {d: 50 for d in DimensionScores.model_fields}
    with pytest.raises(ValidationError):
        DimensionScores(**{**ok, "demand_evidence": value})


def test_unknown_model_invented_fields_are_dropped_not_stored():
    parsed = OpportunityDraft(**draft(estimated_downloads="5,000,000", monthly_revenue_usd=120000, market_size="$4B"))
    dumped = parsed.model_dump()
    for forbidden in ("estimated_downloads", "monthly_revenue_usd", "market_size"):
        assert forbidden not in dumped


def test_competitor_draft_has_no_rating_fields():
    parsed = OpportunityDraft(**draft(competitors=[{"name": "Rival", "rating": 4.9, "downloads": "10M+", "url": "https://x.org"}]))
    competitor = parsed.competitors[0].model_dump()
    assert "rating" not in competitor and "downloads" not in competitor


def test_synthesis_requires_all_required_fields():
    bad = draft()
    del bad["problem"]
    with pytest.raises(ValidationError):
        SynthesisOutput(research_summary="s", opportunities=[bad])


def test_evidence_item_drops_non_http_urls():
    assert EvidenceItem(claim="c", url="javascript:alert(1)").url is None
    assert EvidenceItem(claim="c", url="ftp://x.org/a").url is None
    assert EvidenceItem(claim="c", url="https://x.org/a").url == "https://x.org/a"


def test_synthesis_json_schema_is_producible_for_the_llm():
    schema = SynthesisOutput.model_json_schema()
    assert "opportunities" in schema["properties"]


@pytest.mark.parametrize("a,b", [
    ("https://www.Example.com/path/", "http://example.com/path"),
    ("https://example.com/a?utm_source=x&id=2", "https://example.com/a?id=2"),
    ("https://apps.apple.com/us/app/foo/id1?uo=4", "https://apps.apple.com/us/app/foo/id1"),
    ("https://example.com/a#frag", "https://example.com/a"),
])
def test_url_normalisation_equivalences(a, b):
    assert normalize_url(a) == normalize_url(b)


@pytest.mark.parametrize("bad", [None, "", "not a url", "javascript:alert(1)", "ftp://x.org", "file:///etc/passwd"])
def test_url_normalisation_rejects_non_http(bad):
    assert normalize_url(bad) is None

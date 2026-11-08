import math

import pytest

from app.research import scoring
from app.research.scoring import EvidenceRef, score_opportunity
from tests.helpers import DIMENSIONS, refs


def full(value):
    return {d: value for d in DIMENSIONS}


def test_weights_match_specification_and_sum_to_one():
    assert scoring.WEIGHTS == {
        "unmet_user_need": 0.20, "demand_evidence": 0.20, "monetization_fit": 0.20,
        "retention_potential": 0.15, "competition_gap": 0.15, "build_feasibility": 0.10,
    }
    assert sum(scoring.WEIGHTS.values()) == pytest.approx(1.0)
    assert scoring.RECOMMEND_THRESHOLD == 70


def test_perfect_scores_with_full_evidence_reach_100():
    result = score_opportunity(full(100), refs(3))
    assert result.total_score == 100
    assert result.confidence == 1.0
    assert result.confidence_label == "high"
    assert result.recommended is True


def test_is_deterministic():
    a = score_opportunity(full(83), refs(2))
    b = score_opportunity(full(83), refs(2))
    assert a.total_score == b.total_score and a.breakdown() == b.breakdown()


def test_weighting_is_applied_per_dimension():
    scores = {d: 0 for d in DIMENSIONS}
    scores["demand_evidence"] = 100
    result = score_opportunity(scores, refs(3))
    assert result.total_score == 20.0


def test_missing_dimensions_count_as_zero_and_warn():
    result = score_opportunity({"demand_evidence": 100}, refs(3))
    assert result.total_score == 20.0
    assert any("unmet_user_need" in w and "no score" in w for w in result.warnings)


def test_agent_scores_without_evidence_cannot_be_recommended():
    """The core guard: 100s from the model with zero sources stay at the 40-point ceiling."""
    result = score_opportunity(full(100), [])
    assert result.total_score == 40.0
    assert result.recommended is False
    assert result.confidence == 0.0
    assert result.confidence_label == "low"
    assert all(d.capped for d in result.dimensions.values())


@pytest.mark.parametrize("sources,ceiling", [(0, 40), (1, 65), (2, 85), (3, 100), (6, 100)])
def test_evidence_ceiling_by_number_of_distinct_sources(sources, ceiling):
    result = score_opportunity(full(100), refs(sources))
    assert result.total_score == ceiling


def test_unverified_urls_do_not_count():
    result = score_opportunity(full(100), refs(3, verified=False))
    assert result.total_score == 40.0


def test_assumptions_do_not_count():
    result = score_opportunity(full(100), refs(3, evidence_type="assumption"))
    assert result.total_score == 40.0


def test_estimates_count_half():
    result = score_opportunity(full(100), refs(2, evidence_type="estimate"))  # 2 * 0.5 = 1.0 effective
    assert result.total_score == 65.0


def test_same_host_counts_once():
    same_host = [EvidenceRef(d, "observed", f"https://reddit.com/r/x/{i}", True) for d in DIMENSIONS for i in range(5)]
    assert score_opportunity(full(100), same_host).total_score == 65.0
    www = [EvidenceRef(d, "observed", "https://www.reddit.com/a", True) for d in DIMENSIONS] + \
          [EvidenceRef(d, "observed", "https://reddit.com/b", True) for d in DIMENSIONS]
    assert score_opportunity(full(100), www).total_score == 65.0  # www. and bare host are one source


def test_evidence_for_another_dimension_does_not_help():
    only_demand = [EvidenceRef("demand_evidence", "observed", f"https://s{i}.org/x", True) for i in range(3)]
    result = score_opportunity(full(100), only_demand)
    assert result.dimensions["demand_evidence"].capped_score == 100
    assert result.dimensions["unmet_user_need"].capped_score == 40


def test_threshold_boundary_is_inclusive():
    # 3 sources everywhere -> no caps, so the total equals the agent score.
    assert score_opportunity(full(70), refs(3)).recommended is True
    assert score_opportunity(full(69.99), refs(3)).recommended is False


@pytest.mark.parametrize("bad", [None, "abc", float("nan"), -50, 1e9, "75"])
def test_garbage_input_is_clamped_not_crashing(bad):
    result = score_opportunity({"unmet_user_need": bad}, refs(3))
    assert 0 <= result.total_score <= 100
    assert not math.isnan(result.total_score)


def test_numeric_string_is_accepted():
    assert score_opportunity({"unmet_user_need": "75"}, refs(3)).dimensions["unmet_user_need"].agent_score == 75


@pytest.mark.parametrize("confidence,label", [(0, "low"), (0.33, "low"), (0.34, "medium"), (0.66, "medium"), (0.67, "high"), (1, "high")])
def test_confidence_labels(confidence, label):
    assert scoring.confidence_label(confidence) == label


def test_capped_scores_are_explained_in_warnings():
    result = score_opportunity(full(100), refs(1))
    assert any("capped to 65" in w for w in result.warnings)

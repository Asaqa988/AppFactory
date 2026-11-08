"""The games research domain: scoring rubric, schemas, validation, tools, crew wiring, flow and API."""
import re

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import SessionLocal, engine, init_db
from app.models.entities import BELOW_THRESHOLD, PENDING, Opportunity, ResearchRun
from app.research import scoring, tools
from app.research.crew import INPUT_KEYS, build_research_crew
from app.research.flow import ResearchError, execute_research_flow
from app.research.ledger import EvidenceLedger
from app.research.scoring import EvidenceRef, score_opportunity
from app.research.tools import AppleAppStoreLookupTool, AppleGameChartsTool, ResearchScoringTool
from app.research.validation import GAME_NOVELTY_WARNING, finalize
from app.schemas.research import (
    GameDimensionScores, GameOpportunityDraft, GameSynthesisOutput, ResearchRequest, SynthesisOutput,
)
from app.services import runs
from tests.conftest import ALICE, requires_postgres
from tests.fakes import URLS, factory
from tests.helpers import (
    GAME_DIMENSIONS, game_draft, game_evidence_for_all, game_synthesis, ledger_with, seed_opportunity, seed_run,
)
from tests.test_tools import FakeHttp

BRIEF = {"brief": "Find 2D mobile game concepts one developer could build in Godot with rewarded ads.",
         "regions": ["Jordan"], "categories": ["puzzle"], "max_opportunities": 3}


# ----------------------------------------------------------------- scoring

def test_game_weights_sum_to_one_and_share_build_feasibility():
    assert abs(sum(scoring.GAME_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(scoring.GAME_WEIGHTS) == set(GAME_DIMENSIONS)
    assert "build_feasibility" in scoring.WEIGHTS and "build_feasibility" in scoring.GAME_WEIGHTS
    assert scoring.DOMAIN_VERSIONS == {"apps": "1.0", "games": "games-1.0"}


def _refs(dimensions, count):
    return [EvidenceRef(d, "observed", f"https://site{i}.example.org/{d}", True) for d in dimensions for i in range(count)]


def test_game_score_with_full_evidence_reaches_the_gate():
    result = score_opportunity({d: 90 for d in GAME_DIMENSIONS}, _refs(GAME_DIMENSIONS, 3), weights=scoring.GAME_WEIGHTS)
    assert result.total_score == 90.0 and result.recommended and result.confidence_label == "high"
    assert set(result.dimensions) == set(GAME_DIMENSIONS)


def test_apps_evidence_does_not_back_game_dimensions():
    """Evidence tagged with an apps-only dimension supports nothing in the games rubric."""
    apps_only = _refs(["unmet_user_need", "demand_evidence", "monetization_fit"], 3)
    result = score_opportunity({d: 90 for d in GAME_DIMENSIONS}, apps_only, weights=scoring.GAME_WEIGHTS)
    assert result.total_score == 40.0 and not result.recommended and result.confidence == 0.0


def test_default_weights_are_still_the_apps_rubric():
    result = score_opportunity({d: 90 for d in scoring.WEIGHTS}, _refs(list(scoring.WEIGHTS), 3))
    assert set(result.dimensions) == set(scoring.WEIGHTS) and result.total_score == 90.0


# ----------------------------------------------------------------- schemas

def test_request_domain_defaults_to_apps_and_rejects_unknown_values():
    assert ResearchRequest(**BRIEF).domain == "apps"
    assert ResearchRequest(**BRIEF, domain="games").domain == "games"
    with pytest.raises(ValidationError):
        ResearchRequest(**BRIEF, domain="movies")


def test_game_draft_requires_the_game_fields_and_ignores_invented_ones():
    data = game_draft(estimated_downloads=5_000_000)
    parsed = GameOpportunityDraft(**data)
    assert not hasattr(parsed, "estimated_downloads")
    for missing in ("core_loop", "twist", "genre"):
        broken = {k: v for k, v in data.items() if k != missing}
        with pytest.raises(ValidationError):
            GameOpportunityDraft(**broken)


def test_game_dimension_scores_are_bounded_and_complete():
    with pytest.raises(ValidationError):
        GameDimensionScores(**{d: 101 for d in GAME_DIMENSIONS})
    with pytest.raises(ValidationError):
        GameDimensionScores(**{d: 50 for d in GAME_DIMENSIONS[:-1]})


def test_apps_synthesis_rejects_game_dimension_scores():
    with pytest.raises(ValidationError):
        SynthesisOutput(research_summary="s", opportunities=[game_draft()])


# ----------------------------------------------------------------- validation

def test_finalize_games_uses_the_game_rubric_and_keeps_details():
    ledger = ledger_with(*URLS)
    synth = game_synthesis(game_draft(evidence=game_evidence_for_all(URLS)))
    result = finalize(synth, ledger, max_opportunities=5, default_regions=["US"], threshold=70, domain="games")
    top = result.opportunities[0]
    assert top.domain == "games" and top.scoring_version == "games-1.0"
    assert set(top.score_breakdown) == set(GAME_DIMENSIONS)
    assert top.total_score == 90.0 and top.recommended
    assert top.details["genre"] == "merge puzzle" and top.details["ad_moments"] == ["rewarded revive after a full board"]
    assert GAME_NOVELTY_WARNING in top.warnings


def test_finalize_games_still_downgrades_unverified_evidence():
    ledger = ledger_with(URLS[0])
    fake = [{"claim": f"made up {d}", "url": "https://invented.example.org/x", "dimension": d, "evidence_type": "observed"}
            for d in GAME_DIMENSIONS]
    result = finalize(game_synthesis(game_draft(evidence=fake)), ledger, max_opportunities=5,
                      default_regions=["US"], domain="games")
    top = result.opportunities[0]
    assert all(e.evidence_type == "unverified" for e in top.evidence)
    assert top.total_score == 40.0 and not top.recommended and top.confidence_label == "low"


def test_finalize_defaults_to_the_apps_domain():
    from tests.helpers import draft, evidence_for_all, synthesis

    ledger = ledger_with(*URLS)
    result = finalize(synthesis(draft(evidence=evidence_for_all(URLS))), ledger, max_opportunities=5, default_regions=["US"])
    top = result.opportunities[0]
    assert top.domain == "apps" and top.scoring_version == "1.0" and top.details == {}
    assert GAME_NOVELTY_WARNING not in top.warnings


# ----------------------------------------------------------------- tools

SEARCH_RESULTS = {"results": [
    {"trackName": "Word Puzzle", "trackId": 11, "artistName": "Dev A", "averageUserRating": 4.5, "userRatingCount": 1200,
     "formattedPrice": "Free", "trackViewUrl": "https://apps.apple.com/us/app/word-puzzle/id11?uo=4",
     "primaryGenreName": "Games", "genres": ["Games", "Word"]},
    {"trackName": "Notes Pro", "trackId": 12, "artistName": "Dev B", "trackViewUrl": "https://apps.apple.com/us/app/notes/id12",
     "primaryGenreName": "Productivity", "genres": ["Productivity"]},
]}


def test_games_search_keeps_only_games_and_overfetches(monkeypatch):
    http = FakeHttp({"itunes.apple.com/search": (200, SEARCH_RESULTS)})
    monkeypatch.setattr(tools.httpx, "request", http)
    ledger = EvidenceLedger()
    tool = AppleAppStoreLookupTool(ledger=ledger, games_only=True, name="apple_games_search")
    out = tool.run(term="word puzzle", country="jo")

    assert "Word Puzzle" in out and "Notes Pro" not in out
    assert http.calls[0][2]["params"]["limit"] == 40
    assert ledger.lookup_apple("https://apps.apple.com/us/app/word-puzzle/id11")["rating"] == 4.5
    assert ledger.lookup_apple("https://apps.apple.com/us/app/notes/id12") is None


def test_apps_search_is_unchanged_by_the_games_option(monkeypatch):
    http = FakeHttp({"itunes.apple.com/search": (200, SEARCH_RESULTS)})
    monkeypatch.setattr(tools.httpx, "request", http)
    out = AppleAppStoreLookupTool(ledger=EvidenceLedger()).run(term="notes", country="us")
    assert "Notes Pro" in out and "Word Puzzle" in out and http.calls[0][2]["params"]["limit"] == 10


CHART = {"feed": {"updated": {"label": "2026-11-04T02:39:20-07:00"}, "entry": [
    {"im:name": {"label": "Meowdoku!"}, "id": {"label": "https://apps.apple.com/us/app/meowdoku/id6761760135?uo=2",
                                                 "attributes": {"im:id": "6761760135"}},
     "im:artist": {"label": "Cat Studio"}, "im:price": {"label": "Get"}},
    {"im:name": {"label": "Word Tiles"}, "id": {"label": "https://apps.apple.com/us/app/word-tiles/id6783815362",
                                                  "attributes": {"im:id": "6783815362"}},
     "im:artist": {"label": "Tile Co"}, "im:price": {"label": "Get"}},
]}}


def test_game_charts_returns_ranks_and_records_citeable_sources(monkeypatch):
    http = FakeHttp({"rss/topfreeapplications": (200, CHART)})
    monkeypatch.setattr(tools.httpx, "request", http)
    ledger = EvidenceLedger()
    out = AppleGameChartsTool(ledger=ledger).run(chart="top_free", genre="puzzle", country="us", limit=2)

    url = http.calls[0][1]
    assert url == "https://itunes.apple.com/us/rss/topfreeapplications/limit=2/genre=7012/json"
    assert "UNTRUSTED" in out and "Meowdoku!" in out and f"Source URL to cite: {url}" in out
    chart_entry = ledger.lookup(url)  # the chart itself can be cited, with its date
    assert chart_entry["published_date"] == "2026-11-04T02:39:20-07:00" and "1. Meowdoku!" in chart_entry["snippet"]
    assert ledger.lookup("https://apps.apple.com/us/app/meowdoku/id6761760135") is not None  # ?uo=2 ignored
    assert ledger.successful_searches() == 1


def test_game_charts_validates_arguments_without_any_http_call(monkeypatch):
    http = FakeHttp({})
    monkeypatch.setattr(tools.httpx, "request", http)
    ledger = EvidenceLedger()
    tool = AppleGameChartsTool(ledger=ledger)
    assert tool.run(chart="top_free", genre="not-a-genre").startswith("ERROR")
    assert tool.run(chart="top_free", genre="all", country="usa").startswith("ERROR")
    assert http.calls == [] and ledger.successful_searches() == 0


def test_game_charts_never_raises_on_http_errors(monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"rss/": httpx.ConnectError("down")}))
    ledger = EvidenceLedger()
    out = AppleGameChartsTool(ledger=ledger).run(chart="top_grossing", genre="all", country="sa")
    assert out.startswith("ERROR") and ledger.calls[-1]["ok"] is False


def test_game_charts_respects_the_free_call_budget(monkeypatch):
    monkeypatch.setattr(settings, "max_free_tool_calls_per_run", 0)
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({}))
    assert "budget" in AppleGameChartsTool(ledger=EvidenceLedger()).run(chart="top_free", genre="all")


def test_free_lookups_cannot_starve_paid_web_search(monkeypatch):
    """Regression: the first real games run used up one shared budget on charts, so later agents could not search."""
    monkeypatch.setattr(settings, "max_tool_calls_per_run", 2)
    monkeypatch.setattr(settings, "max_free_tool_calls_per_run", 3)
    ledger = EvidenceLedger()
    for i in range(3):  # three free Apple calls: more than the paid budget of 2
        ledger.record_call("apple_game_charts", f"c{i}", ok=True, result_count=5)
    from app.research.tools import WebResearchTool

    http = FakeHttp({"tavily.com": (200, {"results": [{"title": "t", "url": "https://x.example.org/a", "content": "c"}]})})
    monkeypatch.setattr(tools.httpx, "request", http)
    web = WebResearchTool(ledger=ledger)
    assert not web.run(query="still allowed to search").startswith("ERROR")
    assert not web.run(query="second search allowed").startswith("ERROR")
    assert "budget exhausted" in web.run(query="third search refused")  # the paid budget itself still holds
    # and the free budget is independent of paid calls
    assert "budget exhausted" in AppleGameChartsTool(ledger=ledger).run(chart="top_free", genre="all")


def test_every_listed_genre_id_is_a_games_subgenre_id():
    assert tools.GAME_GENRES["all"] == 6014
    assert all(7000 < v < 7100 for k, v in tools.GAME_GENRES.items() if k != "all")


def test_rubric_tool_explains_the_games_weights():
    games_text = ResearchScoringTool(domain="games").run()
    for name in GAME_DIMENSIONS:
        assert name in games_text
    assert "unmet_user_need" not in games_text
    assert "unmet_user_need" in ResearchScoringTool().run()


# ----------------------------------------------------------------- crew wiring

@pytest.fixture
def games_crew():
    return build_research_crew(EvidenceLedger(), domain="games")


def test_games_crew_has_four_specialists_and_a_toolless_synthesizer(games_crew):
    assert [a.role for a in games_crew.agents] == [
        "Mobile Game Trend Scout", "Game Competitor Analyst", "Player Pain Analyst",
        "Game Design and Ad Analyst", "Research Synthesizer",
    ]
    assert games_crew.agents[-1].tools == [] and all(not a.allow_delegation for a in games_crew.agents)


def test_games_tools_are_wired_to_the_right_agents(games_crew):
    names = {a.role: {t.name for t in a.tools} for a in games_crew.agents}
    assert {"web_search", "apple_game_charts", "apple_games_search"} <= names["Mobile Game Trend Scout"]
    assert {"apple_games_search", "apple_game_charts"} <= names["Game Competitor Analyst"]
    assert "apple_app_reviews" in names["Player Pain Analyst"]
    assert "opportunity_scoring_rubric" in names["Game Design and Ad Analyst"]
    assert "apple_app_store_search" not in set().union(*names.values())  # games use the games-only search


def test_games_crew_shares_one_ledger_and_uses_structured_output():
    ledger = EvidenceLedger()
    crew = build_research_crew(ledger, domain="games")
    ledger_tools = [t for a in crew.agents for t in a.tools if hasattr(t, "ledger")]
    assert ledger_tools and all(t.ledger is ledger for t in ledger_tools)
    assert crew.tasks[-1].output_pydantic is GameSynthesisOutput


def test_games_prompts_only_use_known_placeholders_and_forbid_overclaiming(games_crew):
    for task in games_crew.tasks:
        assert set(re.findall(r"\{(\w+)\}", task.description)) <= set(INPUT_KEYS)
    assert "{max_opportunities}" in games_crew.tasks[-1].description
    first = games_crew.tasks[0].description.lower()
    for phrase in ("never invent", "untrusted", "not found in the sample", "godot"):
        assert phrase in first
    assert "at most 8 evidence items" in games_crew.tasks[-1].description


def test_unknown_domain_is_rejected():
    with pytest.raises(ValueError, match="unknown research domain"):
        build_research_crew(EvidenceLedger(), domain="movies")


# ----------------------------------------------------------------- flow

async def _claimed_games_run():
    return await runs.claim_run(await seed_run(domain="games"), "w1")


async def test_games_flow_scores_with_the_game_rubric_and_stores_details():
    run = await _claimed_games_run()
    synth = game_synthesis(game_draft(name="Gravity Merge", evidence=game_evidence_for_all(URLS)),
                           game_draft(name="Weak idea", scores=30))
    await execute_research_flow(run, "w1", crew_factory=factory(synth=synth))

    async with SessionLocal() as s:
        opps = {o.name: o for o in (await s.execute(select(Opportunity).where(Opportunity.run_id == run.id))).scalars()}
    top = opps["Gravity Merge"]
    assert top.domain == "games" and top.scoring_version == "games-1.0" and top.total_score == 90.0
    assert top.decision_status == PENDING and top.details["core_loop"].startswith("Tilt the phone")
    assert set(top.score_breakdown) == set(GAME_DIMENSIONS)
    assert opps["Weak idea"].decision_status == BELOW_THRESHOLD


async def test_games_gate_is_separate_from_the_apps_gate(monkeypatch):
    monkeypatch.setattr(settings, "recommend_threshold_games", 95.0)  # apps threshold stays 70
    run = await _claimed_games_run()
    synth = game_synthesis(game_draft(evidence=game_evidence_for_all(URLS)))
    await execute_research_flow(run, "w1", crew_factory=factory(synth=synth))
    async with SessionLocal() as s:
        opp = (await s.execute(select(Opportunity).where(Opportunity.run_id == run.id))).scalar_one()
    assert opp.total_score == 90.0 and opp.decision_status == BELOW_THRESHOLD and not opp.recommended


async def test_games_flow_rejects_output_in_the_apps_shape():
    from tests.fakes import good_synthesis

    run = await _claimed_games_run()
    with pytest.raises(ResearchError, match="failed schema validation"):
        await execute_research_flow(run, "w1", crew_factory=factory(synth=good_synthesis(), pydantic_ok=True))
    async with SessionLocal() as s:
        assert (await s.get(ResearchRun, run.id, populate_existing=True)).status == "failed"


async def test_run_domain_reaches_the_default_crew_builder(monkeypatch):
    seen = {}

    def fake_build(ledger, llm=None, domain="apps"):
        seen["domain"] = domain
        return factory(synth=game_synthesis(game_draft(evidence=game_evidence_for_all(URLS))))(ledger)

    monkeypatch.setattr("app.research.flow.build_research_crew", fake_build)
    run = await _claimed_games_run()
    await execute_research_flow(run, "w1")
    assert seen["domain"] == "games"


# ----------------------------------------------------------------- API

async def test_create_games_run_stores_the_domain(client, queue):
    r = await client.post("/api/v1/research-runs", json={**BRIEF, "domain": "games"}, headers=ALICE)
    assert r.status_code == 202
    detail = (await client.get(f"/api/v1/research-runs/{r.json()['run_id']}", headers=ALICE)).json()
    assert detail["domain"] == "games"


async def test_runs_default_to_apps_and_unknown_domains_are_422(client, queue):
    r = await client.post("/api/v1/research-runs", json=BRIEF, headers=ALICE)
    assert (await client.get(f"/api/v1/research-runs/{r.json()['run_id']}", headers=ALICE)).json()["domain"] == "apps"
    bad = await client.post("/api/v1/research-runs", json={**BRIEF, "domain": "movies"}, headers=ALICE)
    assert bad.status_code == 422


async def _seed_game(run_id, **overrides):
    values = dict(
        run_id=run_id, domain="games", details={"genre": "merge puzzle"}, name="game idea", elevator_pitch="p",
        target_audience="a", problem="pr", proposed_solution="s", category="puzzle", monetization_hypothesis="m",
        total_score=60, decision_status=BELOW_THRESHOLD,
    )
    values.update(overrides)
    async with SessionLocal() as s:
        game = Opportunity(**values)
        s.add(game)
        await s.commit()
        return game.id


async def test_opportunities_can_be_filtered_by_domain_and_expose_details(client):
    run_id = await seed_run()
    apps_id = await seed_opportunity(run_id, name="app idea")
    game_id = await _seed_game(run_id)
    games = (await client.get("/api/v1/opportunities?domain=games", headers=ALICE)).json()
    assert [o["id"] for o in games] == [game_id] and games[0]["domain"] == "games"
    assert [o["id"] for o in (await client.get("/api/v1/opportunities?domain=apps", headers=ALICE)).json()] == [apps_id]
    assert (await client.get("/api/v1/opportunities?domain=movies", headers=ALICE)).status_code == 422
    full = (await client.get(f"/api/v1/opportunities/{game_id}", headers=ALICE)).json()
    assert full["details"] == {"genre": "merge puzzle"} and full["domain"] == "games"


async def test_below_threshold_message_uses_the_domain_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "recommend_threshold_games", 55.0)
    game_id = await _seed_game(await seed_run(), total_score=50)
    r = await client.post(f"/api/v1/opportunities/{game_id}/decision", json={"decision": "approved"}, headers=ALICE)
    assert r.status_code == 409 and "55" in r.json()["detail"]


# ----------------------------------------------------------------- schema upgrade of an existing database

@requires_postgres
async def test_init_db_adds_the_new_columns_to_a_database_created_before_games():
    """Simulates a database from the first release (no domain/details columns) holding real rows."""
    run_id = await seed_run()
    await seed_opportunity(run_id, name="legacy idea")
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE research_runs DROP COLUMN domain"))
        await conn.execute(text("ALTER TABLE opportunities DROP COLUMN domain"))
        await conn.execute(text("ALTER TABLE opportunities DROP COLUMN details"))
    await init_db()
    await init_db()  # idempotent
    async with engine.connect() as conn:
        run_domain = (await conn.execute(text("SELECT domain FROM research_runs WHERE id = :i"), {"i": run_id})).scalar_one()
        row = (await conn.execute(text("SELECT domain, details FROM opportunities WHERE name = 'legacy idea'"))).one()
    assert run_domain == "apps" and row.domain == "apps" and row.details == {}


# ----------------------------------------------------------------- ledger caps (regression for the first real games run)

def test_bulk_listing_rows_cannot_crowd_out_web_results():
    """The first real games run recorded 389 chart rows, hit the single cap of 400, and then dropped every web result."""
    from app.research import ledger as ledger_module

    ledger = EvidenceLedger()
    for i in range(ledger_module.MAX_BULK_ENTRIES + 300):
        ledger.record_result("apple_game_charts", f"https://apps.apple.com/us/app/id{i}", "n", None, "rank", bulk=True)
    ledger.record_result("web_search", "https://news.example.org/trend", "t", "2026-11-01", "snippet")
    assert ledger.lookup("https://news.example.org/trend") is not None
    assert ledger.dropped == 300 and ledger.to_dict()["dropped_sources"] == 300


def test_evidence_pages_have_their_own_cap():
    from app.research import ledger as ledger_module

    ledger = EvidenceLedger()
    for i in range(ledger_module.MAX_ENTRIES + 5):
        ledger.record_result("web_search", f"https://site{i}.example.org/p", "t", None, "s")
    assert ledger.dropped == 5 and ledger.lookup("https://site0.example.org/p") is not None


def test_chart_rows_are_recorded_as_bulk(monkeypatch):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"rss/topfreeapplications": (200, CHART)}))
    ledger = EvidenceLedger()
    AppleGameChartsTool(ledger=ledger).run(chart="top_free", genre="puzzle", country="us", limit=2)
    assert ledger._bulk == 2 and ledger._pages == 1  # two listing rows, one chart page


@pytest.mark.parametrize("variant", [
    "https://apps.apple.com/sa/app/id6770160112",
    "https://apps.apple.com/sa/app/some-name/id6770160112?uo=4",
    "https://apps.apple.com/sa/app/%D9%85%D9%8A-%D8%B2-%D8%A7%D9%83%D8%AA%D8%B4%D9%81/id6770160112/",
])
def test_apple_listing_identity_is_the_app_id_not_the_slug(variant):
    from app.research.ledger import normalize_url

    assert normalize_url(variant) == "https://apps.apple.com/sa/app/id6770160112"


def test_apple_listings_of_other_countries_or_apps_stay_distinct():
    from app.research.ledger import normalize_url

    base = normalize_url("https://apps.apple.com/sa/app/id6770160112")
    assert normalize_url("https://apps.apple.com/us/app/id6770160112") != base
    assert normalize_url("https://apps.apple.com/sa/app/id6770160113") != base


def test_cited_listing_without_the_slug_is_verified_against_the_slugged_one():
    ledger = EvidenceLedger()
    ledger.record_result("apple_app_store", "https://apps.apple.com/sa/app/bara-al-salfa/id1486192343?uo=4", "n", None, None, bulk=True)
    ledger.record_call("apple_games_search", "q", ok=True, result_count=1)
    evidence = [{"claim": f"c {d}", "url": "https://apps.apple.com/sa/app/id1486192343", "dimension": d,
                 "evidence_type": "observed"} for d in GAME_DIMENSIONS]
    top = finalize(game_synthesis(game_draft(evidence=evidence)), ledger, max_opportunities=5,
                   default_regions=["SA"], domain="games").opportunities[0]
    assert all(e.url_verified for e in top.evidence)


def test_finalize_warns_when_sources_were_dropped():
    ledger = ledger_with(*URLS)
    ledger.dropped = 7
    top = finalize(game_synthesis(game_draft(evidence=game_evidence_for_all(URLS))), ledger, max_opportunities=5,
                   default_regions=["US"], domain="games").opportunities[0]
    assert any("7 research result(s) could not be recorded" in w for w in top.warnings)

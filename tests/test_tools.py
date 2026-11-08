import json

import httpx
import pytest

from app.core.config import settings
from app.research import tools
from app.research.ledger import EvidenceLedger
from app.research.tools import AppleAppStoreLookupTool, AppleReviewsTool, ResearchScoringTool, WebResearchTool


class FakeHttp:
    """Replaces httpx.request. Maps URL substring -> (status, json) or an exception."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for needle, outcome in self.routes.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                status, body = outcome
                return httpx.Response(status, json=body, request=httpx.Request(method, url))
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)


@pytest.fixture
def ledger():
    return EvidenceLedger()


TAVILY_OK = {"results": [
    {"title": "Trend", "url": "https://news.example.org/x", "content": "growing", "published_date": "2026-02-01"},
    {"title": "No URL"},
]}


def test_web_search_uses_bearer_auth_and_records_ledger(monkeypatch, ledger):
    http = FakeHttp({"tavily.com": (200, TAVILY_OK)})
    monkeypatch.setattr(tools.httpx, "request", http)
    out = WebResearchTool(ledger=ledger).run(query="habit tracker apps trend", max_results=5)

    _, url, kwargs = http.calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {settings.tavily_api_key}"
    assert "api_key" not in kwargs["json"]  # legacy body-embedded key is not used
    assert kwargs["json"]["include_published_date"] is True
    assert "UNTRUSTED" in out and "news.example.org/x" in out
    assert ledger.lookup("https://news.example.org/x")["published_date"] == "2026-02-01"
    assert ledger.successful_searches() == 1


def test_web_search_never_raises_on_http_errors(monkeypatch, ledger):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"tavily.com": (401, {"error": "bad key"})}))
    out = WebResearchTool(ledger=ledger).run(query="anything at all")
    assert out.startswith("ERROR:") and "401" in out
    assert settings.tavily_api_key not in out
    assert ledger.calls[0]["ok"] is False and ledger.successful_searches() == 0


def test_web_search_retries_transient_failures_then_succeeds(monkeypatch, ledger):
    attempts = {"n": 0}

    def flaky(method, url, **kw):
        attempts["n"] += 1
        status = 503 if attempts["n"] < 3 else 200
        return httpx.Response(status, json=TAVILY_OK, request=httpx.Request(method, url))

    monkeypatch.setattr(tools.httpx, "request", flaky)
    assert "news.example.org" in WebResearchTool(ledger=ledger).run(query="retry me please")
    assert attempts["n"] == 3


def test_web_search_survives_network_timeouts(monkeypatch, ledger):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"tavily.com": httpx.ConnectTimeout("slow")}))
    assert WebResearchTool(ledger=ledger).run(query="timeout please").startswith("ERROR:")


def test_web_search_without_key_is_an_error_string(monkeypatch, ledger):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    assert "TAVILY_API_KEY" in WebResearchTool(ledger=ledger).run(query="no key here")


def test_tool_call_budget_is_enforced(monkeypatch, ledger):
    monkeypatch.setattr(settings, "max_tool_calls_per_run", 2)
    http = FakeHttp({"tavily.com": (200, TAVILY_OK)})
    monkeypatch.setattr(tools.httpx, "request", http)
    tool = WebResearchTool(ledger=ledger)
    tool.run(query="first query"); tool.run(query="second query")
    assert "budget exhausted" in tool.run(query="third query")
    assert len(http.calls) == 2


APPLE = {"results": [
    {"trackName": "Rival", "trackId": 42, "artistName": "Dev", "averageUserRating": 4.4,
     "userRatingCount": 999, "formattedPrice": "Free", "trackViewUrl": "https://apps.apple.com/us/app/rival/id42?uo=4",
     "genres": ["Productivity"]},
    {"trackName": "Broken entry without url"},
]}


def test_apple_search_records_structured_ratings_in_ledger(monkeypatch, ledger):
    http = FakeHttp({"itunes.apple.com/search": (200, APPLE)})
    monkeypatch.setattr(tools.httpx, "request", http)
    out = AppleAppStoreLookupTool(ledger=ledger).run(term="habit tracker", country="US")
    assert http.calls[0][2]["params"]["country"] == "us"
    assert json.loads(out.split("\n", 1)[1])[0]["rating"] == 4.4
    record = ledger.lookup_apple("https://apps.apple.com/us/app/rival/id42")
    assert record["rating"] == 4.4 and record["rating_count"] == 999
    assert ledger.lookup("https://apps.apple.com/us/app/rival/id42") is not None  # citable source too


@pytest.mark.parametrize("country", ["usa", "u", "1a", "us/../x"])
def test_apple_search_rejects_bad_country(monkeypatch, ledger, country):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({}))
    assert AppleAppStoreLookupTool(ledger=ledger).run(term="whatever", country=country).startswith("ERROR:")


def test_apple_search_failure_is_contained(monkeypatch, ledger):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"itunes.apple.com": (403, {})}))
    assert AppleAppStoreLookupTool(ledger=ledger).run(term="rate limited").startswith("ERROR:")


FEED = {"feed": {"entry": [
    {"id": {"label": "app itself"}},
    {"im:rating": {"label": "1"}, "title": {"label": "Crashes"}, "content": {"label": "Loses my data " * 100}, "updated": {"label": "2026-03-01"}},
    {"im:rating": {"label": "2"}, "title": {"label": "Ads"}, "content": {"label": "Too many ads"}, "updated": {"label": "2026-03-02"}},
]}}


def test_reviews_parse_skip_app_entry_truncate_and_register_citable_source(monkeypatch, ledger):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"customerreviews": (200, FEED)}))
    out = AppleReviewsTool(ledger=ledger).run(app_id=42, country="us")
    assert "https://apps.apple.com/us/app/id42" in out
    reviews = json.loads(out.split("\n", 2)[2])
    assert [r["rating"] for r in reviews] == [1, 2] and len(reviews[0]["text"]) <= 500
    assert ledger.lookup("https://apps.apple.com/us/app/id42")["tool"] == "apple_app_reviews"


def test_reviews_single_entry_dict_and_failure(monkeypatch, ledger):
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"customerreviews": (200, {"feed": {"entry": FEED["feed"]["entry"][1]}})}))
    assert "Loses my data" in AppleReviewsTool(ledger=ledger).run(app_id=1)
    monkeypatch.setattr(tools.httpx, "request", FakeHttp({"customerreviews": (500, {})}))
    assert AppleReviewsTool(ledger=ledger).run(app_id=2).startswith("ERROR:")


def test_rubric_tool_reflects_the_real_scoring_constants():
    text = ResearchScoringTool().run()
    for fragment in ("unmet_user_need 20%", "build_feasibility 10%", ">= 70", "max 40"):
        assert fragment in text

import asyncio

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import BELOW_THRESHOLD, PENDING, Approval, ResearchRun
from tests.conftest import ALICE, BOB, requires_postgres
from tests.helpers import seed_opportunity, seed_run

BRIEF = {"brief": "Find underserved consumer mobile app opportunities suitable for ad-supported monetization.",
         "regions": ["United States", "Germany"], "categories": ["productivity"], "max_opportunities": 3}


# ----------------------------------------------------------------- health & auth

async def test_health_reports_database_and_redis(client):
    r = await client.get("/health")  # unauthenticated by design
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"] == "up" and body["redis"] == "up"


async def test_health_degraded_when_redis_down_but_still_200(client, queue):
    queue.fail = True
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "degraded" and r.json()["redis"] == "down"


@pytest.mark.parametrize("path", [
    "/api/v1/research-runs", "/api/v1/opportunities", "/api/v1/approvals", "/api/v1/research-runs/x", "/api/v1/opportunities/x",
])
async def test_every_get_endpoint_requires_auth(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-API-Key": "wrong"})).status_code == 401


async def test_write_endpoints_require_auth(client):
    assert (await client.post("/api/v1/research-runs", json=BRIEF)).status_code == 401
    assert (await client.post("/api/v1/opportunities/x/decision", json={"decision": "approved"})).status_code == 401


async def test_non_ascii_api_key_is_401_not_500(client):
    # HTTP headers arrive latin-1 decoded; "clé" is non-ASCII and made secrets.compare_digest raise TypeError.
    r = await client.get("/api/v1/approvals", headers={"X-API-Key": "clé-secret".encode("latin-1")})
    assert r.status_code == 401


async def test_valid_key_is_accepted(client):
    assert (await client.get("/api/v1/approvals", headers=ALICE)).status_code == 200


# ----------------------------------------------------------------- research runs

async def test_create_run_persists_then_enqueues(client, queue):
    r = await client.post("/api/v1/research-runs", json=BRIEF, headers=ALICE)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "queued"
    assert queue.jobs == [("run_research_job", run_id, f"{run_id}:0")]
    async with SessionLocal() as s:
        run = await s.get(ResearchRun, run_id)
        assert run.status == "queued" and run.requested_by == "alice" and run.enqueued_at is not None


async def test_run_is_accepted_and_durable_even_if_redis_is_down(client, queue):
    queue.fail = True
    r = await client.post("/api/v1/research-runs", json=BRIEF, headers=ALICE)
    assert r.status_code == 202
    async with SessionLocal() as s:
        run = await s.get(ResearchRun, r.json()["run_id"])
        assert run.status == "queued" and run.enqueued_at is None  # the worker sweep will deliver it


async def test_create_run_validates_input(client):
    assert (await client.post("/api/v1/research-runs", json={"brief": "short"}, headers=ALICE)).status_code == 422
    assert (await client.post("/api/v1/research-runs", json={**BRIEF, "max_opportunities": 99}, headers=ALICE)).status_code == 422


async def test_create_run_refused_when_providers_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    r = await client.post("/api/v1/research-runs", json=BRIEF, headers=ALICE)
    assert r.status_code == 503 and "TAVILY_API_KEY" in r.json()["detail"]
    assert "tvly" not in r.text  # never echo secret values


async def test_active_run_limit_is_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "max_active_runs", 2)
    for _ in range(2):
        assert (await client.post("/api/v1/research-runs", json=BRIEF, headers=ALICE)).status_code == 202
    assert (await client.post("/api/v1/research-runs", json=BRIEF, headers=ALICE)).status_code == 429


async def test_list_and_get_runs(client):
    done = await seed_run(status="completed")
    failed = await seed_run(status="failed", error_message="boom")
    runs = (await client.get("/api/v1/research-runs", headers=ALICE)).json()
    assert {r["id"] for r in runs} == {done, failed}
    only_failed = (await client.get("/api/v1/research-runs?status=failed", headers=ALICE)).json()
    assert [r["id"] for r in only_failed] == [failed] and only_failed[0]["error_message"] == "boom"
    assert (await client.get("/api/v1/research-runs?status=bogus", headers=ALICE)).status_code == 422
    detail = (await client.get(f"/api/v1/research-runs/{done}", headers=ALICE)).json()
    assert detail["status"] == "completed" and detail["opportunity_ids"] == []
    assert (await client.get("/api/v1/research-runs/nope", headers=ALICE)).status_code == 404


# ----------------------------------------------------------------- opportunities

async def test_opportunity_listing_filters_and_detail(client):
    run = await seed_run(status="completed")
    hi = await seed_opportunity(run, 88, name="hi")
    lo = await seed_opportunity(run, 55, status=BELOW_THRESHOLD, name="lo")
    listed = (await client.get("/api/v1/opportunities", headers=ALICE)).json()
    assert [o["id"] for o in listed] == [hi, lo]  # score descending
    assert [o["id"] for o in (await client.get("/api/v1/opportunities?recommended=true", headers=ALICE)).json()] == [hi]
    assert [o["id"] for o in (await client.get("/api/v1/opportunities?decision_status=pending_approval", headers=ALICE)).json()] == [hi]
    assert [o["id"] for o in (await client.get("/api/v1/opportunities?min_score=60", headers=ALICE)).json()] == [hi]
    assert (await client.get("/api/v1/opportunities?decision_status=bogus", headers=ALICE)).status_code == 422
    detail = (await client.get(f"/api/v1/opportunities/{hi}", headers=ALICE)).json()
    assert detail["name"] == "hi" and detail["decision_status"] == PENDING
    assert (await client.get("/api/v1/opportunities/nope", headers=ALICE)).status_code == 404


# ----------------------------------------------------------------- human approval

async def test_approval_records_authenticated_reviewer_timestamp_and_notes(client):
    opp = await seed_opportunity(await seed_run("completed"), 82)
    r = await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved", "notes": "go"}, headers=BOB)
    assert r.status_code == 200 and r.json()["decision_status"] == "approved" and r.json()["reviewer"] == "bob"
    approvals = (await client.get("/api/v1/approvals", headers=ALICE)).json()
    assert len(approvals) == 1
    a = approvals[0]
    assert (a["opportunity_id"], a["decision"], a["reviewer"], a["notes"]) == (opp, "approved", "bob", "go")
    assert a["previous_status"] == PENDING and a["score_at_decision"] == 82 and a["created_at"]
    assert (await client.get(f"/api/v1/opportunities/{opp}", headers=ALICE)).json()["decision_status"] == "approved"


async def test_rejection_works(client):
    opp = await seed_opportunity(await seed_run("completed"), 75)
    r = await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "rejected", "notes": "crowded"}, headers=ALICE)
    assert r.json()["decision_status"] == "rejected"


async def test_final_decisions_are_immutable(client):
    opp = await seed_opportunity(await seed_run("completed"), 82)
    assert (await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "rejected"}, headers=ALICE)).status_code == 200
    again = await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved"}, headers=ALICE)
    assert again.status_code == 409
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count()).select_from(Approval))).scalar_one() == 1


async def test_revision_requested_can_later_be_decided(client):
    opp = await seed_opportunity(await seed_run("completed"), 82)
    assert (await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "revision_requested", "notes": "more evidence"}, headers=ALICE)).status_code == 200
    assert (await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved"}, headers=ALICE)).status_code == 200
    history = (await client.get(f"/api/v1/approvals?opportunity_id={opp}", headers=ALICE)).json()
    assert [(h["previous_status"], h["decision"]) for h in reversed(history)] == [(PENDING, "revision_requested"), ("revision_requested", "approved")]


async def test_below_threshold_cannot_be_approved(client):
    opp = await seed_opportunity(await seed_run("completed"), 55, status=BELOW_THRESHOLD)
    r = await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved"}, headers=ALICE)
    assert r.status_code == 409 and "threshold" in r.json()["detail"]


async def test_decision_validation_and_404(client):
    opp = await seed_opportunity(await seed_run("completed"), 82)
    assert (await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "auto_approve"}, headers=ALICE)).status_code == 422
    assert (await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved", "reviewer": "mallory"}, headers=ALICE)).status_code == 422
    assert (await client.post("/api/v1/opportunities/nope/decision", json={"decision": "approved"}, headers=ALICE)).status_code == 404


async def test_approving_never_triggers_any_downstream_action(client, queue):
    """Approval only records a decision. It must not enqueue work, build, publish or spend."""
    opp = await seed_opportunity(await seed_run("completed"), 82)
    await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved"}, headers=ALICE)
    assert queue.jobs == []


@requires_postgres
async def test_concurrent_decisions_exactly_one_wins(client):
    opp = await seed_opportunity(await seed_run("completed"), 82)
    results = await asyncio.gather(*[
        client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": d}, headers=ALICE)
        for d in ("approved", "rejected", "approved", "rejected")
    ])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409]
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count()).select_from(Approval))).scalar_one() == 1


@requires_postgres
async def test_approvals_table_is_append_only_in_postgres(client):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    opp = await seed_opportunity(await seed_run("completed"), 82)
    await client.post(f"/api/v1/opportunities/{opp}/decision", json={"decision": "approved"}, headers=ALICE)
    async with SessionLocal() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("UPDATE approvals SET notes='tampered'"))
            await s.commit()
    async with SessionLocal() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("DELETE FROM approvals"))
            await s.commit()

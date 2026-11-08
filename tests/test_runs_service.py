import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import BELOW_THRESHOLD, PENDING, Opportunity, ResearchRun
from app.research.validation import finalize
from app.services import runs
from tests.helpers import draft, evidence_for_all, ledger_with, seed_run, synthesis

URLS = ["https://a.example.org/1", "https://b.example.net/2", "https://c.example.com/3"]


def utc(delta_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)


async def get(run_id) -> ResearchRun:
    async with SessionLocal() as s:
        return await s.get(ResearchRun, run_id, populate_existing=True)


def result_of(*scores):
    ledger = ledger_with(*URLS)
    drafts = [draft(name=f"idea-{s}", scores=s, evidence=evidence_for_all(URLS)) for s in scores]
    return finalize(synthesis(*drafts), ledger, max_opportunities=10, default_regions=["Global"])


# ----------------------------------------------------------------- claiming / duplicate prevention

async def test_claim_moves_queued_to_running_with_lease():
    run_id = await seed_run()
    claimed = await runs.claim_run(run_id, "w1")
    assert claimed and claimed.status == "running" and claimed.locked_by == "w1" and claimed.attempts == 1
    assert claimed.lease_expires_at is not None and claimed.started_at is not None


async def test_only_one_of_many_concurrent_workers_wins_the_claim():
    run_id = await seed_run()
    winners = await asyncio.gather(*[runs.claim_run(run_id, f"w{i}") for i in range(8)])
    assert sum(1 for w in winners if w is not None) == 1
    assert (await get(run_id)).attempts == 1


async def test_cannot_claim_a_run_that_is_not_queued():
    for status in ("running", "completed", "failed"):
        assert await runs.claim_run(await seed_run(status=status), "w1") is None
    assert await runs.claim_run("does-not-exist", "w1") is None


# ----------------------------------------------------------------- completion

async def test_complete_persists_opportunities_and_marks_pending_human_approval():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")
    result = result_of(90, 40)
    assert await runs.complete_run(run_id, "w1", result, raw_output="raw", ledger={"sources": [1, 2]})

    run = await get(run_id)
    assert run.status == "completed" and run.completed_at and run.raw_output == "raw" and run.evidence_ledger == {"sources": [1, 2]}
    async with SessionLocal() as s:
        opps = {o.name: o for o in (await s.execute(select(Opportunity))).scalars()}
    assert opps["idea-90"].decision_status == PENDING and opps["idea-90"].recommended is True
    assert opps["idea-40"].decision_status == BELOW_THRESHOLD and opps["idea-40"].recommended is False
    assert opps["idea-90"].score_breakdown["demand_evidence"]["effective_sources"] == 3
    assert all(o.decision_status != "approved" for o in opps.values())  # nothing is ever auto-approved


async def test_zombie_worker_cannot_overwrite_after_losing_the_lease():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")
    # lease expires, sweep re-queues, a second worker takes over
    async with SessionLocal() as s:
        run = await s.get(ResearchRun, run_id)
        run.lease_expires_at = utc(-10)
        await s.commit()
    assert [r for r, _ in await runs.requeue_expired_leases()] == [run_id]
    await runs.claim_run(run_id, "w2")

    assert await runs.complete_run(run_id, "w1", result_of(90), "stale", {}) is False
    assert await runs.fail_run(run_id, "w1", "stale failure") is False
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one() == 0  # rolled back
    run = await get(run_id)
    assert run.status == "running" and run.locked_by == "w2"


async def test_fail_run_records_error_and_redacts_secrets():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")
    leaked = f"auth failed for key {settings.anthropic_api_key} at upstream"
    assert await runs.fail_run(run_id, "w1", leaked, raw_output="partial", ledger={"calls": []})
    run = await get(run_id)
    assert run.status == "failed" and run.completed_at and run.raw_output == "partial"
    assert settings.anthropic_api_key not in run.error_message and "[redacted]" in run.error_message


# ----------------------------------------------------------------- heartbeat & recovery

async def test_heartbeat_extends_lease_only_for_the_owner():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")
    before = (await get(run_id)).lease_expires_at
    await asyncio.sleep(0.01)
    assert await runs.heartbeat(run_id, "w1") is True
    assert await runs.heartbeat(run_id, "intruder") is False
    assert (await get(run_id)).lease_expires_at >= before


async def test_dead_worker_run_is_requeued_then_failed_after_max_attempts():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")  # attempt 1
    async with SessionLocal() as s:
        (await s.get(ResearchRun, run_id)).lease_expires_at = utc(-5)
        await s.commit()
    assert await runs.requeue_expired_leases() == [(run_id, 1)]
    run = await get(run_id)
    assert run.status == "queued" and run.locked_by is None and run.enqueued_at is None

    await runs.claim_run(run_id, "w2")  # attempt 2 == max_attempts
    async with SessionLocal() as s:
        (await s.get(ResearchRun, run_id)).lease_expires_at = utc(-5)
        await s.commit()
    assert await runs.requeue_expired_leases() == []
    run = await get(run_id)
    assert run.status == "failed" and "lease expired" in run.error_message.lower()


async def test_live_leases_are_left_alone():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")
    assert await runs.requeue_expired_leases() == []
    assert (await get(run_id)).status == "running"


async def test_release_run_makes_it_immediately_recoverable():
    run_id = await seed_run()
    await runs.claim_run(run_id, "w1")
    await runs.release_run(run_id, "w1")
    assert [r for r, _ in await runs.requeue_expired_leases()] == [run_id]


# ----------------------------------------------------------------- redelivery

async def test_runs_to_enqueue_finds_undelivered_and_overdue_but_not_fresh():
    never = await seed_run()
    fresh = await seed_run(enqueued_at=utc(0))
    overdue = await seed_run(enqueued_at=utc(-(settings.redeliver_after_seconds + 30)))
    await seed_run(status="running")
    found = {r for r, _ in await runs.runs_to_enqueue()}
    assert found == {never, overdue} and fresh not in found


async def test_mark_enqueued_sets_timestamp():
    run_id = await seed_run()
    await runs.mark_enqueued(run_id)
    assert (await get(run_id)).enqueued_at is not None
    assert run_id not in {r for r, _ in await runs.runs_to_enqueue()}

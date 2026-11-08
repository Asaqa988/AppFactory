import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.entities import Opportunity, ResearchRun
from app.services import runs
from app.worker import WorkerSettings
from app.worker.main import run_research_job, sweep
from app.worker.queue import enqueue_run
from tests.conftest import FakeQueue
from tests.fakes import factory, good_synthesis
from tests.helpers import seed_run

CTX = {"worker_id": "test-worker"}


async def get(run_id):
    async with SessionLocal() as s:
        return await s.get(ResearchRun, run_id, populate_existing=True)


@pytest.fixture
def patch_crew(monkeypatch):
    def apply(**kwargs):
        make = factory(**kwargs)
        monkeypatch.setattr("app.research.flow.build_research_crew", make)
        return make
    return apply


async def test_job_runs_the_flow_end_to_end(patch_crew):
    patch_crew(synth=good_synthesis((91,)))
    run_id = await seed_run()
    assert await run_research_job(CTX, run_id) == "completed"
    run = await get(run_id)
    assert run.status == "completed" and run.locked_by == "test-worker" and run.attempts == 1
    async with SessionLocal() as s:
        assert [o.decision_status for o in (await s.execute(select(Opportunity))).scalars()] == ["pending_approval"]


async def test_job_failure_is_recorded_not_raised(patch_crew):
    patch_crew(raises=RuntimeError("provider outage"))
    run_id = await seed_run()
    assert await run_research_job(CTX, run_id) == "failed"
    run = await get(run_id)
    assert run.status == "failed" and "provider outage" in run.error_message


async def test_duplicate_delivery_of_the_same_job_runs_the_crew_once(patch_crew):
    make = patch_crew(delay=0.05)
    run_id = await seed_run()
    results = await asyncio.gather(*[run_research_job({"worker_id": f"w{i}"}, run_id) for i in range(5)])
    assert sorted(results) == ["completed", "skipped", "skipped", "skipped", "skipped"]
    assert len(make.created) == 1  # the LLM crew was built and run exactly once
    async with SessionLocal() as s:
        assert len((await s.execute(select(Opportunity))).scalars().all()) == 1


async def test_redelivery_after_completion_is_a_noop(patch_crew):
    make = patch_crew()
    run_id = await seed_run()
    await run_research_job(CTX, run_id)
    assert await run_research_job(CTX, run_id) == "skipped"
    assert len(make.created) == 1


async def test_unknown_run_is_skipped():
    assert await run_research_job(CTX, "does-not-exist") == "skipped"


async def test_cancellation_hands_the_run_back_for_retry(patch_crew):
    patch_crew(delay=2)  # sync sleep in the crew thread; the cancelled job abandons it
    run_id = await seed_run()
    task = asyncio.create_task(run_research_job(CTX, run_id))
    await asyncio.sleep(0.3)
    assert (await get(run_id)).status == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # lease is released, so the next sweep re-queues it (attempts remain)
    assert [r for r, _ in await runs.requeue_expired_leases()] == [run_id]
    assert (await get(run_id)).status == "queued"


async def test_sweep_requeues_dead_workers_and_delivers_undelivered_runs():
    dead = await seed_run(status="running", attempts=1, locked_by="gone",
                          lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    undelivered = await seed_run()
    queue = FakeQueue()
    await sweep({"redis": queue})
    delivered = {(job_id) for _, _, job_id in queue.jobs}
    assert delivered == {f"{dead}:1", f"{undelivered}:0"}  # attempt number keeps job ids unique per delivery
    assert (await get(dead)).status == "queued"
    assert (await get(undelivered)).enqueued_at is not None


async def test_sweep_survives_redis_outage_and_retries_next_tick():
    run_id = await seed_run()
    queue = FakeQueue()
    queue.fail = True
    await sweep({"redis": queue})  # must not raise
    assert (await get(run_id)).enqueued_at is None
    queue.fail = False
    await sweep({"redis": queue})
    assert (await get(run_id)).enqueued_at is not None


async def test_enqueue_is_idempotent_per_attempt():
    run_id = await seed_run()
    queue = FakeQueue()
    assert await enqueue_run(queue, run_id, 0)
    assert queue.jobs == [("run_research_job", run_id, f"{run_id}:0")]


def test_worker_settings_are_wired_for_durability():
    assert [f.__name__ for f in WorkerSettings.functions] == ["run_research_job"]
    assert WorkerSettings.max_tries == 1  # retries owned by the PostgreSQL lease logic
    assert WorkerSettings.keep_result == 0
    assert WorkerSettings.cron_jobs and WorkerSettings.on_startup
    assert WorkerSettings.redis_settings.host

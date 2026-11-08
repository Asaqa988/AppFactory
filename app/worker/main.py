import asyncio
import logging
import os
import socket

from arq import cron

from app.core.config import settings
from app.core.database import engine, init_db
from app.services import runs
from app.worker.queue import enqueue_run, redis_settings

log = logging.getLogger("appfactory.worker")


async def _heartbeat_loop(run_id: str, worker_id: str, lost: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(settings.heartbeat_seconds)
        try:
            if not await runs.heartbeat(run_id, worker_id):
                log.error("Lease for run %s was lost; results will be discarded", run_id)
                lost.set()
                return
        except Exception:  # noqa: BLE001 - a transient DB blip must not kill the run
            log.exception("Heartbeat for run %s failed; will retry", run_id)


async def run_research_job(ctx: dict, run_id: str) -> str:
    # Imported here so the worker module stays importable (and testable) without CrewAI loaded.
    from app.research.flow import execute_research_flow

    worker_id = ctx["worker_id"]
    run = await runs.claim_run(run_id, worker_id)
    if run is None:
        log.info("Run %s not claimable (already running, finished or unknown); skipping", run_id)
        return "skipped"

    lost = asyncio.Event()
    beat = asyncio.create_task(_heartbeat_loop(run_id, worker_id, lost))
    try:
        await execute_research_flow(run, worker_id)
        return "completed"
    except asyncio.CancelledError:
        # Worker shutting down or job_timeout hit: hand the run back so another claim can retry it.
        await asyncio.shield(runs.release_run(run_id, worker_id))
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Run %s failed", run_id)
        await runs.fail_run(run_id, worker_id, f"{type(exc).__name__}: {exc}")
        return "failed"
    finally:
        beat.cancel()


async def sweep(ctx: dict) -> None:
    """Self-healing: recover dead workers' runs and deliver runs Redis never received."""
    pool = ctx["redis"]
    try:
        for run_id, attempts in await runs.requeue_expired_leases():
            log.warning("Re-queued run %s after lease expiry", run_id)
            await enqueue_run(pool, run_id, attempts)
        for run_id, attempts in await runs.runs_to_enqueue():
            await enqueue_run(pool, run_id, attempts)
    except Exception:  # noqa: BLE001
        log.exception("Sweep failed; will retry on the next tick")


async def startup(ctx: dict) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await init_db()
    ctx["worker_id"] = f"{socket.gethostname()}:{os.getpid()}"
    log.info("Worker %s ready (model=%s)", ctx["worker_id"], settings.claude_model)


async def shutdown(ctx: dict) -> None:
    await engine.dispose()


class WorkerSettings:
    functions = [run_research_job]
    cron_jobs = [cron(sweep, second=set(range(0, 60, settings.sweep_interval_seconds)), run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout_seconds
    max_tries = 1  # retries are owned by the PostgreSQL lease logic, not by arq
    keep_result = 0  # job ids embed the attempt number; no result keys needed
    health_check_interval = 30

"""Delivery of run ids to the arq worker over Redis. Failure to enqueue is never fatal:
the run stays `queued` in PostgreSQL and the worker's sweep delivers it later."""
import logging

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings
from app.services import runs

log = logging.getLogger(__name__)

JOB_NAME = "run_research_job"


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def create_redis_pool() -> ArqRedis:
    return await create_pool(redis_settings())


async def enqueue_run(pool: ArqRedis, run_id: str, attempts: int) -> bool:
    """Idempotent: the job id embeds the attempt number, so the same delivery cannot queue twice."""
    try:
        await pool.enqueue_job(JOB_NAME, run_id, _job_id=f"{run_id}:{attempts}")
    except Exception:  # noqa: BLE001 - Redis down must not fail the request; the sweep retries
        log.exception("Could not enqueue run %s; it stays queued in PostgreSQL", run_id)
        return False
    await runs.mark_enqueued(run_id)
    return True

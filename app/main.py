import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import settings
from app.core.database import init_db, ping_db
from app.worker.queue import create_redis_pool

log = logging.getLogger("appfactory.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await init_db()
    # Redis being down at boot must not stop the API: runs are durable in PostgreSQL and the
    # worker's sweep delivers anything that could not be enqueued.
    app.state.queue = None
    try:
        app.state.queue = await create_redis_pool()
    except Exception:  # noqa: BLE001
        log.exception("Redis unavailable at startup; runs will be delivered by the worker sweep")
    yield
    if app.state.queue is not None:
        await app.state.queue.aclose()


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    description="Research MVP for an AI-powered mobile app venture studio. Human approval is mandatory.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["ops"])
async def health():
    """Unauthenticated liveness/readiness. 503 when PostgreSQL is unreachable.
    Redis being down is reported as `degraded` (runs still queue durably) but stays HTTP 200."""
    database = await ping_db()
    redis_ok = False
    queue = getattr(app.state, "queue", None)
    if queue is not None:
        try:
            redis_ok = bool(await queue.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
    body = {
        "status": "ok" if database and redis_ok else ("degraded" if database else "unavailable"),
        "service": settings.app_name,
        "environment": settings.app_env,
        "database": "up" if database else "down",
        "redis": "up" if redis_ok else "down",
    }
    return JSONResponse(body, status_code=200 if database else 503)

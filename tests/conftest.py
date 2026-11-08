"""Offline test harness.

Environment is set BEFORE any `app` import (settings and the DB engine are created at import time).
Credentials are always fake, so these tests can never reach Anthropic or Tavily.
Database: SQLite by default; set TEST_DATABASE_URL=postgresql+asyncpg://... to run the same suite
against PostgreSQL (the PostgreSQL-only tests then un-skip).
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="appfactory-tests-")
os.environ.update({
    "APP_ENV": "test",
    "API_KEYS": "alice:test-key-alice,bob:test-key-bob",
    "API_KEY": "",
    "ANTHROPIC_API_KEY": "sk-ant-fake-key-for-offline-tests",
    "TAVILY_API_KEY": "tvly-fake-key-for-offline-tests",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{_tmp}/test.db",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_DISABLE_TELEMETRY": "true",
})
# CrewAI coordinates internal file/DB locks through Redis whenever REDIS_URL is set in the environment.
# Offline tests have no Redis, so make sure it is unset (the Settings default is never contacted).
os.environ.pop("REDIS_URL", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.core.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

ALICE = {"X-API-Key": "test-key-alice"}
BOB = {"X-API-Key": "test-key-bob"}
IS_POSTGRES = engine.dialect.name == "postgresql"


class FakeQueue:
    """Stands in for the arq Redis pool."""

    def __init__(self):
        self.jobs: list[tuple[str, str, str | None]] = []
        self.fail = False

    async def enqueue_job(self, name, *args, _job_id=None, **kwargs):
        if self.fail:
            raise ConnectionError("redis down")
        self.jobs.append((name, args[0], _job_id))
        return object()

    async def ping(self):
        if self.fail:
            raise ConnectionError("redis down")
        return True

    async def aclose(self):
        pass


@pytest_asyncio.fixture(autouse=True)
async def database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield
    # asyncpg connections are bound to the event loop that opened them and pytest-asyncio gives every
    # test its own loop, so drop the pooled connections after each test.
    await engine.dispose()


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.state.queue = fake
    yield fake
    app.state.queue = None


@pytest_asyncio.fixture
async def client(queue):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


requires_postgres = pytest.mark.skipif(not IS_POSTGRES, reason="needs PostgreSQL (set TEST_DATABASE_URL)")

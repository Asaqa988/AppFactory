from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session():
    async with SessionLocal() as session:
        yield session


_APPROVALS_APPEND_ONLY = [
    """
    CREATE OR REPLACE FUNCTION approvals_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'approvals is an append-only audit table';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS approvals_no_update_delete ON approvals",
    """
    CREATE TRIGGER approvals_no_update_delete
    BEFORE UPDATE OR DELETE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_append_only()
    """,
]


# Columns added after the first release. `create_all` only creates missing TABLES, so an existing database
# needs these (idempotent, PostgreSQL only; fresh databases already get the columns from the models).
_ADDED_COLUMNS = [
    "ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS domain VARCHAR(16) NOT NULL DEFAULT 'apps'",
    "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS domain VARCHAR(16) NOT NULL DEFAULT 'apps'",
    "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb",
]


async def init_db() -> None:
    """Create tables. Safe to call from several containers at once (PostgreSQL advisory lock).

    MVP-level schema management: there is no migration history yet. The `_ADDED_COLUMNS` statements are a
    stopgap for the first additive change; introduce Alembic before any change that renames, drops or
    retypes a column.
    """
    from app.models import entities  # noqa: F401  (register models on Base.metadata)

    async with engine.begin() as conn:
        is_postgres = conn.dialect.name == "postgresql"
        if is_postgres:
            await conn.execute(text("SELECT pg_advisory_xact_lock(727274)"))
        await conn.run_sync(Base.metadata.create_all)
        if is_postgres:
            for statement in _ADDED_COLUMNS:
                await conn.execute(text(statement))
            for statement in _APPROVALS_APPEND_ONLY:
                await conn.execute(text(statement))


async def ping_db() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - health probe must never raise
        return False

"""Research-run lifecycle. PostgreSQL is the source of truth for job state.

Redis (arq) only *delivers* a wake-up message. Correctness never depends on Redis:

  * Duplicate execution is prevented by an atomic compare-and-set claim
    (`UPDATE ... WHERE id=:id AND status='queued'`); at most one worker can win it.
  * A worker that dies mid-run stops heartbeating; its lease expires; `requeue_expired_leases`
    puts the run back in `queued` (or fails it once `max_attempts` claims are used up).
  * A zombie worker that wakes up after losing its lease cannot overwrite anything: every
    completion/failure write is conditioned on `status='running' AND locked_by=:me`.
  * If Redis loses a message (or the API crashed between commit and enqueue), `runs_to_enqueue`
    finds `queued` runs that were never (or not recently) enqueued and they are delivered again.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import (
    BELOW_THRESHOLD, PENDING, Opportunity, ResearchRun,
)
from app.schemas.research import ResearchResult

log = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


def redact(message: str) -> str:
    """Strip configured secrets from text before it is stored or returned."""
    for secret in settings.secret_values():
        message = message.replace(secret, "[redacted]")
    return message[:4000]


async def create_run(
    session: AsyncSession, *, brief: str, regions: list[str], categories: list[str],
    max_opportunities: int, requested_by: str, domain: str = "apps",
) -> ResearchRun:
    run = ResearchRun(
        domain=domain, brief=brief, regions=regions, categories=categories,
        max_opportunities=min(max_opportunities, settings.max_opportunities),
        requested_by=requested_by, status="queued", model=settings.claude_model,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def count_active(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(ResearchRun).where(ResearchRun.status.in_(("queued", "running")))
    )
    return int(result.scalar_one())


async def mark_enqueued(run_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(ResearchRun).where(ResearchRun.id == run_id, ResearchRun.status == "queued")
            .values(enqueued_at=now())
        )
        await session.commit()


async def claim_run(run_id: str, worker_id: str) -> ResearchRun | None:
    """Atomically move queued -> running. Returns None if someone else owns it or it is not queued."""
    t = now()
    async with SessionLocal() as session:
        result = await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "queued")
            .values(
                status="running", locked_by=worker_id, attempts=ResearchRun.attempts + 1,
                heartbeat_at=t, lease_expires_at=t + timedelta(seconds=settings.lease_seconds),
                started_at=t, error_message=None,
            )
        )
        await session.commit()
        if result.rowcount != 1:
            return None
        return await session.get(ResearchRun, run_id, populate_existing=True)


async def heartbeat(run_id: str, worker_id: str) -> bool:
    """Extend the lease. False means the lease was lost and the worker must stop."""
    t = now()
    async with SessionLocal() as session:
        result = await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "running", ResearchRun.locked_by == worker_id)
            .values(heartbeat_at=t, lease_expires_at=t + timedelta(seconds=settings.lease_seconds))
        )
        await session.commit()
        return result.rowcount == 1


async def complete_run(
    run_id: str, worker_id: str, result: ResearchResult, raw_output: str, ledger: dict,
) -> bool:
    """Persist results and mark the run completed in ONE transaction. False if the lease was lost."""
    async with SessionLocal() as session:
        outcome = await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "running", ResearchRun.locked_by == worker_id)
            .values(
                status="completed", completed_at=now(), result_summary=result.research_summary,
                raw_output=raw_output, evidence_ledger=ledger, lease_expires_at=None,
            )
        )
        if outcome.rowcount != 1:
            await session.rollback()
            return False
        for item in result.opportunities:
            session.add(Opportunity(
                run_id=run_id, domain=item.domain, details=item.details,
                name=item.name[:180], elevator_pitch=item.elevator_pitch, target_audience=item.target_audience,
                problem=item.problem, proposed_solution=item.proposed_solution, category=item.category[:100],
                target_regions=item.target_regions,
                competitors=[c.model_dump() for c in item.competitors],
                evidence=[e.model_dump() for e in item.evidence],
                monetization_hypothesis=item.monetization_hypothesis, ad_suitability=item.ad_suitability,
                risks=item.risks, assumptions=item.assumptions, validation_notes=item.validation_notes,
                total_score=item.total_score, confidence=item.confidence, confidence_label=item.confidence_label,
                score_breakdown=item.score_breakdown, scoring_version=item.scoring_version,
                warnings=item.warnings, recommended=item.recommended,
                # Human approval is mandatory: recommended ideas wait for the owner; the rest are
                # stored for audit but are not queued for review.
                decision_status=PENDING if item.recommended else BELOW_THRESHOLD,
            ))
        await session.commit()
        return True


async def fail_run(run_id: str, worker_id: str, error: str, raw_output: str | None = None, ledger: dict | None = None) -> bool:
    async with SessionLocal() as session:
        values = dict(status="failed", completed_at=now(), error_message=redact(error), lease_expires_at=None)
        if raw_output is not None:
            values["raw_output"] = raw_output
        if ledger is not None:
            values["evidence_ledger"] = ledger
        outcome = await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "running", ResearchRun.locked_by == worker_id)
            .values(**values)
        )
        await session.commit()
        return outcome.rowcount == 1


async def release_run(run_id: str, worker_id: str) -> None:
    """Expire our own lease immediately so the next sweep re-queues the run (shutdown / timeout)."""
    async with SessionLocal() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id, ResearchRun.status == "running", ResearchRun.locked_by == worker_id)
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )
        await session.commit()


async def requeue_expired_leases() -> list[tuple[str, int]]:
    """Recover runs whose worker died. Returns (run_id, attempts) pairs that must be re-enqueued."""
    t = now()
    requeued: list[tuple[str, int]] = []
    async with SessionLocal() as session:
        stale = (await session.execute(
            select(ResearchRun.id, ResearchRun.attempts).where(
                ResearchRun.status == "running", ResearchRun.lease_expires_at < t
            )
        )).all()
        for run_id, attempts in stale:
            if attempts >= settings.max_attempts:
                changed = await session.execute(
                    update(ResearchRun)
                    .where(ResearchRun.id == run_id, ResearchRun.status == "running", ResearchRun.lease_expires_at < t)
                    .values(status="failed", completed_at=t, lease_expires_at=None,
                            error_message=f"Worker lease expired and the run already used {attempts} attempt(s).")
                )
            else:
                changed = await session.execute(
                    update(ResearchRun)
                    .where(ResearchRun.id == run_id, ResearchRun.status == "running", ResearchRun.lease_expires_at < t)
                    .values(status="queued", locked_by=None, lease_expires_at=None, enqueued_at=None)
                )
                if changed.rowcount == 1:
                    requeued.append((run_id, attempts))
        await session.commit()
    return requeued


async def runs_to_enqueue() -> list[tuple[str, int]]:
    """Queued runs that were never delivered to Redis, or whose delivery is overdue."""
    cutoff = now() - timedelta(seconds=settings.redeliver_after_seconds)
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ResearchRun.id, ResearchRun.attempts).where(
                ResearchRun.status == "queued",
                or_(ResearchRun.enqueued_at.is_(None), ResearchRun.enqueued_at < cutoff),
            ).order_by(ResearchRun.created_at).limit(50)
        )).all()
    return [(run_id, attempts) for run_id, attempts in rows]

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import Principal, require_api_key
from app.core.config import settings
from app.core.database import get_session
from app.models.entities import (
    BELOW_THRESHOLD, DECIDABLE_STATUSES, Approval, Opportunity, ResearchRun,
)
from app.schemas.api import (
    ApprovalOut, DecisionOut, OpportunityOut, OpportunitySummary, RunCreated, RunDetail, RunSummary,
)
from app.schemas.research import DecisionRequest, ResearchRequest
from app.services import runs
from app.worker.queue import enqueue_run

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

Limit = Query(default=50, ge=1, le=200)
Offset = Query(default=0, ge=0)


@router.post("/research-runs", status_code=202, response_model=RunCreated)
async def create_research_run(
    body: ResearchRequest,
    request: Request,
    principal: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
):
    missing = settings.missing_research_credentials()
    if missing:
        raise HTTPException(503, f"Research providers are not configured: {', '.join(missing)}.")
    if await runs.count_active(session) >= settings.max_active_runs:
        raise HTTPException(429, f"Too many active research runs (limit {settings.max_active_runs}). Try again later.")

    run = await runs.create_run(
        session, brief=body.brief, regions=body.regions, categories=body.categories,
        max_opportunities=body.max_opportunities, requested_by=principal.name, domain=body.domain,
    )
    # The run is durable in PostgreSQL before we touch Redis. If enqueueing fails the worker's
    # sweep delivers it, so the response is 202 either way.
    pool = getattr(request.app.state, "queue", None)
    if pool is not None:
        await enqueue_run(pool, run.id, run.attempts)
    return RunCreated(run_id=run.id, status=run.status)


@router.get("/research-runs", response_model=list[RunSummary])
async def list_research_runs(
    status: str | None = Query(default=None, pattern="^(queued|running|completed|failed)$"),
    limit: int = Limit, offset: int = Offset,
    session: AsyncSession = Depends(get_session),
):
    query = select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(limit).offset(offset)
    if status:
        query = query.where(ResearchRun.status == status)
    return (await session.execute(query)).scalars().all()


@router.get("/research-runs/{run_id}", response_model=RunDetail)
async def get_research_run(run_id: str, session: AsyncSession = Depends(get_session)):
    run = await session.get(ResearchRun, run_id)
    if not run:
        raise HTTPException(404, "Research run not found.")
    ids = (await session.execute(
        select(Opportunity.id).where(Opportunity.run_id == run_id).order_by(Opportunity.total_score.desc())
    )).scalars().all()
    ledger = run.evidence_ledger or {}
    detail = RunDetail.model_validate(run)
    detail.summary = run.result_summary
    detail.opportunity_ids = list(ids)
    detail.source_count = len(ledger.get("sources", []))
    return detail


@router.get("/opportunities", response_model=list[OpportunitySummary])
async def list_opportunities(
    run_id: str | None = None,
    domain: str | None = Query(default=None, pattern="^(apps|games)$"),
    decision_status: str | None = Query(
        default=None, pattern="^(pending_approval|below_threshold|approved|rejected|revision_requested)$"
    ),
    recommended: bool | None = None,
    min_score: float | None = Query(default=None, ge=0, le=100),
    limit: int = Limit, offset: int = Offset,
    session: AsyncSession = Depends(get_session),
):
    query = select(Opportunity).order_by(Opportunity.total_score.desc(), Opportunity.created_at.desc())
    if run_id:
        query = query.where(Opportunity.run_id == run_id)
    if domain:
        query = query.where(Opportunity.domain == domain)
    if decision_status:
        query = query.where(Opportunity.decision_status == decision_status)
    if recommended is not None:
        query = query.where(Opportunity.recommended == recommended)
    if min_score is not None:
        query = query.where(Opportunity.total_score >= min_score)
    return (await session.execute(query.limit(limit).offset(offset))).scalars().all()


@router.get("/opportunities/{opportunity_id}", response_model=OpportunityOut)
async def get_opportunity(opportunity_id: str, session: AsyncSession = Depends(get_session)):
    opportunity = await session.get(Opportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(404, "Opportunity not found.")
    return opportunity


@router.post("/opportunities/{opportunity_id}/decision", response_model=DecisionOut)
async def decide_opportunity(
    opportunity_id: str,
    body: DecisionRequest,
    principal: Principal = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
):
    """Record a human decision. The row lock serialises concurrent decisions so exactly one wins."""
    opportunity = (await session.execute(
        select(Opportunity).where(Opportunity.id == opportunity_id).with_for_update()
    )).scalar_one_or_none()
    if not opportunity:
        raise HTTPException(404, "Opportunity not found.")
    if opportunity.decision_status == BELOW_THRESHOLD:
        raise HTTPException(
            409, f"Score is below the {settings.threshold_for(opportunity.domain):g} review threshold; not eligible for approval."
        )
    if opportunity.decision_status not in DECIDABLE_STATUSES:
        raise HTTPException(409, f"A final decision ('{opportunity.decision_status}') has already been recorded.")

    approval = Approval(
        opportunity_id=opportunity.id, decision=body.decision, previous_status=opportunity.decision_status,
        reviewer=principal.name, notes=body.notes, score_at_decision=opportunity.total_score,
    )
    opportunity.decision_status = body.decision
    session.add(approval)
    await session.commit()
    return DecisionOut(
        opportunity_id=opportunity.id, decision_status=opportunity.decision_status,
        approval_id=approval.id, reviewer=approval.reviewer,
    )


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    opportunity_id: str | None = None,
    decision: str | None = Query(default=None, pattern="^(approved|rejected|revision_requested)$"),
    limit: int = Limit, offset: int = Offset,
    session: AsyncSession = Depends(get_session),
):
    """Append-only decision history (who decided what, when, with which notes)."""
    query = select(Approval).order_by(Approval.created_at.desc()).limit(limit).offset(offset)
    if opportunity_id:
        query = query.where(Approval.opportunity_id == opportunity_id)
    if decision:
        query = query.where(Approval.decision == decision)
    return (await session.execute(query)).scalars().all()

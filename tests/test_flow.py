import asyncio
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.entities import BELOW_THRESHOLD, PENDING, Opportunity, ResearchRun
from app.research.flow import ResearchError, execute_research_flow, extract_json
from app.services import runs
from tests.fakes import factory, good_synthesis
from tests.helpers import seed_run


async def claimed_run():
    run_id = await seed_run()
    return await runs.claim_run(run_id, "w1")


async def stored(run_id):
    async with SessionLocal() as s:
        run = await s.get(ResearchRun, run_id, populate_existing=True)
        opps = (await s.execute(select(Opportunity).where(Opportunity.run_id == run_id))).scalars().all()
        return run, opps


async def test_full_flow_scores_persists_and_marks_pending_approval():
    run = await claimed_run()
    make = factory(synth=good_synthesis(scores=(92, 45)))
    await execute_research_flow(run, "w1", crew_factory=make)

    stored_run, opps = await stored(run.id)
    assert stored_run.status == "completed" and stored_run.result_summary == "summary"
    by_name = {o.name: o for o in opps}
    assert by_name["idea-92"].decision_status == PENDING and by_name["idea-92"].total_score == 92.0
    assert by_name["idea-45"].decision_status == BELOW_THRESHOLD
    assert stored_run.evidence_ledger["token_usage"] == {"total_tokens": 1234}
    assert len(stored_run.evidence_ledger["sources"]) == 3
    # inputs the crew saw came from the stored run
    assert make.created[0].inputs["regions"] == "US" and make.created[0].inputs["categories"] == "productivity"


async def test_flow_fails_when_no_search_returned_results():
    run = await claimed_run()
    with pytest.raises(ResearchError, match="No research tool returned any results"):
        await execute_research_flow(run, "w1", crew_factory=factory(search=False))
    stored_run, opps = await stored(run.id)
    assert stored_run.status == "failed" and "No research tool" in stored_run.error_message and opps == []
    assert stored_run.raw_output  # kept for debugging


async def test_flow_fails_when_crew_produced_nothing():
    run = await claimed_run()
    with pytest.raises(ResearchError, match="no opportunities"):
        await execute_research_flow(run, "w1", crew_factory=factory(synth=good_synthesis(scores=())))
    assert (await stored(run.id))[0].status == "failed"


async def test_flow_falls_back_to_json_parsing_without_structured_output():
    run = await claimed_run()
    raw = "Here you go:\n```json\n" + good_synthesis((88,)).model_dump_json() + "\n```"
    await execute_research_flow(run, "w1", crew_factory=factory(pydantic_ok=False, raw=raw))
    assert (await stored(run.id))[1][0].name == "idea-88"


@pytest.mark.parametrize("raw", ["no json at all", "{not json}", '{"research_summary": "s"}'])
async def test_flow_fails_cleanly_on_unparseable_or_invalid_output(raw):
    run = await claimed_run()
    with pytest.raises(ResearchError):
        await execute_research_flow(run, "w1", crew_factory=factory(pydantic_ok=False, raw=raw))
    stored_run, opps = await stored(run.id)
    assert stored_run.status == "failed" and opps == []


async def test_crew_exception_marks_run_failed_and_preserves_ledger():
    run = await claimed_run()
    with pytest.raises(RuntimeError, match="anthropic exploded"):
        await execute_research_flow(run, "w1", crew_factory=factory(raises=RuntimeError("anthropic exploded")))
    stored_run, _ = await stored(run.id)
    assert stored_run.status == "failed" and "anthropic exploded" in stored_run.error_message
    assert stored_run.evidence_ledger is not None


async def test_flow_refuses_to_start_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    run = await claimed_run()
    with pytest.raises(ResearchError, match="ANTHROPIC_API_KEY"):
        await execute_research_flow(run, "w1", crew_factory=factory())
    assert (await stored(run.id))[0].status == "failed"


async def test_results_discarded_if_lease_was_lost_during_the_crew_run():
    run = await claimed_run()

    loop = asyncio.get_running_loop()

    async def thief():  # another worker takes over while the crew is still working
        await runs.release_run(run.id, "w1")
        await runs.requeue_expired_leases()
        await runs.claim_run(run.id, "w2")

    def stealing_factory(ledger):
        crew = factory()(ledger)
        original = crew.kickoff

        def kickoff(inputs):
            out = original(inputs)
            asyncio.run_coroutine_threadsafe(thief(), loop).result(timeout=10)  # block until taken over
            return out

        crew.kickoff = kickoff
        return crew

    with pytest.raises(ResearchError, match="Lease lost"):
        await execute_research_flow(run, "w1", crew_factory=stealing_factory)
    stored_run, opps = await stored(run.id)
    assert stored_run.status == "running" and stored_run.locked_by == "w2" and opps == []


async def test_event_loop_stays_responsive_while_the_crew_blocks():
    """Regression: a blocking crew call must not starve heartbeats / health checks / sweeps.
    (In the real stack, running the sync Anthropic client on the loop made arq report 'unhealthy'.)"""
    run = await claimed_run()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    await execute_research_flow(run, "w1", crew_factory=factory(delay=0.6))
    task.cancel()
    assert ticks >= 15, f"event loop was blocked during the crew run (only {ticks} ticks)"


async def test_max_opportunities_is_capped_by_settings(monkeypatch):
    monkeypatch.setattr(settings, "max_opportunities", 1)
    run = await claimed_run()
    await execute_research_flow(run, "w1", crew_factory=factory(synth=good_synthesis((90, 80, 70))))
    assert len((await stored(run.id))[1]) == 1


def test_extract_json_handles_fences_and_prose():
    assert extract_json('text ```json\n{"a": 1}\n``` tail') == {"a": 1}
    with pytest.raises(ResearchError):
        extract_json("nothing here")
    assert json.loads('{"a":1}') == extract_json('{"a":1}')

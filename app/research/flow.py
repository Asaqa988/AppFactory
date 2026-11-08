"""ResearchFlow: the workflow around the research Crew.

  intake -> run_agents -> validate_and_score -> persist

Each step is a CrewAI Flow method over typed Pydantic state. Any exception aborts the flow and
propagates to the worker, which records the run as failed.
"""
import asyncio
import json
import logging
import re
from typing import Any, Callable

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, Field

from app.core.config import settings
from app.models.entities import ResearchRun
from app.research.crew import build_research_crew
from app.research.ledger import EvidenceLedger
from app.research.validation import finalize
from app.schemas.research import GameSynthesisOutput, ResearchResult, SynthesisOutput
from app.services import runs

log = logging.getLogger(__name__)


class ResearchError(RuntimeError):
    """A run cannot produce trustworthy output (misconfiguration, no real evidence, unparseable output)."""


class ResearchState(BaseModel):
    run_id: str = ""
    domain: str = "apps"
    brief: str = ""
    regions: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    max_opportunities: int = 5
    raw_output: str = ""
    synthesis: SynthesisOutput | GameSynthesisOutput | None = None
    result: ResearchResult | None = None
    token_usage: dict[str, Any] = Field(default_factory=dict)


def extract_json(raw: str) -> dict:
    """Last-resort parser, used only when CrewAI returned no structured (Pydantic) output."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip())
    start_i, end_i = text.find("{"), text.rfind("}")
    if start_i < 0 or end_i <= start_i:
        raise ResearchError("Crew output contained no JSON object.")
    try:
        return json.loads(text[start_i:end_i + 1])
    except json.JSONDecodeError as exc:
        raise ResearchError(f"Crew output was not valid JSON: {exc}") from exc


CrewFactory = Callable[[EvidenceLedger], Any]


class ResearchFlow(Flow[ResearchState]):
    def __init__(self, *, worker_id: str, ledger: EvidenceLedger | None = None,
                 crew_factory: CrewFactory | None = None, **kwargs):
        super().__init__(**kwargs)
        self.worker_id = worker_id
        self.ledger = ledger or EvidenceLedger()
        self.crew_factory = crew_factory  # None: build_research_crew is looked up at call time (patchable)

    @start()
    async def intake(self) -> None:
        """Accept the brief, regions and categories; refuse to start without real research capability."""
        missing = settings.missing_research_credentials()
        if missing:
            raise ResearchError(f"Missing credentials: {', '.join(missing)}")
        if not self.state.regions:
            self.state.regions = ["Global"]
        self.state.max_opportunities = min(self.state.max_opportunities, settings.max_opportunities)

    @listen(intake)
    async def run_agents(self) -> None:
        domain = self.state.domain
        if self.crew_factory is not None:
            crew = self.crew_factory(self.ledger)
        elif domain == "apps":
            crew = build_research_crew(self.ledger)  # looked up at call time (patchable)
        else:
            crew = build_research_crew(self.ledger, domain=domain)
        output_model = GameSynthesisOutput if domain == "games" else SynthesisOutput
        # CrewAI's tools and Anthropic client are synchronous. Calling crew.akickoff() runs them ON the
        # worker's event loop, which starves the lease heartbeat, arq's health sentinel and the recovery
        # sweep for the length of every LLM call (observed in the real stack). Use a thread instead.
        output = await asyncio.to_thread(crew.kickoff, inputs={
            "brief": self.state.brief,
            "regions": ", ".join(self.state.regions),
            "categories": ", ".join(self.state.categories) or "Open category discovery",
            "max_opportunities": self.state.max_opportunities,
        })
        self.state.raw_output = str(getattr(output, "raw", output))
        usage = getattr(output, "token_usage", None)
        if usage is not None:
            self.state.token_usage = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)

        structured = getattr(output, "pydantic", None)
        if isinstance(structured, output_model):
            self.state.synthesis = structured
            return
        log.warning("Run %s: no structured output from CrewAI; falling back to JSON parsing", self.state.run_id)
        try:
            self.state.synthesis = output_model.model_validate(extract_json(self.state.raw_output))
        except ValueError as exc:  # pydantic.ValidationError subclasses ValueError
            raise ResearchError(f"Crew output failed schema validation: {exc}") from exc

    @listen(run_agents)
    async def validate_and_score(self) -> None:
        if self.ledger.successful_searches() == 0:
            raise ResearchError(
                "No research tool returned any results; refusing to persist output that cannot be sourced."
            )
        if not self.state.synthesis or not self.state.synthesis.opportunities:
            raise ResearchError("The crew produced no opportunities.")
        self.state.result = finalize(
            self.state.synthesis, self.ledger,
            max_opportunities=self.state.max_opportunities,
            default_regions=self.state.regions,
            threshold=settings.threshold_for(self.state.domain),
            domain=self.state.domain,
        )

    @listen(validate_and_score)
    async def persist(self) -> None:
        """Store results and mark them pending human approval, atomically, only if we still hold the lease."""
        ledger = self.ledger.to_dict()
        ledger["token_usage"] = self.state.token_usage
        stored = await runs.complete_run(
            self.state.run_id, self.worker_id, self.state.result, self.state.raw_output, ledger,
        )
        if not stored:
            raise ResearchError("Lease lost before results could be saved; results discarded.")


async def execute_research_flow(
    run: ResearchRun, worker_id: str, *, crew_factory: CrewFactory | None = None,
    ledger: EvidenceLedger | None = None,
) -> ResearchFlow:
    flow = ResearchFlow(worker_id=worker_id, ledger=ledger, crew_factory=crew_factory)
    try:
        await flow.kickoff_async(inputs={
            "run_id": run.id, "domain": run.domain or "apps", "brief": run.brief, "regions": list(run.regions or []),
            "categories": list(run.categories or []), "max_opportunities": run.max_opportunities,
        })
    except Exception as exc:
        # Keep what the agents produced and which sources they saw: failed runs must be debuggable.
        await runs.fail_run(
            run.id, worker_id, f"{type(exc).__name__}: {exc}",
            raw_output=flow.state.raw_output or None, ledger=flow.ledger.to_dict(),
        )
        raise
    return flow

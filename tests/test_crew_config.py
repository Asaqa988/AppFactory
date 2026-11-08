"""CrewAI wiring checks. These construct real Agent/Task/Crew/LLM objects (no network, no LLM calls)."""
import re

import pytest
from crewai import Process
from crewai.flow.flow import Flow

from app.core.config import settings
from app.research.crew import INPUT_KEYS, build_llm, build_research_crew
from app.research.flow import ResearchFlow, ResearchState
from app.research.ledger import EvidenceLedger
from app.schemas.research import SynthesisOutput


@pytest.fixture
def crew():
    return build_research_crew(EvidenceLedger())


def test_llm_is_native_anthropic_claude():
    llm = build_llm()
    assert type(llm).__module__.startswith("crewai.llms.providers.anthropic")
    assert llm.model == settings.claude_model.split("/", 1)[1]
    assert settings.claude_model.startswith("anthropic/")


def test_llm_sends_no_temperature():
    """Live check showed the configured model answers HTTP 400 when `temperature` is sent."""
    assert build_llm().temperature is None


def test_llm_output_budget_fits_the_structured_report():
    """The first live run died because an 8192-token cap truncated the synthesis JSON mid-string."""
    llm = build_llm()
    assert llm.max_tokens == settings.llm_max_tokens >= 12000


def test_synthesis_prompt_asks_for_compact_output(crew):
    assert "at most 12 evidence items" in crew.tasks[-1].description


def test_llm_requires_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm()


def test_four_specialists_plus_a_toolless_synthesizer(crew):
    roles = [a.role for a in crew.agents]
    assert roles == [
        "Global Mobile App Trend Researcher", "Mobile App Competitor Analyst",
        "User Pain Analyst", "Ad Monetization Analyst", "Research Synthesizer",
    ]
    assert crew.agents[-1].tools == []
    assert all(not a.allow_delegation for a in crew.agents)


def test_tools_are_wired_to_the_right_agents(crew):
    names = {a.role: {t.name for t in a.tools} for a in crew.agents}
    assert "web_search" in names["Global Mobile App Trend Researcher"]
    assert {"web_search", "apple_app_store_search"} <= names["Mobile App Competitor Analyst"]
    assert {"web_search", "apple_app_reviews"} <= names["User Pain Analyst"]
    assert "opportunity_scoring_rubric" in names["Ad Monetization Analyst"]


def test_all_tools_share_the_runs_ledger():
    ledger = EvidenceLedger()
    crew = build_research_crew(ledger)
    ledger_tools = [t for a in crew.agents for t in a.tools if hasattr(t, "ledger")]
    assert ledger_tools and all(t.ledger is ledger for t in ledger_tools)


def test_limits_come_from_settings(crew):
    for agent in crew.agents:
        assert agent.max_iter == settings.research_max_iterations
        assert agent.max_execution_time == settings.research_max_execution_seconds


def test_process_memory_and_task_chain(crew):
    assert crew.process == Process.sequential
    assert not crew.memory  # memory would require an embeddings provider
    trend, competitor, pain, monetization, synthesis = crew.tasks
    assert competitor.context == [trend]
    assert pain.context == [trend, competitor]
    assert monetization.context == [trend, competitor, pain]
    assert synthesis.context == [trend, competitor, pain, monetization]


def test_synthesis_uses_native_structured_output(crew):
    assert crew.tasks[-1].output_pydantic is SynthesisOutput


def test_only_known_placeholders_in_prompts(crew):
    """CrewAI interpolates {name} in task text; any unknown placeholder would raise or corrupt prompts."""
    for task in crew.tasks:
        assert set(re.findall(r"\{(\w+)\}", task.description)) <= set(INPUT_KEYS)
    assert "{max_opportunities}" in crew.tasks[-1].description


def test_prompts_forbid_fabrication(crew):
    text = crew.tasks[0].description.lower()
    for phrase in ("never invent", "untrusted", "observed", "estimate", "assumption"):
        assert phrase in text


def test_flow_is_a_crewai_flow_with_the_required_stages():
    assert issubclass(ResearchFlow, Flow)
    flow = ResearchFlow(worker_id="w")
    assert isinstance(flow.state, ResearchState)
    for step in ("intake", "run_agents", "validate_and_score", "persist"):
        assert hasattr(ResearchFlow, step)

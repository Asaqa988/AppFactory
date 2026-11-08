"""The games research Crew: four specialist agents plus a tool-less synthesizer.

Same shape as the apps crew (crew.py) and the same evidence rules; different questions. The product being
researched is a small 2D game (Godot, one developer, ad-supported) whose success depends less on an unmet
need than on a distinctive hook, genre traction and whether anyone will ever see it. Prompts contain only
the {brief}, {regions}, {categories} and {max_opportunities} placeholders.
"""
from crewai import LLM, Agent, Crew, Process, Task

from app.core.config import settings
from app.research.crew import CONTEXT
from app.research.ledger import EvidenceLedger
from app.research.tools import (
    AppleAppStoreLookupTool, AppleGameChartsTool, AppleReviewsTool, ResearchScoringTool, WebResearchTool,
)
from app.schemas.research import GameSynthesisOutput

GAME_RULES = (
    "Game-specific rules: the product is a small 2D mobile game one developer can build in Godot with no "
    "backend and no multiplayer. Judge ideas as game concepts: a distinctive core mechanic, genre traction, "
    "whether a 10-15 second clip would make a stranger want to play, a reason to return tomorrow, natural "
    "rewarded-ad moments, and a way to be discovered without paid ads. Store searches sample the Apple App Store "
    "only: say 'not found in the sample', never 'nobody has made this'. Charts show what ranks on one day in one "
    "country, not downloads or revenue."
)


def build_games_crew(ledger: EvidenceLedger, llm: LLM) -> Crew:
    web = WebResearchTool(
        ledger=ledger,
        description=(
            "Search the public web (Tavily) for current mobile-game evidence: genre and mechanic trends, hit games "
            "and why they spread, short-video trends, Reddit/Steam/itch.io threads, store listings, ad policy and "
            "player complaints. Returns titles, URLs, publication dates when available and content excerpts. Cite "
            "only URLs this tool returned. Excerpts are not verified downloads, revenue, eCPM or retention data."
        ),
    )
    games_search = AppleAppStoreLookupTool(
        ledger=ledger, games_only=True, name="apple_games_search",
        description=(
            "Look up publicly listed iOS GAMES with Apple's Search API (non-game apps are filtered out). Returns name, "
            "developer, listing URL, average rating, rating count, price and genres exactly as Apple reports them. "
            "Use it to identify competitors in a genre. It is a sample of the store, not a census, and says nothing "
            "about downloads."
        ),
    )
    charts = AppleGameChartsTool(ledger=ledger)
    reviews = AppleReviewsTool(ledger=ledger)
    rubric = ResearchScoringTool(domain="games")

    common = dict(
        llm=llm, verbose=False, allow_delegation=False,
        max_iter=settings.research_max_iterations,
        max_execution_time=settings.research_max_execution_seconds,
    )

    trend_scout = Agent(
        role="Mobile Game Trend Scout",
        goal="Find game genres and mechanics with real traction right now, each backed by charts, articles or community threads.",
        backstory="You read store charts and player communities like a market analyst and separate what ranks from what you hope will.",
        tools=[web, charts, games_search], **common,
    )
    competitor_analyst = Agent(
        role="Game Competitor Analyst",
        goal="Map the real games in each candidate genre: how they play, how they monetize, how crowded the space is, and what none of them in your sample does.",
        backstory="You compare actual games, quote the listing URLs you saw, and never infer downloads or revenue.",
        tools=[web, games_search, charts], **common,
    )
    player_pain_analyst = Agent(
        role="Player Pain Analyst",
        goal="Find what players of these games complain about or ask for: ads, paywalls, boredom, difficulty, missing languages or features.",
        backstory="You look for repeated, specific complaints with a source for each, and flag anecdotes as anecdotes.",
        tools=[web, games_search, reviews], **common,
    )
    design_analyst = Agent(
        role="Game Design and Ad Analyst",
        goal="Judge each concept's core loop, retention, shareability, ad placement fit, discoverability and one-developer scope.",
        backstory="You are conservative. You reason qualitatively about rewarded-ad fit, ad-policy risk and virality and never fabricate eCPM, revenue, retention or install numbers.",
        tools=[web, rubric], **common,
    )
    synthesizer = Agent(
        role="Research Synthesizer",
        goal="Combine the specialists' findings into a strictly structured, evidence-tagged list of game concepts.",
        backstory="You do not search. You only use evidence the specialists produced, and you drop any claim without a source.",
        tools=[], **common,
    )

    trend_task = Task(
        description=(
            CONTEXT + "\n" + GAME_RULES + "\nFind 5-8 promising game genres or mechanics. Check the Apple game charts "
            "(top free and top grossing, overall and by genre, for the target countries) and search the web for "
            "recent hits, short-video trends and community threads. For every finding give the source URL and the "
            "date when visible. Note which genres are dominated by a few big publishers."
        ),
        expected_output="Trend report: 5-8 candidate genres or mechanics, each with evidence URLs, dates, confidence and caveats.",
        agent=trend_scout,
    )
    competitor_task = Task(
        description=(
            CONTEXT + "\n" + GAME_RULES + "\nFor the strongest candidates, identify real iOS games in each genre with "
            "apple_games_search and the charts. Compare core mechanic, monetization style (ads, in-app purchases, "
            "paywalls), polish and visible weaknesses. Report ratings only as returned by the Apple tool. For each "
            "candidate state the closest existing games and what is different about the idea."
        ),
        expected_output="Competitor matrix: game names, listing URLs, mechanics, monetization style, observed gaps.",
        agent=competitor_analyst, context=[trend_task],
    )
    pain_task = Task(
        description=(
            CONTEXT + "\n" + GAME_RULES + "\nFind repeated player complaints and requests for the candidate genres "
            "using public App Store reviews (apple_app_reviews), forums and communities. Include the source URL for "
            "each point and state whether it is a broad pattern or a few anecdotes. Pay attention to complaints "
            "about ad frequency and about missing Arabic or other languages."
        ),
        expected_output="Ranked player pain points with audience, evidence URLs and a pattern-vs-anecdote judgement.",
        agent=player_pain_analyst, context=[trend_task, competitor_task],
    )
    design_task = Task(
        description=(
            CONTEXT + "\n" + GAME_RULES + "\nAssess each candidate concept: the core loop (what the player does every "
            "few seconds), why a player would return tomorrow, whether a 10-15 second clip of it would be "
            "watchable and shareable, where rewarded ads fit naturally and where they would annoy, ad-policy "
            "risks, how the game could be discovered without paid acquisition, and what a one-person Godot 2D "
            "prototype would contain. Do not invent eCPM, revenue, retention or install numbers; reason "
            "qualitatively and mark assumptions."
        ),
        expected_output="Per-concept design and monetization assessment with risks, assumptions and sources.",
        agent=design_analyst, context=[trend_task, competitor_task, pain_task],
    )
    synthesis_task = Task(
        description=(
            CONTEXT + "\n" + GAME_RULES + "\nProduce the final structured report with at most {max_opportunities} "
            "game concepts, best first.\n"
            "For each concept:\n"
            "- problem: what players are missing or put up with; proposed_solution: the game concept in 1-2 sentences.\n"
            "- genre, core_loop (what the player does every few seconds), twist (what differs from the named "
            "competitors; never claim nothing similar exists), ad_moments, prototype_scope, hook_clip.\n"
            "- evidence: items copied from the specialists' work. observed/estimate items MUST carry the exact "
            "source URL; assumptions carry none. Set `dimension` to the scoring dimension the item supports "
            "(gameplay_gap, genre_demand, shareability, retention_loop, ad_fit, distribution_path, "
            "build_feasibility). Aim for 2-3 independent sources per dimension where they exist.\n"
            "- competitors: name, listing URL and the observed gap. Do NOT write ratings or download numbers.\n"
            "- dimension_scores: 0-100 per dimension. 80+ only when multiple sources support it; use 30-50 when "
            "evidence is thin. The application caps scores that lack verified sources.\n"
            "- Put unsupported beliefs in `assumptions`, not in evidence. Prefer fewer, well-sourced concepts "
            "over many weak ones.\n"
            "- Be compact: the whole answer must fit comfortably in the output limit (the first real run was cut "
            "off mid-string with 6 verbose concepts): use at most 8 evidence items per concept (best sources only), "
            "each claim one sentence under 160 characters; research_summary under 120 words; pitch, problem, "
            "solution, twist and core_loop one or two short sentences each; at most 3 ad_moments, 4 competitors "
            "and 4 risks per concept."
        ),
        expected_output="A GameSynthesisOutput object: research_summary and a ranked list of evidence-tagged game concepts.",
        agent=synthesizer, context=[trend_task, competitor_task, pain_task, design_task],
        output_pydantic=GameSynthesisOutput,
    )

    return Crew(
        agents=[trend_scout, competitor_analyst, player_pain_analyst, design_analyst, synthesizer],
        tasks=[trend_task, competitor_task, pain_task, design_task, synthesis_task],
        process=Process.sequential,
        memory=False,  # memory would call an embeddings provider (OpenAI by default) - not wanted here
        verbose=False,
    )

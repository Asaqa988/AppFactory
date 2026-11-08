# Implementation Log

Chronological record of significant changes and the evidence for each. Dates 2026-11-03. All runs used Docker
(`python:3.12-slim`, `postgres:16-alpine`, `redis:7-alpine`); versions pinned in `constraints.txt`.

## Steps

| # | Issue | Change | Verification | Result |
|---|---|---|---|---|
| 1 | Repo nested one level deep in the zip; nothing had ever run | Extracted to its own directory, flattened, inspected all 24 files | `docs/IMPLEMENTATION_AUDIT.md` | 19 findings |
| 2 | `crewai` without Anthropic extra cannot build an LLM (F1); no worker lib | `crewai[anthropic]`, `arq`; `constraints.txt` frozen from the tested image; `requirements-dev.txt` | Built image on 3.12; probed `LLM` → `AnthropicCompletion`, `anthropic 0.73.0`, no `litellm` | OK |
| 3 | Probe CrewAI 1.15.34 API before relying on it | Toy Flow with async steps, typed state, inputs, error propagation | Ran; also confirmed `output_pydantic`, `akickoff`, `BaseTool` arbitrary types | OK |
| 4 | Scores from LLM used as data (F7) | `scoring.py` (caps by verified evidence, confidence, threshold); `ledger.py`; `validation.py`; draft/stored schemas | 31 scoring + 24 schema + 13 validation tests | OK |
| 5 | No durable jobs / duplicate runs (F4, F5) | `services/runs.py` (CAS claim, lease, heartbeat, fencing, sweep), `worker/` (arq), enqueue-after-commit | 12 service + 10 worker tests; real-stack tests below | OK |
| 6 | Tools fragile / unverifiable (F9, F10) | Rewrote tools: Bearer auth, retries, error strings, call budget, ledger, Apple reviews tool | 15 tool tests with mocked HTTP | OK |
| 7 | Free-text parsing, one-step flow (F6) | `ResearchFlow` (intake → run_agents → validate_and_score → persist), tool-less Synthesizer, `output_pydantic` | 12 flow + 11 crew-config tests | OK |
| 8 | API gaps (F2, F3, F11, F12) | Auth rewrite, reviewer identity, row-locked decisions, response models, pagination, 429/503 guards, DB+Redis health | 32 API tests; SQLite and PostgreSQL | OK |
| 9 | Insecure Docker/compose (F14) | Secret-required compose, 127.0.0.1 API, Redis password+AOF, internal network, hardened containers, worker service, multi-stage Dockerfile | Booted stack; inspected bindings, egress, auth | OK |
| 10 | **Bug (mine):** config validator unpacked key/name backwards and would leak the secret | Fixed | Caught by `test_config.py` | Fixed |
| 11 | **Bug (CrewAI behaviour):** `akickoff` blocks the event loop → worker `unhealthy`, heartbeats stall (N1) | `asyncio.to_thread(crew.kickoff)` + regression test | Real stack: worker `healthy`, heartbeat age ≤ 6 s over a 90 s+ run | Fixed |
| 12 | Hidden coupling: CrewAI locks via `REDIS_URL` (F17) | Documented; tests unset the variable | Test failure reproduced and understood | Documented |

## Test evidence

| Suite | Command | Result |
|---|---|---|
| Offline, SQLite | `docker build --target test -t appfactory-test . && docker run --rm appfactory-test` | **170 passed, 2 skipped** (the 2 need PostgreSQL) |
| PostgreSQL 16 (compose service, scratch DB) | `docker run --rm --network appfactory-ai_internal -e TEST_DATABASE_URL=postgresql+asyncpg://… appfactory-test pytest -q` | **172 passed** — includes concurrent-decision race and append-only trigger |

Coverage by requirement: syntax/import (`test_imports.py`: every module imports; OpenAPI lists all 8 required
endpoints), scoring, schemas, API, DB persistence, CrewAI configuration (real `Agent/Task/Crew/LLM` objects),
worker/queue behaviour. Docker build, container health checks and live behaviour are in the next table.

Offline tests use fake credentials and a scripted crew double — **they cannot reach Anthropic or Tavily** and say
nothing about live research quality.

## Live stack evidence (docker compose, dummy provider keys)

| Check | Observation |
|---|---|
| Services start | `postgres`, `redis`, `api`, `worker` all `healthy` (API via Dockerfile HEALTHCHECK on `/health`; worker via `arq --check`) |
| `/health` | `{"status":"ok","database":"up","redis":"up"}` |
| Exposure | `docker inspect`: only `api` has a host binding (`127.0.0.1:8000`); `postgres`/`redis` ports are `null`. (A naive port probe of 5432/6379 shows "open" on this PC — those are an unrelated native PostgreSQL and `chatwoot-redis-1`.) Postgres container cannot reach the internet; worker can reach Apple |
| Redis auth | unauthenticated `redis-cli ping` → `WRONGPASS` |
| Auth | no key / wrong key → 401; valid → 200; unauthenticated POST → 401 |
| End-to-end path | POST → PG row → Redis job `<run>:0` → worker claim → CrewAI native Anthropic client → **real call to api.anthropic.com → `401 invalid x-api-key`** (expected for a dummy key; response carried a genuine `request_id`) → run `failed` with that error recorded |
| API restart + Redis restart with a queued job | queue length stayed 1 across `restart redis` (AOF); after the worker started the run was claimed and executed |
| Redis total loss (`FLUSHALL`) | run re-delivered by the sweep after the 90 s redelivery window and executed |
| Crashed worker (run `running` with an expired lease, attempt 1) | swept, re-queued, re-claimed (`attempts=2`) and executed within ~10 s |
| Process exit mid-run (SIGTERM to PID 1) | container restarted by Docker (`restarts=1`); in-flight run re-claimed by the new worker (`attempts 1→2`) |
| API process exit | restarted by Docker; `/health` ok again |
| Approval workflow over HTTP (seeded opportunities) | approve as `bob` → recorded reviewer `bob`; second decision → 409; below-threshold → 409; `reviewer` in body → 422; `/approvals` returns decision, previous status, score, timestamp; `UPDATE`/`DELETE` on `approvals` → rejected by trigger |

## First live research runs (real Anthropic + Tavily + Apple)

| Run | Outcome | Notes |
|---|---|---|
| 1 (`--max 3`, productivity, US) | **failed** at the last step | 40 tool calls, 178 sources recorded, all successful. Claude's structured JSON was truncated mid-string at the 8192-token output cap. Fixed: `LLM_MAX_TOKENS=16000` + compact-output prompt |
| 2 (same brief) | **completed**, 5 min 36 s | 28 tool calls, 138 sources, 50 LLM requests, ~1.28 M tokens (1.05 M prompt incl. 225 k cached, 223 k completion). 3 opportunities, scores 46 / 43.25 / 41.25, **none recommended** (all `below_threshold`) |

Observations from run 2:

* **Anti-fabrication held.** 28 of 30 evidence items were `observed`/`estimate` and URL-verified; 0 `unverified`; 2 `assumption`. Competitor ratings came only from Apple API records (e.g. Finch 4.95 / 770,935 ratings).
* **Low scores were mostly the agents' own judgement**, not the caps: average agent scores were competition gap 30, monetization 35, demand 45. Caps only reduced build feasibility and retention (~10 points). "Productivity" is a crowded category, so "nothing to recommend" is a plausible honest answer.
* **Evidence per dimension is thin** (~1.3 verified sources on average vs 3 for full credit) even though the prompt asks for 2–3.
* **Claim spot-check (5 random observed claims vs stored text): 3 confirmed; 2 not confirmable** because the ledger kept only 400 of the up to 1200 characters shown to the model. Ledger now stores the full 1200.
* **Cost driver:** prompt tokens (context is re-sent each agent iteration), not output.

### Three niche runs (Arabic MENA / emerging-market low-end Android / seniors & caregivers), `--max 3` each

All three completed (6.6–7.5 min, 40–49 tool calls, 214–242 sources, 1.9–2.3 M tokens each; ~7.65 M tokens for all four completed runs in total).
12 opportunities, **top score 55.5, none ≥ 70, none recommended**. 136 evidence items, **0 unverified**.

| Score | Opportunity |
|---|---|
| 55.5 | Free no-activation offline JAMB/WAEC practice app (Nigeria) |
| 52.25 | Narrow family caregiver-coordination slice (US/UK/CA) |
| 51.25 | Free simple medication reminder, no paywall |
| 50.5 | Arabic-first reliable expense tracker (Egypt-first) |
| 49.25 | Ad-light offline document scanner |

Finding: **the 70 gate looks unreachable with the current cap table, not just for crowded niches.** No public source gives AdMob eCPM, so
`monetization_fit` stays ~35–60; `build_feasibility` and `retention_potential` are inferential and rarely have web sources, so they are capped to 40–50
even when the agent scores them 65–80; evidence per dimension averages ~1–2 sources vs 3 for full credit. Calibration (not code) decision for the owner:
keep as an honest "nothing recommended" gate, let sourced estimates count fully for the inferential dimensions, or add a below-70 watchlist.

### Targeted deep-dive: free medication reminder, Arabic markets (Jordan, Saudi, Egypt, UAE, Kuwait)

Run `130bbb4c`: completed in 8.8 min, 49 tool calls, 239 sources, ~3.56 M tokens. Two variants, **both below threshold: 43.5** (Arabic-first elderly/caregiver, Android first)
and **38** (bilingual chronic-condition); 24/24 evidence items verified. The generic English-market version of the same idea had scored 51.25, so
**deeper, more specific research lowered the score** — it found existing Arabic competitors that match the spec (Katbi, Dawai; MyTherapy supports Arabic and large text),
and that Google classifies chronic-condition content as sensitive for personalized ads. Need is documented (Saudi adherence surveys, paywall complaints in Saudi/Egypt
Medisafe reviews). Unconfirmed by any source: search volume, Arabic/RTL quality, AdMob eCPM and country limits, regulation in Jordan/Egypt/Kuwait,
Android availability of Katbi/Dawai, whether caregivers are the installers. No approval was recorded (below threshold).

### Games domain (2026-11-04)

Added `domain: "games"` to research runs (see README "Games domain" and ARCHITECTURE "Domains").

| Change | Verification |
|---|---|
| Scoring: `GAME_WEIGHTS` (`games-1.0`), `score_opportunity(..., weights=)`; apps default unchanged | 4 scoring tests; the 31 existing scoring tests pass unchanged |
| Schemas: `domain` on the request, `GameDimensionScores`, `GameOpportunityDraft`, `GameSynthesisOutput`, `details` on stored opportunities | schema tests: required game fields, bounds, apps shape rejects game scores |
| Games crew (`crew_games.py`): 4 specialists + tool-less synthesizer, game prompts, "not found in the sample" rule | 5 crew-config tests (roles, tool wiring, placeholders, structured output) |
| Tools: `apple_games_search` (games-only filter on Apple search), `apple_game_charts` (public chart feed, citeable) | 8 mocked-HTTP tests, **plus a run against the real endpoints**: US puzzle chart, SA top grossing, JO top paid word games, EG top free word games and a games search all returned data and were recorded in the ledger |
| Flow/validation/API: domain flows through; separate gate `RECOMMEND_THRESHOLD_GAMES`; `GET /opportunities?domain=`; `details` in the opportunity response | flow tests (rubric, separate gate, wrong-shape output rejected, default crew builder gets the domain) and API tests |
| Schema upgrade for existing databases: idempotent `ADD COLUMN IF NOT EXISTS` in `init_db()` | PostgreSQL test drops the new columns from a database holding rows, runs `init_db()` twice, rows read back as `apps` |

Test results at this step: **205 passed, 3 skipped** on SQLite (the 3 need PostgreSQL); **208 passed** on PostgreSQL 16 (final counts after the fixes below: 215 / 218).

#### First real games runs (2026-11-04) - what went wrong, and what is and is not trustworthy

| Run | Outcome |
|---|---|
| 1 (`--max 6`) | **failed** at the last step after 12 m 52 s: the structured output was cut off mid-string (33,569 characters). Also, 13 of 73 tool calls were refused because one shared budget of 60 had been used up by 25 chart and 22 store lookups, so the reviews and web research of the later agents were starved. The truncated text is **not** kept when CrewAI raises (known limit), so nothing could be salvaged. Fixes: compact games synthesis prompt (8 evidence items per concept), separate budgets for paid web search (60) and free Apple lookups (80). |
| 2 (`--max 4`) | **completed** in 9 m 43 s, 83 tool calls, 4.59 M tokens, 4 concepts, scores 45 / 44.5 / 42.5 / 35.75, all `below_threshold`, confidence low. **These scores are not valid measurements.** The evidence ledger had one shared cap of 400 sources; 28 chart calls filled it (389 chart rows) before the first web search, so **all 89 web results and all review pages were dropped** and every citation to them was marked `unverified`. Only Apple chart and store evidence could count. |

Root cause and fix: a single cap shared by bulk listing rows and evidence pages. Now two caps (600 evidence pages, 2000 listing rows),
a `dropped_sources` counter in the stored ledger, and a per-opportunity warning whenever anything was dropped. A second, smaller false negative was
also fixed: Apple listing URLs were compared with their cosmetic name slug (percent-encoded Arabic, or absent), so `/sa/app/id123` did not match
`/sa/app/<slug>/id123`; the app id is now the identity. Regression tests cover both. The model also wrote one chart URL wrongly (`.../sa/app/topfreeapplications/...`
instead of `.../sa/rss/...`); that is correctly still unverified, and the prompt now asks for URLs copied character for character.

What the run 2 content is still good for (it is what the agents read from the tools, but their evidence labels and scores are unreliable):
the agents' summary says Arabic word charts are open to individual developers; an association game ranks in the word top 10 of all four countries;
Word Crush (kalimat crush) holds the swipe-a-word mechanic at #1/#2/#3 in EG/JO/SA word charts; ball-merge and tilt-merge titles already exist;
the dallah pour game is distinctive but unproven (the only dallah-named result in the Saudi sample was an RPG with 0 ratings); reviews show ad fatigue.
These are one-day, one-country chart snapshots and a sample of the App Store only.

**Run 3 (`--max 4`, same brief, fixed ledger)** - completed in 9 m 0 s, 3.85 M tokens, 71 tool calls (21 chart, 25 store search, 14 web, 11 review pages), **0 budget refusals**,
564 sources recorded (76 from web search, 0 dropped), **0 unverified evidence items** across the four concepts. This is the first valid games run.

| Score | Confidence | Concept |
|---|---|---|
| 49.00 | low (0.23) | Arabic daily word-guess with dialect packs (Gulf / Egyptian / Levantine) |
| 47.85 | low (0.28) | Ring Weave: rotate-the-rings puzzle with an Arabic geometric skin |
| 47.50 | medium (0.34) | Dallah Pour: Arabic-coffee pour-timing game |
| 47.50 | low (0.27) | Arabic Word Blast: swipe letters, the word explodes, letters above fall |

None reached 70 (nothing recommended; nothing approvable). The caps still bind: every concept has at least two dimensions with 0 verified sources
(typically shareability and retention_loop, which are judgements rather than things a web page states), so those dimensions stop at 40, and
web evidence averaged about one source per dimension. The agents' own scores were modest too (gameplay_gap 28-68, genre_demand 40-72).
As with the apps rubric, the score is a ranking aid, not a calibrated probability; there is no outcome data to say what a "good" score is.

Findings the agents backed with verified sources (Apple charts for 2026-11-04, one day per storefront; store search samples; 76 web results):
Arabic word games chart strongly in SA/EG/JO; one publisher (Al Zytoona) dominates the word-search / letter-wheel games; Arabic Wordle clones are many and low-polish;
a rotate-rings puzzle is trending in the US and Saudi Arabia while clones arrive; forced ads are the top review complaint, so every concept is rewarded-only;
tilt-merge was dropped (ball-merge occupied by large publishers, no tilt-merge demand evidence, hard to show in a clip); a dallah pour-timing game was not found in the sample
(demand is indirect: one Snapchat Spotlight video about pouring from a dallah was verified).

**Run-to-run inconsistency (honest limit):** run 2 said a leading Arabic word game "holds the swipe mechanic" at #1/#2/#3, which made the swipe-and-collapse concept look crowded;
run 3 says the same publisher dominates word-search / wheel games and that an Arabic swipe-and-collapse board was **not found in the Apple sample**. These are different mechanics
and the model's reading of them varied. One run is one sample of the model's judgement; the verified charts are facts, the "this mechanic exists / does not exist" conclusions are not.

The weights remain a heuristic; no outcome data exists to calibrate `RECOMMEND_THRESHOLD_GAMES`, so it stays at 70.

### Not tested / known limits of the evidence

* **A successful research run** (needs real `ANTHROPIC_API_KEY` + `TAVILY_API_KEY`): not executed. Prompt quality, cost, duration, and
  how often the model cites only real URLs are unmeasured.
* `docker kill appfactory-worker` (CLI) did **not** trigger Docker's restart policy — Docker treats it as a manual stop. A first attempt
  to crash the container with `kill -9 1` from inside was invalid (the kernel ignores it for PID 1) and proved nothing; SIGTERM was used instead.
* "Worker killed mid-run" was exercised via graceful SIGTERM and via a simulated expired lease, not an abrupt `SIGKILL` of a live container
  that Docker then restarted on its own.
* Apple review feed and the Google Play side were not exercised live.
* Load, long-duration soak, and multi-worker contention beyond the 8-way claim test were not run.

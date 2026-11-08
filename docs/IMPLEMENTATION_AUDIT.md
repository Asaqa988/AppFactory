# Implementation Audit — original `appfactory-ai` starter

Audit of the repository as received (`appfactory-ai-research-mvp.zip`, 24 files) against the Research MVP brief.
Environment used: Docker 29.4.1, `python:3.12-slim`, `crewai 1.15.34`, `anthropic 0.73.0` (the host has Python 3.14/3.13/3.11
only; CrewAI does not install on 3.14, so all checks ran in Python 3.12 containers).

**Evidence labels.** **[RAN]** = I executed something and observed the result. **[READ]** = concluded from reading code or
vendor documentation, not executed. **[UNVERIFIED]** = needs credentials or accounts I do not have.

**Verdict on the original:** a readable skeleton, but it could not have run as shipped (F1), had no durable job
execution (F4), treated LLM-written scores as data (F7), and shipped an open-by-default credential (F2). It had never
been executed: its 2 tests had no test runner configured (F18).

## Findings

| ID | Sev | Area | Finding | Evidence | Resolution |
|---|---|---|---|---|---|
| F1 | **Critical** | CrewAI / Claude | `requirements.txt` listed `crewai` without the Anthropic provider. Constructing `LLM(model="anthropic/…")` raises `ImportError: Anthropic native provider not available, to install: uv add "crewai[anthropic]"`. The pipeline could not start. | **[RAN]** removed `anthropic` from the image and built the LLM | Requirement is now `crewai[anthropic]`; `tests/test_crew_config.py` builds a real `LLM` and asserts the native provider |
| F2 | **High** | Security | Default `API_KEY="change-this-api-key"` in code, `.env.example` **and** the README curl example. Anyone who copies the example runs an open API. `APP_SECRET_KEY` (default `unsafe-development-secret`) was never used. | **[READ]** | Startup rejects missing/short (<24)/placeholder keys; `scripts/generate_env.py` writes random secrets; dead secret removed |
| F3 | **High** | Security | `secrets.compare_digest(str, str)` raises `TypeError` for a non-ASCII `X-API-Key` → unhandled 500 instead of 401. | **[RAN]** `TypeError: comparing strings with non-ASCII characters is not supported` | SHA-256 digests + `hmac.compare_digest`; regression test sends a latin-1 header |
| F4 | **High** | Reliability | Research ran in FastAPI `BackgroundTasks`: lost on API restart, left in `running` forever, no retry. Redis was started in compose and `redis` was in requirements, but **no code used it** (README line 4 and the brief imply otherwise). | **[READ]** | Real `arq` worker. PostgreSQL = job ledger, Redis = delivery. See ARCHITECTURE.md; proven in the log |
| F5 | **High** | Reliability | No duplicate-execution guard; two deliveries/processes would both run (and bill) the same crew. | **[READ]** | Atomic claim (CAS), lease + heartbeat, fencing on every write; 8-way concurrent-claim test |
| F6 | **High** | Correctness | Agent output was parsed with regex over free text (`_extract_json`). Pydantic schemas (`OpportunityCandidate`, `OpportunityBatch`) were defined but **never used**. The brief asks for structured output where supported. | **[READ]**; `Task.output_pydantic` exists in 1.15.34 **[RAN]** | Synthesis task uses `output_pydantic=SynthesisOutput`; JSON parsing only as logged fallback |
| F7 | **High** | Correctness | LLM-proposed dimension scores were used as-is. Nothing reduced confidence for missing evidence, there was no confidence at all, and the 70-point threshold was never applied anywhere (no "recommended" concept). | **[READ]** | `scoring.py`: scores capped by verified evidence, confidence, `recommended = score ≥ 70`; 31 scoring tests |
| F8 | **High** | Anti-fabrication | Nothing stopped the model citing invented URLs or inventing competitor ratings; free-form `competitors: list[dict]` and `evidence` accepted anything. | **[READ]** | Evidence ledger: cited URLs must have been returned by a tool; ratings only from recorded Apple API data; draft schemas carry no metric fields |
| F9 | Med | Tools | Tavily key sent in the JSON body (docs now show `Authorization: Bearer`); no `include_published_date` so dates were never available; any HTTP error raised through the agent loop; results returned as Python `repr`; no retry; no call budget; no prompt-injection labelling. | **[READ]** vs Tavily docs fetched during the audit | Rewritten: Bearer auth, published dates, retries with backoff, errors returned as strings, per-run call cap, untrusted-content labels |
| F10 | Med | Agents | Agent 3 (User Pain) was required to use reviews but had no review tool. `research_max_iterations` was a dead setting; agents had default `max_iter=25` and no time cap; the Monetization analyst also did synthesis. | **[READ]** | `apple_app_reviews` tool (unofficial public feed, best effort); iteration/time caps wired to settings; tool-less Synthesizer |
| F11 | Med | API / audit | Reviewer could not be recorded (hard-coded default `"factory-owner"`), so the "reviewer" requirement was unmet. No row lock → two concurrent decisions both succeeded. Approvals `ON DELETE CASCADE` and editable. Opportunities below 70 could be approved. | **[READ]** | Reviewer = authenticated key name; `SELECT … FOR UPDATE`; `RESTRICT` + append-only trigger; below-threshold ineligible. Race and trigger tests ran on PostgreSQL |
| F12 | Med | API | No response models, no pagination, free-text filters, `/health` did not touch the database, raw exception text (possibly containing secrets) stored on failed runs, no limit on concurrent runs (cost). | **[READ]** | Response models, `limit/offset`, validated filters, DB+Redis health, secret redaction, `MAX_ACTIVE_RUNS` → 429 |
| F13 | Med | Database | `create_all` run unlocked by every container at startup (race with >1 process); `JSON` not `JSONB`; `migrations/001_initial.sql` never executed and would drift from the models. | **[READ]** | Advisory-lock init; JSONB on PostgreSQL; stale SQL file removed. **No migration history yet — add Alembic before the first schema change that must keep data** |
| F14 | Med | Docker | PostgreSQL password hard-coded in `docker-compose.yml` and `.env.example`; API published on `0.0.0.0:8000`; Redis unauthenticated; no `no-new-privileges`/`cap_drop`; no worker; no network isolation; `build-essential`+`curl` installed in the runtime image although wheels suffice; no test stage. | **[READ]**; wheels sufficient **[RAN]** (image builds without them) | Secrets required via `${VAR:?}`; API on 127.0.0.1; Redis password + AOF; `internal` network without internet for data stores; hardened containers; worker service; multi-stage Dockerfile |
| F15 | Low | Docs | README says Claude is used "through CrewAI's LiteLLM integration". With `crewai[anthropic]` the **native** Anthropic provider is used and `litellm` is not installed. | **[RAN]** `litellm NOT INSTALLED`; `LLM` class is `…providers.anthropic.completion.AnthropicCompletion` | Docs corrected |
| F16 | Low | Config | Default `CLAUDE_MODEL=anthropic/claude-sonnet-4-5` — availability on the owner's account is unknown, and newer models exist. | **[UNVERIFIED]** (needs a key) | Default `anthropic/claude-sonnet-5-5`; `scripts/check_integrations.py` lists `/v1/models` and fails clearly if the configured id is not available. **Run it first** |
| F17 | Info | CrewAI | CrewAI takes cross-process locks through Redis whenever `REDIS_URL` is set in the environment (`crewai_core/lock_store.py`). Building a Crew with an unreachable `REDIS_URL` raises `ConnectionError`. | **[RAN]** — it broke my first test run | Documented; the worker shares the compose Redis. Tests unset `REDIS_URL` |
| F18 | Low | Tests | 2 tests; `pytest` not in any requirements; no config; never executed. | **[READ]** | 172 tests, run on SQLite and on PostgreSQL 16 |
| F19 | Info | Non-issue | I suspected the synthesis prompt's literal `{…}` JSON example would be mangled by CrewAI's `{placeholder}` templating. | **[RAN]** it interpolates correctly | No change needed for correctness; the prompt was replaced anyway because structured output removed the need for an inline schema |

### Found while verifying my own changes (not defects of the original)

| ID | Finding | How found | Fix |
|---|---|---|---|
| N1 | `Crew.akickoff()` runs CrewAI's **synchronous** tools and Anthropic client on the caller's event loop. In the worker this froze the loop for the length of each LLM call: the lease heartbeat, arq's health sentinel (Docker showed the worker `unhealthy`: "no health check sentinel value found") and the recovery sweep all stalled. | **[RAN]** real stack with a slow upstream; log timestamps showed ~21 s blocking gaps | Crew now runs via `asyncio.to_thread(crew.kickoff)`. Re-verified: worker `healthy`, heartbeat age ≤ 6 s through a 90 s+ run. Regression test added |
| N2 | My own config validator unpacked `principals` (key→name) backwards, so it validated the *name* and would have echoed the secret in its error message. | unit test | Fixed; the message now names the reviewer only |
| N3 | `docker kill` (CLI) leaves a `restart: unless-stopped` container stopped. A process-initiated exit (SIGTERM from inside) is restarted. | **[RAN]** | Documented in README; operationally, use `docker compose start worker` after a manual kill |

## Reviewed and found acceptable

* Non-root runtime user (uid 10001); PostgreSQL not published; async SQLAlchemy engine with `pool_pre_ping`.
* Apple Search API usage (`itunes.apple.com/search`, `entity=software`) is the documented public endpoint.
* Human-approval idea (a pending state plus a decision endpoint) was sound; it needed identity, locking and immutability.

## Residual risks (not fixed)

1. **No live LLM run has been executed.** Everything up to and including a real HTTPS call to `api.anthropic.com` was verified; a successful research run requires your `ANTHROPIC_API_KEY` and `TAVILY_API_KEY`. Output quality, token cost and run time are therefore unmeasured.
2. **Claim-level fact checking is not done.** The ledger proves a cited URL was *returned by a tool*, not that the page supports the claim. Scoring caps limit the damage; a human still reviews every evidence list.
3. **Evidence cap table is a judgement call**, not calibrated on data (`SCORING_VERSION = 1.0`).
4. **Apple review feed is unofficial** and may change; Google Play has no structured tool (Android competitors come from web search only, with no ratings attached).
5. **Prompt injection** from web pages/reviews is mitigated (labelling, tool-less synthesizer, no side-effect tools, schema validation) but not eliminable.
6. **Single shared-key auth** per reviewer, no roles, no TLS, no rate limiting beyond the active-run cap, no secret rotation.
7. **No Alembic**; schema changes after first real data need migrations.
8. A cancelled/timed-out run abandons its crew *thread* (it ends at the agent time caps or process exit); `arq` is in maintenance mode upstream.
9. If all workers are down, orphaned runs wait — recovery is performed by workers.

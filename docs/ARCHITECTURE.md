# Architecture

Scope: the **Research MVP only**. Nothing here builds, publishes or spends. Human approval is a hard gate.

## Components

```
                         X-API-Key                      ┌────────────────────┐
  owner / reviewer ───────────────────►  api  (FastAPI)  │  PostgreSQL 16     │
                                          │   ▲          │  source of truth   │
                         POST /research-runs  │ reads     │  runs, opps,       │
                                          │   │ writes    │  approvals (audit) │
                       1. INSERT run      ▼   │          └─────────▲──────────┘
                          (queued) ──────────►│                    │ claim / heartbeat /
                       2. enqueue id          │                    │ complete (CAS updates)
                          ▼                                         │
                  ┌──────────────┐  job id   ┌───────────────────────┴────────────┐
                  │ Redis (AOF,  │──────────►│ worker (arq)                       │
                  │ password)    │           │  ResearchFlow ─► research Crew     │──► Tavily
                  │ delivery only│◄──────────│  sweep cron: re-queue / re-deliver │──► Apple Search API
                  └──────────────┘           └────────────────────────────────────┘──► Anthropic Claude
```

| Service | Image | Role | Reachable from |
|---|---|---|---|
| `api` | app (runtime stage) | HTTP API, auth, enqueue, approvals | host `127.0.0.1:8000` only |
| `worker` | app (runtime stage) | executes research runs, recovers dead runs | nothing inbound |
| `postgres` | `postgres:16-alpine` | all durable state | `internal` network only |
| `redis` | `redis:7-alpine` | delivery of run ids (AOF + password) | `internal` network only |

Networks: `internal` has `internal: true` (no route to the internet); `egress` is a normal bridge used only by `api`
and `worker`, which must call Anthropic, Tavily and Apple.

## What Redis is, and is not, responsible for

The original repository started a Redis container but never used it. It is now used by a real worker (`arq`), with
an explicit division of labour:

* **PostgreSQL decides truth.** A run's state (`queued → running → completed | failed`), its owner (`locked_by`) and
  its lease (`lease_expires_at`) live in `research_runs`.
* **Redis only delivers a wake-up** (`run_id:attempt`). Losing Redis entirely loses *no* runs: the worker's `sweep`
  cron re-delivers any `queued` run that was never enqueued or whose delivery is overdue (default 90 s).
* Redis has AOF persistence (`appendfsync everysec`), so queued deliveries normally survive a Redis restart; the
  sweep is the safety net if they do not.

Both behaviours were exercised against the real stack (see `IMPLEMENTATION_LOG.md`).

> Hidden coupling worth knowing: CrewAI itself takes cross-process locks through Redis whenever the `REDIS_URL`
> environment variable is set (`crewai_core/lock_store.py`). In compose it points at the same Redis. If Redis is
> down when the worker builds a crew, the run fails with a connection error rather than hanging.

## Job lifecycle and duplicate prevention

```
queued ──claim (CAS)──► running ──complete (CAS, one transaction)──► completed
  ▲                        │ └──fail (CAS)───────────────────────► failed
  └── lease expired, ──────┘
      attempts < MAX_ATTEMPTS        (lease expired and attempts exhausted ──► failed)
```

* **Claim** is `UPDATE … SET status='running', locked_by=:me, attempts=attempts+1 WHERE id=:id AND status='queued'`.
  Exactly one of N concurrent workers gets `rowcount = 1`. Redis delivering the same job twice is harmless.
* **Lease + heartbeat.** The worker extends `lease_expires_at` every `HEARTBEAT_SECONDS`. If it dies, the lease
  lapses and any worker's sweep re-queues the run (up to `MAX_ATTEMPTS` claims, default 2), otherwise fails it.
* **Zombie fencing.** Every completion/failure write is conditioned on `status='running' AND locked_by=:me`, so a
  worker that lost its lease cannot overwrite the new owner's result. Results and the status change commit in **one
  transaction**; a lost lease rolls everything back.
* **The crew runs in a thread** (`asyncio.to_thread(crew.kickoff)`). CrewAI's tools and Anthropic client are
  synchronous; running them on the worker's event loop froze heartbeats, arq's health sentinel and the sweep during
  every LLM call (observed; see audit N1).
* **arq job ids** are `run_id:attempts`, unique per delivery generation, so a re-delivery is never swallowed by a
  stale arq result key. arq retries are disabled (`max_tries=1`); retry policy lives in PostgreSQL.
* **Exceptions are not auto-retried** (LLM spend). A failed run records the error (secrets redacted), the raw
  agent output and the evidence ledger; submit a new run to retry.
* **Graceful shutdown/timeouts** release the lease immediately so the next sweep re-queues the run.
* **Cost guards:** `MAX_ACTIVE_RUNS` (HTTP 429), `MAX_TOOL_CALLS_PER_RUN`, per-agent iteration and wall-clock caps.

If *every* worker is down, orphaned runs wait (status `running`/`queued`) until one returns; recovery is performed
by workers, not the API. A cancelled or timed-out run abandons its crew thread (it ends at the agent time caps or
process exit).

## Research pipeline

`ResearchFlow` (CrewAI **Flow**, typed `ResearchState`) wraps a CrewAI **Crew**:

| Step | What happens |
|---|---|
| `intake` | normalises brief/regions/categories; refuses to start if `ANTHROPIC_API_KEY` or `TAVILY_API_KEY` is missing |
| `run_agents` | sequential Crew: Trend Researcher → Competitor Analyst → User Pain Analyst → Monetization Analyst → Synthesizer. The synthesis task uses `output_pydantic=SynthesisOutput` (native structured output). Regex JSON parsing is only a logged fallback when CrewAI returns no structured object |
| `validate_and_score` | fails the run if no tool returned a result; verifies evidence against the ledger; attaches competitor ratings from Apple data; scores in Python |
| `persist` | one transaction: mark run `completed`, insert opportunities as `pending_approval` (score ≥ threshold) or `below_threshold` |

Four specialist agents carry tools; the fifth (**Synthesizer**) has none, so it can only combine what the specialists
produced. Tools: `web_search` (Tavily), `apple_app_store_search`, `apple_app_reviews`, `opportunity_scoring_rubric`.

### Anti-fabrication design

1. **Evidence ledger.** Every tool records the URLs/records it returned (`app/research/ledger.py`).
2. **URL verification.** An evidence URL counts only if it appears in the ledger. Otherwise it is stored as
   `unverified` and contributes nothing.
3. **No agent-written metrics.** Draft schemas have no download/revenue/eCPM/market-size/rating fields; unknown
   fields from the model are dropped. Competitor `rating`, `rating_count`, `price` are copied from recorded Apple API
   responses only, with `rating_source` and `rating_retrieved_at`.
4. **Typed evidence** — `observed` / `estimate` / `assumption` (+ `unverified`) — stored per claim with
   `retrieved_at` and `published_at` when available.
5. **Untrusted content.** Search results and reviews are labelled as untrusted third-party text in tool output and in
   agent prompts (prompt-injection mitigation, not a guarantee).
6. **Refusal to persist unsourced output** when no search succeeded.

Limit: the ledger proves a URL was *returned by a tool*, not that the page supports the claim.

### Scoring (`app/research/scoring.py`, pure Python)

Weights: unmet need 20 %, demand evidence 20 %, monetization fit 20 %, retention 15 %, competition gap 15 %,
build feasibility 10 %. Agents propose a 0–100 score per dimension; each is **capped by verified evidence** for that
dimension:

| Effective verified sources* | Ceiling |
|---|---|
| 0 | 40 |
| ≥ 0.5 | 50 |
| ≥ 1 | 65 |
| ≥ 2 | 85 |
| ≥ 3 | 100 |

\* distinct hostnames of verified `observed` evidence + 0.5 per distinct host of verified `estimate`s; assumptions and
unverified URLs count 0.

`confidence = Σ weight × min(1, sources/3)` → `low < 0.34 ≤ medium < 0.67 ≤ high`. An opportunity is
`recommended` iff `total_score ≥ 70` (`RECOMMEND_THRESHOLD`). Because of the caps, an opportunity with no verified
evidence tops out at 40 regardless of what the model claims. Every cap is reported in `warnings`;
`score_breakdown` keeps the agent score, capped score, source count and confidence per dimension. The cap table is
a judgement call, versioned as `SCORING_VERSION` and stored on each opportunity so it can be recalibrated.

### Domains (`apps` and `games`)

A run has a `domain`. It selects the crew (`crew.py` or `crew_games.py`), the structured output model
(`SynthesisOutput` or `GameSynthesisOutput`), the scoring weights and version (`scoring.DOMAIN_WEIGHTS`,
`DOMAIN_VERSIONS`) and the approval gate (`Settings.threshold_for`). Everything downstream of the synthesis is shared:
evidence verification, caps, confidence, persistence, approval. Evidence may carry any dimension tag of either domain;
the scorer only looks at the tags that belong to the run's domain (`build_feasibility` is shared). Game-specific
descriptive fields live in `Opportunity.details` (JSON); `domain` is a column on both `research_runs` and `opportunities`.

Schema upgrade: `create_all` does not alter existing tables, so `init_db()` runs idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements (PostgreSQL) for the three added columns; existing rows become
`apps`. This is a stopgap for additive changes only - introduce Alembic before renaming or retyping anything.

## Human approval

* New opportunities are `pending_approval` (recommended) or `below_threshold` (not eligible; cannot be approved).
* `POST /opportunities/{id}/decision` accepts `approved | rejected | revision_requested`. `approved`/`rejected` are
  final; `revision_requested` may be decided later. The opportunity row is locked (`SELECT … FOR UPDATE`) so concurrent
  decisions serialise — exactly one wins.
* The reviewer is the **authenticated API key's name** (`API_KEYS=alice:…`); it cannot be supplied in the body.
* `approvals` is append-only: `ON DELETE RESTRICT` plus a PostgreSQL trigger rejecting `UPDATE`/`DELETE`.
  Each row stores decision, previous status, reviewer, notes, score at decision time and timestamp.
* Nothing downstream exists to trigger. Approval records a decision only: no repository creation, builds, store
  access, purchases, subscriptions or ad spend are implemented anywhere in this codebase.

## Security model

* Every `/api/v1/*` route requires `X-API-Key` (compared via SHA-256 digests + `hmac.compare_digest`, safe for
  non-ASCII input). `/health` is unauthenticated and exposes no secrets.
* Startup refuses missing, short (< 24 chars) or placeholder API keys.
* Secrets come from `.env` (git/docker-ignored); error messages are scrubbed of configured secret values.
* Containers: non-root user, `cap_drop: ALL`, `no-new-privileges`.
* Not included (documented gaps): TLS termination, per-user identity/roles, rate limiting beyond the active-run
  cap, Alembic migrations, structured metrics/alerting, secret rotation tooling.

## Data model

`research_runs` (job state, lease, raw output, evidence ledger) → `opportunities` (content, scores, evidence,
competitors, status) → `approvals` (append-only audit). JSON columns are `JSONB` on PostgreSQL. Tables are created
at startup under a PostgreSQL advisory lock (safe with several containers). There is no migration history yet —
introduce Alembic before the first schema change that must preserve data.

## Decisions

* **ADR-001 Flow around Crew.** Flow = workflow/state/validation/persistence; Crew = collaborative research.
* **ADR-002 Claude via CrewAI's native Anthropic provider** (`crewai[anthropic]`, `anthropic/<model>`), not
  LiteLLM. Model id is configuration (`CLAUDE_MODEL`) and verified by `scripts/check_integrations.py`.
* **ADR-003 Evidence before assertions.** See *Anti-fabrication design*.
* **ADR-004 Deterministic scoring.** LLM opinions are inputs, never outputs; caps keyed on verified evidence.
* **ADR-005 Human approval is a hard boundary.** Enforced in code, schema and database.
* **ADR-006 PostgreSQL is the job ledger; Redis delivers.** Chosen so Redis loss cannot lose or duplicate work.
* **ADR-007 `arq`** for the Redis worker: small, async, supports cron for the sweep. It is in maintenance mode
  upstream; the PostgreSQL-owned state keeps it replaceable (e.g. Celery/Dramatiq/Temporal) without data migration.

# AppFactory AI

**An AI research assistant for app and game ideas.** You give it a brief. A team of AI agents searches the web and
the Apple App Store, and returns a short list of scored ideas with the evidence behind each score. **You** then
approve or reject every idea.

> **Nothing is built, published or paid for.** This is the *research* phase only. "Approve" just records your decision.

| | |
|---|---|
| **Input** | a short brief, plus optional regions and categories (`apps` or `games` mode) |
| **Output** | ideas scored 0 to 100, with cited sources, competitors, warnings and a confidence label |
| **Your job** | read the evidence, then approve / reject / ask for a revision |
| **Stack** | Python 3.12 · FastAPI · CrewAI (Flow + Crew) · Anthropic Claude · Tavily search · PostgreSQL 16 · Redis · Docker Compose |

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [What you need](#2-what-you-need)
3. [Quick start](#3-quick-start)
4. [Run your first research](#4-run-your-first-research)
5. [Review and approve ideas](#5-review-and-approve-ideas)
6. [Games mode](#6-games-mode)
7. [API reference](#7-api-reference)
8. [Configuration](#8-configuration-env)
9. [How scores are calculated](#9-how-scores-are-calculated)
10. [Project layout](#10-project-layout)
11. [Tests](#11-tests)
12. [Running it safely](#12-running-it-safely)
13. [Known limits](#13-known-limits)
14. [More documentation](#14-more-documentation)
15. [ملخص بالعربي](#15-ملخص-بالعربي)

---

## 1. How it works

```
you ──► API (FastAPI) ──► queue ──► worker ──► AI agents search the web + Apple App Store
                                                      │
                          you approve / reject ◄── scored ideas + evidence saved in PostgreSQL
```

1. You send a brief to the API (`POST /api/v1/research-runs`). It answers immediately with a `run_id`.
2. A background **worker** picks the run up. Five CrewAI agents work in sequence: Trend Researcher, Competitor Analyst,
   User Pain Analyst, Monetization Analyst, then a Synthesizer that combines their findings.
3. The agents use real tools only: **Tavily** web search and the **Apple App Store** search and review APIs.
4. **Python code, not the AI, checks and scores the result.** A cited source counts only if a tool really returned it.
   Scores are capped when the evidence is thin.
5. Every idea is saved. Ideas scoring **70 or more** become `pending_approval`. Lower ones are saved as
   `below_threshold` and cannot be approved.
6. You review the evidence and decide. The decision is stored in an append-only audit trail.

The four services in `docker-compose.yml`:

| Service | What it does | Reachable from |
|---|---|---|
| `api` | HTTP API, authentication, approvals | your machine only (`127.0.0.1:8000`) |
| `worker` | runs the research jobs | nothing can connect to it |
| `postgres` | stores everything that must survive a restart | internal Docker network only |
| `redis` | passes job ids from the API to the worker | internal Docker network only |

---

## 2. What you need

* **Docker** with Docker Compose v2
* **Python 3** (only to run the helper scripts `generate_env.py` and `live_research.py` on your machine)
* An **Anthropic API key**: https://console.anthropic.com
* A **Tavily API key**: https://tavily.com

Real research runs **spend credits** on both Anthropic and Tavily. The limits in [Configuration](#8-configuration-env)
cap the spend per run.

---

## 3. Quick start

Run these from the repository folder.

**Step 1. Create your `.env` file with random secrets.**

```bash
python scripts/generate_env.py --reviewer alice --reviewer bob
```

This writes `.env` with fresh passwords and one API key per reviewer. It never overwrites an existing `.env`.
Use your own names instead of `alice` and `bob`. With no `--reviewer` the single reviewer is called `owner`.

**Step 2. Add your two provider keys.** Open `.env` and fill in:

```
ANTHROPIC_API_KEY=...
TAVILY_API_KEY=...
```

**Step 3. Start everything.**

```bash
docker compose up --build -d
```

**Step 4. Check that the API is alive.**

```bash
curl http://127.0.0.1:8000/health
```

**Step 5. Check your providers before spending money.**

```bash
docker compose run --rm api python -m scripts.check_integrations --llm-ping
```

It verifies the Anthropic key, that the model in `CLAUDE_MODEL` exists on your account (it lists the valid ones if
not), the Tavily key, and the Apple Search API.

The interactive API docs are at http://127.0.0.1:8000/docs.

> **Windows PowerShell:** use `curl.exe` instead of `curl` (plain `curl` is an alias for another command), and set
> variables with `$env:APPFACTORY_API_KEY = "..."` instead of `export`.

---

## 4. Run your first research

1. Open `.env`, find `API_KEYS`, for example `alice:AbC...xyz,bob:...`. Your key is the part **after** the colon.
2. Put it in an environment variable and start a run:

```bash
export APPFACTORY_API_KEY=<the key after "alice:">
python scripts/live_research.py --region "United States" --region "United Kingdom" \
    --category productivity --category "personal finance" --max 5
```

This **spends credits** and takes several minutes. When it finishes, your ideas are in the database.

---

## 5. Review and approve ideas

Every `/api/v1/*` request needs the header `X-API-Key: <your key>`.

```bash
# 1. ideas waiting for a decision
curl -H "X-API-Key: $APPFACTORY_API_KEY" \
     "http://127.0.0.1:8000/api/v1/opportunities?decision_status=pending_approval"

# 2. one idea in full: evidence, scores, competitors, warnings
curl -H "X-API-Key: $APPFACTORY_API_KEY" http://127.0.0.1:8000/api/v1/opportunities/<id>

# 3. decide: approved, rejected or revision_requested
curl -X POST -H "X-API-Key: $APPFACTORY_API_KEY" -H "Content-Type: application/json" \
     -d '{"decision":"approved","notes":"validated with 3 sources"}' \
     http://127.0.0.1:8000/api/v1/opportunities/<id>/decision
```

Rules worth knowing:

* The reviewer name recorded in the audit trail is the **name of your API key**. You cannot type it in the request.
* `approved` and `rejected` are final. `revision_requested` can be decided again later.
* An idea in `below_threshold` cannot be approved (the API answers `409`).
* Approving does **not** start anything. No repository, build, store upload, purchase or ad spend exists in this code.

---

## 6. Games mode

Send `"domain": "games"` to research **2D game concepts** (Godot, one developer, rewarded ads) instead of utility apps.
It uses the same pipeline, the same evidence checks and the same human approval, with different agents and scoring.

```bash
APPFACTORY_API_KEY=<key> python scripts/live_research.py --domain games --region Jordan --category puzzle --max 3
```

* **Agents:** Mobile Game Trend Scout, Game Competitor Analyst, Player Pain Analyst, Game Design and Ad Analyst, Synthesizer.
* **Extra tools:** `apple_games_search` and `apple_game_charts` (Apple's public top-free, top-grossing and top-paid game charts).
* **Scoring weights:** gameplay gap 20 %, genre demand 20 %, shareability 15 %, retention loop 15 %, ad fit 15 %,
  distribution path 10 %, build feasibility 5 %. These weights are a judgement call, not calibrated on real data.
* Each game concept also stores `genre`, `core_loop`, `twist`, `ad_moments`, `prototype_scope` and `hook_clip`.
* Every games result carries a warning: novelty is judged against a sample of the **Apple App Store only**
  (Google Play, Steam and itch.io are not searched).

---

## 7. API reference

All `/api/v1/*` routes need the `X-API-Key` header.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/v1/research-runs` | Queue a run. Returns `202 {run_id}`. Body field `domain` is `apps` (default) or `games`. `503` if provider keys are missing, `429` if too many runs are active. |
| `GET` | `/api/v1/research-runs` | List runs. |
| `GET` | `/api/v1/research-runs/{id}` | One run: status, error, summary, opportunity ids, source count. |
| `GET` | `/api/v1/opportunities` | List ideas. Filters: run, domain, status, recommended, minimum score. |
| `GET` | `/api/v1/opportunities/{id}` | One idea in full (games fields are under `details`). |
| `POST` | `/api/v1/opportunities/{id}/decision` | `approved`, `rejected` or `revision_requested`. `409` if already final or below the threshold. |
| `GET` | `/api/v1/approvals` | The append-only decision history (reviewer, notes, time, score at decision). |
| `GET` | `/health` | No key needed. `ok`, `degraded` (Redis down, still `200`) or `503` (PostgreSQL down). |

A run's status goes `queued`, then `running`, then `completed` or `failed`.

---

## 8. Configuration (`.env`)

`python scripts/generate_env.py` creates `.env` from `.env.example`. Never commit `.env`: it is git-ignored.

| Setting | Default | Meaning |
|---|---|---|
| `API_PORT` | `8000` | Host port of the API (bound to `127.0.0.1` only) |
| `API_KEYS` | generated | `name:key,name2:key2`. Each key needs at least 24 characters. The name is the reviewer. |
| `API_KEY` | empty | Alternative single key (reviewer name `owner`). Leave empty when using `API_KEYS`. |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | generated | Passwords for the data stores. Startup refuses placeholder values. |
| `ANTHROPIC_API_KEY` | empty | **You must fill this in.** |
| `CLAUDE_MODEL` | `anthropic/claude-sonnet-5-5` | Model id in the form `anthropic/<model>` |
| `TAVILY_API_KEY` | empty | **You must fill this in.** |
| `TAVILY_SEARCH_DEPTH` | `advanced` | `basic` costs 1 Tavily credit per search, `advanced` costs 2 |
| `MAX_SEARCH_RESULTS` | `8` | Results per search |
| `MAX_OPPORTUNITIES` | `10` | Ideas per run |
| `MAX_TOOL_CALLS_PER_RUN` | `60` | Hard cap on searches and lookups per run (cost control) |
| `MAX_ACTIVE_RUNS` | `3` | Queued plus running runs at once. More get HTTP `429`. |
| `RESEARCH_MAX_ITERATIONS` | `10` | Reasoning and tool iterations per agent |
| `RESEARCH_MAX_EXECUTION_SECONDS` | `900` | Time limit per agent |
| `RECOMMEND_THRESHOLD` | `70` | Minimum score to reach `pending_approval` (apps) |
| `RECOMMEND_THRESHOLD_GAMES` | `70` | Same, for games |
| `WORKER_CONCURRENCY` | `2` | Runs a worker handles at once |
| `JOB_TIMEOUT_SECONDS` | `2700` | Time limit for one whole run |
| `LEASE_SECONDS` | `300` | How long a worker owns a run before others may take it over |
| `MAX_ATTEMPTS` | `2` | How many times a run may be claimed (grows only if a worker dies) |

---

## 9. How scores are calculated

1. Each agent proposes a score from 0 to 100 for every dimension.
2. Python applies fixed weights. For apps: unmet need 20 %, demand 20 %, monetization 20 %, retention 15 %,
   competition gap 15 %, feasibility 10 %.
3. Each dimension is **capped by how many distinct sources a tool really returned for it**:

| Distinct verified sources | Highest score allowed |
|---|---|
| 0 | 40 |
| 0.5 or more | 50 |
| 1 or more | 65 |
| 2 or more | 85 |
| 3 or more | 100 |

4. A confidence label (`low`, `medium`, `high`) is derived from how much verified evidence exists. Every cap that was
   applied is listed in the result's `warnings`.

An idea with no verified evidence can never score above 40, whatever the AI claims. **Scores are hypotheses to help
you decide, not market data.**

---

## 10. Project layout

```
app/
  main.py              FastAPI application
  api/                 routes and API-key authentication
  core/                settings (config.py) and database setup
  models/              database tables (runs, opportunities, approvals)
  schemas/             request/response and agent-output models
  research/            the research engine
    flow.py              the step-by-step workflow (CrewAI Flow)
    crew.py              agents for apps          crew_games.py   agents for games
    tools.py             Tavily and Apple tools   ledger.py       records what each tool returned
    validation.py        checks cited evidence    scoring.py      the scoring rules
  services/runs.py     creating and claiming runs
  worker/              the background worker (arq)
scripts/
  generate_env.py      create .env with random secrets
  check_integrations.py  test the Anthropic, Tavily and Apple connections
  live_research.py     start a real run from the command line
tests/                 offline test suite
docs/                  ARCHITECTURE.md, IMPLEMENTATION_AUDIT.md, IMPLEMENTATION_LOG.md
docker-compose.yml     api, worker, postgres, redis
Dockerfile             image with a runtime stage and a test stage
```

---

## 11. Tests

The offline tests use fake keys and cannot reach any external service.

```bash
docker build --target test -t appfactory-test .
docker run --rm appfactory-test
```

To run them against PostgreSQL, use a **scratch database** (the suite drops all tables):

```bash
docker run --rm --network appfactory-ai_internal \
  -e TEST_DATABASE_URL=postgresql+asyncpg://appfactory:<POSTGRES_PASSWORD>@postgres:5432/appfactory_test \
  appfactory-test pytest -q
```

Live checks against the real providers are `scripts/check_integrations.py` and `scripts/live_research.py`.

---

## 12. Running it safely

* Only the API is published, and only on `127.0.0.1`. For anything beyond your own machine, put a TLS reverse proxy
  in front of it.
* PostgreSQL and Redis are reachable only on the internal Docker network.
* Data lives in the `postgres_data` and `redis_data` volumes. `docker compose down` keeps them, while
  `docker compose down -v` **deletes them**.
* If a container is stopped with `docker kill`, start the worker again with `docker compose start worker`. Crashes
  and `docker stop` are handled by `restart: unless-stopped`, and unfinished runs are re-queued automatically.
* **Never commit `.env`.** Rotate any key that was ever pasted into a chat, ticket or screenshot.

---

## 13. Known limits

* **No real LLM run was executed during development** (no keys were available), so the live behaviour is untested.
  See `docs/IMPLEMENTATION_LOG.md` for what was tested and what was not.
* The evidence check proves that a cited URL was *returned by a tool*. It does not prove the page supports the claim,
  so a human must read the evidence before approving.
* One API key per reviewer, no TLS built in, no roles, and no database migrations yet (tables are created at startup).
* Android competitors come from web search only.
* A dashboard, app generation, QA, store release and revenue monitoring are future phases and are deliberately not
  implemented.

---

## 14. More documentation

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): how it works in depth (job lifecycle, anti-fabrication design, security)
* [`docs/IMPLEMENTATION_AUDIT.md`](docs/IMPLEMENTATION_AUDIT.md): what was wrong with the starter project
* [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md): what was tested, with evidence and gaps

---

## 15. ملخص بالعربي

**AppFactory AI** مساعد بحث بالذكاء الاصطناعي لأفكار التطبيقات والألعاب. بتعطيه وصف قصير، فبيبحث فريق وكلاء ذكاء
اصطناعي بالإنترنت وبمتجر آبل، وبيرجّع أفكار مقيّمة من ٠ لـ١٠٠ مع المصادر. **إنت** بتوافق أو بترفض كل فكرة.

* ما بيبني ولا بينشر ولا بيدفع شي. «موافقة» بتسجّل قرارك بس.
* التقييم بيحسبه كود بايثون مش الذكاء الاصطناعي، والمصدر ما بينحسب إلا إذا أداة بحث رجّعته فعلًا.
* التشغيل: `python scripts/generate_env.py` ثم ضع مفتاحَي Anthropic وTavily بملف `.env` ثم `docker compose up --build -d`.
* كل جولة بحث حقيقية **بتصرف رصيد** من Anthropic وTavily.
* ملف `.env` فيه أسرارك: **ما ترفعه على GitHub أبدًا**.

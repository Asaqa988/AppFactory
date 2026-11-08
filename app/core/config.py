import re
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that appear in .env.example or in tutorials. Refusing them stops a copied example file
# from silently becoming the production credential.
PLACEHOLDER_MARKERS = ("change-this", "changeme", "replace-with", "your-", "example", "unsafe", "password")
MIN_SECRET_LENGTH = 24
REVIEWER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AppFactory AI"
    app_env: Literal["development", "test", "production"] = "development"

    # Authentication. Either API_KEYS="alice:key1,bob:key2" (named reviewers, recommended) or a
    # single API_KEY (reviewer name "owner"). The authenticated name is what the audit trail records.
    api_key: str = ""
    api_keys: str = ""

    database_url: str = "postgresql+asyncpg://appfactory:unset@localhost:5432/appfactory"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    # CrewAI model identifier: "anthropic/<model id>". Confirm it is enabled for your account with
    # `python -m scripts.check_integrations`.
    claude_model: str = "anthropic/claude-sonnet-5-5"
    tavily_api_key: str = ""

    # Research controls
    tavily_search_depth: Literal["basic", "advanced"] = "advanced"  # advanced costs 2 Tavily credits/search
    max_search_results: int = 8
    max_tool_calls_per_run: int = 60  # hard cap on paid web-search (Tavily) calls per run (cost control)
    max_free_tool_calls_per_run: int = 80  # separate cap on free Apple lookups (search, charts, reviews)
    max_opportunities: int = 10
    research_max_iterations: int = 10  # per-agent tool/reasoning iteration cap
    research_max_execution_seconds: int = 900  # per-agent wall-clock cap
    recommend_threshold: float = 70.0
    # Games are scored on a different rubric (see scoring.GAME_WEIGHTS); the gate is separate so it can be
    # tuned after real games runs without touching the apps gate. Same default: nothing is loosened silently.
    recommend_threshold_games: float = 70.0
    # Output budget per LLM call. The synthesis step emits every opportunity with all its cited evidence as one
    # JSON object; 8192 truncated it in the first live run. Keep <= ~20000: larger non-streaming requests are
    # rejected by the Anthropic SDK as likely to exceed 10 minutes.
    llm_max_tokens: int = 16000

    # Worker / queue controls
    worker_concurrency: int = 2
    job_timeout_seconds: int = 2700
    lease_seconds: int = 300
    heartbeat_seconds: int = 60
    max_attempts: int = 2  # claims per run; only grows when a worker dies mid-run
    sweep_interval_seconds: int = 30
    redeliver_after_seconds: int = 90
    max_active_runs: int = 3  # queued+running; protects against runaway LLM spend

    @model_validator(mode="after")
    def _validate_secrets(self):
        if self.app_env == "test":
            return self
        keys = self.principals
        if not keys:
            raise ValueError(
                "No API credentials configured. Set API_KEY or API_KEYS "
                "(run `python scripts/generate_env.py` to create a .env with random secrets)."
            )
        for key, name in keys.items():
            if len(key) < MIN_SECRET_LENGTH or any(m in key.lower() for m in PLACEHOLDER_MARKERS):
                # Name the reviewer only - never echo the key itself.
                raise ValueError(
                    f"API key for '{name}' is a placeholder or shorter than {MIN_SECRET_LENGTH} characters."
                )
        return self

    @property
    def principals(self) -> dict[str, str]:
        """Map of API key -> reviewer name."""
        result: dict[str, str] = {}
        if self.api_keys.strip():
            for item in self.api_keys.split(","):
                name, sep, key = item.strip().partition(":")
                name, key = name.strip(), key.strip()
                if not sep or not key or not REVIEWER_NAME_RE.match(name):
                    raise ValueError("API_KEYS must look like 'name:key,name2:key2' with simple names.")
                result[key] = name
        if self.api_key.strip():
            result.setdefault(self.api_key.strip(), "owner")
        return result

    def threshold_for(self, domain: str) -> float:
        return self.recommend_threshold_games if domain == "games" else self.recommend_threshold

    def missing_research_credentials(self) -> list[str]:
        missing = []
        if not self.anthropic_api_key.strip():
            missing.append("ANTHROPIC_API_KEY")
        if not self.tavily_api_key.strip():
            missing.append("TAVILY_API_KEY")
        return missing

    def secret_values(self) -> list[str]:
        """Secrets that must never be echoed into logs, error messages or API responses."""
        values = [self.anthropic_api_key, self.tavily_api_key, *self.principals.keys()]
        return [v for v in values if v and len(v) >= 8]


settings = Settings()

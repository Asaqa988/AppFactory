"""LIVE integration check. Needs real credentials and internet; makes a handful of tiny requests.

    docker compose run --rm api python -m scripts.check_integrations [--llm-ping]

Verifies, in order: Anthropic key + configured model id, Tavily key, Apple Search API (no key),
and with --llm-ping a one-line completion through CrewAI's own LLM class (a few tokens of spend).
Never prints secret values. Exit code 0 only if every enabled check passed.
"""
import argparse
import sys

import httpx

from app.core.config import settings


def report(name: str, ok: bool | None, detail: str) -> bool:
    mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    print(f"[{mark}] {name}: {detail}")
    return ok is not False


def check_anthropic() -> bool:
    if not settings.anthropic_api_key:
        return report("anthropic", None, "ANTHROPIC_API_KEY not set")
    model = settings.claude_model.split("/", 1)[-1]
    try:
        response = httpx.get(
            "https://api.anthropic.com/v1/models", params={"limit": 1000}, timeout=30,
            headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"},
        )
    except httpx.HTTPError as exc:
        return report("anthropic", False, f"network error: {type(exc).__name__}")
    if response.status_code != 200:
        return report("anthropic", False, f"HTTP {response.status_code} (check the key)")
    ids = [m["id"] for m in response.json().get("data", [])]
    if model in ids:
        return report("anthropic", True, f"key valid; model '{model}' is available")
    return report("anthropic", False, f"key valid but model '{model}' is NOT available. "
                  f"Set CLAUDE_MODEL to one of: {', '.join(ids[:12]) or '(none listed)'}")


def check_tavily() -> bool:
    if not settings.tavily_api_key:
        return report("tavily", None, "TAVILY_API_KEY not set")
    try:
        response = httpx.post(
            "https://api.tavily.com/search", timeout=30,
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            json={"query": "most downloaded mobile app categories", "search_depth": "basic", "max_results": 1},
        )
    except httpx.HTTPError as exc:
        return report("tavily", False, f"network error: {type(exc).__name__}")
    if response.status_code != 200:
        return report("tavily", False, f"HTTP {response.status_code} (check the key / plan limits)")
    return report("tavily", True, f"{len(response.json().get('results', []))} result(s) returned")


def check_apple() -> bool:
    try:
        response = httpx.get("https://itunes.apple.com/search", timeout=30,
                             params={"term": "notes", "entity": "software", "country": "us", "limit": 1})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return report("apple-search-api", False, f"{type(exc).__name__}")
    return report("apple-search-api", True, f"{len(response.json().get('results', []))} result(s) returned")


def check_llm_ping() -> bool:
    if not settings.anthropic_api_key:
        return report("crewai-llm", None, "ANTHROPIC_API_KEY not set")
    from app.research.crew import build_llm
    try:
        reply = str(build_llm().call("Reply with exactly the single word: OK")).strip()
    except Exception as exc:  # noqa: BLE001
        return report("crewai-llm", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    return report("crewai-llm", True, f"model replied: {reply[:40]!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-ping", action="store_true", help="also send one tiny prompt through CrewAI")
    args = parser.parse_args()
    results = [check_anthropic(), check_tavily(), check_apple()]
    if args.llm_ping:
        results.append(check_llm_ping())
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

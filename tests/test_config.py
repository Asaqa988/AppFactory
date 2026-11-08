import pytest
from pydantic import ValidationError

from app.core.config import Settings

GOOD = "k" * 32 + "-real-looking-secret"


def make(**kw):
    kw.setdefault("app_env", "production")
    kw.setdefault("api_key", "")  # the test harness exports API_KEYS; start every case from a clean slate
    kw.setdefault("api_keys", "")
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("key", ["change-this-api-key", "short", "replace-with-a-long-random-secret-value", "password-password-password-1"])
def test_placeholder_or_short_keys_are_rejected(key):
    with pytest.raises(ValidationError):
        make(api_key=key)


def test_missing_credentials_are_rejected():
    with pytest.raises(ValidationError, match="No API credentials"):
        make()


def test_single_key_maps_to_owner():
    assert make(api_key=GOOD).principals == {GOOD: "owner"}


def test_named_keys_map_to_reviewers():
    s = make(api_keys=f"alice:{GOOD}1,bob:{GOOD}2")
    assert s.principals == {f"{GOOD}1": "alice", f"{GOOD}2": "bob"}


@pytest.mark.parametrize("bad", ["nocolon", "alice:", ":key", "bad name:" + GOOD, "a/b:" + GOOD])
def test_malformed_api_keys_rejected(bad):
    with pytest.raises(ValidationError):
        make(api_keys=bad)


def test_missing_research_credentials_are_reported_by_name_only():
    s = make(api_key=GOOD, anthropic_api_key="", tavily_api_key="tvly-abcdefghij")
    assert s.missing_research_credentials() == ["ANTHROPIC_API_KEY"]


def test_secret_values_cover_all_credentials():
    s = make(api_key=GOOD, anthropic_api_key="sk-ant-abcdefghij", tavily_api_key="tvly-abcdefghij")
    assert {GOOD, "sk-ant-abcdefghij", "tvly-abcdefghij"} <= set(s.secret_values())


def test_default_model_is_an_anthropic_claude_identifier():
    assert make(api_key=GOOD).claude_model.startswith("anthropic/claude-")

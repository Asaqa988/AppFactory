"""A scripted stand-in for the CrewAI Crew: same `akickoff(inputs)` contract, no LLM, no network."""
import json
from types import SimpleNamespace

from tests.helpers import draft, evidence_for_all, synthesis

URLS = ["https://news.example.org/a", "https://forum.example.net/b", "https://blog.example.com/c"]


def good_synthesis(scores=(90,)):
    return synthesis(*[draft(name=f"idea-{s}", scores=s, evidence=evidence_for_all(URLS)) for s in scores])


class FakeCrew:
    def __init__(self, ledger, *, synth="default", raw=None, search=True, raises=None, pydantic_ok=True, delay=None):
        self.ledger, self.search, self.raises, self.delay = ledger, search, raises, delay
        self.synth = good_synthesis() if synth == "default" else synth
        self.raw = raw
        self.pydantic_ok = pydantic_ok
        self.inputs = None

    def kickoff(self, inputs):  # synchronous, like the real Crew.kickoff; the flow runs it in a thread
        import time
        self.inputs = inputs
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise self.raises
        if self.search:
            for url in URLS:
                self.ledger.record_result("web_search", url, "t", "2026-01-02", "snippet")
            self.ledger.record_call("web_search", "q", ok=True, result_count=len(URLS))
        raw = self.raw if self.raw is not None else (self.synth.model_dump_json() if self.synth else "")
        usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 1234})
        return SimpleNamespace(raw=raw, pydantic=self.synth if self.pydantic_ok else None, token_usage=usage)


def factory(**kwargs):
    created = []

    def make(ledger):
        crew = FakeCrew(ledger, **kwargs)
        created.append(crew)
        return crew

    make.created = created
    return make


__all__ = ["FakeCrew", "factory", "good_synthesis", "URLS", "json"]

"""LLM client and summary guard. No network or database needed."""
import pytest

from blast_radius.agents.base import known_ids, summarize
from blast_radius.llm import LLM, LLMUnavailable, Provider, ProviderError, _parse_json


class Fake(Provider):
    def __init__(self, name, reply=None, error=None):
        super().__init__(name, "fake-model", "key")
        self.reply, self.error, self.calls = reply, error, 0

    def complete_json(self, system, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


def http_error(code):
    return ProviderError(code, "err")


def test_first_provider_used_when_healthy():
    a, b = Fake("gemini", {"ok": 1}), Fake("groq", {"ok": 2})
    llm = LLM([a, b])
    assert llm.complete_json("s", "p") == {"ok": 1}
    assert (a.calls, b.calls) == (1, 0) and llm.last_provider == "gemini:fake-model"


def test_falls_back_on_rate_limit():
    a, b = Fake("gemini", error=http_error(429)), Fake("groq", {"ok": 2})
    llm = LLM([a, b])
    assert llm.complete_json("s", "p") == {"ok": 2}
    assert llm.last_provider == "groq:fake-model" and "HTTP 429" in llm.errors[0]


def test_all_providers_down_raises():
    llm = LLM([Fake("gemini", error=http_error(503)), Fake("groq", error=TimeoutError("slow"))])
    with pytest.raises(LLMUnavailable) as exc:
        llm.complete_json("s", "p")
    assert "503" in str(exc.value) and "TimeoutError" in str(exc.value)


def test_parse_json_handles_code_fences():
    assert _parse_json('```json\n{"summary": "x"}\n```') == {"summary": "x"}
    with pytest.raises(ValueError):
        _parse_json("no json here")


FACTS = {"incident": "inc-alert-1", "suspect_deploys": [{"deployment": "deploy-0042", "service": "payments"}]}


def test_known_ids_walks_nested_facts():
    assert {"inc-alert-1", "deploy-0042", "payments"} <= known_ids(FACTS)


def test_summary_accepted_when_citations_are_real():
    llm = LLM([Fake("gemini", {"summary": "Likely [deploy-0042] on payments."})])
    text, source = summarize(llm, "triage", "role", FACTS, "fallback")
    assert text == "Likely [deploy-0042] on payments." and source == "gemini:fake-model"


def test_summary_rejected_when_model_invents_an_id():
    llm = LLM([Fake("gemini", {"summary": "Caused by [deploy-9999]."})])
    text, source = summarize(llm, "triage", "role", FACTS, "fallback")
    assert text == "fallback" and "rejected" in source


def test_summary_without_llm_uses_template():
    assert summarize(None, "triage", "role", FACTS, "fallback") == ("fallback", "template")
    down = LLM([Fake("gemini", error=http_error(429))])
    assert summarize(down, "triage", "role", FACTS, "fallback")[1] == "template (LLM unavailable)"

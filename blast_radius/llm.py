"""LLM client: Gemini first, Groq as fallback, both on their free tiers.

The LLM never decides facts. Agents get facts from graph tools; the LLM only
writes summaries and, for follow-up questions, chooses which graph tool to call.
Every call asks for a JSON object back so the output can be checked.
"""
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config  # noqa: F401  (loads .env before keys are read)

TIMEOUT_S = 30


class LLMUnavailable(RuntimeError):
    """Raised when every configured provider failed (rate limit, outage, no key)."""


@dataclass
class Provider:
    name: str
    model: str
    api_key: str

    def complete_json(self, system: str, prompt: str) -> dict:
        raise NotImplementedError


def _post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"model did not return JSON: {text[:200]!r}")
    return json.loads(text[start : end + 1])


class Gemini(Provider):
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def complete_json(self, system: str, prompt: str) -> dict:
        data = _post(
            f"{self.BASE}/{self.model}:generateContent",
            {"x-goog-api-key": self.api_key},
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
            },
        )
        parts = data["candidates"][0]["content"]["parts"]
        return _parse_json("".join(p.get("text", "") for p in parts))


class Groq(Provider):
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def complete_json(self, system: str, prompt: str) -> dict:
        data = _post(
            self.URL,
            {"Authorization": f"Bearer {self.api_key}"},
            {
                "model": self.model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            },
        )
        return _parse_json(data["choices"][0]["message"]["content"])


class LLM:
    """Tries each provider in order; moves to the next on any failure."""

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self.last_provider: str | None = None
        self.errors: list[str] = []

    def complete_json(self, system: str, prompt: str) -> dict:
        self.errors = []
        for p in self.providers:
            try:
                out = p.complete_json(system, prompt)
                self.last_provider = f"{p.name}:{p.model}"
                return out
            except urllib.error.HTTPError as exc:
                self.errors.append(f"{p.name}: HTTP {exc.code}")
            except Exception as exc:  # timeout, bad JSON, network
                self.errors.append(f"{p.name}: {type(exc).__name__}: {exc}"[:200])
        raise LLMUnavailable("; ".join(self.errors) or "no LLM provider configured")


def from_env() -> LLM:
    gemini = Gemini("gemini", os.getenv("GEMINI_MODEL", "gemini-flash-latest"), os.getenv("GEMINI_API_KEY", ""))
    groq = Groq("groq", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), os.getenv("GROQ_API_KEY", ""))
    order = [groq, gemini] if os.getenv("LLM_PROVIDER", "gemini").lower() == "groq" else [gemini, groq]
    return LLM([p for p in order if p.api_key])

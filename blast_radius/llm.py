"""LLM client: Gemini first, Groq as fallback, both on their free tiers.

The LLM never decides facts. Agents get facts from graph tools; the LLM only
writes summaries and, for follow-up questions, chooses which graph tool to call.
Every call asks for a JSON object back so the output can be checked.
"""
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config  # noqa: F401  (loads .env before keys are read)

TIMEOUT_S = 20


class LLMUnavailable(RuntimeError):
    """Raised when every configured provider failed (rate limit, outage, no key)."""


@dataclass
class Provider:
    name: str
    model: str
    api_key: str

    def complete_json(self, system: str, prompt: str) -> dict:
        raise NotImplementedError


class ProviderError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def _post(url: str, headers: dict, body: dict, retries: int = 2) -> dict:
    # A real User-Agent matters: some API gateways reject Python's default one with 403.
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "blast-radius/0.2", **headers},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300].replace("\n", " ")
            # 503 = model overloaded: worth one or two quick retries before falling back.
            if exc.code in (500, 502, 503) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ProviderError(exc.code, detail) from None


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
                # No strict JSON mode: reasoning models (gpt-oss) often fail Groq's validator.
                # We ask for JSON in the prompt and extract it ourselves.
                **({"reasoning_effort": "low"} if "gpt-oss" in self.model else {}),
                "messages": [
                    {"role": "system", "content": system + " Respond with a single JSON object and nothing else."},
                    {"role": "user", "content": prompt},
                ],
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
            except Exception as exc:  # timeout, bad JSON, network
                self.errors.append(f"{p.name}: {type(exc).__name__}: {exc}"[:200])
        raise LLMUnavailable("; ".join(self.errors) or "no LLM provider configured")


def from_env() -> LLM:
    gemini = Gemini("gemini", os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"), os.getenv("GEMINI_API_KEY", ""))
    groq = Groq("groq", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"), os.getenv("GROQ_API_KEY", ""))
    order = [groq, gemini] if os.getenv("LLM_PROVIDER", "gemini").lower() == "groq" else [gemini, groq]
    return LLM([p for p in order if p.api_key])

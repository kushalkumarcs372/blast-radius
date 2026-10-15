"""Shared helpers for agents: LLM summaries with a guard against made-up citations."""
import json
import re

from ..llm import LLM, LLMUnavailable

_CITE = re.compile(r"\[([a-z0-9][a-z0-9\-]+)\]")

SYSTEM = (
    "You are the {agent} agent in an incident response team. {role} "
    "Write for an on-call engineer at 2 AM: short, specific, no filler. "
    "Use ONLY the facts in FACTS. Do not add services, people, numbers or causes that are not there. "
    "When you mention a node, cite its id in square brackets, e.g. [deploy-0042]. "
    'Reply with a JSON object: {{"summary": "<at most 4 sentences>"}}'
)


def known_ids(facts) -> set[str]:
    """Every string in the facts that looks like a node id."""
    found: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9\-]+", x):
            found.add(x)

    walk(facts)
    return found


def summarize(llm: LLM | None, agent: str, role: str, facts: dict, fallback: str) -> tuple[str, str]:
    """Return (summary, source). source is the model used, or 'template' if the LLM was skipped.

    An LLM summary that cites an id not present in the facts is rejected, because
    that means the model invented something.
    """
    if llm is None or not llm.providers:
        return fallback, "template"
    try:
        out = llm.complete_json(SYSTEM.format(agent=agent, role=role), "FACTS:\n" + json.dumps(facts, default=str))
        text = str(out.get("summary", "")).strip()
        invented = set(_CITE.findall(text)) - known_ids(facts)
        if not text or invented:
            return fallback, "template (LLM output rejected)"
        return text, llm.last_provider or "llm"
    except LLMUnavailable:
        return fallback, "template (LLM unavailable)"

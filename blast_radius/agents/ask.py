"""Assistant agent: answers follow-up questions by choosing graph tools.

The model picks one tool at a time from a fixed list (it never writes Cypher),
sees the result, and either calls another tool or answers. Each question and
answer is stored as an Episode, so a later session can pick up the conversation.
"""
import inspect
import json

from .. import memory
from ..llm import LLMUnavailable
from ..tools import graph_tools as T
from .base import _CITE, known_ids

MAX_STEPS = 5
MAX_RESULT_CHARS = 4000

TOOL_DOCS = {
    "list_incidents": "list_incidents() -> open incidents the agents are working on",
    "incident_memory": "incident_memory(incident) -> everything agents recorded on an incident, with suspected causes",
    "open_alerts": "open_alerts() -> alerts that have no incident yet",
    "blast_radius": "blast_radius(service, max_hops=4) -> services that break if this one goes down",
    "dependencies": "dependencies(service, max_hops=4) -> what this service depends on",
    "recent_deploys": "recent_deploys(service, hours=24) -> deploys to this service or services within 2 hops",
    "critical_services": "critical_services(limit=10) -> most critical services by PageRank",
    "connection_path": "connection_path(source, target) -> how two services are connected",
    "owners": "owners(service) -> owning team, channel, members, who is on call",
    "fix_history": "fix_history(service) -> people who resolved incidents on this service",
    "similar_incidents": "similar_incidents(service, signal='') -> past incidents on this service",
    "runbook": "runbook(service) -> the on-call runbook",
    "decision_trace": "decision_trace(service) -> design decisions about the service and their source documents",
}

SYSTEM = (
    "You answer questions from on-call engineers about their company's systems. "
    "Facts come ONLY from tool results; never guess names, numbers or causes. "
    "Call one tool at a time. Available tools:\n{tools}\n\n"
    'To call a tool reply {{"tool": "<name>", "args": {{...}}}}. '
    'When you can answer, reply {{"answer": "<2-5 sentences>"}} and cite node ids from tool results in square '
    "brackets, e.g. [inc-003] or [deploy-0042]. If the tools cannot answer, say so. "
    "Questions about a current or recent alert or incident: use incident_memory on the incident listed "
    "under OPEN INCIDENTS first; it already holds the agents' findings."
)


def _call(g, name: str, args: dict):
    if name == "list_incidents":
        return memory.list_incidents(g)
    if name == "incident_memory":
        return memory.incident_memory(g, **args)
    fn = T.TOOLS[name]
    sig = inspect.signature(fn)
    unknown = set(args) - (set(sig.parameters) - {"g"})
    if unknown:
        raise TypeError(f"unknown arguments for {name}: {sorted(unknown)}")
    return fn(g, **args).to_dict()


def history(g, session: str, limit: int = 6) -> list[dict]:
    rows = g.query(
        "MATCH (:Agent {name: 'assistant'})-[:RECORDED]->(e:Episode {session: $s, kind: 'qa'}) "
        "RETURN e.findings, e.ts AS ts ORDER BY ts DESC LIMIT $n",
        {"s": session, "n": limit},
    ).result_set
    return [json.loads(f) for f, _ in reversed(rows)]


def ask(g, question: str, llm, session: str, incident: str | None = None) -> dict:
    memory.ensure_agents(g)
    if llm is None or not llm.providers:
        return {"answer": "No LLM key configured. Add GEMINI_API_KEY or GROQ_API_KEY to .env.", "steps": []}

    system = SYSTEM.format(tools="\n".join(f"- {d}" for d in TOOL_DOCS.values()))
    earlier = history(g, session)
    past = [{"q": h["question"], "a": h["answer"]} for h in earlier]
    transcript = [f"Earlier in this conversation: {json.dumps(past)}"] if past else []
    open_now = memory.list_incidents(g)
    transcript.append(f"OPEN INCIDENTS: {json.dumps(open_now, default=str)}")
    if incident:
        transcript.append(f"The engineer is asking about incident {incident}.")
    transcript.append(f"QUESTION: {question}")

    steps = []
    # Ids already established earlier in this conversation may be cited again.
    seen = [h.get("cited", []) for h in earlier]
    answer = None
    for _ in range(MAX_STEPS):
        try:
            out = llm.complete_json(system, "\n".join(transcript))
        except LLMUnavailable as exc:
            answer = f"The language model is unavailable right now ({exc}). Try a direct tool command."
            break
        if "answer" in out:
            answer = str(out["answer"])
            break
        name, args = out.get("tool"), out.get("args") or {}
        if name not in TOOL_DOCS or not isinstance(args, dict):
            transcript.append(f"Invalid tool request {json.dumps(out)}. Use one of: {', '.join(TOOL_DOCS)}.")
            continue
        try:
            result = _call(g, name, args)
            seen.append(result)
            text = json.dumps(result, default=str)[:MAX_RESULT_CHARS]
        except Exception as exc:
            text = f"ERROR: {exc}"
        steps.append({"tool": name, "args": args, "result_chars": len(text)})
        transcript.append(f"TOOL {name}({json.dumps(args)}) RESULT: {text}")
    if answer is None:
        answer = "I could not finish within the step limit. Try a narrower question."

    cited = set(_CITE.findall(answer))
    valid = cited & known_ids(seen)
    invented = sorted(cited - valid)
    record = {"question": question, "answer": answer, "tools": steps, "invented_ids": invented,
              "cited": sorted(valid),
              "provider": llm.last_provider}
    memory.record_episode(g, agent="assistant", incident=incident, session=session, kind="qa",
                          summary=answer, findings=record, cites=_resolve_cites(g, valid))
    return record


def _resolve_cites(g, ids: set[str]) -> list[tuple[str, str]]:
    """Find the label of each cited id so the Episode can link to the real node."""
    if not ids:
        return []
    out = []
    for label in memory.CITE_LABELS:
        rows = g.query(f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id", {"ids": sorted(ids)}).result_set
        out += [(label, r[0]) for r in rows]
    return out

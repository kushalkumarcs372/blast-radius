"""Assistant agent: answers follow-up questions by choosing graph tools.

The model picks one tool at a time from a fixed list (it never writes Cypher),
sees the result, and either calls another tool or answers. Each question and
answer is stored as an Episode, so a later session can pick up the conversation.
"""
import inspect
import json
import re

from .. import memory
from ..llm import LLMUnavailable
from ..tools import graph_tools as T
from .base import _CITE, known_ids

MAX_STEPS = 8
MAX_TOOLS_PER_STEP = 4
MAX_RESULT_CHARS = 4000

TOOL_DOCS = {
    "list_incidents": "list_incidents() -> incidents the agents have worked on (open and resolved)",
    "list_services": "list_services() -> every service in this company with tier, owning team, dependency count",
    "documents": "documents(service) -> all documents about a service: design decisions (ADRs) and incident postmortems",
    "incident_memory": "incident_memory(incident) -> any incident, past or current: who resolved it (resolved_by), "
                       "its confirmed cause (caused_by), affected services, agent findings, suspected causes, documents",
    "open_alerts": "open_alerts() -> alerts that have no incident yet",
    "blast_radius": "blast_radius(service, max_hops=4) -> services that break if this one goes down; "
                    "extra.unaffected_services lists the ones that do not",
    "what_if_cut": "what_if_cut(service, cut_from, cut_to) -> blast radius of service if the dependency "
                   "cut_from -> cut_to had a circuit breaker or fallback; which services would be saved",
    "breaker_advice": "breaker_advice(service) -> the dependencies where one circuit breaker would protect the "
                      "most services (customer-facing first) from an outage of service",
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
    "Facts come ONLY from tool results; never guess names, numbers, paths or causes. Available tools:\n{tools}\n\n"
    'To call tools reply {{"tools": [{{"tool": "<name>", "args": {{...}}}}, ...]}} (up to 4 at once; for a '
    "question with several parts, request every tool you need in the same step). "
    'When you can answer, reply {{"answer": "<answer; one short paragraph per part of the question>"}}. '
    "Citations: put in square brackets ONLY ids or service names that appear in the tool results, e.g. "
    "[inc-003], [deploy-0042], [doc-pm-inc-alert-live], [checkout]. Never invent tags such as [path-001] or "
    "[blast-rad-002]; a fact without an id is written without brackets. For paths, copy the path from "
    "connection_path exactly. If the tools cannot answer, say so. "
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
        return {"question": question, "answer": "No LLM key configured. Add GEMINI_API_KEY or GROQ_API_KEY to .env.",
                "tools": [], "invented_ids": [], "cited": [], "provider": None}

    system = SYSTEM.format(tools="\n".join(f"- {d}" for d in TOOL_DOCS.values()))
    earlier = history(g, session)
    # Ids already established earlier in this conversation (or by the agents) may be cited again.
    seen = [h.get("cited", []) for h in earlier]
    past = [{"q": h["question"], "a": h["answer"]} for h in earlier]
    transcript = [f"Earlier in this conversation: {json.dumps(past)}"] if past else []
    open_now = memory.list_incidents(g)
    transcript.append(f"OPEN INCIDENTS: {json.dumps(open_now, default=str)}")
    if incident:
        # Preload what the agents already found, so follow-ups start with context and need fewer calls.
        mem = memory.incident_memory(g, incident)
        if "error" not in mem:
            brief = {"incident": incident, "title": mem["title"], "status": mem["status"],
                     "suspected_causes": mem["suspected_causes"], "documents": mem["documents"],
                     "agent_findings": [{"agent": e["agent"], "summary": e["summary"], "cites": e["cites"][:15]}
                                        for e in mem["episodes"] if e["agent"] != "assistant"]}
            seen.append(brief)
            transcript.append(f"CURRENT INCIDENT (already investigated): {json.dumps(brief, default=str)}")
        transcript.append(f"The engineer is asking about incident {incident}.")
    transcript.append(f"QUESTION: {question}")

    steps = []
    answer = None
    pushed_back = False
    for _ in range(MAX_STEPS):
        try:
            out = llm.complete_json(system, "\n".join(transcript))
        except LLMUnavailable as exc:
            answer = f"The language model is unavailable right now ({exc}). Try a direct tool command."
            break
        if "answer" in out:
            if not steps and not pushed_back:
                # No tool was called for this question: make the model look the facts up first.
                pushed_back = True
                transcript.append("You answered without calling any tool for this QUESTION. Earlier answers may "
                                  "be about another incident. Call the tools you need now, then answer.")
                continue
            answer = str(out["answer"])
            break
        calls = out.get("tools") if isinstance(out.get("tools"), list) else [out]
        for call in calls[:MAX_TOOLS_PER_STEP]:
            name, args = (call or {}).get("tool"), (call or {}).get("args") or {}
            if name not in TOOL_DOCS or not isinstance(args, dict):
                transcript.append(f"Invalid tool request {json.dumps(call)}. Use one of: {', '.join(TOOL_DOCS)}.")
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
    # Guard: a citation that is not in any tool result is removed from the answer and reported.
    for bad in invented:
        answer = answer.replace(f" [{bad}]", "").replace(f"[{bad}]", "")
    # Bracketed field names such as [unaffected_services] are not ids: keep the words, drop the brackets.
    answer = re.sub(r"\[([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\]", lambda m: m.group(1).replace("_", " "), answer)
    unknown = unknown_names(g, answer)
    if unknown:
        answer += f" (Warning: not found in the graph, treat as unverified: {', '.join(unknown)}.)"
    record = {"question": question, "answer": answer, "tools": steps, "invented_ids": invented,
              "unknown_names": unknown, "cited": sorted(valid),
              "provider": llm.last_provider}
    memory.record_episode(g, agent="assistant", incident=incident, session=session, kind="qa",
                          summary=answer, findings=record, cites=_resolve_cites(g, valid))
    return record


_NAME = re.compile(r"(?<![\w-])([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)(?![\w-])")
_ID_PREFIXES = ("svc-", "deploy-", "inc-", "alert-", "doc-", "adr-", "pm-", "runbook-", "decision-", "team-")


def unknown_names(g, text: str) -> list[str]:
    """Id-like names in an answer (svc-auth, ADR-42, deploy-1234) that are not in the graph.

    A name counts as id-like when it has a digit or starts with an id prefix; plain hyphenated
    words such as "customer-facing" are ignored.
    """
    candidates = {m.lower() for m in _NAME.findall(text)}
    candidates = {c for c in candidates if any(ch.isdigit() for ch in c) or c.startswith(_ID_PREFIXES)}
    if not candidates:
        return []
    rows = g.query("MATCH (n) WHERE n.id IN $c OR n.name IN $c RETURN DISTINCT coalesce(n.id, ''), "
                   "coalesce(n.name, '')", {"c": sorted(candidates)}).result_set
    known = {v.lower() for r in rows for v in r if v}
    # Names that appear inside document titles ("ADR-007: ...") are real too.
    for (title,) in g.query("MATCH (n) WHERE n.title IS NOT NULL RETURN n.title").result_set:
        known |= {m.lower() for m in _NAME.findall(str(title))}
    return sorted(candidates - known)


def _resolve_cites(g, ids: set[str]) -> list[tuple[str, str]]:
    """Find the label of each cited id so the Episode can link to the real node."""
    if not ids:
        return []
    out = []
    for label in memory.CITE_LABELS:
        rows = g.query(f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id", {"ids": sorted(ids)}).result_set
        out += [(label, r[0]) for r in rows]
    return out

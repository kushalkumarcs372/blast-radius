"""HTTP API and web UI.

Run:  uvicorn blast_radius.api:app --reload      then open http://localhost:8000
"""
import os
import re
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import memory
from .db import graph, list_tenants
from .llm import from_env
from .tools import graph_tools as T

WEB = Path(__file__).resolve().parent.parent / "web"
ASK_LIMIT_PER_HOUR = int(os.getenv("ASK_LIMIT_PER_HOUR", "30"))
ALLOW_RESET = os.getenv("ALLOW_RESET", "true").lower() == "true"

@asynccontextmanager
async def lifespan(_app):
    # A fresh database (e.g. a new cloud instance) gets the synthetic companies loaded once.
    if not list_tenants():
        reset_data()
    yield


app = FastAPI(title="Blast Radius", version="0.3", lifespan=lifespan)
_llm = from_env()
_ask_log: dict[str, deque] = defaultdict(deque)


def _empty_sandbox(tenant: str) -> bool:
    """The sandbox company exists only after something has been built in it."""
    from .ingest import SANDBOX

    return tenant == SANDBOX and SANDBOX not in list_tenants()


def _graph(tenant: str):
    if tenant not in list_tenants():
        raise HTTPException(404, f"unknown tenant {tenant!r}")
    return graph(tenant)


def _rate_limited(ip: str) -> bool:
    """Protects the free LLM quota on the public demo."""
    now, log = time.time(), _ask_log[ip]
    while log and now - log[0] > 3600:
        log.popleft()
    if len(log) >= ASK_LIMIT_PER_HOUR:
        return True
    log.append(now)
    return False


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "tenants": list_tenants(), "llm": [f"{p.name}:{p.model}" for p in _llm.providers]}


@app.get("/api/tenants")
def tenants():
    from .ingest import SANDBOX

    return [t for t in list_tenants() if t != SANDBOX] + [SANDBOX]


@app.get("/api/graph")
def service_graph(tenant: str = "acme"):
    """Services, owners and dependencies, for drawing the map."""
    if _empty_sandbox(tenant):
        return {"nodes": [], "edges": []}
    g = _graph(tenant)
    nodes = g.query(
        "MATCH (s:Service) OPTIONAL MATCH (t:Team)-[:OWNS]->(s) "
        "RETURN s.name AS name, s.tier, s.language, t.name ORDER BY name"
    ).result_set
    edges = g.query("MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) RETURN a.name, b.name").result_set
    crit = {r["service"]: r["pagerank"] for r in T.critical_services(g, 50).results}
    return {
        "nodes": [{"id": n, "tier": t, "language": l, "team": tm, "pagerank": crit.get(n, 0)} for n, t, l, tm in nodes],
        "edges": [{"source": a, "target": b} for a, b in edges],
    }


@app.get("/api/alerts")
def alerts(tenant: str = "acme"):
    if _empty_sandbox(tenant):
        return []
    return T.open_alerts(_graph(tenant)).results


class RespondIn(BaseModel):
    alert: str
    tenant: str = "acme"


@app.post("/api/respond")
def respond(body: RespondIn):
    from .agents.orchestrator import handle_alert

    g = _graph(body.tenant)
    try:
        out = handle_alert(g, body.alert, llm=_llm)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"incident": out["incident"], "session": out["session"], "total_ms": out["total_ms"],
            "steps": [{k: s[k] for k in ("agent", "episode", "summary", "summary_source", "ms", "cypher")}
                      for s in out["steps"]]}


class ReportIn(BaseModel):
    text: str
    tenant: str = "acme"
    session: str = "web"
    incident: str | None = None


@app.post("/api/report")
def report(body: ReportIn, request: Request):
    """Free-text incident report -> facts in the graph -> the three agents investigate."""
    from .agents.orchestrator import handle_alert
    from .intake import ReportError, parse_report, record_report

    if _rate_limited(request.client.host if request.client else "unknown"):
        raise HTTPException(429, "Report limit reached for this hour; the demo runs on free LLM tiers.")
    g = _graph(body.tenant)
    try:
        facts = parse_report(g, body.text, _llm)
    except ReportError as exc:
        # A question typed into the report box goes to the assistant instead of failing.
        from .agents.ask import ask as run_ask
        from .intake import looks_like_question

        if looks_like_question(body.text) or "question" in str(exc).lower():
            return {"routed": "ask", **run_ask(g, body.text, _llm, body.session, body.incident)}
        raise HTTPException(422, str(exc))
    alert = record_report(g, facts, body.text)
    out = handle_alert(g, alert, llm=_llm)
    return {"incident": out["incident"], "session": out["session"], "total_ms": out["total_ms"], "facts": facts,
            "steps": [{k: s[k] for k in ("agent", "episode", "summary", "summary_source", "ms", "cypher")}
                      for s in out["steps"]]}


@app.get("/api/incidents")
def incidents(tenant: str = "acme"):
    if _empty_sandbox(tenant):
        return []
    return memory.list_incidents(_graph(tenant))


class BuildIn(BaseModel):
    text: str


@app.post("/api/sandbox/build")
def sandbox_build(body: BuildIn, request: Request):
    """Wipe the sandbox company and build a new graph from a plain-English description."""
    from .db import graph as get_graph
    from .ingest import SANDBOX, IngestError, build_from_text

    if _rate_limited(request.client.host if request.client else "unknown"):
        raise HTTPException(429, "Limit reached for this hour; the demo runs on free LLM tiers.")
    try:
        return build_from_text(get_graph(SANDBOX), body.text, _llm)
    except IngestError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/sandbox/clear")
def sandbox_clear():
    """Delete the sandbox graph entirely: the map goes blank, 0 nodes and 0 edges."""
    from .db import graph as get_graph
    from .ingest import SANDBOX

    if not _empty_sandbox(SANDBOX):
        get_graph(SANDBOX).delete()
    return {"ok": True, "tenant": SANDBOX, "nodes": 0, "edges": 0}


@app.get("/api/sandbox/example")
def sandbox_example():
    from .ingest import EXAMPLE

    return {"text": EXAMPLE}


@app.get("/api/incidents/{incident}")
def incident(incident: str, tenant: str = "acme"):
    """Everything the UI needs to show one incident: memory plus each agent's latest findings."""
    g = _graph(tenant)
    mem = memory.incident_memory(g, incident)
    if "error" in mem:
        raise HTTPException(404, mem["error"])
    for agent in ("triage", "investigator", "responder"):
        ep = memory.latest_episode(g, incident, agent)
        mem[agent] = ep["findings"] if ep else None
    _ground(g, mem["episodes"])
    return mem


_ID = re.compile(r"\[([a-z0-9][a-z0-9\-]+)\]")


def _ground(g, episodes: list[dict]) -> None:
    """For each episode: how many node ids its summary cites, and how many exist in FalkorDB."""
    ids = sorted({i for e in episodes for i in _ID.findall(e["summary"] or "")})
    found: set[str] = set()
    if ids:  # a citation may be a node id or a service name
        found = {r[0] for r in g.query("MATCH (n) WHERE n.id IN $ids RETURN n.id", {"ids": ids}).result_set}
        found |= {r[0] for r in g.query("MATCH (s:Service) WHERE s.name IN $ids RETURN s.name", {"ids": ids}).result_set}
    for e in episodes:
        cited = sorted(set(_ID.findall(e["summary"] or "")))
        e["grounding"] = {"cited": len(cited), "verified": sum(c in found for c in cited),
                          "unverified": [c for c in cited if c not in found],
                          "evidence_nodes": len(e["cites"])}


class TenantIn(BaseModel):
    tenant: str = "acme"
    session: str = "web"


@app.post("/api/incidents/{incident}/resolve")
def resolve(incident: str, body: TenantIn):
    """Confirm the cause and close the incident; the graph remembers it for next time."""
    try:
        return memory.resolve_incident(_graph(body.tenant), incident, body.session)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/incidents/{incident}/refire")
def refire(incident: str, body: TenantIn):
    """Fire a new alert like this incident's and run the agents, to show what they learned."""
    from .agents.orchestrator import handle_alert

    g = _graph(body.tenant)
    try:
        alert = memory.refire_alert(g, incident)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    out = handle_alert(g, alert, llm=_llm)
    return {"incident": out["incident"], "session": out["session"], "total_ms": out["total_ms"],
            "steps": [{k: s[k] for k in ("agent", "episode", "summary", "summary_source", "ms", "cypher")}
                      for s in out["steps"]]}


class AskIn(BaseModel):
    question: str
    tenant: str = "acme"
    session: str = "web"
    incident: str | None = None


@app.post("/api/ask")
def ask(body: AskIn, request: Request):
    from .agents.ask import ask as run_ask

    if not body.question.strip() or len(body.question) > 1500:
        raise HTTPException(400, "question must be 1-1500 characters")
    if _rate_limited(request.client.host if request.client else "unknown"):
        raise HTTPException(429, "Question limit reached for this hour; the demo runs on free LLM tiers.")
    return run_ask(_graph(body.tenant), body.question, _llm, body.session, body.incident)


@app.get("/api/tools/{name}")
def tool(name: str, request: Request, tenant: str = "acme"):
    """Run one graph tool directly, e.g. /api/tools/blast_radius?service=kafka"""
    if name not in T.TOOLS:
        raise HTTPException(404, f"unknown tool {name!r}")
    args = {k: (int(v) if v.isdigit() else v) for k, v in request.query_params.items() if k != "tenant"}
    try:
        return T.TOOLS[name](_graph(tenant), **args).to_dict()
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


def reset_data() -> list[str]:
    from .seed.generator import TENANTS, generate
    from .seed.loader import load

    now = int(time.time() * 1000)
    for tenant in TENANTS:
        load(graph(tenant), generate(tenant, now))
    return list(TENANTS)


@app.post("/api/reset")
def reset():
    """Reload the synthetic companies so the demo alert is open again."""
    if not ALLOW_RESET:
        raise HTTPException(403, "reset disabled")
    return {"ok": True, "tenants": reset_data()}

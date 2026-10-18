"""HTTP API and web UI.

Run:  uvicorn blast_radius.api:app --reload      then open http://localhost:8000
"""
import os
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
    return list_tenants()


@app.get("/api/graph")
def service_graph(tenant: str = "acme"):
    """Services, owners and dependencies, for drawing the map."""
    g = _graph(tenant)
    nodes = g.query(
        "MATCH (t:Team)-[:OWNS]->(s:Service) RETURN s.name, s.tier, s.language, t.name ORDER BY s.name"
    ).result_set
    edges = g.query("MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) RETURN a.name, b.name").result_set
    crit = {r["service"]: r["pagerank"] for r in T.critical_services(g, 50).results}
    return {
        "nodes": [{"id": n, "tier": t, "language": l, "team": tm, "pagerank": crit.get(n, 0)} for n, t, l, tm in nodes],
        "edges": [{"source": a, "target": b} for a, b in edges],
    }


@app.get("/api/alerts")
def alerts(tenant: str = "acme"):
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
        raise HTTPException(422, str(exc))
    alert = record_report(g, facts, body.text)
    out = handle_alert(g, alert, llm=_llm)
    return {"incident": out["incident"], "session": out["session"], "total_ms": out["total_ms"], "facts": facts,
            "steps": [{k: s[k] for k in ("agent", "episode", "summary", "summary_source", "ms", "cypher")}
                      for s in out["steps"]]}


@app.get("/api/incidents")
def incidents(tenant: str = "acme"):
    return memory.list_incidents(_graph(tenant))


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
    return mem


class AskIn(BaseModel):
    question: str
    tenant: str = "acme"
    session: str = "web"
    incident: str | None = None


@app.post("/api/ask")
def ask(body: AskIn, request: Request):
    from .agents.ask import ask as run_ask

    if not body.question.strip() or len(body.question) > 500:
        raise HTTPException(400, "question must be 1-500 characters")
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

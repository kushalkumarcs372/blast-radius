"""Runs Triage -> Investigator -> Responder on one alert.

The orchestrator only decides the order. Each agent reads the previous agent's
Episode from the graph, so the same incident can be resumed in a later session.
"""
import time
import uuid

from .. import memory
from . import investigator, responder, triage


def new_session() -> str:
    return f"session-{uuid.uuid4().hex[:8]}"


def handle_alert(g, alert_id: str, llm=None, session: str | None = None) -> dict:
    session = session or new_session()
    memory.ensure_agents(g)
    steps = []
    t0 = time.perf_counter()
    tri = triage.run(g, alert_id, session, llm)
    steps.append({**tri, "ms": round((time.perf_counter() - t0) * 1000)})
    for agent in (investigator, responder):
        t = time.perf_counter()
        out = agent.run(g, tri["incident"], session, llm)
        steps.append({**out, "ms": round((time.perf_counter() - t) * 1000)})
    return {"session": session, "incident": tri["incident"], "steps": steps,
            "total_ms": round((time.perf_counter() - t0) * 1000)}

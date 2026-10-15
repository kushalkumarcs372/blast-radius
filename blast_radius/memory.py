"""Agent memory stored in the graph (Track 02).

  (Agent)-[:RECORDED]->(Episode)-[:ABOUT]->(Incident)
  (Episode)-[:HANDED_OFF_TO]->(Agent)
  (Episode)-[:CITES]->(Service | Deployment | Person | Incident | Runbook | Document | Alert)
  (Incident)-[:SUSPECTED_CAUSE {confidence}]->(Deployment)
  (Incident)-[:SIMILAR_TO {score}]->(Incident)

Episodic memory  = Episode nodes: what each agent found, when, and why.
Semantic memory  = the company graph itself.
Procedural memory = Runbook nodes and SIMILAR_TO links to past fixes.

Agents never pass findings to each other in Python. Each one writes an Episode
and the next agent reads it back from the graph, so any later session (or a
different process) sees the same shared state.
"""
import json
import time
import uuid

AGENTS = {
    "triage": "Sizes the impact of an alert and lists suspect deploys",
    "investigator": "Compares with past incidents and picks the likely cause",
    "responder": "Finds who should act and proposes the fix",
    "assistant": "Answers follow-up questions from engineers",
}

CITE_LABELS = ("Service", "Deployment", "Person", "Incident", "Runbook", "Document", "Alert")


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_agents(g) -> None:
    for name, role in AGENTS.items():
        g.query("MERGE (a:Agent {name: $name}) SET a.role = $role", {"name": name, "role": role})
    for stmt in ("CREATE INDEX FOR (e:Episode) ON (e.id)", "CREATE INDEX FOR (e:Episode) ON (e.session)"):
        try:
            g.query(stmt)
        except Exception as exc:
            if "already" not in str(exc).lower():
                raise


def open_incident(g, alert_id: str) -> dict:
    """Create (or reuse) the live incident for an alert. Idempotent: re-running reuses it."""
    rows = g.query(
        "MATCH (a:Alert {id: $alert})-[:FIRED_ON]->(s:Service) RETURN a.signal, a.severity, a.ts, s.name",
        {"alert": alert_id},
    ).result_set
    if not rows:
        raise ValueError(f"unknown alert: {alert_id}")
    signal, severity, ts, service = rows[0]
    inc_id = f"inc-{alert_id}"
    g.query(
        "MATCH (a:Alert {id: $alert})-[:FIRED_ON]->(s:Service) "
        "MERGE (i:Incident {id: $inc}) "
        "ON CREATE SET i.title = $title, i.severity = $sev, i.status = 'investigating', "
        "              i.started = $ts, i.service = s.name "
        "MERGE (i)-[:TRIGGERED_BY]->(a) "
        "MERGE (i)-[:AFFECTED]->(s)",
        {"alert": alert_id, "inc": inc_id, "title": f"{signal} on {service}", "sev": severity, "ts": ts},
    )
    return {"incident": inc_id, "alert": alert_id, "signal": signal, "severity": severity,
            "alert_ts": ts, "service": service}


def record_episode(g, *, agent: str, incident: str | None, session: str, kind: str, summary: str,
                   findings: dict, cites: list[tuple[str, str]] = (), handoff_to: str | None = None,
                   confidence: float | None = None) -> str:
    """Write one Episode and link it to its agent, incident, evidence and next agent."""
    ep_id = f"ep-{uuid.uuid4().hex[:12]}"
    g.query(
        "MATCH (ag:Agent {name: $agent}) "
        "CREATE (ag)-[:RECORDED]->(e:Episode {id: $id, agent: $agent, session: $session, kind: $kind, "
        "ts: $ts, summary: $summary, findings: $findings})",
        {"agent": agent, "id": ep_id, "session": session, "kind": kind, "ts": now_ms(),
         "summary": summary, "findings": json.dumps(findings, default=str)},
    )
    if confidence is not None:
        g.query("MATCH (e:Episode {id: $e}) SET e.confidence = $c", {"e": ep_id, "c": float(confidence)})
    if incident:
        g.query("MATCH (e:Episode {id: $e}), (i:Incident {id: $i}) CREATE (e)-[:ABOUT]->(i)",
                {"e": ep_id, "i": incident})
    if handoff_to:
        g.query("MATCH (e:Episode {id: $e}), (a:Agent {name: $a}) CREATE (e)-[:HANDED_OFF_TO]->(a)",
                {"e": ep_id, "a": handoff_to})
    by_label: dict[str, list[str]] = {}
    for label, node_id in cites:
        if label not in CITE_LABELS:
            raise ValueError(f"cannot cite label {label}")
        by_label.setdefault(label, []).append(node_id)
    for label, ids in by_label.items():
        g.query(
            f"MATCH (e:Episode {{id: $e}}) UNWIND $ids AS nid MATCH (n:{label} {{id: nid}}) "
            "MERGE (e)-[:CITES]->(n)",
            {"e": ep_id, "ids": sorted(set(ids))},
        )
    return ep_id


def latest_episode(g, incident: str, agent: str) -> dict | None:
    """What the given agent last recorded on this incident (used for handoffs)."""
    rows = g.query(
        "MATCH (:Agent {name: $agent})-[:RECORDED]->(e:Episode)-[:ABOUT]->(:Incident {id: $inc}) "
        "RETURN e.id, e.summary, e.findings, e.confidence, e.ts AS ts ORDER BY ts DESC LIMIT 1",
        {"agent": agent, "inc": incident},
    ).result_set
    if not rows:
        return None
    ep, summary, findings, conf, ts = rows[0]
    return {"episode": ep, "summary": summary, "findings": json.loads(findings), "confidence": conf, "ts": ts}


def incident_memory(g, incident: str) -> dict:
    """Everything agents have recorded on an incident, oldest first, with what each cited."""
    head = g.query(
        "MATCH (i:Incident {id: $inc}) RETURN i.title, i.status, i.severity, i.service, i.started",
        {"inc": incident},
    ).result_set
    if not head:
        return {"incident": incident, "error": "unknown incident"}
    title, status, sev, service, started = head[0]
    rows = g.query(
        "MATCH (ag:Agent)-[:RECORDED]->(e:Episode)-[:ABOUT]->(:Incident {id: $inc}) "
        "OPTIONAL MATCH (e)-[:HANDED_OFF_TO]->(nx:Agent) "
        "OPTIONAL MATCH (e)-[:CITES]->(c) "
        "RETURN e.id, ag.name, e.kind, e.session, e.ts AS ts, e.summary, e.confidence, nx.name, "
        "collect(DISTINCT c.id) ORDER BY ts",
        {"inc": incident},
    ).result_set
    episodes = [
        {"episode": e, "agent": a, "kind": k, "session": s, "ts": ts, "summary": summ,
         "confidence": conf, "handed_off_to": nx, "cites": sorted(c for c in cites if c)}
        for e, a, k, s, ts, summ, conf, nx, cites in rows
    ]
    suspects = g.query(
        "MATCH (:Incident {id: $inc})-[r:SUSPECTED_CAUSE]->(d:Deployment)-[:CHANGED]->(s:Service) "
        "RETURN d.id, d.summary, s.name, r.confidence AS conf ORDER BY conf DESC",
        {"inc": incident},
    ).result_set
    return {
        "incident": incident, "title": title, "status": status, "severity": sev, "service": service,
        "started": started, "episodes": episodes,
        "suspected_causes": [{"deployment": d, "summary": su, "service": s, "confidence": c}
                             for d, su, s, c in suspects],
    }


def list_incidents(g, status: str | None = None) -> list[dict]:
    q = ("MATCH (i:Incident) WHERE i.status <> 'resolved' "
         "OPTIONAL MATCH (e:Episode)-[:ABOUT]->(i) "
         "RETURN i.id, i.title, i.status, i.started AS started, count(e) ORDER BY started DESC")
    if status == "all":
        q = q.replace("WHERE i.status <> 'resolved' ", "")
    return [{"incident": i, "title": t, "status": s, "started": st, "episodes": n}
            for i, t, s, st, n in g.query(q).result_set]

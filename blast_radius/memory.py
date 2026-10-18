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
        "collect(DISTINCT c.id), e.findings ORDER BY ts",
        {"inc": incident},
    ).result_set
    episodes = [
        {"episode": e, "agent": a, "kind": k, "session": s, "ts": ts, "summary": summ,
         "confidence": conf, "handed_off_to": nx, "cites": sorted(c for c in cites if c),
         "summary_source": json.loads(f or "{}").get("summary_source")}
        for e, a, k, s, ts, summ, conf, nx, cites, f in rows
    ]
    suspects = g.query(
        "MATCH (:Incident {id: $inc})-[r:SUSPECTED_CAUSE]->(d:Deployment)-[:CHANGED]->(s:Service) "
        "RETURN d.id, d.summary, s.name, r.confidence AS conf ORDER BY conf DESC",
        {"inc": incident},
    ).result_set
    docs = g.query("MATCH (d:Document)-[:MENTIONS]->(:Incident {id: $inc}) RETURN d.id, d.title, d.type",
                   {"inc": incident}).result_set
    # Facts recorded on the incident itself: who fixed it, what caused it, what it hit.
    # Past incidents (seeded or built from text) have these but no agent episodes.
    fixers = [r[0] for r in g.query(
        "MATCH (p:Person)-[:RESOLVED]->(:Incident {id: $inc}) RETURN p.name", {"inc": incident}).result_set]
    causes = [{"deployment": d, "summary": su, "service": s} for d, su, s in g.query(
        "MATCH (:Incident {id: $inc})-[:CAUSED_BY]->(d:Deployment) OPTIONAL MATCH (d)-[:CHANGED]->(s:Service) "
        "RETURN d.id, d.summary, s.name", {"inc": incident}).result_set]
    affected = [r[0] for r in g.query(
        "MATCH (:Incident {id: $inc})-[:AFFECTED]->(s:Service) RETURN s.name", {"inc": incident}).result_set]
    return {
        "incident": incident, "title": title, "status": status, "severity": sev, "service": service,
        "started": started, "resolved_by": fixers, "caused_by": causes, "affected_services": affected,
        "episodes": episodes,
        "documents": [{"document": d, "title": t, "type": ty} for d, t, ty in docs],
        "suspected_causes": [{"deployment": d, "summary": su, "service": s, "confidence": c}
                             for d, su, s, c in suspects],
    }


def list_incidents(g, status: str | None = None) -> list[dict]:
    """Incidents the agents have worked on, plus any still open. Newest first."""
    q = ("MATCH (i:Incident) OPTIONAL MATCH (e:Episode)-[:ABOUT]->(i) "
         "WITH i, count(e) AS n WHERE i.status <> 'resolved' OR n > 0 "
         "RETURN i.id, i.title, i.status, i.started AS started, n ORDER BY started DESC")
    if status == "all":
        q = ("MATCH (i:Incident) OPTIONAL MATCH (e:Episode)-[:ABOUT]->(i) "
             "RETURN i.id, i.title, i.status, i.started AS started, count(e) ORDER BY started DESC")
    return [{"incident": i, "title": t, "status": s, "started": st, "episodes": n}
            for i, t, s, st, n in g.query(q).result_set]


def resolve_incident(g, incident: str, session: str, cause_deployment: str | None = None,
                     resolved_by: str | None = None) -> dict:
    """Close the loop: write what fixed the incident back into the graph.

    The confirmed cause becomes CAUSED_BY, the person who fixed it gets a RESOLVED edge,
    a postmortem Document is linked, and the Responder records a resolution Episode.
    After this, similar_incidents and fix_history find this incident like any past one,
    so the next alert of this kind is investigated with this experience (procedural memory).
    """
    head = g.query("MATCH (i:Incident {id: $i}) RETURN i.title, i.status, i.service", {"i": incident}).result_set
    if not head:
        raise ValueError(f"unknown incident: {incident}")
    title, status, service = head[0]
    if status == "resolved":
        raise ValueError(f"{incident} is already resolved")
    if cause_deployment is None:  # default: the Investigator's top suspect
        rows = g.query("MATCH (:Incident {id: $i})-[r:SUSPECTED_CAUSE]->(d:Deployment) "
                       "RETURN d.id, r.confidence AS c ORDER BY c DESC LIMIT 1", {"i": incident}).result_set
        cause_deployment = rows[0][0] if rows else None
    if resolved_by is None:  # default: who the Responder paged
        ep = latest_episode(g, incident, "responder")
        name = (ep or {}).get("findings", {}).get("on_call")
        rows = g.query("MATCH (p:Person {name: $n}) RETURN p.id LIMIT 1", {"n": name}).result_set if name else []
        resolved_by = rows[0][0] if rows else None

    ts = now_ms()
    g.query("MATCH (i:Incident {id: $i}) SET i.status = 'resolved', i.resolved = $ts", {"i": incident, "ts": ts})
    cites: list[tuple[str, str]] = [("Incident", incident)]
    if cause_deployment:
        g.query("MATCH (i:Incident {id: $i}), (d:Deployment {id: $d}) MERGE (i)-[:CAUSED_BY]->(d)",
                {"i": incident, "d": cause_deployment})
        cites.append(("Deployment", cause_deployment))
    if resolved_by:
        g.query("MATCH (p:Person {id: $p}), (i:Incident {id: $i}) MERGE (p)-[:RESOLVED]->(i)",
                {"p": resolved_by, "i": incident})
        cites.append(("Person", resolved_by))
    doc_id = f"doc-pm-{incident}"
    g.query("MATCH (i:Incident {id: $i}) MERGE (d:Document {id: $d}) "
            "SET d.title = $t, d.type = 'postmortem', d.source = 'blast-radius' MERGE (d)-[:MENTIONS]->(i)",
            {"i": incident, "d": doc_id, "t": f"Postmortem: {title}"})
    cites.append(("Document", doc_id))
    fixer = g.query("MATCH (p:Person {id: $p}) RETURN p.name", {"p": resolved_by}).result_set if resolved_by else []
    summary_text = (f"Resolved [{incident}]. Confirmed cause: "
                    f"{('[' + cause_deployment + ']') if cause_deployment else 'not confirmed'}; "
                    f"fixed by {fixer[0][0] if fixer else 'unknown'}. Postmortem [{doc_id}] linked. "
                    f"Future alerts on {service} will be compared with this incident.")
    record_episode(g, agent="responder", incident=incident, session=session, kind="resolution",
                   summary=summary_text,
                   findings={"cause_deployment": cause_deployment, "resolved_by": resolved_by, "postmortem": doc_id},
                   cites=cites)
    return {"incident": incident, "status": "resolved", "cause_deployment": cause_deployment,
            "resolved_by": resolved_by, "postmortem": doc_id, "summary": summary_text}


def refire_alert(g, incident: str) -> str:
    """Fire a new alert with the same signal on the same service, to see what the agents learned."""
    rows = g.query("MATCH (:Incident {id: $i})-[:TRIGGERED_BY]->(a:Alert)-[:FIRED_ON]->(s:Service) "
                   "RETURN a.signal, a.severity, s.name", {"i": incident}).result_set
    if not rows:
        raise ValueError(f"unknown incident: {incident}")
    signal, severity, service = rows[0]
    alert_id = f"alert-again-{uuid.uuid4().hex[:8]}"
    g.query("MATCH (s:Service {name: $svc}) "
            "CREATE (:Alert {id: $id, signal: $sig, severity: $sev, ts: $ts, service: $svc})-[:FIRED_ON]->(s)",
            {"svc": service, "id": alert_id, "sig": signal, "sev": severity, "ts": now_ms()})
    return alert_id

"""Graph tools: the fixed Cypher queries the agents are allowed to call.

Every tool returns a ToolResult holding the rows, the graph paths that support
them (``evidence``) and the exact Cypher that ran, so an answer can always be
traced back to the graph.
"""
import time
from dataclasses import asdict, dataclass, field

HOUR_MS = 3_600_000
MAX_HOPS_LIMIT = 6


@dataclass
class ToolResult:
    tool: str
    params: dict
    results: list[dict]
    evidence: list[list[str]] = field(default_factory=list)
    cypher: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _hops(max_hops: int) -> int:
    # Variable-length bounds cannot be query parameters, so validate before formatting.
    if not isinstance(max_hops, int) or not 1 <= max_hops <= MAX_HOPS_LIMIT:
        raise ValueError(f"max_hops must be an int between 1 and {MAX_HOPS_LIMIT}")
    return max_hops


def _rows(g, query: str, params: dict | None = None) -> list[list]:
    return g.query(query, params or {}).result_set


def _service_exists(g, name: str) -> bool:
    return bool(_rows(g, "MATCH (s:Service {name: $name}) RETURN count(s)", {"name": name})[0][0])


def _missing(tool: str, params: dict, name: str) -> ToolResult:
    return ToolResult(tool, params, [], error=f"unknown service: {name}")


# --- Q1: what breaks if X goes down? ------------------------------------------

def blast_radius(g, service: str, max_hops: int = 4) -> ToolResult:
    params = {"service": service, "max_hops": max_hops}
    if not _service_exists(g, service):
        return _missing("blast_radius", params, service)
    q = (
        f"MATCH p = (up:Service)-[:DEPENDS_ON*1..{_hops(max_hops)}]->(s:Service {{name: $service}}) "
        "RETURN up.name, up.tier, length(p), [n IN nodes(p) | n.name]"
    )
    best: dict[str, tuple] = {}
    for name, tier, hops, path in _rows(g, q, {"service": service}):
        if name not in best or hops < best[name][1]:
            best[name] = (tier, hops, path)
    ordered = sorted(best.items(), key=lambda kv: (kv[1][1], kv[0]))
    return ToolResult(
        "blast_radius", params,
        [{"service": n, "tier": t, "hops": h} for n, (t, h, _) in ordered],
        evidence=[p for _, (_, _, p) in ordered],
        cypher=[q],
    )


# --- Q2: what does X depend on? -----------------------------------------------

def dependencies(g, service: str, max_hops: int = 4) -> ToolResult:
    params = {"service": service, "max_hops": max_hops}
    if not _service_exists(g, service):
        return _missing("dependencies", params, service)
    q = (
        f"MATCH p = (s:Service {{name: $service}})-[:DEPENDS_ON*1..{_hops(max_hops)}]->(d:Service) "
        "RETURN d.name, d.tier, length(p), [n IN nodes(p) | n.name]"
    )
    best: dict[str, tuple] = {}
    for name, tier, hops, path in _rows(g, q, {"service": service}):
        if name not in best or hops < best[name][1]:
            best[name] = (tier, hops, path)
    ordered = sorted(best.items(), key=lambda kv: (kv[1][1], kv[0]))
    return ToolResult(
        "dependencies", params,
        [{"service": n, "tier": t, "hops": h} for n, (t, h, _) in ordered],
        evidence=[p for _, (_, _, p) in ordered],
        cypher=[q],
    )


# --- Q3: which deploys touched X or its neighbours recently? ------------------

def recent_deploys(g, service: str, hours: int = 24, now_ms: int | None = None, radius: int = 2) -> ToolResult:
    params = {"service": service, "hours": hours, "radius": radius}
    if not _service_exists(g, service):
        return _missing("recent_deploys", params, service)
    until = now_ms if now_ms is not None else int(time.time() * 1000)
    since = until - hours * HOUR_MS
    q_self = (
        "MATCH (a:Person)-[:AUTHORED]->(d:Deployment)-[:CHANGED]->(s:Service {name: $service}) "
        "WHERE d.ts >= $since AND d.ts <= $until "
        "RETURN d.id, d.version, d.ts, d.summary, s.name, a.name, 0, [s.name]"
    )
    q_near = (
        f"MATCH p = (s:Service {{name: $service}})-[:DEPENDS_ON*1..{_hops(radius)}]-(n:Service) "
        "MATCH (a:Person)-[:AUTHORED]->(d:Deployment)-[:CHANGED]->(n) "
        "WHERE d.ts >= $since AND d.ts <= $until "
        "RETURN d.id, d.version, d.ts, d.summary, n.name, a.name, length(p), [x IN nodes(p) | x.name]"
    )
    found: dict[str, tuple] = {}
    for q in (q_self, q_near):
        for dep_id, version, ts, summary, svc, author, hops, path in _rows(
            g, q, {"service": service, "since": since, "until": until}
        ):
            if dep_id not in found or hops < found[dep_id][0]["hops"]:
                found[dep_id] = ({
                    "deployment": dep_id, "version": version, "ts": ts, "summary": summary,
                    "service": svc, "author": author, "hops": hops,
                }, path)
    ordered = sorted(found.values(), key=lambda x: -x[0]["ts"])
    return ToolResult(
        "recent_deploys", params,
        [r for r, _ in ordered],
        evidence=[p for _, p in ordered],
        cypher=[q_self, q_near],
    )


# --- Q4: which services are most critical? ------------------------------------

def critical_services(g, limit: int = 10) -> ToolResult:
    q_rank = (
        "CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score "
        "RETURN node.name, score ORDER BY score DESC LIMIT $limit"
    )
    q_deps = (
        "MATCH (u:Service)-[:DEPENDS_ON*1..6]->(s:Service) "
        "RETURN s.name, count(DISTINCT u)"
    )
    dependents = {name: n for name, n in _rows(g, q_deps)}
    results = [
        {"service": name, "pagerank": round(score, 4), "transitive_dependents": dependents.get(name, 0)}
        for name, score in _rows(g, q_rank, {"limit": limit})
    ]
    return ToolResult("critical_services", {"limit": limit}, results, cypher=[q_rank, q_deps])


# --- Q5: how are A and B connected? -------------------------------------------

def connection_path(g, source: str, target: str) -> ToolResult:
    params = {"source": source, "target": target}
    for name in (source, target):
        if not _service_exists(g, name):
            return _missing("connection_path", params, name)
    q_short = (
        "MATCH (a:Service {name: $a}), (b:Service {name: $b}) "
        "WITH shortestPath((a)-[:DEPENDS_ON*]->(b)) AS p "
        "WHERE p IS NOT NULL "
        "RETURN [n IN nodes(p) | n.name], length(p)"
    )
    q_any = (
        "MATCH p = (a:Service {name: $a})-[:DEPENDS_ON*1..4]-(b:Service {name: $b}) "
        "RETURN [n IN nodes(p) | n.name], length(p) ORDER BY length(p) LIMIT 1"
    )
    # Prefer a directed dependency chain either way; fall back to any short undirected link.
    for q, a, b, kind in (
        (q_short, source, target, "depends_on"),
        (q_short, target, source, "depended_on_by"),
        (q_any, source, target, "related"),
    ):
        rows = _rows(g, q, {"a": a, "b": b})
        if rows:
            path, hops = rows[0]
            if kind == "depended_on_by":
                path = list(reversed(path))
            return ToolResult(
                "connection_path", params,
                [{"relation": kind, "hops": hops}],
                evidence=[path], cypher=[q],
            )
    return ToolResult("connection_path", params, [], cypher=[q_short, q_any],
                      error="no connection within 4 hops")


# --- Q8: who owns X and who is on call? ---------------------------------------

def owners(g, service: str) -> ToolResult:
    params = {"service": service}
    if not _service_exists(g, service):
        return _missing("owners", params, service)
    q = (
        "MATCH (t:Team)-[:OWNS]->(s:Service {name: $service}) "
        "OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t) "
        "RETURN t.name, t.channel, p.name, p.role, p.on_call, p.id"
    )
    rows = _rows(g, q, {"service": service})
    if not rows:
        return ToolResult("owners", params, [], cypher=[q], error="no owning team recorded")
    team, channel = rows[0][0], rows[0][1]
    people = sorted(
        ({"name": n, "role": r, "on_call": bool(oc), "id": pid} for _, _, n, r, oc, pid in rows if n),
        key=lambda p: (not p["on_call"], p["name"]),
    )
    return ToolResult(
        "owners", params,
        [{"team": team, "channel": channel, "members": people}],
        evidence=[[p["name"], team, service] for p in people],
        cypher=[q],
    )


# --- Q9: who has fixed problems on X before? ----------------------------------

def fix_history(g, service: str) -> ToolResult:
    params = {"service": service}
    if not _service_exists(g, service):
        return _missing("fix_history", params, service)
    q = (
        "MATCH (p:Person)-[:RESOLVED]->(i:Incident)-[:AFFECTED]->(s:Service {name: $service}) "
        "OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team) "
        "RETURN p.name AS person, t.name, count(DISTINCT i) AS fixes, collect(DISTINCT i.id), p.id "
        "ORDER BY fixes DESC, person"
    )
    rows = _rows(g, q, {"service": service})
    return ToolResult(
        "fix_history", params,
        [{"person": p, "team": t, "fixes": n, "incidents": sorted(ids), "id": pid} for p, t, n, ids, pid in rows],
        evidence=[[p, i, service] for p, _, _, ids, _ in rows for i in sorted(ids)],
        cypher=[q],
    )


# --- alerts that have no incident yet -----------------------------------------

def open_alerts(g) -> ToolResult:
    q = (
        "MATCH (a:Alert)-[:FIRED_ON]->(s:Service) "
        "OPTIONAL MATCH (i:Incident)-[:TRIGGERED_BY]->(a) "
        "WITH a, s, i WHERE i IS NULL "
        "RETURN a.id, a.signal, a.severity, a.ts, s.name ORDER BY a.ts DESC"
    )
    rows = _rows(g, q)
    return ToolResult(
        "open_alerts", {},
        [{"alert": a, "signal": sig, "severity": sev, "ts": ts, "service": s} for a, sig, sev, ts, s in rows],
        evidence=[[a, s] for a, _, _, _, s in rows],
        cypher=[q],
    )


# --- Q6: which past incidents look like this one? -----------------------------

def _words(text: str | None) -> set[str]:
    return {w for w in (text or "").lower().replace(":", " ").split() if len(w) > 3}


def similar_incidents(g, service: str, signal: str = "", suspect_summary: str = "",
                      suspect_service: str = "", limit: int = 5) -> ToolResult:
    """Past resolved incidents that affected this service, scored by how much they resemble now.

    score = 3 if the alert signal matches + 2 if the cause was a deploy to the same service
            as today's suspect + 2 x word overlap between the two change summaries.
    """
    params = {"service": service, "signal": signal, "suspect_service": suspect_service}
    if not _service_exists(g, service):
        return _missing("similar_incidents", params, service)
    q = (
        "MATCH (past:Incident)-[:AFFECTED]->(s:Service {name: $service}) "
        "WHERE past.status = 'resolved' "
        "OPTIONAL MATCH (past)-[:TRIGGERED_BY]->(al:Alert) "
        "OPTIONAL MATCH (past)-[:CAUSED_BY]->(d:Deployment)-[:CHANGED]->(ds:Service) "
        "OPTIONAL MATCH (fixer:Person)-[:RESOLVED]->(past) "
        "OPTIONAL MATCH (doc:Document)-[:MENTIONS]->(past) "
        "RETURN past.id, past.title, past.started, past.resolved, al.signal, "
        "d.id, d.summary, ds.name, fixer.name, doc.title"
    )
    scored = []
    suspect_words = _words(suspect_summary)
    for inc, title, started, resolved, sig, dep, dsum, dsvc, fixer, doc in _rows(g, q, {"service": service}):
        overlap = 0.0
        if suspect_words and dsum:
            w = _words(dsum)
            overlap = len(w & suspect_words) / max(1, len(w | suspect_words))
        score = 3 * (bool(signal) and sig == signal) + 2 * (bool(suspect_service) and dsvc == suspect_service) + 2 * overlap
        scored.append(({
            "incident": inc, "title": title, "started": started,
            "minutes_to_resolve": (resolved - started) // 60_000 if resolved else None,
            "signal": sig, "cause_deployment": dep, "cause_summary": dsum, "cause_service": dsvc,
            "resolved_by": fixer, "postmortem": doc, "score": round(score, 2),
        }, [x for x in (fixer, inc, service) if x]))
    scored.sort(key=lambda x: (-x[0]["score"], -x[0]["started"]))
    top = scored[:limit]
    return ToolResult("similar_incidents", params, [r for r, _ in top],
                      evidence=[p for _, p in top], cypher=[q])


# --- runbook for a service ----------------------------------------------------

def runbook(g, service: str) -> ToolResult:
    params = {"service": service}
    if not _service_exists(g, service):
        return _missing("runbook", params, service)
    q = "MATCH (r:Runbook)-[:FOR]->(s:Service {name: $service}) RETURN r.id, r.title, r.steps"
    rows = _rows(g, q, {"service": service})
    return ToolResult("runbook", params,
                      [{"runbook": i, "title": t, "steps": st} for i, t, st in rows],
                      evidence=[[i, service] for i, _, _ in rows], cypher=[q])


# --- Q10: why is X built this way? --------------------------------------------

def decision_trace(g, service: str) -> ToolResult:
    params = {"service": service}
    if not _service_exists(g, service):
        return _missing("decision_trace", params, service)
    q = (
        "MATCH (d:Decision)-[:ABOUT]->(s:Service {name: $service}) "
        "MATCH (d)-[:SOURCED_FROM]->(doc:Document) "
        "RETURN d.id, d.title, d.summary, doc.id, doc.title, doc.source ORDER BY d.date DESC"
    )
    rows = _rows(g, q, {"service": service})
    return ToolResult(
        "decision_trace", params,
        [{"decision": d, "title": t, "summary": s, "document": di, "document_title": dt, "source": src}
         for d, t, s, di, dt, src in rows],
        evidence=[[service, d, di] for d, _, _, di, _, _ in rows], cypher=[q],
    )


TOOLS = {
    "open_alerts": open_alerts,
    "similar_incidents": similar_incidents,
    "runbook": runbook,
    "decision_trace": decision_trace,
    "blast_radius": blast_radius,
    "dependencies": dependencies,
    "recent_deploys": recent_deploys,
    "critical_services": critical_services,
    "connection_path": connection_path,
    "owners": owners,
    "fix_history": fix_history,
}

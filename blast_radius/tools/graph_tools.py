"""Graph tools: the fixed Cypher queries the agents are allowed to call.

Every tool returns a ToolResult holding the rows, the graph paths that support
them (``evidence``) and the exact Cypher that ran, so an answer can always be
traced back to the graph.
"""
import re
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
    extra: dict = field(default_factory=dict)  # context that is not a row, e.g. unaffected services

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
    q_all = "MATCH (s:Service) RETURN s.name ORDER BY s.name"
    everything = [r[0] for r in _rows(g, q_all)]
    unaffected = [n for n in everything if n not in best and n != service]
    return ToolResult(
        "blast_radius", params,
        [{"service": n, "tier": t, "hops": h} for n, (t, h, _) in ordered],
        evidence=[p for _, (_, _, p) in ordered],
        cypher=[q, q_all],
        extra={"total_services": len(everything), "affected_count": len(ordered),
               "unaffected_services": unaffected},
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
            "signal": sig, "cause_deployment": dep, "change_that_caused_it": dsum, "cause_service": dsvc,
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
    # A decision counts if it is attached to the service, or its text names the service as a
    # whole word (ADR-003 is about `payments` but explains why `postgres-payments` exists;
    # `auth` must not match "authorisation"). The word match runs in Python on the few rows returned.
    q = (
        "MATCH (d:Decision)-[:ABOUT]->(about:Service) "
        "MATCH (d)-[:SOURCED_FROM]->(doc:Document) "
        "RETURN d.id, d.title, d.summary, doc.id, doc.title, doc.source, about.name, d.date AS date "
        "ORDER BY date DESC"
    )
    word = re.compile(rf"(?<![\w-]){re.escape(service)}(?![\w-])")
    rows = [r for r in _rows(g, q) if r[6] == service or word.search(r[2] or "")]
    return ToolResult(
        "decision_trace", params,
        [{"decision": d, "title": t, "summary": s, "document": di, "document_title": dt, "source": src,
          "about_service": ab} for d, t, s, di, dt, src, ab, _ in rows],
        evidence=[[service, d, di] for d, _, _, di, _, _, _, _ in rows], cypher=[q],
    )


# --- what if we cut one dependency? -------------------------------------------

def what_if_cut(g, service: str, cut_from: str, cut_to: str, max_hops: int = 4) -> ToolResult:
    """Blast radius of `service` if the edge cut_from -> cut_to stopped carrying failures
    (a circuit breaker, a fallback cache, or removing the dependency).

    The graph returns every dependency path into `service`; a service stays at risk only if
    at least one of its paths avoids the cut edge.
    """
    params = {"service": service, "cut_from": cut_from, "cut_to": cut_to, "max_hops": max_hops}
    for name in (service, cut_from, cut_to):
        if not _service_exists(g, name):
            return _missing("what_if_cut", params, name)
    q_edge = "MATCH (:Service {name: $a})-[r:DEPENDS_ON]->(:Service {name: $b}) RETURN count(r)"
    if not _rows(g, q_edge, {"a": cut_from, "b": cut_to})[0][0]:
        return ToolResult("what_if_cut", params, [], cypher=[q_edge],
                          error=f"{cut_from} does not depend on {cut_to}")
    q = (
        f"MATCH p = (up:Service)-[:DEPENDS_ON*1..{_hops(max_hops)}]->(s:Service {{name: $service}}) "
        "RETURN up.name, up.tier, length(p), [n IN nodes(p) | n.name]"
    )
    before: dict[str, tuple] = {}
    after: dict[str, tuple] = {}
    for name, tier, hops, path in _rows(g, q, {"service": service}):
        uses_cut = any(a == cut_from and b == cut_to for a, b in zip(path, path[1:]))
        for bucket in ((before,) if uses_cut else (before, after)):
            if name not in bucket or hops < bucket[name][1]:
                bucket[name] = (tier, hops, path)
    saved = sorted(set(before) - set(after))
    edge = lambda d: sorted(n for n, (t, _, _) in d.items() if t == "edge")  # noqa: E731
    result = {
        "cut": f"{cut_from} -> {cut_to}",
        "at_risk_before": len(before), "at_risk_after": len(after),
        "customer_facing_before": len(edge(before)), "customer_facing_after": len(edge(after)),
        "saved": saved,
        "still_at_risk": sorted(after),
    }
    return ToolResult("what_if_cut", params, [result],
                      evidence=[after[n][2] for n in sorted(after)], cypher=[q_edge, q])


def breaker_advice(g, service: str, max_hops: int = 4, limit: int = 3) -> ToolResult:
    """Which single dependency, if it had a circuit breaker, protects the most from a `service` outage?

    One traversal returns every path into `service`; for each edge on those paths we count the
    services (customer-facing first) that would no longer have any path avoiding it.
    """
    params = {"service": service, "max_hops": max_hops}
    if not _service_exists(g, service):
        return _missing("breaker_advice", params, service)
    q = (
        f"MATCH p = (up:Service)-[:DEPENDS_ON*1..{_hops(max_hops)}]->(s:Service {{name: $service}}) "
        "RETURN up.name, up.tier, [n IN nodes(p) | n.name]"
    )
    paths: dict[str, list] = {}
    tier: dict[str, str] = {}
    for name, t, path in _rows(g, q, {"service": service}):
        paths.setdefault(name, []).append(list(zip(path, path[1:])))
        tier[name] = t
    edges = sorted({e for plist in paths.values() for p in plist for e in p})
    ranked = []
    for e in edges:
        saved = sorted(n for n, plist in paths.items() if all(e in p for p in plist))
        if saved:
            ranked.append({"cut": f"{e[0]} -> {e[1]}", "cut_from": e[0], "cut_to": e[1],
                           "saves": len(saved), "customer_facing_saved": sum(tier[n] == "edge" for n in saved),
                           "saved": saved})
    ranked.sort(key=lambda r: (-r["customer_facing_saved"], -r["saves"], r["cut"]))
    top = ranked[:limit]
    return ToolResult("breaker_advice", params, top, evidence=[[r["cut_from"], r["cut_to"]] for r in top], cypher=[q])


# --- inventory and documents ----------------------------------------------------

def list_services(g) -> ToolResult:
    """Every service with its tier, owning team and number of direct dependencies."""
    q = ("MATCH (s:Service) OPTIONAL MATCH (t:Team)-[:OWNS]->(s) OPTIONAL MATCH (s)-[:DEPENDS_ON]->(d:Service) "
         "RETURN s.name AS name, s.tier, t.name, count(DISTINCT d) ORDER BY name")
    rows = _rows(g, q)
    return ToolResult("list_services", {},
                      [{"service": n, "tier": t, "team": tm, "depends_on": k} for n, t, tm, k in rows],
                      cypher=[q], extra={"total_services": len(rows)})


def documents(g, service: str) -> ToolResult:
    """Every document connected to a service: design decisions (ADRs) and postmortems of its incidents."""
    params = {"service": service}
    if not _service_exists(g, service):
        return _missing("documents", params, service)
    q_adr = ("MATCH (d:Decision)-[:ABOUT]->(:Service {name: $service}) MATCH (d)-[:SOURCED_FROM]->(doc:Document) "
             "RETURN doc.id, doc.title, doc.type, d.id")
    q_pm = ("MATCH (doc:Document)-[:MENTIONS]->(i:Incident)-[:AFFECTED]->(:Service {name: $service}) "
            "RETURN DISTINCT doc.id, doc.title, doc.type, i.id")
    rows = _rows(g, q_adr, {"service": service}) + _rows(g, q_pm, {"service": service})
    return ToolResult(
        "documents", params,
        [{"document": d, "title": t, "type": ty, "linked_to": via} for d, t, ty, via in rows],
        evidence=[[service, via, d] for d, _, _, via in rows], cypher=[q_adr, q_pm],
    )


TOOLS = {
    "list_services": list_services,
    "documents": documents,
    "breaker_advice": breaker_advice,
    "what_if_cut": what_if_cut,
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

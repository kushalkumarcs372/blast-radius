"""Pure-Python answers computed straight from generated data, without the database.

Tests compare these with what the Cypher tools return from FalkorDB. If both
agree, the queries are doing what we claim.
"""
from collections import defaultdict, deque

from .generator import CompanyData


def _service_graph(data: CompanyData):
    name = {s["id"]: s["name"] for s in data.nodes["Service"]}
    out, inc = defaultdict(set), defaultdict(set)
    for _, a, _, b, _ in data.edges["DEPENDS_ON"]:
        out[name[a]].add(name[b])
        inc[name[b]].add(name[a])
    return out, inc


def _bfs(adj, start: str, max_hops: int) -> dict[str, int]:
    seen, queue = {start: 0}, deque([start])
    while queue:
        cur = queue.popleft()
        if seen[cur] == max_hops:
            continue
        for nxt in sorted(adj[cur]):
            if nxt not in seen:
                seen[nxt] = seen[cur] + 1
                queue.append(nxt)
    seen.pop(start)
    return seen


def blast_radius(data: CompanyData, service: str, max_hops: int = 4) -> dict[str, int]:
    _, inc = _service_graph(data)
    return _bfs(inc, service, max_hops)


def dependencies(data: CompanyData, service: str, max_hops: int = 4) -> dict[str, int]:
    out, _ = _service_graph(data)
    return _bfs(out, service, max_hops)


def neighbourhood(data: CompanyData, service: str, radius: int = 2) -> set[str]:
    out, inc = _service_graph(data)
    both = defaultdict(set)
    for k in set(out) | set(inc):
        both[k] = out[k] | inc[k]
    return set(_bfs(both, service, radius)) | {service}


def recent_deploys(data: CompanyData, service: str, since_ms: int, radius: int = 2) -> set[str]:
    near = neighbourhood(data, service, radius)
    return {d["id"] for d in data.nodes["Deployment"] if d["service"] in near and d["ts"] >= since_ms}


def owning_team(data: CompanyData, service: str) -> str:
    sid = f"svc-{service}"
    team_name = {t["id"]: t["name"] for t in data.nodes["Team"]}
    return next(team_name[a] for _, a, _, b, _ in data.edges["OWNS"] if b == sid)


def fix_counts(data: CompanyData, service: str) -> dict[str, int]:
    sid = f"svc-{service}"
    person = {p["id"]: p["name"] for p in data.nodes["Person"]}
    affected = {i for _, i, _, s, _ in data.edges["AFFECTED"] if s == sid}
    counts: dict[str, set] = defaultdict(set)
    for _, p, _, i, _ in data.edges["RESOLVED"]:
        if i in affected:
            counts[person[p]].add(i)
    return {k: len(v) for k, v in counts.items()}

"""Integration tests: run every graph tool against a real FalkorDB and compare the
answer with an independent Python calculation over the same generated data.

Needs FalkorDB running (docker compose up -d). Skipped automatically otherwise.
"""
import pytest

from blast_radius.seed import reference
from blast_radius.seed.generator import HOUR, generate
from blast_radius.seed.loader import load
from blast_radius.tools import graph_tools as T

NOW = 1_792_000_000_000
TEST_GRAPHS = ("tenant_pytest_a", "tenant_pytest_b")


@pytest.fixture(scope="module")
def db():
    try:
        from blast_radius.db import client

        c = client()
        c.connection.ping()
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    yield c
    for name in TEST_GRAPHS:
        try:
            c.select_graph(name).delete()
        except Exception:
            pass


@pytest.fixture(scope="module")
def acme():
    return generate("acme", NOW)


@pytest.fixture(scope="module")
def g(db, acme):
    graph = db.select_graph(TEST_GRAPHS[0])
    load(graph, acme)
    return graph


def test_loaded_counts_match(g, acme):
    for label, rows in acme.nodes.items():
        assert g.query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0] == len(rows), label
    for rel, edges in acme.edges.items():
        assert g.query(f"MATCH ()-[r:{rel}]->() RETURN count(r)").result_set[0][0] == len(edges), rel


@pytest.mark.parametrize("service", ["postgres-payments", "kafka", "users", "redis-cache", "web-frontend"])
def test_blast_radius_matches_reference(g, acme, service):
    res = T.blast_radius(g, service)
    assert res.error is None
    assert {r["service"]: r["hops"] for r in res.results} == reference.blast_radius(acme, service)
    for row, path in zip(res.results, res.evidence):
        assert path[0] == row["service"] and path[-1] == service and len(path) == row["hops"] + 1


@pytest.mark.parametrize("service", ["checkout", "api-gateway", "payments", "kafka"])
def test_dependencies_match_reference(g, acme, service):
    res = T.dependencies(g, service)
    assert {r["service"]: r["hops"] for r in res.results} == reference.dependencies(acme, service)


def test_recent_deploys_finds_the_demo_suspect(g, acme):
    res = T.recent_deploys(g, "postgres-payments", hours=6, now_ms=NOW)
    assert [r["deployment"] for r in res.results] == ["deploy-demo-suspect"]
    top = res.results[0]
    assert top["service"] == "payments" and top["hops"] == 1
    assert res.evidence[0] == ["postgres-payments", "payments"]


@pytest.mark.parametrize("service,hours", [("checkout", 72), ("kafka", 240), ("users", 720)])
def test_recent_deploys_match_reference(g, acme, service, hours):
    res = T.recent_deploys(g, service, hours=hours, now_ms=NOW)
    expected = reference.recent_deploys(acme, service, NOW - hours * HOUR)
    assert {r["deployment"] for r in res.results} == expected
    assert [r["ts"] for r in res.results] == sorted((r["ts"] for r in res.results), reverse=True)


def test_critical_services_ranks_shared_infrastructure_high(g, acme):
    res = T.critical_services(g, limit=10)
    assert len(res.results) == 10
    names = [r["service"] for r in res.results]
    scores = [r["pagerank"] for r in res.results]
    assert scores == sorted(scores, reverse=True)
    # Nothing depends on the user-facing edge services, so they cannot be the most critical.
    assert not {"web-frontend", "mobile-bff", "admin-portal"} & set(names)
    for r in res.results:
        assert r["transitive_dependents"] == len(reference.blast_radius(acme, r["service"], 6))


def test_connection_path_directed(g):
    res = T.connection_path(g, "web-frontend", "postgres-payments")
    assert res.results[0]["relation"] == "depends_on"
    path = res.evidence[0]
    assert path[0] == "web-frontend" and path[-1] == "postgres-payments"
    assert res.results[0]["hops"] == len(path) - 1 == 4


def test_connection_path_reverse(g):
    res = T.connection_path(g, "postgres-payments", "checkout")
    assert res.results[0]["relation"] == "depended_on_by"
    path = res.evidence[0]
    # checkout reaches postgres-payments through payments or loyalty; both are 2 hops.
    assert path[0] == "postgres-payments" and path[-1] == "checkout" and len(path) == 3
    assert path[1] in {"payments", "loyalty"}


def test_owners(g, acme):
    res = T.owners(g, "payments")
    row = res.results[0]
    assert row["team"] == reference.owning_team(acme, "payments") == "payments"
    assert row["channel"] == "#team-payments"
    assert sum(m["on_call"] for m in row["members"]) == 1
    assert row["members"][0]["on_call"] is True


def test_fix_history_matches_reference(g, acme):
    for service in ("postgres-payments", "payments", "checkout"):
        res = T.fix_history(g, service)
        assert {r["person"]: r["fixes"] for r in res.results} == reference.fix_counts(acme, service)


def test_unknown_service_returns_error_not_exception(g):
    assert T.blast_radius(g, "does-not-exist").error == "unknown service: does-not-exist"


def test_invalid_hops_rejected(g):
    with pytest.raises(ValueError):
        T.blast_radius(g, "payments", max_hops=50)


def test_tenants_are_isolated(db, g, acme):
    other = db.select_graph(TEST_GRAPHS[1])
    load(other, generate("globex", NOW))
    acme_people = {r[0] for r in g.query("MATCH (p:Person) RETURN p.name").result_set}
    globex_people = {r[0] for r in other.query("MATCH (p:Person) RETURN p.name").result_set}
    assert acme_people != globex_people
    # The same id resolves to each company's own data: payments in acme, notifications in globex.
    q = "MATCH (d:Deployment {id: 'deploy-demo-suspect'})-[:CHANGED]->(s:Service) RETURN s.name"
    assert g.query(q).result_set == [["payments"]]
    assert other.query(q).result_set == [["notifications"]]


def test_decision_trace_finds_decisions_attached_or_mentioning(g):
    direct = T.decision_trace(g, "auth")
    assert [r["document"] for r in direct.results] == ["doc-adr-00"]
    # ADR-003 is attached to `payments` but its text explains `postgres-payments`.
    db = T.decision_trace(g, "postgres-payments")
    assert ("decision-01", "payments", "doc-adr-01") in [
        (r["decision"], r["about_service"], r["document"]) for r in db.results
    ]
    assert T.decision_trace(g, "web-frontend").results == []


def test_similar_incidents_ranks_the_demo_match_first(g):
    res = T.similar_incidents(g, "postgres-payments", "Connection pool exhausted",
                              "Raise connection pool size from 50 to 400", "payments")
    top = res.results[0]
    assert top["incident"] == "inc-demo-old" and top["score"] >= 5
    assert top["change_that_caused_it"] == "Raise connection pool size"

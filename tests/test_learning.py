"""Resolve-and-learn loop: a resolved incident becomes experience for the next alert."""
import time

import pytest

from blast_radius import memory
from blast_radius.agents.orchestrator import handle_alert
from blast_radius.seed.generator import generate
from blast_radius.seed.loader import load
from blast_radius.tools import graph_tools as T

NOW = int(time.time() * 1000)  # real clock: the re-fired alert happens "now", like in the app


@pytest.fixture(scope="module")
def g():
    try:
        from blast_radius.db import client

        c = client()
        c.connection.ping()
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    graph = c.select_graph("tenant_pytest_learning")
    load(graph, generate("acme", NOW))
    handle_alert(graph, "alert-demo-live", llm=None, session="s1")
    yield graph
    graph.delete()


def test_resolve_writes_cause_fixer_and_postmortem(g):
    before = {r["person"]: r["fixes"] for r in T.fix_history(g, "postgres-payments").results}
    out = memory.resolve_incident(g, "inc-alert-demo-live", "s1")
    assert out["cause_deployment"] == "deploy-demo-suspect" and out["resolved_by"]
    rows = g.query("MATCH (p:Person)-[:RESOLVED]->(i:Incident {id: 'inc-alert-demo-live'})-[:CAUSED_BY]->(d) "
                   "RETURN p.name, i.status, d.id").result_set
    assert len(rows) == 1 and rows[0][1:] == ["resolved", "deploy-demo-suspect"]
    fixer = rows[0][0]
    after = {r["person"]: r["fixes"] for r in T.fix_history(g, "postgres-payments").results}
    assert after[fixer] == before.get(fixer, 0) + 1  # expertise grows with each fix
    assert g.query("MATCH (:Document {type: 'postmortem'})-[:MENTIONS]->(:Incident {id: 'inc-alert-demo-live'}) "
                   "RETURN count(*)").result_set[0][0] == 1
    kinds = [e["kind"] for e in memory.incident_memory(g, "inc-alert-demo-live")["episodes"]]
    assert kinds[-1] == "resolution"


def test_cannot_resolve_twice(g):
    with pytest.raises(ValueError):
        memory.resolve_incident(g, "inc-alert-demo-live", "s1")


def test_next_alert_learns_from_the_resolved_incident(g):
    alert = memory.refire_alert(g, "inc-alert-demo-live")
    out = handle_alert(g, alert, llm=None, session="s2")
    inv = out["steps"][1]["findings"]
    assert inv["similar_past_incidents"][0]["incident"] == "inc-alert-demo-live"
    assert inv["likely_cause"]["deployment"] == "deploy-demo-suspect"
    listed = [i["incident"] for i in memory.list_incidents(g)]
    assert out["incident"] in listed and "inc-alert-demo-live" in listed  # resolved ones stay visible


def test_postmortem_is_findable_after_resolving(g):
    docs = {d["document"] for d in T.documents(g, "postgres-payments").results}
    assert "doc-pm-inc-alert-demo-live" in docs
    mem = memory.incident_memory(g, "inc-alert-demo-live")
    assert [d["document"] for d in mem["documents"]] == ["doc-pm-inc-alert-demo-live"]

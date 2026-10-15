"""Agents end to end on a real FalkorDB, with a scripted fake LLM (no API keys needed)."""
import json

import pytest

from blast_radius import memory
from blast_radius.agents import ask as ask_agent
from blast_radius.agents.investigator import score_suspect
from blast_radius.agents.orchestrator import handle_alert
from blast_radius.llm import LLM, Provider
from blast_radius.seed import reference
from blast_radius.seed.generator import generate
from blast_radius.seed.loader import load

NOW = 1_792_000_000_000
GRAPH = "tenant_pytest_agents"


class Scripted(Provider):
    """Returns the queued replies in order, like a model would over several turns."""

    def __init__(self, replies):
        super().__init__("fake", "scripted", "key")
        self.replies, self.prompts = list(replies), []

    def complete_json(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


@pytest.fixture(scope="module")
def db():
    try:
        from blast_radius.db import client

        c = client()
        c.connection.ping()
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    yield c
    try:
        c.select_graph(GRAPH).delete()
    except Exception:
        pass


@pytest.fixture(scope="module")
def acme():
    return generate("acme", NOW)


@pytest.fixture(scope="module")
def run(db, acme):
    g = db.select_graph(GRAPH)
    load(g, acme)
    return g, handle_alert(g, "alert-demo-live", llm=None, session="s-night")


def test_three_agents_ran_in_order(run):
    _, out = run
    assert [s["agent"] for s in out["steps"]] == ["triage", "investigator", "responder"]
    assert out["incident"] == "inc-alert-demo-live"
    assert all(s["summary_source"] == "template" for s in out["steps"])


def test_triage_matches_graph_reference(run, acme):
    g, out = run
    f = out["steps"][0]["findings"]
    expected = reference.blast_radius(acme, "postgres-payments")
    assert {r["service"]: r["hops"] for r in f["at_risk"]} == expected
    assert set(f["customer_facing_at_risk"]) == {"api-gateway", "web-frontend", "mobile-bff", "admin-portal"}
    assert [s["deployment"] for s in f["suspects"]] == ["deploy-demo-suspect"]
    assert f["suspects"][0]["minutes_before_alert"] == 38
    n = g.query("MATCH (:Incident {id: 'inc-alert-demo-live'})-[:AT_RISK]->(s) RETURN count(s)").result_set[0][0]
    assert n == len(expected)


def test_investigator_picks_the_suspect_with_high_confidence(run):
    g, out = run
    cause = out["steps"][1]["findings"]["likely_cause"]
    assert cause["deployment"] == "deploy-demo-suspect" and cause["confidence"] >= 0.8
    past = out["steps"][1]["findings"]["similar_past_incidents"][0]
    assert past["incident"] == "inc-demo-old" and past["score"] >= 5
    rows = g.query("MATCH (:Incident {id: 'inc-alert-demo-live'})-[:SIMILAR_TO]->(p) RETURN p.id").result_set
    assert ["inc-demo-old"] in rows


def test_responder_proposes_rollback_and_names_people(run, acme):
    g, out = run
    f = out["steps"][2]["findings"]
    assert f["actions"][0].startswith("Roll back deploy-demo-suspect")
    on_call = next(p["name"] for p in acme.nodes["Person"]
                   if p["on_call"] and ("Person", p["id"], "Team", "team-payments")
                   in [(a, b, c, d) for a, b, c, d, _ in acme.edges["MEMBER_OF"]])
    assert f["on_call"] == on_call and f["owning_team"] == "payments"
    assert any("runbook-postgres-payments" in a for a in f["actions"])
    assert g.query("MATCH (i:Incident {id: 'inc-alert-demo-live'}) RETURN i.status").result_set[0][0] == "mitigation_proposed"


def test_handoffs_are_stored_in_the_graph(run):
    g, _ = run
    rows = g.query(
        "MATCH (a:Agent)-[:RECORDED]->(e:Episode)-[:ABOUT]->(:Incident {id: 'inc-alert-demo-live'}) "
        "OPTIONAL MATCH (e)-[:HANDED_OFF_TO]->(n:Agent) RETURN a.name, n.name"
    ).result_set
    assert sorted(map(tuple, rows)) == [("investigator", "responder"), ("responder", None), ("triage", "investigator")]


def test_episodes_cite_real_nodes(run):
    g, _ = run
    cited = {r[0] for r in g.query(
        "MATCH (e:Episode)-[:CITES]->(n) WHERE e.agent = 'investigator' RETURN n.id").result_set}
    assert {"deploy-demo-suspect", "inc-demo-old"} <= cited


def test_memory_is_readable_from_a_new_connection(db, run):
    fresh = db.select_graph(GRAPH)  # stands in for a later session
    m = memory.incident_memory(fresh, "inc-alert-demo-live")
    assert [e["agent"] for e in m["episodes"]] == ["triage", "investigator", "responder"]
    assert m["suspected_causes"][0]["deployment"] == "deploy-demo-suspect"


def test_alert_is_no_longer_open(run):
    from blast_radius.tools.graph_tools import open_alerts
    g, _ = run
    assert "alert-demo-live" not in [a["alert"] for a in open_alerts(g).results]


def test_score_suspect_bounds():
    s = {"minutes_before_alert": 0, "hops": 0}
    assert score_suspect(s, 10) == 1.0
    assert score_suspect({"minutes_before_alert": 360, "hops": 2}, 0) == 0.12


def test_ask_uses_tools_and_remembers_the_conversation(run):
    g, _ = run
    llm = LLM([Scripted([
        {"tool": "incident_memory", "args": {"incident": "inc-alert-demo-live"}},
        {"answer": "The likely cause is [deploy-demo-suspect]; it matches [inc-demo-old]."},
        {"answer": "Rolling back [deploy-demo-suspect] is the first action."},
    ])])
    first = ask_agent.ask(g, "What caused last night's payments alert?", llm, session="s-morning")
    assert first["tools"][0]["tool"] == "incident_memory" and first["invented_ids"] == []

    second = ask_agent.ask(g, "What should we do first?", llm, session="s-morning")
    assert "deploy-demo-suspect" in second["answer"]
    # The second question saw the first Q&A, loaded back from the graph.
    assert "What caused last night" in llm.providers[0].prompts[-1]
    rows = g.query("MATCH (e:Episode {session: 's-morning', kind: 'qa'})-[:CITES]->(d:Deployment) "
                   "RETURN count(DISTINCT e)").result_set
    assert rows[0][0] == 2


def test_ask_flags_invented_ids(run):
    g, _ = run
    llm = LLM([Scripted([{"answer": "It was [deploy-1234]."}])])
    out = ask_agent.ask(g, "What broke?", llm, session="s-other")
    assert out["invented_ids"] == ["deploy-1234"]


def test_ask_rejects_unknown_tools(run):
    g, _ = run
    llm = LLM([Scripted([
        {"tool": "drop_database", "args": {}},
        {"tool": "owners", "args": {"service": "payments", "g": "x"}},
        {"answer": "Team payments owns it."},
    ])])
    out = ask_agent.ask(g, "Who owns payments?", llm, session="s-tools")
    assert [s["tool"] for s in out["tools"]] == ["owners"]
    assert "ERROR" in llm.providers[0].prompts[-1]

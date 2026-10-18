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
    s = {"minutes_before_alert": 0, "hops": 0, "deployment": "deploy-0001"}
    assert score_suspect(s, 10) == 1.0
    assert score_suspect({"minutes_before_alert": 360, "hops": 2, "deployment": "deploy-0002"}, 0) == 0.12


def test_ask_uses_tools_and_remembers_the_conversation(run):
    g, _ = run
    llm = LLM([Scripted([
        {"tool": "incident_memory", "args": {"incident": "inc-alert-demo-live"}},
        {"answer": "The likely cause is [deploy-demo-suspect]; it matches [inc-demo-old]."},
        {"answer": "Rolling back [deploy-demo-suspect] is the first action."},  # pushed back: no tool yet
        {"tool": "runbook", "args": {"service": "postgres-payments"}},
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
    llm = LLM([Scripted([{"answer": "It was [deploy-1234]."}, {"answer": "It was [deploy-1234]."}])])
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


def test_ask_runs_several_tools_per_step_and_strips_invented_citations(run):
    g, _ = run
    llm = LLM([Scripted([
        {"tools": [{"tool": "blast_radius", "args": {"service": "postgres-payments"}},
                   {"tool": "connection_path", "args": {"source": "web-frontend", "target": "postgres-payments"}},
                   {"tool": "documents", "args": {"service": "postgres-payments"}}]},
        {"answer": "Unaffected include [kafka] [blast-rad-002]. Path via [checkout] [path-001]."},
    ])])
    out = ask_agent.ask(g, "Which services are unaffected, and how is web-frontend connected?", llm, session="s-multi")
    assert [t["tool"] for t in out["tools"]] == ["blast_radius", "connection_path", "documents"]
    assert out["invented_ids"] == ["blast-rad-002", "path-001"]
    assert "[path-001]" not in out["answer"] and "[blast-rad-002]" not in out["answer"]
    assert "[kafka]" in out["answer"] and set(out["cited"]) == {"kafka", "checkout"}
    # The unaffected list reached the model, so it never has to guess service names.
    assert "unaffected_services" in llm.providers[0].prompts[-1]


def test_reported_change_counts_half_when_a_real_deploy_is_suspect():
    from blast_radius.agents.investigator import rank_suspects

    ranked = rank_suspects([
        {"deployment": "deploy-report-ab-0", "verified": False, "confidence": 0.56},
        {"deployment": "deploy-0042", "verified": True, "confidence": 0.45},
    ])
    assert ranked[0]["deployment"] == "deploy-0042" and ranked[1]["confidence"] == 0.35
    alone = rank_suspects([{"deployment": "deploy-report-ab-0", "verified": False, "confidence": 0.56}])
    assert alone[0]["confidence"] == 0.56  # no real deploy to compare with: keeps the 80% weight


def test_incident_memory_shows_who_fixed_a_past_incident(run, acme):
    g, _ = run
    m = memory.incident_memory(g, "inc-demo-old")
    fixer = next(p["name"] for p in acme.nodes["Person"]
                 for _, pid, _, i, _ in acme.edges["RESOLVED"] if i == "inc-demo-old" and pid == p["id"])
    assert m["resolved_by"] == [fixer]
    assert m["caused_by"][0]["deployment"] == "deploy-demo-old" and "postgres-payments" in m["affected_services"]


def test_ask_unbrackets_field_names(run):
    g, _ = run
    llm = LLM([Scripted([{"answer": "x"}, {"answer": "Safe: [kafka] [unaffected_services]."}])])
    out = ask_agent.ask(g, "What is safe?", llm, session="s-fields")
    assert out["answer"] == "Safe: unaffected services."  # [kafka] was never in a tool result, so it is removed


def test_ask_must_call_a_tool_and_flags_names_not_in_the_graph(run):
    g, _ = run
    llm = LLM([Scripted([
        {"answer": "Unaffected: svc-ledger-legacy. See ADR-42."},  # no tool called: pushed back
        {"tool": "blast_radius", "args": {"service": "postgres-payments"}},
        {"answer": "Unaffected: kafka and svc-ledger-legacy; see ADR-42 and deploy-demo-suspect, owned by customer-facing teams."},
    ])])
    out = ask_agent.ask(g, "Which services are unaffected?", llm, session="s-names")
    assert [t["tool"] for t in out["tools"]] == ["blast_radius"]
    assert "pushed" not in out["answer"] and "without calling any tool" in llm.providers[0].prompts[1]
    assert out["unknown_names"] == ["adr-42", "svc-ledger-legacy"]  # deploy-demo-suspect is real; customer-facing is a word
    assert "not found in the graph" in out["answer"]


def test_responder_summary_may_cite_the_past_incident_it_names(run):
    from blast_radius.agents.base import known_ids
    _, out = run
    f = out["steps"][2]["findings"]
    named = [a for a in f["actions"] if "resolved inc-" in a]
    if named:  # the expert line names a past incident: its id must be citable
        assert "inc-demo-old" in known_ids(f)

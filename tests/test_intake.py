"""Free-text incident reports: parsing, validation, and the agents investigating them."""
import pytest

from blast_radius.intake import ReportError, parse_report, record_report
from blast_radius.llm import LLM, Provider

NOW = 1_792_000_000_000


class Scripted(Provider):
    def __init__(self, replies):
        super().__init__("fake", "scripted", "key")
        self.replies, self.prompts = list(replies), []

    def complete_json(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class FakeGraph:
    """Answers the two lookups parse_report makes, without a database."""

    class R:
        def __init__(self, rows):
            self.result_set = rows

    def query(self, q, params=None):
        if "s.name" in q:
            return self.R([["checkout"], ["pricing"], ["kafka"], ["auth"], ["users"]])
        return self.R([["Elevated 5xx error rate"], ["Consumer lag growing"]])


def test_llm_output_is_validated_against_the_graph():
    llm = LLM([Scripted([{
        "service": "checkout", "signal": "Elevated 5xx error rate", "severity": "sev1", "minutes_ago": "15",
        "changes": [{"service": "pricing", "summary": "Pricing rules change", "minutes_ago": 25},
                    {"service": "billing-v2", "summary": "Made-up service", "minutes_ago": 5}],
    }])])
    f = parse_report(FakeGraph(), "Checkout throws 500s, we changed pricing rules 25 min ago", llm)
    assert (f["service"], f["severity"], f["minutes_ago"]) == ("checkout", "SEV1", 15)
    assert f["changes"] == [{"service": "pricing", "summary": "Pricing rules change", "minutes_ago": 25}]
    assert f["dropped_changes"] == ["Made-up service"]  # unknown service is not written to the graph
    assert "SERVICES" in llm.providers[0].prompts[0] and "KNOWN SIGNALS" in llm.providers[0].prompts[0]


def test_invented_failing_service_is_rejected():
    llm = LLM([Scripted([{"service": "payroll", "signal": "down"}])])
    with pytest.raises(ReportError):
        parse_report(FakeGraph(), "Payroll is down for everyone right now", llm)


def test_model_can_say_it_does_not_match():
    llm = LLM([Scripted([{"error": "report is about the office wifi"}])])
    with pytest.raises(ReportError):
        parse_report(FakeGraph(), "The office wifi is slow today", llm)


def test_keyword_fallback_without_llm():
    f = parse_report(FakeGraph(), "kafka consumers are falling behind. It started a few minutes ago.", None)
    assert f["service"] == "kafka" and f["parsed_by"] == "keywords"
    with pytest.raises(ReportError):
        parse_report(FakeGraph(), "something is broken somewhere", None)


def test_length_limits():
    with pytest.raises(ReportError):
        parse_report(FakeGraph(), "short", None)


# ---------------------------------------------------------------- with FalkorDB
@pytest.fixture(scope="module")
def g():
    try:
        from blast_radius.db import client

        c = client()
        c.connection.ping()
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    from blast_radius.seed.generator import generate
    from blast_radius.seed.loader import load

    graph = c.select_graph("tenant_pytest_intake")
    load(graph, generate("acme", NOW))
    yield graph
    graph.delete()


def test_report_becomes_an_investigated_incident(g):
    import time

    from blast_radius.agents.orchestrator import handle_alert

    llm = LLM([Scripted([{
        "service": "checkout", "signal": "Elevated 5xx error rate", "severity": "SEV1", "minutes_ago": 15,
        "changes": [{"service": "pricing", "summary": "Change pricing rules", "minutes_ago": 25}],
    }])])
    text = "Checkout is throwing 500 errors for 15 minutes. We pushed a pricing rules change 25 minutes ago."
    facts = parse_report(g, text, llm)
    alert = record_report(g, facts, text, now_ms=int(time.time() * 1000))
    assert g.query("MATCH (a:Alert {id: $a})-[:FIRED_ON]->(s:Service) RETURN s.name, a.report",
                   {"a": alert}).result_set == [["checkout", text]]

    out = handle_alert(g, alert, llm=None)
    tri, inv, resp = (s["findings"] for s in out["steps"])
    assert tri["service"] == "checkout" and "api-gateway" in tri["customer_facing_at_risk"]
    assert [s["service"] for s in tri["suspects"]] == ["pricing"]
    assert tri["suspects"][0]["minutes_before_alert"] == 10
    assert inv["likely_cause"]["service"] == "pricing" and inv["likely_cause"]["confidence"] >= 0.5
    assert resp["actions"][0].startswith("Roll back deploy-report-")

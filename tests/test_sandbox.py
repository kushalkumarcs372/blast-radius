"""Build-a-company-from-text: validation of extracted entities, and agents on the built graph."""
import pytest

from blast_radius.ingest import IngestError, build_company, build_from_text
from blast_radius.llm import LLM, Provider

NOW = 1_792_000_000_000

# What a model returns for ingest.EXAMPLE, including the kinds of mistakes models make.
EXTRACTED = {
    "company": "FoodDash",
    "services": [{"name": "Mobile App", "tier": "edge"}, {"name": "web-app", "tier": "edge"},
                 {"name": "api-gateway", "tier": "edge"}, {"name": "orders", "tier": "core"},
                 {"name": "payments", "tier": "weird-tier"}, {"name": "dispatch"}],
    "dependencies": [
        {"from": "Mobile App", "to": "api-gateway"}, {"from": "web-app", "to": "api-gateway"},
        {"from": "api-gateway", "to": "orders"}, {"from": "api-gateway", "to": "menu"},
        {"from": "api-gateway", "to": "accounts"}, {"from": "orders", "to": "payments"},
        {"from": "orders", "to": "dispatch"}, {"from": "orders", "to": "postgres-orders"},
        {"from": "payments", "to": "razorpay-adapter"}, {"from": "dispatch", "to": "maps"},
        {"from": "dispatch", "to": "rider-tracking"}, {"from": "rider-tracking", "to": "redis"},
        {"from": "orders", "to": "dispatch"},  # duplicate
        {"from": "orders", "to": "orders"},    # self-loop
    ],
    "teams": [
        {"name": "Checkout", "owns": ["orders", "payments"],
         "members": [{"name": "Asha Rao", "role": "Engineer", "on_call": True}, {"name": "Vivek Iyer"}]},
        {"name": "Logistics", "owns": ["dispatch", "maps", "rider-tracking"],
         "members": [{"name": "Meena Pillai", "on_call": True}, {"name": "Arjun Das"}]},
        {"name": "Platform", "owns": ["api-gateway", "redis"], "members": [{"name": "Rahul Menon", "on_call": True}]},
    ],
    "deployments": [{"service": "rider-tracking", "summary": "Raise GPS update frequency from 10s to 1s",
                     "author": "Arjun Das", "minutes_ago": 35},
                    {"service": "", "summary": "no service given", "minutes_ago": 5}],
    "past_incidents": [{"service": "redis", "signal": "Out of memory", "cause_service": "rider-tracking",
                        "cause_summary": "Store full GPS history", "resolved_by": "Meena Pillai",
                        "days_ago": 14, "minutes_to_resolve": 50}],
    "alert": {"service": "dispatch", "signal": "Requests timing out", "severity": "SEV1", "minutes_ago": 10},
}


def test_entities_are_validated_and_normalised():
    data, summary = build_company(EXTRACTED, NOW)
    names = {s["name"] for s in data.nodes["Service"]}
    assert "mobile-app" in names and "Mobile App" not in names         # names slugged
    assert {"menu", "accounts", "maps", "redis", "postgres-orders"} <= names  # services only seen in edges
    assert summary["services"] == 13 and summary["dependencies"] == 12   # duplicate and self-loop removed
    assert {s["tier"] for s in data.nodes["Service"]} <= {"edge", "core", "data", "platform", "external"}
    assert summary["alert"] == "alert-live" and summary["past_incidents"] == 1
    assert any("no service given" in d for d in summary["dropped"])


def test_every_edge_points_at_a_node():
    data, _ = build_company(EXTRACTED, NOW)
    ids = {label: {n["id"] for n in rows} for label, rows in data.nodes.items()}
    for rel, edges in data.edges.items():
        for src_label, src, dst_label, dst, _ in edges:
            assert src in ids[src_label] and dst in ids[dst_label], (rel, src, dst)


def test_no_services_is_an_error():
    with pytest.raises(IngestError):
        build_company({"services": [], "dependencies": []}, NOW)


def test_needs_an_llm():
    with pytest.raises(IngestError):
        build_from_text(None, "FoodDash runs a food-delivery app with many services talking to each other.", None)


# ---------------------------------------------------------------- with FalkorDB
class Scripted(Provider):
    def __init__(self, replies):
        super().__init__("fake", "scripted", "key")
        self.replies = list(replies)

    def complete_json(self, system, prompt):
        return self.replies.pop(0)


@pytest.fixture(scope="module")
def g():
    try:
        from blast_radius.db import client

        c = client()
        c.connection.ping()
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    graph = c.select_graph("tenant_pytest_sandbox")
    yield graph
    try:
        graph.delete()
    except Exception:
        pass


def test_agents_investigate_a_graph_built_from_text(g):
    import time

    from blast_radius.agents.orchestrator import handle_alert
    from blast_radius.ingest import EXAMPLE

    summary = build_from_text(g, EXAMPLE, LLM([Scripted([dict(EXTRACTED)])]), now_ms=int(time.time() * 1000))
    assert summary["parsed_by"] == "fake:scripted"
    assert g.query("MATCH (s:Service) RETURN count(s)").result_set[0][0] == 13
    assert g.query("MATCH (m:Meta) RETURN m.text").result_set[0][0] == EXAMPLE  # provenance kept

    out = handle_alert(g, summary["alert"], llm=None)
    tri, inv, resp = (s["findings"] for s in out["steps"])
    assert tri["service"] == "dispatch"
    assert {"orders", "api-gateway", "mobile-app", "web-app"} <= {r["service"] for r in tri["at_risk"]}
    assert [s["service"] for s in tri["suspects"]] == ["rider-tracking"]
    assert inv["likely_cause"]["service"] == "rider-tracking" and inv["likely_cause"]["verified"]
    assert resp["on_call"] == "Meena Pillai" and resp["owning_team"] == "logistics"


def test_rebuild_replaces_the_previous_company(g):
    small = {"services": [{"name": "alpha"}, {"name": "beta"}], "dependencies": [{"from": "alpha", "to": "beta"}]}
    build_from_text(g, "A tiny company with two services: alpha calls beta. Nothing is failing.",
                    LLM([Scripted([small])]))
    assert g.query("MATCH (s:Service) RETURN count(s)").result_set[0][0] == 2
    assert g.query("MATCH (e:Episode) RETURN count(e)").result_set[0][0] == 0


def test_ids_from_the_text_are_kept_and_teams_are_not_double_prefixed():
    import copy

    raw = copy.deepcopy(EXTRACTED)
    raw["deployments"][0]["id"] = "deploy-quant-804"
    raw["past_incidents"][0]["id"] = "INC-ledger-099"
    raw["deployments"][1] = {"id": "deploy-quant-804", "service": "orders", "summary": "dup id", "minutes_ago": 5}
    raw["teams"][0]["name"] = "team-quant"
    data, _ = build_company(raw, NOW)
    deploys = [d["id"] for d in data.nodes["Deployment"]]
    assert "deploy-quant-804" in deploys and "deploy-001" in deploys  # a repeated id falls back to a generated one
    assert [i["id"] for i in data.nodes["Incident"]] == ["inc-ledger-099"]
    assert ("team-quant", "#team-quant") in [(t["id"], t["channel"]) for t in data.nodes["Team"]]


def test_gateway_called_by_apps_is_not_customer_facing():
    data, _ = build_company(EXTRACTED, NOW)
    tiers = {s["name"]: s["tier"] for s in data.nodes["Service"]}
    assert tiers["mobile-app"] == tiers["web-app"] == "edge"
    assert tiers["api-gateway"] == "platform"

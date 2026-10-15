"""HTTP API end to end on the local FalkorDB, with the LLM switched off.

Note: these tests call /api/reset, which reloads the demo companies.
"""
import pytest

pytest.importorskip("fastapi")


@pytest.fixture(scope="module")
def client():
    try:
        from blast_radius.db import client as db

        db().connection.ping()
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    from fastapi.testclient import TestClient

    from blast_radius import api
    from blast_radius.llm import LLM

    api._llm = LLM([])  # no API calls from tests
    with TestClient(api.app) as c:
        assert c.post("/api/reset").json()["ok"]
        yield c
        c.post("/api/reset")  # leave the demo alert open for the next manual run


def test_health_and_tenants(client):
    assert client.get("/api/health").json()["ok"]
    assert {"acme", "globex", "initech"} <= set(client.get("/api/tenants").json())


def test_graph_endpoint(client):
    g = client.get("/api/graph?tenant=acme").json()
    assert len(g["nodes"]) == 38 and len(g["edges"]) == 54
    assert {"id", "tier", "team", "pagerank"} <= set(g["nodes"][0])


def test_unknown_tenant_is_404(client):
    assert client.get("/api/graph?tenant=nope").status_code == 404


def test_tool_endpoint(client):
    r = client.get("/api/tools/blast_radius?tenant=acme&service=postgres-payments").json()
    assert len(r["results"]) == 8 and r["cypher"]
    assert client.get("/api/tools/drop_everything").status_code == 404
    assert client.get("/api/tools/blast_radius?service=kafka&max_hops=99").status_code == 400


def test_respond_then_view_incident(client):
    alerts = client.get("/api/alerts?tenant=acme").json()
    assert alerts[0]["alert"] == "alert-demo-live"
    out = client.post("/api/respond", json={"alert": "alert-demo-live", "tenant": "acme"}).json()
    assert [s["agent"] for s in out["steps"]] == ["triage", "investigator", "responder"]

    inc = client.get(f"/api/incidents/{out['incident']}?tenant=acme").json()
    assert len(inc["triage"]["at_risk"]) == 8
    assert inc["investigator"]["likely_cause"]["deployment"] == "deploy-demo-suspect"
    assert inc["responder"]["actions"][0].startswith("Roll back")
    assert client.get("/api/alerts?tenant=acme").json() == []
    assert out["incident"] in [i["incident"] for i in client.get("/api/incidents?tenant=acme").json()]


def test_ask_without_llm_explains_itself(client):
    out = client.post("/api/ask", json={"question": "Who owns payments?"}).json()
    assert "No LLM key" in out["answer"] and out["tools"] == []
    assert client.post("/api/ask", json={"question": ""}).status_code == 400


def test_index_serves_the_ui(client):
    r = client.get("/")
    assert r.status_code == 200 and "Blast" in r.text

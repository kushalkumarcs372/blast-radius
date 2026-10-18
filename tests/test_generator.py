"""Checks on the synthetic data itself. These need no database."""
from collections import defaultdict

from blast_radius.seed import catalog, reference
from blast_radius.seed.generator import DAY, MINUTE, TENANTS, generate

NOW = 1_792_000_000_000  # fixed clock so results are repeatable


def test_generation_is_deterministic():
    a, b = generate("acme", NOW), generate("acme", NOW)
    assert a.nodes == b.nodes and a.edges == b.edges


def test_every_edge_points_at_existing_nodes():
    for tenant in TENANTS:
        data = generate(tenant, NOW)
        ids = {label: {n["id"] for n in rows} for label, rows in data.nodes.items()}
        for rel, edges in data.edges.items():
            for src_label, src, dst_label, dst, _ in edges:
                assert src in ids[src_label], (rel, src)
                assert dst in ids[dst_label], (rel, dst)


def test_ids_are_unique_per_label():
    data = generate("acme", NOW)
    for label, rows in data.nodes.items():
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), label


def test_dependency_graph_has_no_cycles():
    adj = defaultdict(list)
    for src, dst in catalog.DEPENDENCIES:
        adj[src].append(dst)
    state = {}

    def visit(n):
        state[n] = "open"
        for m in adj[n]:
            assert state.get(m) != "open", f"cycle through {n} -> {m}"
            if m not in state:
                visit(m)
        state[n] = "done"

    for s in catalog.SERVICES:
        if s not in state:
            visit(s)


def test_every_service_has_one_owner_and_every_team_one_on_call():
    data = generate("acme", NOW)
    owned = [b for _, _, _, b, _ in data.edges["OWNS"]]
    assert sorted(owned) == sorted(s["id"] for s in data.nodes["Service"])
    on_call = defaultdict(int)
    team_of = {p: t for _, p, _, t, _ in data.edges["MEMBER_OF"]}
    for p in data.nodes["Person"]:
        on_call[team_of[p["id"]]] += p["on_call"]
    assert set(on_call.values()) == {1}


def test_no_timestamps_in_the_future():
    data = generate("acme", NOW)
    for label in ("Deployment", "Alert", "Incident"):
        for n in data.nodes[label]:
            assert n.get("ts", n.get("started")) <= NOW
            assert n.get("resolved", 0) <= NOW


def test_every_company_has_its_own_live_incident():
    from blast_radius.seed.generator import DEMO_SCENARIOS

    services = set()
    for tenant, sc in DEMO_SCENARIOS.items():
        data = generate(tenant, NOW)
        suspect = next(d for d in data.nodes["Deployment"] if d["id"] == "deploy-demo-suspect")
        assert suspect["service"] == sc["cause_service"]
        assert suspect["ts"] == NOW - sc["minutes_before"] * MINUTE
        # The cause sits next to the alerting service...
        assert sc["cause_service"] in reference.neighbourhood(data, sc["alert_service"], 1)
        # ...and is the only change near it in the 6 hours before the alert.
        alert_ts = NOW - 2 * MINUTE
        assert reference.recent_deploys(data, sc["alert_service"], alert_ts - 6 * 60 * MINUTE) == {"deploy-demo-suspect"}
        assert reference.owning_team(data, sc["cause_service"]) == sc["team"]
        services.add(sc["alert_service"])
    assert len(services) == len(DEMO_SCENARIOS)  # a different failing service per company


def test_postgres_payments_outage_reaches_the_edge():
    acme = generate("acme", NOW)
    radius = reference.blast_radius(acme, "postgres-payments")
    assert {"payments", "refunds", "loyalty", "checkout", "api-gateway"} <= set(radius)
    assert radius["payments"] == 1 and radius["api-gateway"] == 3


def test_tenants_have_different_people():
    names = {t: {p["name"] for p in generate(t, NOW).nodes["Person"]} for t in TENANTS}
    assert names["acme"] != names["globex"] != names["initech"]


def test_sizes_match_the_plan():
    data = generate("acme", NOW)
    assert len(data.nodes["Service"]) == len(catalog.SERVICES) >= 35
    assert len(data.nodes["Deployment"]) == 150 + 2
    assert len(data.nodes["Incident"]) == 25 + 1
    assert 27 <= len(data.nodes["Person"]) <= 36
    assert all(d["ts"] >= NOW - 30 * DAY - 1 for d in data.nodes["Deployment"])

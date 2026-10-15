"""Deterministic synthetic company data.

The same (tenant, now_ms) always produces the same data, so tests can compute
expected answers independently and compare them with what FalkorDB returns.
"""
import random
from collections import defaultdict
from dataclasses import dataclass, field

from . import catalog

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# Tenant -> (random seed, deployments, past incidents)
TENANTS = {
    "acme": (7, 150, 25),
    "globex": (21, 60, 10),
    "initech": (42, 40, 8),
}


@dataclass
class CompanyData:
    tenant: str
    now_ms: int
    nodes: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    # relationship type -> list of (from_label, from_id, to_label, to_id, props)
    edges: dict[str, list[tuple]] = field(default_factory=lambda: defaultdict(list))

    def add_node(self, label: str, **props) -> dict:
        self.nodes[label].append(props)
        return props

    def add_edge(self, rel: str, src_label: str, src_id: str, dst_label: str, dst_id: str, **props) -> None:
        self.edges[rel].append((src_label, src_id, dst_label, dst_id, props))

    def count(self) -> tuple[int, int]:
        return (sum(len(v) for v in self.nodes.values()), sum(len(v) for v in self.edges.values()))


def svc_id(name: str) -> str:
    return f"svc-{name}"


def team_id(name: str) -> str:
    return f"team-{name}"


def generate(tenant: str, now_ms: int) -> CompanyData:
    seed, n_deploys, n_incidents = TENANTS[tenant]
    rng = random.Random(seed)
    data = CompanyData(tenant=tenant, now_ms=now_ms)

    # --- teams and services -------------------------------------------------
    for team, channel in catalog.TEAMS.items():
        data.add_node("Team", id=team_id(team), name=team, channel=channel)

    for name, (tier, language, team) in catalog.SERVICES.items():
        data.add_node("Service", id=svc_id(name), name=name, tier=tier, language=language)
        data.add_edge("OWNS", "Team", team_id(team), "Service", svc_id(name))

    for src, dst in catalog.DEPENDENCIES:
        data.add_edge("DEPENDS_ON", "Service", svc_id(src), "Service", svc_id(dst))

    # --- people: 3-4 per team, one on call ----------------------------------
    names = [f"{f} {l}" for f in catalog.FIRST_NAMES for l in catalog.LAST_NAMES]
    rng.shuffle(names)
    members: dict[str, list[str]] = defaultdict(list)
    person_name: dict[str, str] = {}
    i = 0
    for team in catalog.TEAMS:
        size = rng.choice([3, 3, 4])
        on_call = rng.randrange(size)
        for k in range(size):
            pid = f"person-{i:03d}"
            person_name[pid] = names[i]
            data.add_node("Person", id=pid, name=names[i], role=rng.choice(catalog.ROLES),
                          on_call=(k == on_call))
            data.add_edge("MEMBER_OF", "Person", pid, "Team", team_id(team))
            members[team].append(pid)
            i += 1

    owner_team = {name: team for name, (_, _, team) in catalog.SERVICES.items()}
    service_names = list(catalog.SERVICES)

    def add_deploy(dep_id: str, service: str, ts: int, summary: str, author: str | None = None) -> dict:
        author = author or rng.choice(members[owner_team[service]])
        version = f"v{rng.randint(1, 9)}.{rng.randint(0, 40)}.{rng.randint(0, 20)}"
        node = data.add_node("Deployment", id=dep_id, version=version, ts=ts, summary=summary, service=service)
        data.add_edge("AUTHORED", "Person", author, "Deployment", dep_id)
        data.add_edge("CHANGED", "Deployment", dep_id, "Service", svc_id(service))
        return node

    # --- deployments over the last 30 days, none in the last 6 hours --------
    deploys: list[dict] = []
    for d in range(n_deploys):
        service = rng.choice(service_names)
        ts = now_ms - rng.randint(6 * HOUR, 30 * DAY)
        deploys.append(add_deploy(f"deploy-{d:04d}", service, ts, rng.choice(catalog.CHANGE_TEMPLATES)))

    # --- dependency maps used to make incidents realistic --------------------
    dependents: dict[str, list[str]] = defaultdict(list)
    for src, dst in catalog.DEPENDENCIES:
        dependents[dst].append(src)

    # Expertise: a couple of people per team fix most incidents.
    experts = {team: ppl[: 2] for team, ppl in members.items()}

    def add_incident(inc_id: str, service: str, title: str, severity: str, started: int,
                     cause: dict | None, resolver: str | None, status: str = "resolved") -> None:
        affected = [service] + dependents[service][: rng.randint(0, 3)]
        resolved = None
        if status == "resolved":
            resolved = min(started + rng.randint(20 * MINUTE, 6 * HOUR), now_ms - MINUTE)
        props = dict(id=inc_id, title=f"{title} on {service}", severity=severity,
                     status=status, started=started, service=service)
        if resolved is not None:
            props["resolved"] = resolved
        data.add_node("Incident", **props)
        alert_id = f"alert-{inc_id}"
        data.add_node("Alert", id=alert_id, signal=title, severity=severity, ts=started, service=service)
        data.add_edge("FIRED_ON", "Alert", alert_id, "Service", svc_id(service))
        data.add_edge("TRIGGERED_BY", "Incident", inc_id, "Alert", alert_id)
        for s in affected:
            data.add_edge("AFFECTED", "Incident", inc_id, "Service", svc_id(s))
        if cause is not None:
            data.add_edge("CAUSED_BY", "Incident", inc_id, "Deployment", cause["id"])
        if resolver is not None:
            data.add_edge("RESOLVED", "Person", resolver, "Incident", inc_id)
        if severity in ("SEV1", "SEV2"):
            doc_id = f"doc-pm-{inc_id}"
            data.add_node("Document", id=doc_id, title=f"Postmortem: {props['title']}",
                          type="postmortem", source=f"wiki/postmortems/{inc_id}")
            data.add_edge("MENTIONS", "Document", doc_id, "Incident", inc_id)

    # --- past incidents, each caused by an earlier deploy nearby ------------
    for n in range(n_incidents):
        cause = rng.choice(deploys)
        service = cause["service"]
        title, severity = rng.choice(catalog.INCIDENT_PATTERNS)
        started = cause["ts"] + rng.randint(5 * MINUTE, 3 * HOUR)
        team = owner_team[service]
        resolver = rng.choice(experts[team] * 3 + members[team])
        add_incident(f"inc-{n:03d}", service, title, severity, started, cause, resolver)

    # --- runbooks for core and data services ---------------------------------
    for name, (tier, _, _) in catalog.SERVICES.items():
        if tier in ("core", "data"):
            rb = f"runbook-{name}"
            data.add_node("Runbook", id=rb, title=f"{name} on-call runbook",
                          steps=f"Check dashboards for {name}; roll back the latest deploy; page the owning team.")
            data.add_edge("FOR", "Runbook", rb, "Service", svc_id(name))

    # --- design decisions with their source documents ------------------------
    for k, (title, service, summary, doc_title, doc_type) in enumerate(catalog.DECISIONS):
        dec_id, doc_id = f"decision-{k:02d}", f"doc-adr-{k:02d}"
        data.add_node("Decision", id=dec_id, title=title, summary=summary,
                      date=now_ms - rng.randint(60, 700) * DAY)
        data.add_node("Document", id=doc_id, title=doc_title, type=doc_type, source=f"wiki/adr/{k:02d}")
        data.add_edge("ABOUT", "Decision", dec_id, "Service", svc_id(service))
        data.add_edge("SOURCED_FROM", "Decision", dec_id, "Document", doc_id)

    # --- the scripted demo scenario (acme only) -------------------------------
    if tenant == "acme":
        _add_demo_scenario(data, add_deploy, add_incident, members, now_ms)

    return data


def _add_demo_scenario(data, add_deploy, add_incident, members, now_ms) -> None:
    """2 AM alert: postgres-payments connections exhausted 40 min after a payments deploy.

    A similar incident 3 weeks ago had the same cause and was fixed by a payments engineer.
    """
    payments_people = members["payments"]
    old_cause = add_deploy("deploy-demo-old", "payments", now_ms - 21 * DAY,
                           "Raise connection pool size", author=payments_people[1])
    add_incident("inc-demo-old", "postgres-payments", "Connection pool exhausted", "SEV2",
                 now_ms - 21 * DAY + 50 * MINUTE, old_cause, payments_people[0])

    add_deploy("deploy-demo-suspect", "payments", now_ms - 40 * MINUTE,
               "Raise connection pool size from 50 to 400", author=payments_people[2])
    data.add_node("Alert", id="alert-demo-live", signal="Connection pool exhausted",
                  severity="SEV1", ts=now_ms - 2 * MINUTE, service="postgres-payments")
    data.add_edge("FIRED_ON", "Alert", "alert-demo-live", "Service", svc_id("postgres-payments"))

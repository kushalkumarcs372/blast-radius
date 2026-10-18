"""Build a company graph from a plain-English description (the "sandbox" company).

Someone describes a company in a paragraph: its services and what calls what, the teams
and who is on call, recent deploys, past incidents, and what is failing now. The LLM
extracts structured entities; this module validates them (names, references, limits) and
loads them into an empty FalkorDB graph. Nothing comes from the synthetic generator: the
agents then reason over a graph built entirely from the text.
"""
import json
import re
import time

from .llm import LLMUnavailable
from .seed.generator import DAY, MINUTE, CompanyData

SANDBOX = "sandbox"
MAX_SERVICES, MAX_EDGES, MAX_PEOPLE = 60, 200, 60
TIERS = {"edge", "core", "data", "platform", "external"}

SYSTEM = (
    "You extract a company's system architecture and incident context from a description, for a graph database. "
    "Use ONLY what the text states; never invent services, people, deploys or incidents. "
    "Service names: short lowercase-with-hyphens (\"Payments API\" -> payments-api). "
    "A dependency {from, to} means FROM calls or needs TO. tier is one of edge (user-facing), core, data, platform, external. "
    "Reply with one JSON object exactly in this shape (use [] or null when the text has nothing):\n"
    '{"company": "<name or null>",\n'
    ' "services": [{"name": "...", "tier": "..."}],\n'
    ' "dependencies": [{"from": "<service>", "to": "<service>"}],\n'
    ' "teams": [{"name": "...", "owns": ["<service>"], "members": [{"name": "...", "role": "...", "on_call": true}]}],\n'
    ' "deployments": [{"id": "<id if the text gives one, else null>", "service": "...", "summary": "...", '
    '"author": "<person or null>", "minutes_ago": 30}],\n'
    ' "past_incidents": [{"id": "<id if the text gives one, else null>", "service": "...", "signal": "...", '
    '"cause_service": "<service or null>", '
    '"cause_summary": "<change that caused it, or null>", "resolved_by": "<person or null>", "days_ago": 14, '
    '"minutes_to_resolve": 45}],\n'
    ' "alert": {"service": "...", "signal": "...", "severity": "SEV1|SEV2|SEV3", "minutes_ago": 5} or null}'
)

EXAMPLE = (
    "FoodDash runs a food-delivery app. The mobile app and the web app both call the api-gateway. The gateway "
    "calls orders, menu and accounts. Orders calls payments, dispatch and the orders database (postgres-orders). "
    "Payments uses a Razorpay adapter and its own payments database. Dispatch calls the maps service and the "
    "rider-tracking service, which writes to Redis. Team Checkout (Asha Rao, on call; Vivek Iyer) owns orders and "
    "payments. Team Logistics (Meena Pillai, on call; Arjun Das) owns dispatch, maps and rider-tracking. Team "
    "Platform (Rahul Menon, on call) owns the gateway and Redis. 35 minutes ago Arjun Das deployed rider-tracking "
    "to raise the GPS update frequency from every 10 seconds to every second. Two weeks ago Redis ran out of memory "
    "after a rider-tracking change that stored full GPS history; Meena Pillai fixed it in 50 minutes. Right now "
    "orders are stuck in 'finding a rider' and dispatch is timing out, started 10 minutes ago."
)


class IngestError(ValueError):
    pass


def slug(name) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:40]


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


def given_id(raw_id, default: str, used: set[str]) -> str:
    """Keep an id written in the description (e.g. deploy-quant-804) when it is valid and unused."""
    cand = str(raw_id or "").strip().lower()
    if _ID_RE.match(cand) and cand not in used and cand not in ("null", "none"):
        used.add(cand)
        return cand
    used.add(default)
    return default


def _int(v, default: int, lo: int = 0, hi: int = 365 * 24 * 60) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def build_company(raw: dict, now_ms: int) -> tuple[CompanyData, dict]:
    """Validate extracted entities and turn them into nodes and edges. Returns (data, summary)."""
    data = CompanyData(tenant=SANDBOX, now_ms=now_ms)
    dropped: list[str] = []

    services: dict[str, str] = {}

    def add_service(name, tier="core") -> str | None:
        s = slug(name)
        if not s:
            return None
        if s not in services:
            if len(services) >= MAX_SERVICES:
                dropped.append(f"service {s} (limit {MAX_SERVICES})")
                return None
            services[s] = tier if tier in TIERS else "core"
        return s

    for svc in raw.get("services") or []:
        if isinstance(svc, dict):
            add_service(svc.get("name"), str(svc.get("tier", "core")).lower())
    for d in (raw.get("dependencies") or [])[:MAX_EDGES]:
        if not isinstance(d, dict):
            continue
        a, b = add_service(d.get("from")), add_service(d.get("to"))
        if a and b and a != b:
            data.add_edge("DEPENDS_ON", "Service", f"svc-{a}", "Service", f"svc-{b}")
    data.edges["DEPENDS_ON"] = list({e[:4]: e for e in data.edges["DEPENDS_ON"]}.values())  # dedupe

    people: dict[str, str] = {}

    def add_person(name, role="Engineer", on_call=False) -> str | None:
        n = str(name or "").strip()[:60]
        if not n or n.lower() in ("null", "none", "unknown"):
            return None
        key = n.lower()
        if key not in people:
            if len(people) >= MAX_PEOPLE:
                return None
            pid = f"person-{len(people):03d}"
            people[key] = pid
            data.add_node("Person", id=pid, name=n, role=str(role or "Engineer")[:40], on_call=bool(on_call))
        return people[key]

    for t in raw.get("teams") or []:
        if not isinstance(t, dict) or not slug(t.get("name")):
            continue
        team = re.sub(r"^team-", "", slug(t["name"])) or slug(t["name"])  # "Team Quant" -> quant, not team-team-quant
        data.add_node("Team", id=f"team-{team}", name=team, channel=f"#team-{team}")
        for svc in t.get("owns") or []:
            s = add_service(svc)
            if s:
                data.add_edge("OWNS", "Team", f"team-{team}", "Service", f"svc-{s}")
        for m in t.get("members") or []:
            if isinstance(m, dict):
                pid = add_person(m.get("name"), m.get("role"), m.get("on_call"))
                if pid:
                    data.add_edge("MEMBER_OF", "Person", pid, "Team", f"team-{team}")

    def deploy(dep_id, service, ts, summary, author) -> None:
        data.add_node("Deployment", id=dep_id, version="described", ts=ts, summary=summary, service=service)
        data.add_edge("CHANGED", "Deployment", dep_id, "Service", f"svc-{service}")
        pid = add_person(author) or add_person("unknown author")
        data.add_edge("AUTHORED", "Person", pid, "Deployment", dep_id)

    used_ids: set[str] = set()
    for i, d in enumerate((raw.get("deployments") or [])[:30]):
        if not isinstance(d, dict):
            continue
        s, summary = add_service(d.get("service")), str(d.get("summary") or "").strip()[:160]
        if s and summary:
            deploy(given_id(d.get("id"), f"deploy-{i:03d}", used_ids), s,
                   now_ms - _int(d.get("minutes_ago"), 60) * MINUTE, summary, d.get("author"))
        else:
            dropped.append(f"deployment {summary or d}")

    for i, inc in enumerate((raw.get("past_incidents") or [])[:30]):
        if not isinstance(inc, dict):
            continue
        s = add_service(inc.get("service"))
        signal = str(inc.get("signal") or "").strip()[:80]
        if not (s and signal):
            dropped.append(f"past incident {signal or inc}")
            continue
        started = now_ms - _int(inc.get("days_ago"), 14, 1, 3650) * DAY
        inc_id = given_id(inc.get("id"), f"inc-{i:03d}", used_ids)
        alert_id = f"alert-past-{i:03d}"
        data.add_node("Incident", id=inc_id, title=f"{signal} on {s}", severity="SEV2", status="resolved",
                      started=started, resolved=started + _int(inc.get("minutes_to_resolve"), 60, 1) * MINUTE,
                      service=s)
        data.add_node("Alert", id=alert_id, signal=signal, severity="SEV2", ts=started, service=s)
        data.add_edge("FIRED_ON", "Alert", alert_id, "Service", f"svc-{s}")
        data.add_edge("TRIGGERED_BY", "Incident", inc_id, "Alert", alert_id)
        data.add_edge("AFFECTED", "Incident", inc_id, "Service", f"svc-{s}")
        cause_svc, cause = add_service(inc.get("cause_service")), str(inc.get("cause_summary") or "").strip()[:160]
        if cause and cause.lower() not in ("null", "none"):
            dep_id = f"deploy-past-{i:03d}"
            deploy(dep_id, cause_svc or s, started - 30 * MINUTE, cause, None)
            data.add_edge("CAUSED_BY", "Incident", inc_id, "Deployment", dep_id)
        fixer = add_person(inc.get("resolved_by"))
        if fixer:
            data.add_edge("RESOLVED", "Person", fixer, "Incident", inc_id)

    alert = raw.get("alert") if isinstance(raw.get("alert"), dict) else None
    alert_id = None
    if alert:
        s, signal = add_service(alert.get("service")), str(alert.get("signal") or "").strip()[:80]
        if s and signal:
            sev = str(alert.get("severity", "SEV1")).upper()
            alert_id = "alert-live"
            data.add_node("Alert", id=alert_id, signal=signal, severity=sev if sev in ("SEV1", "SEV2", "SEV3") else "SEV1",
                          ts=now_ms - _int(alert.get("minutes_ago"), 5) * MINUTE, service=s)
            data.add_edge("FIRED_ON", "Alert", alert_id, "Service", f"svc-{s}")

    if not services:
        raise IngestError("No services found in the description. Name the services and what calls what.")
    # Customer-facing means users hit it directly. A gateway or router that other user-facing apps
    # call is internal plumbing, even if the model labelled it "edge".
    callers: dict[str, set] = {}
    for _, a, _, b, _ in data.edges["DEPENDS_ON"]:
        callers.setdefault(b[4:], set()).add(a[4:])
    for name, tier in list(services.items()):
        if tier == "edge" and any(services.get(c) == "edge" for c in callers.get(name, ())):
            services[name] = "platform"
    for name, tier in sorted(services.items()):
        data.add_node("Service", id=f"svc-{name}", name=name, tier=tier, language="unknown")

    n, e = data.count()
    summary = {"company": raw.get("company"), "services": len(services), "dependencies": len(data.edges["DEPENDS_ON"]),
               "teams": len(data.nodes["Team"]), "people": len(data.nodes["Person"]),
               "deployments": len(data.nodes["Deployment"]), "past_incidents": len(data.nodes["Incident"]),
               "alert": alert_id, "nodes": n, "edges": e, "dropped": dropped}
    return data, summary


def extract(text: str, llm) -> dict:
    text = text.strip()
    if not 40 <= len(text) <= 6000:
        raise IngestError("Describe the company in 40 to 6000 characters.")
    if llm is None or not llm.providers:
        raise IngestError("Building a graph from text needs an LLM key (GROQ_API_KEY or GEMINI_API_KEY).")
    try:
        raw = llm.complete_json(SYSTEM, f"DESCRIPTION:\n{text}")
    except LLMUnavailable as exc:
        raise IngestError(f"The language model is unavailable right now: {exc}") from None
    if not isinstance(raw, dict):
        raise IngestError("The model did not return a JSON object.")
    raw["_parsed_by"] = llm.last_provider
    return raw


def build_from_text(g, text: str, llm, now_ms: int | None = None) -> dict:
    """Extract, validate, wipe the sandbox graph and load the new company. Returns a summary."""
    from .seed.loader import load

    raw = extract(text, llm)
    data, summary = build_company(raw, now_ms or int(time.time() * 1000))
    load(g, data, reset=True)
    g.query("MERGE (n:Meta {id: 'description'}) SET n.text = $t, n.extracted = $x",
            {"t": text[:6000], "x": json.dumps({k: v for k, v in raw.items() if k != "_parsed_by"})[:20000]})
    summary["parsed_by"] = raw.get("_parsed_by")
    return summary

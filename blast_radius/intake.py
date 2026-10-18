"""Turn a free-text incident report into graph facts the agents can investigate.

A person writes what they see ("checkout is throwing 500s for 20 minutes, we pushed a
pricing change half an hour ago"). The LLM extracts structured facts; every service it
names is checked against the graph, so it cannot invent a service. The facts become an
Alert node (and Deployment nodes for any changes mentioned), and then the same three
agents run on it as on any other alert.
"""
import json
import re
import time
import uuid

from .llm import LLMUnavailable

MINUTE = 60_000
REPORTER = {"id": "person-reporter", "name": "the incident reporter", "role": "Reporter", "on_call": False}

SYSTEM = (
    "You turn an engineer's incident report into JSON for an incident-response system. "
    "SERVICES lists the only valid service names; map what the report describes to them "
    "(e.g. 'login' -> auth, 'the payments database' -> postgres-payments). "
    "KNOWN SIGNALS lists symptom names used in past incidents; reuse one if it fits, otherwise write a "
    "short symptom phrase. Use only what the report says. Reply with JSON exactly like: "
    '{"service": "<failing service from SERVICES>", "signal": "<symptom>", "severity": "SEV1|SEV2|SEV3", '
    '"minutes_ago": <when it started, default 5>, '
    '"changes": [{"service": "<service from SERVICES>", "summary": "<what changed>", "minutes_ago": <int>}]} '
    'If no service in SERVICES matches, reply {"error": "<short reason>"}.'
)


class ReportError(ValueError):
    pass


def _services(g) -> list[str]:
    return sorted(r[0] for r in g.query("MATCH (s:Service) RETURN s.name").result_set)


def _known_signals(g) -> list[str]:
    return sorted({r[0] for r in g.query("MATCH (a:Alert) RETURN DISTINCT a.signal").result_set if r[0]})


def _keyword_parse(text: str, services: list[str]) -> dict:
    """Fallback when no LLM is available: find service names written in the text."""
    low = text.lower()
    found = sorted(
        ((low.find(s), s) for s in services
         if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", low)),
    )
    if not found:
        raise ReportError("No known service is named in the report. Mention one, e.g. checkout or kafka.")
    first = text.strip().split(".")[0][:80]
    return {"service": found[0][1], "signal": first or "Reported problem", "severity": "SEV2",
            "minutes_ago": 5, "changes": []}


def _int(v, default: int, lo: int = 0, hi: int = 24 * 60) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def parse_report(g, text: str, llm) -> dict:
    """Return validated facts: service, signal, severity, minutes_ago, changes, parsed_by."""
    text = text.strip()
    if not 10 <= len(text) <= 1500:
        raise ReportError("Describe the incident in 10 to 1500 characters.")
    services = _services(g)
    parsed_by = "keywords"
    raw = None
    if llm is not None and llm.providers:
        prompt = (f"SERVICES: {json.dumps(services)}\nKNOWN SIGNALS: {json.dumps(_known_signals(g))}\n"
                  f"REPORT:\n{text}")
        try:
            raw = llm.complete_json(SYSTEM, prompt)
            parsed_by = llm.last_provider or "llm"
        except LLMUnavailable:
            raw = None
    if raw is None:
        raw = _keyword_parse(text, services)
        parsed_by = "keywords"
    if raw.get("error"):
        raise ReportError(f"Could not match the report to a service: {raw['error']}")

    service = str(raw.get("service", "")).strip()
    if service not in services:
        raise ReportError(f"'{service}' is not a service in this company's graph.")
    changes, dropped = [], []
    for c in raw.get("changes") or []:
        if not isinstance(c, dict):
            continue
        cs = str(c.get("service", "")).strip()
        summary = str(c.get("summary", "")).strip()[:160]
        if cs in services and summary:
            changes.append({"service": cs, "summary": summary, "minutes_ago": _int(c.get("minutes_ago"), 30)})
        elif summary:
            dropped.append(summary)
    severity = str(raw.get("severity", "SEV2")).upper()
    return {
        "service": service,
        "signal": (str(raw.get("signal", "")).strip() or "Reported problem")[:80],
        "severity": severity if severity in ("SEV1", "SEV2", "SEV3") else "SEV2",
        "minutes_ago": _int(raw.get("minutes_ago"), 5),
        "changes": changes[:5],
        "dropped_changes": dropped,
        "parsed_by": parsed_by,
    }


def record_report(g, facts: dict, text: str, now_ms: int | None = None) -> str:
    """Write the report into the graph as an Alert (+ Deployments). Returns the alert id."""
    now_ms = now_ms or int(time.time() * 1000)
    tag = uuid.uuid4().hex[:8]
    alert_id = f"alert-report-{tag}"
    g.query(
        "MATCH (s:Service {name: $svc}) "
        "CREATE (a:Alert {id: $id, signal: $signal, severity: $sev, ts: $ts, service: $svc, "
        "source: 'report', report: $text})-[:FIRED_ON]->(s)",
        {"svc": facts["service"], "id": alert_id, "signal": facts["signal"], "sev": facts["severity"],
         "ts": now_ms - facts["minutes_ago"] * MINUTE, "text": text[:1500]},
    )
    if facts["changes"]:
        g.query("MERGE (p:Person {id: $id}) ON CREATE SET p.name = $name, p.role = $role, p.on_call = $oc",
                {"id": REPORTER["id"], "name": REPORTER["name"], "role": REPORTER["role"], "oc": REPORTER["on_call"]})
    for i, c in enumerate(facts["changes"]):
        g.query(
            "MATCH (p:Person {id: $pid}), (s:Service {name: $svc}) "
            "CREATE (p)-[:AUTHORED]->(:Deployment {id: $id, version: 'reported', ts: $ts, summary: $summary, "
            "service: $svc, source: 'report'})-[:CHANGED]->(s)",
            {"pid": REPORTER["id"], "svc": c["service"], "id": f"deploy-report-{tag}-{i}",
             "ts": now_ms - c["minutes_ago"] * MINUTE, "summary": c["summary"]},
        )
    return alert_id

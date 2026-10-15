"""Triage agent: how big is the impact, and what changed just before the alert?"""
from .. import memory
from ..tools import graph_tools as T
from .base import summarize

ROLE = "You size the impact of an alert and list the deploys that could have caused it."
SUSPECT_WINDOW_HOURS = 6


def run(g, alert_id: str, session: str, llm=None) -> dict:
    inc = memory.open_incident(g, alert_id)
    service = inc["service"]

    radius = T.blast_radius(g, service)
    deploys = T.recent_deploys(g, service, hours=SUSPECT_WINDOW_HOURS, now_ms=inc["alert_ts"])
    critical = T.critical_services(g, limit=10)

    at_risk = [{**row, "path": path} for row, path in zip(radius.results, radius.evidence)]
    customer_facing = [r["service"] for r in at_risk if r["tier"] == "edge"]
    suspects = [
        {**row, "minutes_before_alert": (inc["alert_ts"] - row["ts"]) // 60_000, "path": path}
        for row, path in zip(deploys.results, deploys.evidence)
    ]
    rank = next((i + 1 for i, r in enumerate(critical.results) if r["service"] == service), None)

    # Record which services are at risk so the UI and later sessions can see it in the graph.
    if at_risk:
        g.query(
            "MATCH (i:Incident {id: $inc}) UNWIND $rows AS r MATCH (s:Service {name: r.service}) "
            "MERGE (i)-[x:AT_RISK]->(s) SET x.hops = r.hops",
            {"inc": inc["incident"], "rows": [{"service": r["service"], "hops": r["hops"]} for r in at_risk]},
        )

    facts = {
        "incident": inc["incident"], "alert": alert_id, "signal": inc["signal"], "service": service,
        "severity": inc["severity"], "services_at_risk": len(at_risk), "customer_facing_at_risk": customer_facing,
        "criticality_rank": rank,
        "suspect_deploys": [
            {k: s[k] for k in ("deployment", "service", "summary", "author", "minutes_before_alert", "hops")}
            for s in suspects
        ],
    }
    if suspects:
        sus = "; ".join(f"[{s['deployment']}] on {s['service']} {s['minutes_before_alert']} min earlier"
                        for s in suspects[:3])
        sus_text = f"{len(suspects)} deploy(s) touched it or its neighbours in the {SUSPECT_WINDOW_HOURS} h before the alert: {sus}."
    else:
        sus_text = f"No deploys touched it or its neighbours in the {SUSPECT_WINDOW_HOURS} h before the alert."
    fallback = (
        f"{inc['signal']} on [{service}]. {len(at_risk)} services depend on it within 4 hops"
        + (f", including customer-facing {', '.join(customer_facing)}" if customer_facing else "")
        + f". {sus_text}"
    )
    summary, source = summarize(llm, "triage", ROLE, facts, fallback)

    findings = {**facts, "at_risk": at_risk, "suspects": suspects, "summary_source": source}
    cites = ([("Alert", alert_id), ("Service", f"svc-{service}")]
             + [("Service", f"svc-{r['service']}") for r in at_risk]
             + [("Deployment", s["deployment"]) for s in suspects])
    episode = memory.record_episode(
        g, agent="triage", incident=inc["incident"], session=session, kind="triage",
        summary=summary, findings=findings, cites=cites, handoff_to="investigator",
    )
    return {"agent": "triage", "episode": episode, "incident": inc["incident"], "summary": summary,
            "summary_source": source, "findings": findings,
            "cypher": radius.cypher + deploys.cypher + critical.cypher}

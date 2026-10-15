"""Responder agent: who should act, and what should they do?"""
from .. import memory
from ..tools import graph_tools as T
from .base import summarize

ROLE = "You decide who should act on the incident and propose concrete next steps."
ROLLBACK_THRESHOLD = 0.5


def run(g, incident: str, session: str, llm=None) -> dict:
    inv = memory.latest_episode(g, incident, "investigator")
    if inv is None:
        raise RuntimeError(f"no investigator episode for {incident}; run the investigator first")
    f = inv["findings"]
    service, cause = f["service"], f["likely_cause"]
    cause_service = cause["service"] if cause else service

    own_alert = T.owners(g, service)
    own_cause = T.owners(g, cause_service) if cause_service != service else own_alert
    fixers = T.fix_history(g, service)
    books = T.runbook(g, service)
    decisions = T.decision_trace(g, cause_service)

    def on_call(res):
        if res.error or not res.results:
            return None, None
        team = res.results[0]
        person = next((m for m in team["members"] if m["on_call"]), None)
        return team, person

    team_c, oncall_c = on_call(own_cause)
    team_a, oncall_a = on_call(own_alert)
    past = [p for p in f["similar_past_incidents"] if p["score"] >= 3]
    expert_name = past[0]["resolved_by"] if past else (fixers.results[0]["person"] if fixers.results else None)
    expert = next((r for r in fixers.results if r["person"] == expert_name), None)

    actions, cites = [], []
    if cause and cause["confidence"] >= ROLLBACK_THRESHOLD:
        actions.append(f"Roll back {cause['deployment']} ('{cause['summary']}') on {cause['service']}; "
                       f"author {cause['author']} should confirm.")
        cites.append(("Deployment", cause["deployment"]))
    elif cause:
        actions.append(f"Review {cause['deployment']} on {cause['service']} before rolling back "
                       f"(confidence only {cause['confidence']:.2f}).")
        cites.append(("Deployment", cause["deployment"]))
    if team_c and oncall_c:
        actions.append(f"Page {oncall_c['name']}, on call for team {team_c['team']} ({team_c['channel']}), "
                       f"which owns {cause_service}.")
        cites.append(("Person", oncall_c["id"]))
    if team_a and oncall_a and team_a["team"] != (team_c or {}).get("team"):
        actions.append(f"Notify {oncall_a['name']} on {team_a['team']} ({team_a['channel']}), owner of {service}.")
        cites.append(("Person", oncall_a["id"]))
    if expert and (not oncall_c or expert["person"] != oncall_c["name"]):
        why = f"resolved {past[0]['incident']} with the same signal" if past else f"has fixed {expert['fixes']} incident(s) here"
        actions.append(f"Loop in {expert['person']}, who {why}.")
        cites.append(("Person", expert["id"]))
    elif expert:
        actions.append(f"{expert['person']} is on call and has fixed this before.")
    for b in books.results:
        actions.append(f"Follow runbook {b['runbook']}: {b['steps']}")
        cites.append(("Runbook", b["runbook"]))
    for d in decisions.results[:1]:
        actions.append(f"Context: {d['title']} ({d['document_title']}). {d['summary']}")
        cites.append(("Document", d["document"]))

    g.query("MATCH (i:Incident {id: $inc}) SET i.status = 'mitigation_proposed'", {"inc": incident})

    facts = {
        "incident": incident, "service": service, "likely_cause": cause,
        "owning_team": team_c["team"] if team_c else None, "on_call": oncall_c["name"] if oncall_c else None,
        "expert": expert["person"] if expert else None, "actions": actions,
        "node_ids": sorted({node_id for _, node_id in cites}),
    }
    fallback = " ".join(f"{i}. {a}" for i, a in enumerate(actions[:3], 1)) or "No owner or runbook found."
    summary, source = summarize(llm, "responder", ROLE, facts, fallback)
    episode = memory.record_episode(
        g, agent="responder", incident=incident, session=session, kind="response",
        summary=summary, findings={**facts, "summary_source": source}, cites=cites,
    )
    return {"agent": "responder", "episode": episode, "incident": incident, "summary": summary,
            "summary_source": source, "findings": facts,
            "cypher": own_alert.cypher + fixers.cypher + books.cypher + decisions.cypher}

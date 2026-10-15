"""Investigator agent: which suspect is the likely cause, judged against past incidents?

It reads the Triage agent's Episode from the graph rather than receiving it in
memory, so the handoff works across processes and sessions.
"""
from .. import memory
from ..tools import graph_tools as T
from .base import summarize

ROLE = ("You compare the current incident with past ones and name the most likely cause, with a confidence. "
        "In similar_past_incidents, change_that_caused_it is the change that CAUSED that past incident, "
        "never the fix. Do not recommend actions; the Responder agent does that.")
WINDOW_MIN = 6 * 60


def score_suspect(suspect: dict, history_score: float) -> float:
    """Confidence in [0, 1] from three signals the graph gives us.

    recency   (35%): deployed closer to the alert scores higher
    proximity (25%): on the alerting service or a direct neighbour scores higher
    history   (40%): a past incident here had the same signal and a similar cause
    """
    recency = max(0.0, 1 - suspect["minutes_before_alert"] / WINDOW_MIN)
    proximity = 1.0 if suspect["hops"] <= 1 else 0.5
    history = min(1.0, history_score / 5)
    return round(0.35 * recency + 0.25 * proximity + 0.40 * history, 2)


def run(g, incident: str, session: str, llm=None) -> dict:
    triage = memory.latest_episode(g, incident, "triage")
    if triage is None:
        raise RuntimeError(f"no triage episode for {incident}; run triage first")
    tf = triage["findings"]
    service, signal = tf["service"], tf["signal"]

    assessed, cypher = [], []
    for s in tf["suspects"]:
        sim = T.similar_incidents(g, service, signal, s["summary"], s["service"])
        cypher = sim.cypher
        best = sim.results[0] if sim.results else None
        assessed.append({
            "deployment": s["deployment"], "service": s["service"], "summary": s["summary"],
            "author": s["author"], "minutes_before_alert": s["minutes_before_alert"], "hops": s["hops"],
            "confidence": score_suspect(s, best["score"] if best else 0.0),
            "best_match": best,
        })
    assessed.sort(key=lambda a: -a["confidence"])
    likely = assessed[0] if assessed else None

    if likely is None:  # nothing changed recently: still look at history for this service
        sim = T.similar_incidents(g, service, signal)
        cypher = sim.cypher
        history = sim.results[:3]
    else:
        history = [a["best_match"] for a in assessed if a["best_match"]][:3]

    for a in assessed:
        g.query(
            "MATCH (i:Incident {id: $inc}), (d:Deployment {id: $dep}) "
            "MERGE (i)-[r:SUSPECTED_CAUSE]->(d) SET r.confidence = $c",
            {"inc": incident, "dep": a["deployment"], "c": a["confidence"]},
        )
    for h in history:
        if h and h["score"] >= 3:
            g.query(
                "MATCH (i:Incident {id: $inc}), (p:Incident {id: $past}) "
                "MERGE (i)-[r:SIMILAR_TO]->(p) SET r.score = $s",
                {"inc": incident, "past": h["incident"], "s": h["score"]},
            )

    facts = {
        "incident": incident, "service": service, "signal": signal,
        "likely_cause": None if likely is None else {k: likely[k] for k in
                        ("deployment", "service", "summary", "author", "minutes_before_alert", "confidence")},
        "other_suspects": [{"deployment": a["deployment"], "confidence": a["confidence"]} for a in assessed[1:]],
        "similar_past_incidents": [
            {k: h[k] for k in ("incident", "title", "change_that_caused_it", "resolved_by", "minutes_to_resolve", "score")}
            for h in history if h
        ],
    }
    if likely:
        match = likely["best_match"]
        past = (f" A past incident [{match['incident']}] had the same signal; its cause was "
                f"'{match['change_that_caused_it']}' and {match['resolved_by']} resolved it in "
                f"{match['minutes_to_resolve']} min." if match and match["score"] >= 3 else "")
        fallback = (f"Likely cause: [{likely['deployment']}] '{likely['summary']}' on {likely['service']}, "
                    f"deployed {likely['minutes_before_alert']} min before the alert "
                    f"(confidence {likely['confidence']:.2f}).{past}")
    else:
        fallback = (f"No recent deploy explains this alert on [{service}]. "
                    f"Check infrastructure and traffic; {len(history)} past incident(s) on this service may help.")
    summary, source = summarize(llm, "investigator", ROLE, facts, fallback)

    cites = ([("Deployment", a["deployment"]) for a in assessed]
             + [("Incident", h["incident"]) for h in history if h])
    episode = memory.record_episode(
        g, agent="investigator", incident=incident, session=session, kind="investigation",
        summary=summary, findings={**facts, "assessed": assessed, "summary_source": source},
        cites=cites, handoff_to="responder", confidence=likely["confidence"] if likely else 0.0,
    )
    return {"agent": "investigator", "episode": episode, "incident": incident, "summary": summary,
            "summary_source": source, "findings": facts, "cypher": cypher}

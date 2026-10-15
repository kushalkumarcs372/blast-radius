"""Command line entry points.

  python -m blast_radius.cli seed            load all tenants into FalkorDB
  python -m blast_radius.cli demo            run every graph tool on the demo scenario
  python -m blast_radius.cli tool NAME ...   run one tool, e.g. tool blast_radius service=kafka
  python -m blast_radius.cli alerts          list alerts with no incident yet
  python -m blast_radius.cli respond [ALERT] run Triage -> Investigator -> Responder (default: newest alert)
  python -m blast_radius.cli incidents       list open incidents
  python -m blast_radius.cli memory INC      show everything agents recorded on an incident
  python -m blast_radius.cli ask "QUESTION" [--session S] [--incident INC]
  python -m blast_radius.cli llm-check       test the Gemini and Groq keys

Add --tenant NAME to any command (default: acme).
"""
import json
import sys
import time

from .db import graph, list_tenants
from .seed.generator import TENANTS, generate
from .seed.loader import load
from . import memory
from .tools.graph_tools import TOOLS


def seed() -> None:
    now_ms = int(time.time() * 1000)
    for tenant in TENANTS:
        data = generate(tenant, now_ms)
        start = time.perf_counter()
        load(graph(tenant), data)
        n, e = data.count()
        print(f"tenant_{tenant}: {n} nodes, {e} edges loaded in {time.perf_counter() - start:.1f}s")


def _show(result) -> None:
    print(f"\n== {result.tool} {result.params}")
    if result.error:
        print("   error:", result.error)
        return
    paths = result.evidence if len(result.evidence) == len(result.results) else [None] * len(result.results)
    for row, path in zip(result.results, paths):
        line = "   " + ", ".join(f"{k}={v}" for k, v in row.items() if k != "members")
        print(line)
        if path:
            print("     path:", " -> ".join(map(str, path)))
        for m in row.get("members", []):
            print(f"     - {m['name']} ({m['role']}){'  ON CALL' if m['on_call'] else ''}")


def demo(tenant: str = "acme") -> None:
    g = graph(tenant)
    print(f"Demo on tenant_{tenant}: alert 'Connection pool exhausted' on postgres-payments")
    _show(TOOLS["blast_radius"](g, "postgres-payments"))
    _show(TOOLS["recent_deploys"](g, "postgres-payments", hours=6))
    _show(TOOLS["owners"](g, "payments"))
    _show(TOOLS["fix_history"](g, "postgres-payments"))
    _show(TOOLS["connection_path"](g, "web-frontend", "postgres-payments"))
    _show(TOOLS["critical_services"](g, limit=5))


def run_tool(name: str, args: list[str], tenant: str = "acme") -> None:
    kwargs = {}
    for a in args:
        k, v = a.split("=", 1)
        kwargs[k] = int(v) if v.isdigit() else v
    print(json.dumps(TOOLS[name](graph(tenant), **kwargs).to_dict(), indent=2))


def _opt(argv: list[str], flag: str, default=None):
    if flag in argv:
        i = argv.index(flag)
        value = argv[i + 1]
        del argv[i : i + 2]
        return value
    return default


def respond(tenant: str, alert: str | None) -> None:
    from .agents.orchestrator import handle_alert
    from .llm import from_env

    g = graph(tenant)
    if alert is None:
        alerts = TOOLS["open_alerts"](g).results
        if not alerts:
            print("No open alerts. Run `seed` to reset the demo.")
            return
        alert = alerts[0]["alert"]
    out = handle_alert(g, alert, llm=from_env())
    print(f"Incident {out['incident']}  (session {out['session']}, {out['total_ms']} ms)")
    for step in out["steps"]:
        print(f"\n[{step['agent'].upper()}] episode {step['episode']}  ({step['ms']} ms, summary by {step['summary_source']})")
        print("  " + step["summary"])
    actions = out["steps"][-1]["findings"]["actions"]
    if actions:
        print("\nProposed actions:")
        for i, a in enumerate(actions, 1):
            print(f"  {i}. {a}")


def show_memory(tenant: str, incident: str) -> None:
    m = memory.incident_memory(graph(tenant), incident)
    if "error" in m:
        print(m["error"])
        return
    print(f"{m['incident']}: {m['title']}  [{m['status']}]")
    for c in m["suspected_causes"]:
        print(f"  suspected: {c['deployment']} on {c['service']} ({c['confidence']:.2f}) - {c['summary']}")
    for e in m["episodes"]:
        nxt = f" -> {e['handed_off_to']}" if e["handed_off_to"] else ""
        print(f"\n  {e['agent']}{nxt}  ({e['kind']}, {e['session']})")
        print(f"    {e['summary']}")
        print(f"    cites: {', '.join(e['cites'][:12])}{' ...' if len(e['cites']) > 12 else ''}")


def ask_cmd(tenant: str, argv: list[str]) -> None:
    from .agents.ask import ask
    from .llm import from_env

    session = _opt(argv, "--session", "cli")
    incident = _opt(argv, "--incident")
    out = ask(graph(tenant), " ".join(argv), from_env(), session, incident)
    for s in out.get("tools", []):
        print(f"  tool: {s['tool']}({s['args']})")
    print("\n" + out["answer"])
    if out.get("invented_ids"):
        print(f"\n(warning: cited ids not found in tool results: {out['invented_ids']})")


def llm_check() -> None:
    from .llm import LLM, from_env

    llm = from_env()
    if not llm.providers:
        print("No keys found. Put GEMINI_API_KEY and/or GROQ_API_KEY in .env")
    for p in llm.providers:
        try:
            out = LLM([p]).complete_json("Reply with JSON.", 'Return {"ok": true}')
            print(f"{p.name} ({p.model}): OK {out}")
        except Exception as exc:
            print(f"{p.name} ({p.model}): FAILED {exc}")


def main(argv: list[str]) -> None:
    tenant = _opt(argv, "--tenant", "acme")
    cmd = argv[0] if argv else "help"
    if cmd == "seed":
        seed()
        print("tenants:", ", ".join(list_tenants()))
    elif cmd == "demo":
        demo(tenant)
    elif cmd == "tool" and len(argv) >= 2:
        run_tool(argv[1], argv[2:], tenant)
    elif cmd == "alerts":
        for a in TOOLS["open_alerts"](graph(tenant)).results:
            print(f"{a['alert']}: {a['signal']} on {a['service']} ({a['severity']})")
    elif cmd == "respond":
        respond(tenant, argv[1] if len(argv) > 1 else None)
    elif cmd == "incidents":
        for i in memory.list_incidents(graph(tenant)):
            print(f"{i['incident']}: {i['title']} [{i['status']}], {i['episodes']} episodes")
    elif cmd == "memory" and len(argv) == 2:
        show_memory(tenant, argv[1])
    elif cmd == "ask" and len(argv) >= 2:
        ask_cmd(tenant, argv[1:])
    elif cmd == "llm-check":
        llm_check()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

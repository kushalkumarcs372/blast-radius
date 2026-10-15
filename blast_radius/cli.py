"""Command line entry points.

  python -m blast_radius.cli seed            load all tenants into FalkorDB
  python -m blast_radius.cli demo            run every graph tool on the demo scenario
  python -m blast_radius.cli tool NAME ...   run one tool, e.g. tool blast_radius service=kafka
"""
import json
import sys
import time

from .db import graph, list_tenants
from .seed.generator import TENANTS, generate
from .seed.loader import load
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


def main(argv: list[str]) -> None:
    if not argv or argv[0] == "seed":
        seed()
        print("tenants:", ", ".join(list_tenants()))
    elif argv[0] == "demo":
        demo(*argv[1:2])
    elif argv[0] == "tool" and len(argv) >= 2:
        run_tool(argv[1], argv[2:])
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

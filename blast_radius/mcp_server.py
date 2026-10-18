"""MCP server: exposes the graph tools to any MCP client (Claude Desktop, IDEs, other agents).

Run:  python -m blast_radius.mcp_server
The client sees one tool per graph query. It chooses which to call; the Cypher stays fixed.
"""
from mcp.server.fastmcp import FastMCP

from . import memory
from .db import graph, list_tenants
from .tools import graph_tools as T

mcp = FastMCP("blast-radius")


def _g(tenant: str):
    return graph(tenant)


@mcp.tool()
def tenants() -> list[str]:
    """List the companies (tenants) available. Each one is an isolated FalkorDB graph."""
    return list_tenants()


@mcp.tool()
def open_alerts(tenant: str = "acme") -> dict:
    """Alerts that have fired but have no incident yet."""
    return T.open_alerts(_g(tenant)).to_dict()


@mcp.tool()
def blast_radius(service: str, max_hops: int = 4, tenant: str = "acme") -> dict:
    """Services that break if `service` goes down, with the dependency path to each."""
    return T.blast_radius(_g(tenant), service, max_hops).to_dict()


@mcp.tool()
def dependencies(service: str, max_hops: int = 4, tenant: str = "acme") -> dict:
    """Everything `service` depends on, directly or indirectly."""
    return T.dependencies(_g(tenant), service, max_hops).to_dict()


@mcp.tool()
def recent_deploys(service: str, hours: int = 24, tenant: str = "acme") -> dict:
    """Deploys in the last `hours` to `service` or any service within 2 dependency hops."""
    return T.recent_deploys(_g(tenant), service, hours).to_dict()


@mcp.tool()
def critical_services(limit: int = 10, tenant: str = "acme") -> dict:
    """Most critical services by PageRank over the dependency graph."""
    return T.critical_services(_g(tenant), limit).to_dict()


@mcp.tool()
def connection_path(source: str, target: str, tenant: str = "acme") -> dict:
    """How two services are connected through dependencies."""
    return T.connection_path(_g(tenant), source, target).to_dict()


@mcp.tool()
def owners(service: str, tenant: str = "acme") -> dict:
    """The team that owns `service`, its chat channel, members and who is on call."""
    return T.owners(_g(tenant), service).to_dict()


@mcp.tool()
def fix_history(service: str, tenant: str = "acme") -> dict:
    """People who resolved past incidents on `service`, ranked by count."""
    return T.fix_history(_g(tenant), service).to_dict()


@mcp.tool()
def similar_incidents(service: str, signal: str = "", tenant: str = "acme") -> dict:
    """Past resolved incidents on `service`, scored by similarity to the given alert signal."""
    return T.similar_incidents(_g(tenant), service, signal).to_dict()


@mcp.tool()
def runbook(service: str, tenant: str = "acme") -> dict:
    """The on-call runbook for `service`."""
    return T.runbook(_g(tenant), service).to_dict()


@mcp.tool()
def decision_trace(service: str, tenant: str = "acme") -> dict:
    """Design decisions about `service` and the documents they come from."""
    return T.decision_trace(_g(tenant), service).to_dict()


@mcp.tool()
def list_services(tenant: str = "acme") -> dict:
    """Every service with its tier, owning team and number of direct dependencies."""
    return T.list_services(_g(tenant)).to_dict()


@mcp.tool()
def documents(service: str, tenant: str = "acme") -> dict:
    """Design decisions and incident postmortems connected to a service."""
    return T.documents(_g(tenant), service).to_dict()


@mcp.tool()
def what_if_cut(service: str, cut_from: str, cut_to: str, tenant: str = "acme") -> dict:
    """Blast radius of `service` if the dependency cut_from -> cut_to had a circuit breaker or fallback."""
    return T.what_if_cut(_g(tenant), service, cut_from, cut_to).to_dict()


@mcp.tool()
def breaker_advice(service: str, tenant: str = "acme") -> dict:
    """Where a single circuit breaker would protect the most services from an outage of `service`."""
    return T.breaker_advice(_g(tenant), service).to_dict()


@mcp.tool()
def resolve_incident(incident: str, tenant: str = "acme") -> dict:
    """Close an incident with its confirmed cause and fixer, so future alerts learn from it."""
    return memory.resolve_incident(_g(tenant), incident, "mcp")


@mcp.tool()
def incident_memory(incident: str, tenant: str = "acme") -> dict:
    """Everything the agents recorded on an incident: findings, suspected causes, citations."""
    return memory.incident_memory(_g(tenant), incident)


@mcp.tool()
def list_incidents(tenant: str = "acme") -> list[dict]:
    """Open incidents the agents are working on."""
    return memory.list_incidents(_g(tenant))


@mcp.tool()
def respond_to_alert(alert: str, tenant: str = "acme") -> dict:
    """Run Triage, Investigator and Responder on an alert. Writes findings into the graph."""
    from .agents.orchestrator import handle_alert

    out = handle_alert(_g(tenant), alert, llm=None)  # the MCP client is the LLM here
    return {"incident": out["incident"], "session": out["session"],
            "steps": [{"agent": s["agent"], "summary": s["summary"], "findings": s["findings"]} for s in out["steps"]]}


if __name__ == "__main__":
    mcp.run()

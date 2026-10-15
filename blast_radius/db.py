"""FalkorDB connection and tenant isolation.

Every company (tenant) lives in its own named graph, e.g. ``tenant_acme``.
One FalkorDB instance holds all of them, and a query against one graph can
never see nodes from another. This is how we isolate data per tenant.
"""
import re

from falkordb import FalkorDB

from .config import settings

_TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_client: FalkorDB | None = None


def client() -> FalkorDB:
    global _client
    if _client is None:
        _client = FalkorDB(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            username=settings.falkordb_username,
            password=settings.falkordb_password,
            ssl=settings.falkordb_tls,
        )
    return _client


def graph_name(tenant: str) -> str:
    if not _TENANT_RE.match(tenant):
        raise ValueError(f"invalid tenant name: {tenant!r}")
    return f"tenant_{tenant}"


def graph(tenant: str):
    """Return the isolated graph for one tenant."""
    return client().select_graph(graph_name(tenant))


def list_tenants() -> list[str]:
    return sorted(g.removeprefix("tenant_") for g in client().list_graphs() if g.startswith("tenant_"))

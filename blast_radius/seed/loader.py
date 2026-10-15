"""Load generated company data into a tenant's FalkorDB graph using batched UNWIND."""
from collections import defaultdict

from ..schema import create_indexes
from .generator import CompanyData

BATCH = 200


def _batches(rows: list, size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def load(g, data: CompanyData, reset: bool = True) -> None:
    if reset:
        try:
            g.delete()
        except Exception:  # graph does not exist yet
            pass
    create_indexes(g)

    for label, rows in data.nodes.items():
        # Group rows by their set of keys so every CREATE lists exactly the properties present.
        by_keys: dict[tuple, list] = defaultdict(list)
        for row in rows:
            by_keys[tuple(sorted(row))].append(row)
        for keys, group in by_keys.items():
            props = ", ".join(f"{k}: r.{k}" for k in keys)
            query = f"UNWIND $rows AS r CREATE (:{label} {{{props}}})"
            for batch in _batches(group):
                g.query(query, {"rows": batch})

    for rel, edges in data.edges.items():
        by_labels: dict[tuple, list] = defaultdict(list)
        for src_label, src_id, dst_label, dst_id, props in edges:
            if props:
                raise ValueError("edge properties are not supported by the loader yet")
            by_labels[(src_label, dst_label)].append({"s": src_id, "d": dst_id})
        for (src_label, dst_label), rows in by_labels.items():
            query = (
                f"UNWIND $rows AS r "
                f"MATCH (a:{src_label} {{id: r.s}}), (b:{dst_label} {{id: r.d}}) "
                f"CREATE (a)-[:{rel}]->(b)"
            )
            for batch in _batches(rows):
                g.query(query, {"rows": batch})

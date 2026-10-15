"""Graph schema: node labels, relationship types and indexes.

Nodes
  Service, Deployment, Alert, Incident, Runbook, Person, Team, Decision, Document
  (Agent and Episode are added on Day 2 for agent memory)

Relationships
  (Person)-[:MEMBER_OF]->(Team)-[:OWNS]->(Service)
  (Service)-[:DEPENDS_ON]->(Service)
  (Person)-[:AUTHORED]->(Deployment)-[:CHANGED]->(Service)
  (Alert)-[:FIRED_ON]->(Service)
  (Incident)-[:AFFECTED]->(Service)
  (Incident)-[:CAUSED_BY]->(Deployment)
  (Incident)-[:TRIGGERED_BY]->(Alert)
  (Person)-[:RESOLVED]->(Incident)
  (Runbook)-[:FOR]->(Service)
  (Decision)-[:ABOUT]->(Service)
  (Decision)-[:SOURCED_FROM]->(Document)
  (Document)-[:MENTIONS]->(Incident)
"""

LABELS = [
    "Service", "Deployment", "Alert", "Incident", "Runbook",
    "Person", "Team", "Decision", "Document",
]

# Every node has a unique string `id`; services and teams are also looked up by name.
INDEXES = [f"CREATE INDEX FOR (n:{label}) ON (n.id)" for label in LABELS] + [
    "CREATE INDEX FOR (s:Service) ON (s.name)",
    "CREATE INDEX FOR (t:Team) ON (t.name)",
    "CREATE INDEX FOR (d:Deployment) ON (d.ts)",
]


def create_indexes(g) -> None:
    for stmt in INDEXES:
        try:
            g.query(stmt)
        except Exception as exc:  # index already exists on re-run
            if "already" not in str(exc).lower():
                raise

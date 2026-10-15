# blast-radius

AI on-call agents that trace blast radius, root cause and ownership through a FalkorDB knowledge graph. Built for **Graph Hacks 2026** (WeMakeDevs × FalkorDB).

**Tracks entered:** 01 Agents That Act on Connected Data (primary) · 02 Agent Memory and Coordination · 03 Company Brain

> Status: Day 1 of 4 — graph model, synthetic data and graph tools. Agents, UI and deployment follow.

## The problem

When an alert fires, the on-call engineer needs four answers fast: what else is affected, what caused it, who should fix it, and has this happened before. That context is spread across dependency maps, deploy logs, ownership pages and old postmortems. Searching documents one at a time misses how those things connect.

## The approach

Blast Radius keeps that context in a FalkorDB graph and answers each question with a fixed, tested Cypher query. Every answer carries the graph path it came from, so it can be checked.

## Quick start (local)

Requirements: Docker Desktop, Python 3.12.

```bash
docker compose up -d                 # FalkorDB on :6379, Browser on http://localhost:3000
py -3.12 -m venv .venv               # Windows; on macOS/Linux: python3.12 -m venv .venv
.venv\Scripts\activate               # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env               # macOS/Linux: cp .env.example .env

python -m blast_radius.cli seed      # load 3 synthetic companies
python -m blast_radius.cli demo      # run the graph tools on the demo incident
pytest                               # data checks + query checks against FalkorDB
```

Run one tool directly:

```bash
python -m blast_radius.cli tool blast_radius service=kafka
python -m blast_radius.cli tool connection_path source=mobile-bff target=token-store
```

## Graph data model

Each company is a separate named graph (`tenant_acme`, `tenant_globex`, `tenant_initech`) in one FalkorDB instance, so tenants never see each other's data.

| Node | Key properties |
| --- | --- |
| Service | id, name, tier, language |
| Team | id, name, channel |
| Person | id, name, role, on_call |
| Deployment | id, version, ts (epoch ms), summary, service |
| Alert | id, signal, severity, ts, service |
| Incident | id, title, severity, status, started, resolved |
| Runbook | id, title, steps |
| Decision | id, title, summary, date |
| Document | id, title, type (postmortem, adr), source |

```
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
```

Indexes: `id` on every label, plus `Service.name`, `Team.name` and `Deployment.ts` (see `blast_radius/schema.py`).

The `acme` tenant has 38 services, 54 dependencies, 9 teams, about 30 people, 152 deployments, 26 incidents, runbooks and design decisions (about 330 nodes and 630 edges).

## Graph tools and their Cypher

All queries live in `blast_radius/tools/graph_tools.py`. Hop limits are validated integers; everything else is passed as a query parameter.

| Tool | Question | Core Cypher |
| --- | --- | --- |
| `blast_radius` | What breaks if X goes down? | `MATCH p = (up:Service)-[:DEPENDS_ON*1..4]->(s:Service {name: $service}) RETURN up.name, length(p), [n IN nodes(p) \| n.name]` |
| `dependencies` | What does X depend on? | `MATCH p = (s:Service {name: $service})-[:DEPENDS_ON*1..4]->(d:Service) RETURN ...` |
| `recent_deploys` | Which deploys touched X or services within 2 hops? | `MATCH p = (s:Service {name: $service})-[:DEPENDS_ON*1..2]-(n:Service) MATCH (a:Person)-[:AUTHORED]->(d:Deployment)-[:CHANGED]->(n) WHERE d.ts >= $since RETURN ...` |
| `critical_services` | Which services matter most? | `CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score RETURN node.name, score ORDER BY score DESC LIMIT $limit` |
| `connection_path` | How are A and B connected? | `WITH shortestPath((a)-[:DEPENDS_ON*]->(b)) AS p RETURN [n IN nodes(p) \| n.name]` |
| `owners` | Who owns X, who is on call? | `MATCH (t:Team)-[:OWNS]->(s:Service {name: $service}) OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(t) RETURN ...` |
| `fix_history` | Who has fixed X before? | `MATCH (p:Person)-[:RESOLVED]->(i:Incident)-[:AFFECTED]->(s:Service {name: $service}) RETURN p.name, count(DISTINCT i) ...` |

## How the queries are verified

`tests/test_graph_tools.py` loads a generated company into FalkorDB, runs each tool, and compares the result with an independent breadth-first search in plain Python (`blast_radius/seed/reference.py`) over the same data. `tests/test_generator.py` checks the data itself: no dependency cycles, every edge points at a real node, one owner per service, one on-call person per team, no future timestamps.

## Data

All data is synthetic and generated by `blast_radius/seed/generator.py`. No real company, person or private information is used.

## AI assistance disclosure

This project is built with Claude (Anthropic) as an AI coding assistant, as allowed by the hackathon rules. Claude drafted most of the code; I ran and verified every module, reviewed the queries, and made the design decisions. *(Kushal: rewrite this section in your own words before submitting.)*

## License

MIT

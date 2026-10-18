# blast-radius

AI on-call agents that trace blast radius, root cause and ownership through a FalkorDB knowledge graph. Built for **Graph Hacks 2026** (WeMakeDevs × FalkorDB).

**Tracks entered:** 01 Agents That Act on Connected Data (primary) · 02 Agent Memory and Coordination · 03 Company Brain

> Status: Day 3 of 4 — graph tools, cooperating agents with graph memory, a question-answering agent, an MCP server and a web UI.

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
python -m blast_radius.cli respond   # Triage -> Investigator -> Responder on the open alert
pytest                               # data, query, agent and LLM checks
```

### Web UI

```bash
uvicorn blast_radius.api:app --reload
```

Open http://localhost:8000. Click **Run agents** on the open alert: the map dims everything except the services at risk (orange), the alerting service (red) and the link to the suspect deploy (blue dashed). The right panel shows each agent's summary with the graph nodes it cites, the Cypher it ran, the proposed actions, and an **Ask the graph** box. Click any service on the map to preview its blast radius.

### Describe an incident in your own words

The **Describe an incident** box (or `python -m blast_radius.cli report "..."`) takes any free-text report, for example *"Checkout is throwing 500 errors for the last 15 minutes. We pushed a pricing rules change about 25 minutes ago."* The LLM maps it onto the company's graph: the failing service, the symptom (reusing a known signal when one fits), the severity, and any changes mentioned. Every service it names is checked against the graph, so it cannot invent one; anything that does not match is shown as ignored. The facts are written to FalkorDB as an `Alert` (and `Deployment` nodes for reported changes), and the three agents investigate it like any other alert. Without an LLM key, a keyword fallback finds service names in the text.

### MCP server

The same graph tools are exposed over the Model Context Protocol, so any MCP client can drive them. For Claude Desktop, add to `claude_desktop_config.json` (adjust the paths):

```json
{
  "mcpServers": {
    "blast-radius": {
      "command": "D:\\path\\to\\blast-radius\\.venv\\Scripts\\python.exe",
      "args": ["-m", "blast_radius.mcp_server"],
      "cwd": "D:\\path\\to\\blast-radius"
    }
  }
}
```

Tools exposed: `tenants`, `open_alerts`, `blast_radius`, `dependencies`, `recent_deploys`, `critical_services`, `connection_path`, `owners`, `fix_history`, `similar_incidents`, `runbook`, `decision_trace`, `incident_memory`, `list_incidents`, `respond_to_alert`.

### Command line

Agents work without API keys (they fall back to template summaries). For LLM-written summaries and free-form questions, add `GEMINI_API_KEY` and/or `GROQ_API_KEY` to `.env`, then:

```bash
python -m blast_radius.cli llm-check
python -m blast_radius.cli ask "What caused the payments alert and who fixed it last time?"
python -m blast_radius.cli memory inc-alert-demo-live
```

Run one tool directly:

```bash
python -m blast_radius.cli tool blast_radius service=kafka
python -m blast_radius.cli tool connection_path source=mobile-bff target=token-store
```

## Agents

Three agents handle an alert in order. They do not pass findings to each other in code: each writes an `Episode` node and the next one reads it back from the graph, so a later session (or another process) sees exactly the same shared state.

| Agent | Reads from the graph | Writes to the graph |
| --- | --- | --- |
| Triage | `blast_radius`, `recent_deploys` (6 h before the alert), `critical_services` | `Incident`, `AT_RISK` edges, Episode citing the alert, services and suspect deploys |
| Investigator | Triage's Episode, `similar_incidents` for each suspect | `SUSPECTED_CAUSE {confidence}`, `SIMILAR_TO {score}`, Episode citing deploys and past incidents |
| Responder | Investigator's Episode, `owners`, `fix_history`, `runbook`, `decision_trace` | Proposed actions, incident status, Episode citing people, runbook and design docs |
| Assistant | Any tool above plus incident memory, chosen by the LLM one call at a time | Each question and answer as an Episode, so the conversation continues across sessions |

**The graph decides, the LLM explains.** Which services are at risk, which deploy is suspected, and who to page all come from Cypher. The LLM writes the summaries and, for follow-up questions, chooses which fixed tool to call; it never writes Cypher. A summary that cites a node id not present in the facts is rejected and replaced by a template. Gemini is tried first and Groq is the fallback; if both are rate limited the agents still finish.

Suspect confidence = 35% recency (deployed closer to the alert) + 25% proximity (on the alerting service or a direct neighbour) + 40% history (a past incident with the same signal and a similar change).

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

Agent memory:

```
(Agent)-[:RECORDED]->(Episode)-[:ABOUT]->(Incident)
(Episode)-[:HANDED_OFF_TO]->(Agent)
(Episode)-[:CITES]->(Service | Deployment | Person | Incident | Runbook | Document | Alert)
(Incident)-[:AT_RISK {hops}]->(Service)
(Incident)-[:SUSPECTED_CAUSE {confidence}]->(Deployment)
(Incident)-[:SIMILAR_TO {score}]->(Incident)
```

Episodic memory is the `Episode` nodes, semantic memory is the company graph, and procedural memory is `Runbook` nodes plus `SIMILAR_TO` links to past fixes.

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
| `similar_incidents` | Which past incidents look like this one? | `MATCH (past:Incident)-[:AFFECTED]->(s:Service {name: $service}) WHERE past.status = 'resolved' OPTIONAL MATCH (past)-[:CAUSED_BY]->(d:Deployment)-[:CHANGED]->(ds:Service) ...` |
| `runbook` | How do I handle X? | `MATCH (r:Runbook)-[:FOR]->(s:Service {name: $service}) RETURN ...` |
| `decision_trace` | Why is X built this way? | `MATCH (d:Decision)-[:ABOUT]->(s:Service {name: $service}) MATCH (d)-[:SOURCED_FROM]->(doc:Document) RETURN ...` |
| `open_alerts` | Which alerts have no incident yet? | `MATCH (a:Alert)-[:FIRED_ON]->(s:Service) OPTIONAL MATCH (i:Incident)-[:TRIGGERED_BY]->(a) WITH a, s, i WHERE i IS NULL RETURN ...` |

## How the queries are verified

`tests/test_graph_tools.py` loads a generated company into FalkorDB, runs each tool, and compares the result with an independent breadth-first search in plain Python (`blast_radius/seed/reference.py`) over the same data. `tests/test_agents.py` runs the three agents and the assistant on FalkorDB with a scripted fake LLM, so no API key is needed: it checks the agents pick the right deploy, write the handoff chain into the graph, and that memory is readable from a new connection. `tests/test_llm.py` checks provider fallback and that invented citations are rejected. `tests/test_generator.py` checks the data itself: no dependency cycles, every edge points at a real node, one owner per service, one on-call person per team, no future timestamps.

## Deployment

The API and UI ship as one Docker image (`Dockerfile`); FalkorDB runs as a managed FalkorDB Cloud instance. `render.yaml` describes the Render web service. Set `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`, `GROQ_API_KEY` and `GEMINI_API_KEY` as environment variables. On first start against an empty database the API loads the synthetic companies. `/api/ask` is rate limited per IP to protect the free LLM quotas.

## Data

All data is synthetic and generated by `blast_radius/seed/generator.py`. No real company, person or private information is used.

## AI assistance disclosure

I used Claude (Anthropic) as an AI coding assistant. It drafted most of the code under my direction; I ran and tested every module, reviewed the graph queries, and made the design decisions.

**Live demo:** https://blast-radius-duqf.onrender.com (free tier: the first load after idle can take ~1 minute; use "Reset demo" to fire the demo alert again)

## License

MIT

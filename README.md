# BlastRadius

**On-call agents that trace blast radius, root cause and ownership through a FalkorDB knowledge graph.**

Built for **Graph Hacks 2026** (WeMakeDevs × FalkorDB).

| | |
| --- | --- |
| **Live demo** | https://blast-radius-duqf.onrender.com (free tier: the first load after idle can take about a minute) |
| **Tracks** | 01 Agents That Act on Connected Data (primary) · 02 Agent Memory and Coordination · 03 Company Brain |
| **Stack** | FalkorDB Cloud · Python 3.12 · FastAPI · Groq `openai/gpt-oss-120b` (Gemini fallback) · Cytoscape.js · MCP · Render |

---

## The problem

When an alert fires at 2 AM, the on-call engineer needs four answers fast:

1. **What else is affected?**
2. **What changed?**
3. **Who should fix it, and who has fixed it before?**
4. **Has this happened before, and what did we learn?**

The answers live in the *relationships* between services, deploys, teams, people, runbooks and old postmortems, and that context is spread across dependency maps, deploy logs, ownership pages and wikis. Searching documents one at a time misses how those things connect.

## The approach

BlastRadius keeps that context in a **FalkorDB graph** and lets three cooperating agents work on it.

> **The graph decides, the LLM explains.** Which services are at risk, which deploy is suspected and who to page all come from tested Cypher queries. The LLM writes the summaries and chooses which fixed tool to call for follow-up questions. It never writes Cypher, and every id it cites is checked against FalkorDB.

```
 alert ──► Triage ──Episode──► Investigator ──Episode──► Responder ──► actions
             │                      │                        │
             ▼                      ▼                        ▼
      blast radius,          score suspects,          owners, on-call,
      nearby deploys,        match past incidents     past fixers, runbook,
      PageRank criticality                            circuit-breaker advice
                     all reads and writes go through FalkorDB
```

## What it can do

- **Run agents on a live alert.** Three agents hand off through the graph. The map replays the traversal hop by hop: the alerting service in red, services at risk in orange, the suspect deploy as a blue dashed link.
- **Describe any company in plain English (sandbox).** Paste a paragraph about services, teams, deploys, past incidents and what is failing now. The LLM extracts a graph, the code validates it and loads it into FalkorDB, and the agents investigate immediately. Nothing is scripted.
- **Describe an incident in your own words.** A free-text report ("checkout is throwing 500s, we pushed a pricing change 25 minutes ago") is mapped onto the existing graph and investigated. Reported changes count as *unverified* claims.
- **What if…** Simulate a circuit breaker on any dependency. The graph recomputes the blast radius and shows which services would have stayed up. `breaker_advice` names the single best place for one.
- **Resolve and learn.** *Mark resolved* writes the confirmed cause, the fixer and a postmortem into the graph. The next similar alert finds that incident as precedent.
- **Ask the graph.** A tool-using assistant answers multi-part questions with 17 graph tools and remembers the conversation across sessions.
- **MCP server.** The same tools are exposed over the Model Context Protocol, for any MCP client.

## Agents

The agents do not pass findings to each other in code. Each one writes an `Episode` node and the next reads it back from the graph, so a later session or another process sees the same shared state.

| Agent | Reads from the graph | Writes to the graph |
| --- | --- | --- |
| **Triage** | `blast_radius`, `recent_deploys` (6 h before the alert), `critical_services` | `Incident`, `AT_RISK` edges, Episode citing the alert, services and suspect deploys |
| **Investigator** | Triage's Episode, `similar_incidents` for each suspect | `SUSPECTED_CAUSE {confidence}`, `SIMILAR_TO {score}`, Episode citing deploys and past incidents |
| **Responder** | Investigator's Episode, `owners`, `fix_history`, `runbook`, `decision_trace`, `breaker_advice` | Proposed actions, incident status, Episode citing people, runbooks and documents |
| **Assistant** | Any tool plus incident memory, up to 4 tools per step and 8 steps | Each question and answer as an Episode, so the conversation continues across sessions |

**Suspect confidence** = 35% recency + 25% graph proximity + 40% history (a past incident with the same signal and a similar change). A change that was only *reported* in free text counts 20% less, and 50% less when real deploy records are also suspects.

### Guards against hallucination

| Guard | What it does |
| --- | --- |
| Citation check | A summary that cites an id not present in the agent's graph facts is rejected and replaced by a template built from the graph. The UI shows which id was blocked. |
| Invented citations in answers | Any `[id]` in an assistant answer that is not in a tool result is removed and reported under the answer. |
| Tools before answers | The assistant must call at least one graph tool before answering a question. |
| Names not in the graph | Id-like names in an answer (`svc-…`, `deploy-…`, `ADR-42`) are looked up in FalkorDB. Unknown ones get a red "Not in the graph" badge. |
| Validated extraction | Text-built graphs are normalised and de-duplicated, with no dangling edges. A repair pass asks the model again for any name in the text it left out. |
| Grounding badges | Every agent card shows how many cited ids were verified to exist in FalkorDB. |

The agents still finish without any API key: they fall back to template summaries built from the graph.

## Graph data model

Each company is a separate named graph (`tenant_acme`, `tenant_globex`, `tenant_initech`, `tenant_sandbox`) in one FalkorDB instance, so tenants never see each other's data.

| Node | Key properties |
| --- | --- |
| Service | id, name, tier (edge, core, data, platform, external), language |
| Team | id, name, channel |
| Person | id, name, role, on_call |
| Deployment | id, version, ts (epoch ms), summary, service |
| Alert | id, signal, severity, ts, service |
| Incident | id, title, severity, status, started, resolved |
| Runbook | id, title, steps |
| Decision | id, title, summary, date |
| Document | id, title, type (postmortem, adr), source |
| Agent / Episode | agent name; episode kind, session, summary, findings, ts |

```
(Person)-[:MEMBER_OF]->(Team)-[:OWNS]->(Service)
(Service)-[:DEPENDS_ON]->(Service)
(Person)-[:AUTHORED]->(Deployment)-[:CHANGED]->(Service)
(Alert)-[:FIRED_ON]->(Service)
(Incident)-[:TRIGGERED_BY]->(Alert)
(Incident)-[:AFFECTED]->(Service)
(Incident)-[:CAUSED_BY]->(Deployment)
(Person)-[:RESOLVED]->(Incident)
(Runbook)-[:FOR]->(Service)
(Decision)-[:ABOUT]->(Service)
(Decision)-[:SOURCED_FROM]->(Document)
(Document)-[:MENTIONS]->(Incident)
```

Agent memory and coordination:

```
(Agent)-[:RECORDED]->(Episode)-[:ABOUT]->(Incident)
(Episode)-[:HANDED_OFF_TO]->(Agent)
(Episode)-[:CITES]->(Service | Deployment | Person | Incident | Runbook | Document | Alert)
(Incident)-[:AT_RISK {hops}]->(Service)
(Incident)-[:SUSPECTED_CAUSE {confidence}]->(Deployment)
(Incident)-[:SIMILAR_TO {score}]->(Incident)
```

The memory has three layers:
- **Episodic memory** is the `Episode` nodes.
- **Semantic memory** is the company graph.
- **Procedural memory** is the `Runbook` nodes, plus resolved incidents linked by `SIMILAR_TO`, `CAUSED_BY` and `RESOLVED`.

Indexes: `id` on every label, plus `Service.name`, `Team.name` and `Deployment.ts` (see `blast_radius/schema.py`). Data is loaded in batches with `UNWIND`.

The `acme` tenant has 38 services, 54 dependencies, 9 teams, about 30 people, 152 deployments, 26 incidents, runbooks and design decisions: about 330 nodes and 630 edges.

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
| `list_services` | What services exist? | `MATCH (s:Service) OPTIONAL MATCH (t:Team)-[:OWNS]->(s) OPTIONAL MATCH (s)-[:DEPENDS_ON]->(d:Service) RETURN ...` |
| `documents` | What is written about X? | ADRs via `(Decision)-[:ABOUT]->(Service)` plus postmortems via `(Document)-[:MENTIONS]->(Incident)-[:AFFECTED]->(Service)` |
| `what_if_cut` | What if A -> B had a circuit breaker? | Same path query as `blast_radius`; a service stays at risk only if one of its paths avoids the cut edge |
| `breaker_advice` | Where would one circuit breaker help most? | One path query into the failing service; each edge on those paths is scored by the services whose every path uses it |
| `open_alerts` | Which alerts have no incident yet? | `MATCH (a:Alert)-[:FIRED_ON]->(s:Service) OPTIONAL MATCH (i:Incident)-[:TRIGGERED_BY]->(a) WITH a, s, i WHERE i IS NULL RETURN ...` |
| `incident_memory` | What do we know about an incident? | Episodes, suspected causes, fixer (`RESOLVED`), cause (`CAUSED_BY`), affected services and postmortems |

## Quick start (local)

Requirements: Docker Desktop and Python 3.12.

```bash
docker compose up -d                 # FalkorDB on :6379, Browser on http://localhost:3000
py -3.12 -m venv .venv               # Windows; on macOS/Linux: python3.12 -m venv .venv
.venv\Scripts\activate               # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env               # macOS/Linux: cp .env.example .env

python -m blast_radius.cli seed      # load 3 synthetic companies
python -m blast_radius.cli respond   # Triage -> Investigator -> Responder on the open alert
pytest                               # graph, agent, LLM, API and sandbox tests
uvicorn blast_radius.api:app --reload
```

Open http://localhost:8000.

For LLM-written summaries, free-form questions and the sandbox, add a free `GROQ_API_KEY` and/or `GEMINI_API_KEY` to `.env`. `LLM_PROVIDER=groq` tries Groq first and Gemini second.

### Using the web UI

1. Pick a company (acme, globex or initech). Each one has its own live incident.
2. Click **Run agents** on the open alert. Read each agent's summary, its grounding badge and the Cypher it ran.
3. Under **What if…**, pick a dependency and click **Simulate cut**. Saved services turn green.
4. Click **Mark resolved**, then **Fire a similar alert**. The Investigator now cites the incident you just resolved.
5. Ask a question in **Ask the graph**, for example *"What caused this, who fixed it last time, and which services are unaffected?"*
6. Pick **sandbox (build your own)**, click **Use an example** or paste your own company, and click **Build graph & investigate**.

**Reset demo** reloads the three synthetic companies.

### Command line

```bash
python -m blast_radius.cli llm-check
python -m blast_radius.cli ask "What caused the payments alert and who fixed it last time?"
python -m blast_radius.cli report "Checkout is throwing 500s since 15 minutes ago"
python -m blast_radius.cli memory inc-alert-demo-live
python -m blast_radius.cli tool blast_radius service=kafka
python -m blast_radius.cli tool connection_path source=mobile-bff target=token-store
```

Add `--tenant globex` (or `initech`) to any command.

### MCP server

For Claude Desktop, add this to `claude_desktop_config.json` (adjust the paths):

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

Tools exposed: `tenants`, `open_alerts`, `blast_radius`, `dependencies`, `recent_deploys`, `critical_services`, `connection_path`, `owners`, `fix_history`, `similar_incidents`, `runbook`, `decision_trace`, `list_services`, `documents`, `what_if_cut`, `breaker_advice`, `incident_memory`, `list_incidents`, `respond_to_alert`, `resolve_incident`.

## How it is verified

There are 98 tests. The graph and agent tests need FalkorDB; the LLM tests use a scripted fake model, so no API key is needed.

| Test file | Checks |
| --- | --- |
| `test_graph_tools.py` | Every graph tool against an independent breadth-first search in plain Python (`blast_radius/seed/reference.py`) over the same data, including what-if cuts and breaker advice |
| `test_agents.py` | The three agents pick the right deploy, write the handoff chain into the graph, and memory is readable from a new connection. The assistant guards (invented citations, tools before answers, names not in the graph) are covered too. |
| `test_learning.py` | Resolving an incident writes cause, fixer and postmortem; the next similar alert finds it |
| `test_sandbox.py` | Text-to-graph validation, id handling, the repair pass for missed services, and agents on a text-built company |
| `test_intake.py` | Free-text incident reports, unverified changes and question routing |
| `test_llm.py` | Provider fallback and rejection of invented citations |
| `test_api.py` | Every HTTP endpoint, grounding badges and the resolve → refire loop |
| `test_generator.py` | The synthetic data itself: no dependency cycles, no dangling edges, one owner per service, one on-call person per team, no future timestamps |

Before release, the sandbox was also checked by hand on three companies written from scratch (a trading platform, a telehealth app and a learning platform). Each one includes a decoy deploy that the agents correctly do not blame.

## Deployment

- **App:** the API and UI ship as one Docker image (`Dockerfile`) on a Render free web service (`render.yaml`).
- **Database:** FalkorDB runs as a FalkorDB Cloud free instance.
- **Environment variables:** set `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`, `GROQ_API_KEY` and `GEMINI_API_KEY`. Keys are never committed.
- **First start:** against an empty database, the API loads the synthetic companies.
- **Rate limit:** `/api/ask` is rate limited per IP to protect the free LLM quotas.

Everything runs on free tiers.

## Project layout

```
blast_radius/
  tools/graph_tools.py   Cypher graph tools used by agents, API and MCP
  agents/                triage, investigator, responder, orchestrator, ask (assistant)
  memory.py              episodes, incidents, resolve and refire
  ingest.py              text -> validated company graph (sandbox)
  intake.py              free-text incident reports
  llm.py                 Groq and Gemini clients with fallback
  seed/                  synthetic data generator, loader, Python reference implementation
  api.py  mcp_server.py  cli.py
web/index.html           single-page UI (Cytoscape.js)
tests/                   pytest suite
```

## Data

All data is synthetic, generated by `blast_radius/seed/generator.py` or written by the user in the sandbox. No real company, person or private information is used.

## AI assistance disclosure

I used Claude (Anthropic) as an AI coding assistant. It drafted most of the code under my direction; I ran and tested every module, reviewed the graph queries, and made the design decisions.

## License

MIT

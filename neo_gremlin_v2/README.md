# Graph Database API v2 -- FastAPI + Neo4j / Cosmos DB Gremlin

Implements `graph-api-v2.yaml`: graph lifecycle, schema (constraints/indexes),
node/relationship upserts, read/write queries, stats, and health -- all under
`/api/v2`, protected by a bearer token. Backed by **either** Neo4j **or**
Azure Cosmos DB's Gremlin API, switchable via `DB_TYPE` in `.env` -- no code
changes required.

## Files

- `graph_adapter.py` -- the `GraphAdapter` interface both backends implement, plus shared errors/utilities. `main.py` and `llm.py` depend only on this file, never on a concrete backend.
- `neo4j_ops.py` -- Neo4j implementation of `GraphAdapter` (`Neo4jGraphService`)
- `gremlin_ops.py` -- Cosmos DB Gremlin implementation of `GraphAdapter` (`GremlinGraphService`)
- `main.py` -- FastAPI routes + Pydantic request/response models. Picks a backend at startup via `DB_TYPE`.
- `prompts.py` -- system prompt text for natural-language-to-query translation (Cypher and Gremlin dialects)
- `llm.py` -- Azure OpenAI client + query generation/validation. Used by
  `/query` and `/write` in two ways: (1) when the caller sends a `prompt`
  (natural language) instead of a query, it's generated directly in the
  active backend's dialect; (2) when the caller sends `query` (always
  Cypher) against a non-Neo4j backend, it's translated into that backend's
  dialect. Neo4j backends skip the LLM entirely for `query` bodies.
- `requirements.txt`, `.env.example`

## Adapter pattern

```
                    ┌───────────────┐
 HTTP request  ───▶ │    main.py     │  depends only on GraphAdapter
                    └───────┬───────┘
                            │
                    ┌───────▼───────┐
                    │ graph_adapter │  ABC: create_graph, upsert_nodes,
                    │     .py       │  execute_query, get_schema, ...
                    └───────┬───────┘
                ┌───────────┴────────────┐
                ▼                        ▼
      Neo4jGraphService          GremlinGraphService
      (neo4j_ops.py)             (gremlin_ops.py)
      DB_TYPE=neo4j              DB_TYPE=gremlin
```

`main.py` calls a small factory (`_build_service()`) at startup that reads
`DB_TYPE` and lazily imports + constructs the matching class. Only the
driver package for the backend you're using needs to actually work at
runtime (the other's import is never reached).

### Neo4j design note

Neo4j Community Edition only has one database. Rather than requiring
Enterprise multi-database, each "graph" from the spec is a **logical
namespace** inside your single `NEO4J_DB` database: every node gets an extra
label `Graph_<uuid>` so it can be scoped/queried/counted cheaply. Constraints
and indexes are real Neo4j schema objects (which are inherently global to a
label in Neo4j), tracked per-graph via metadata nodes so `GET /schema` and
`GET /stats` report correctly per graph.

### Gremlin / Cosmos DB design note

Same idea, different vocabulary: each "graph" is a scoping property on
every vertex inside your single Cosmos Gremlin collection.

**Partition key -- read this first.** Cosmos DB requires every vertex to
carry the container's actual partition key property, or writes fail with
`Cannot add a vertex where the partition key property has value 'null'`.
This adapter reuses its graph-scoping property as the partition key, so its
name has to match your container's real partition key path. Check yours in
the Azure Portal: **Cosmos DB account -> Data Explorer -> your graph ->
Scale & Settings -> Partition Key** (commonly `/pk`, sometimes something
custom). Set `GREMLIN_PARTITION_KEY` in `.env` to that path *without* the
leading slash. It defaults to `graph_id`, which only works if your
container happens to use that exact path.

A few other real differences from the Neo4j path, worth knowing before you
rely on them:

- **Multiple labels**: a node's `labels` array maps to a single Gremlin
  vertex label (`labels[0]`) since Cosmos vertices only have one label;
  any extra labels are stored on an `extra_labels` property instead.
- **Constraints**: Cosmos Gremlin has no native unique-constraint feature.
  `POST /schema/constraints` records the intent as metadata (visible via
  `GET /schema`) but does **not** enforce it. If you need real uniqueness,
  use that property as the node's `id` -- `id` is the only property this
  adapter treats as a dedup key on upsert.
- **Indexes**: Cosmos indexes all properties automatically, so
  `POST /schema/indexes` is a metadata-only no-op (nothing to create).
- **Atomicity**: a relationship upsert is 2-3 separate Gremlin calls
  (find/create start vertex, find/create end vertex, merge edge) rather
  than Cypher's single atomic `MERGE` statement -- fine for normal use, just
  not transactionally atomic end-to-end.
- **Write stats**: Gremlin doesn't expose per-mutation counters the way
  Neo4j's `summary.counters` does, so `POST /write` reports only a result
  count, with a note in the response.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp env .env
# set values
```

### Option A: Neo4j (`DB_TYPE=neo4j`)

```bash
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password neo4j:5

uvicorn main:app --reload --port 8000
```

### Option B: Cosmos DB Gremlin (`DB_TYPE=gremlin`)

Fill in `GREMLIN_HOST`, `GREMLIN_DATABASE`, `GREMLIN_GRAPH`, and
`GREMLIN_KEY` in `.env` from your Cosmos account's Keys blade, then:

```bash
uvicorn main:app --reload --port 8000
```

Both options serve the exact same `/api/v2` routes -- the curl commands
below work unchanged against either.

Docs at http://localhost:8000/docs once it's running.

All requests below assume `API_BEARER_TOKEN=dev-token` (the default) and
the API running at `http://localhost:8000`.

## 1. Create a graph

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "name": "atlas-code-knowledge",
    "description": "Atlas code knowledge graph for legacy modernization"
  }'
```

Save the returned `id` for the rest of the calls:

```bash
export GRAPH_ID=$(curl -s -X POST "http://localhost:8000/api/v2/graphs" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"name":"atlas-code-knowledge-2","description":"demo"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo $GRAPH_ID
```

## 2. Get graph status

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GRAPH_ID" -H "Authorization: Bearer dev-token"
```

## 3. Create constraints (single)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/schema/constraints" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"label":"Program","properties":["program_id"],"type":"unique"}'
```

## 4. Create constraints (multiple)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/schema/constraints" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "constraints": [
      {"label": "Program", "properties": ["program_id"], "type": "unique"},
      {"label": "BusinessRule", "properties": ["rule_id"], "type": "unique"}
    ]
  }'
```

## 5. Create indexes

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/schema/indexes" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "indexes": [
      {"label": "Program", "properties": ["name"], "type": "btree"},
      {"label": "BusinessRule", "properties": ["category"], "type": "btree"}
    ]
  }'
```

## 6. Get current schema

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GRAPH_ID/schema" -H "Authorization: Bearer dev-token"
```

## 7. Upsert a single node

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/nodes" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "id": "PROGRAM::CUSTMGR",
    "labels": ["Program"],
    "properties": {
      "program_id": "CUSTMGR",
      "name": "CUSTMGR",
      "language": "COBOL",
      "source_path": "src/cobol/CUSTMGR.cbl",
      "enterprise_context": "retail-banking"
    }
  }'
```

## 8. Upsert multiple nodes

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/nodes" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "nodes": [
      {
        "id": "PROGRAM::ACCTUPD",
        "labels": ["Program"],
        "properties": {"program_id": "ACCTUPD", "name": "ACCTUPD", "language": "COBOL"}
      },
      {
        "id": "BR::VAL::001",
        "labels": ["BusinessRule"],
        "properties": {
          "rule_id": "BR-VAL-001",
          "name": "Validate account status before debit",
          "category": "Validation",
          "lifecycle_status": "DISCOVERED",
          "enterprise_context": "retail-banking"
        }
      }
    ]
  }'
```

## 9. Upsert a single relationship

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/relationships" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "upsert_missing_nodes": false,
    "relationship": {
      "type": "ENFORCED_BY",
      "start_node": {"label": "BusinessRule", "key": {"id": "BR::VAL::001"}},
      "end_node": {"label": "Program", "key": {"id": "PROGRAM::CUSTMGR"}},
      "properties": {"enforcement_type": "legacy-program", "confidence": 0.91}
    }
  }'
```

## 10. Upsert multiple relationships (auto-create missing endpoints)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/relationships" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "upsert_missing_nodes": true,
    "relationships": [
      {
        "type": "CALLS",
        "start_node": {"label": "Program", "key": {"id": "PROGRAM::CUSTMGR"}},
        "end_node": {"label": "Program", "key": {"id": "PROGRAM::ACCTUPD"}},
        "properties": {"call_type": "static", "source_file": "CUSTMGR.cbl"}
      }
    ]
  }'
```

## 11. Execute a named read query

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/query" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"name": "rules_enforced_by_program", "parameters": {"program_name": "CUSTMGR"}}'
```

## 12. Execute a freeform read query

`query` is always **Cypher**, regardless of `DB_TYPE` -- it's the API's one
canonical query language:

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/query" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "query": "MATCH (br:BusinessRule)-[:ENFORCED_BY]->(p:Program {id: $program_id}) RETURN br.rule_id AS rule_id, br.name AS rule_name",
    "parameters": {"program_id": "PROGRAM::CUSTMGR"}
  }'
```

- If `DB_TYPE=neo4j`, this Cypher is executed as-is (no LLM call).
- If `DB_TYPE=gremlin`, the Cypher is translated into an equivalent Gremlin
  traversal by Azure OpenAI before it's executed (`graph_id` is bound
  automatically, and every domain vertex carries it). The response includes
  a `translated_query` field with the Gremlin that actually ran, handy for
  debugging. This adds an LLM round-trip to every `/query`/`/write` call
  against a Gremlin backend.

## 13. Execute a parameterized write query

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/write" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "query": "MERGE (br:BusinessRule {id: $rule_id}) SET br.rule_id = $rule_id, br.name = $name, br.category = $category RETURN br",
    "parameters": {
      "rule_id": "BR::VAL::042",
      "name": "Validate transaction amount",
      "category": "Validation"
    }
  }'
```

## 14. Get graph statistics

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GRAPH_ID/stats" -H "Authorization: Bearer dev-token"
```

## 15. Health check

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GRAPH_ID/health" -H "Authorization: Bearer dev-token"
```

## Error cases to try

```bash
# 401 - bad/missing token
curl -s -i "http://localhost:8000/api/v2/graphs/$GRAPH_ID" -H "Authorization: Bearer wrong-token"

# 404 - unknown graph id
curl -s -i "http://localhost:8000/api/v2/graphs/00000000-0000-0000-0000-000000000000" -H "Authorization: Bearer dev-token"

# 409 - duplicate graph name
curl -s -i -X POST "http://localhost:8000/api/v2/graphs" -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"name": "atlas-code-knowledge-2"}'

# 422 - relationship endpoint missing, upsert_missing_nodes not set
curl -s -i -X POST "http://localhost:8000/api/v2/graphs/$GRAPH_ID/relationships" -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"relationship": {"type": "CALLS", "start_node": {"label": "Program", "key": {"id": "PROGRAM::DOES_NOT_EXIST"}}, "end_node": {"label": "Program", "key": {"id": "PROGRAM::CUSTMGR"}}}}'
```

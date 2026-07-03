# Graph Database API v2 -- FastAPI + Neo4j

Implements `graph-api-v2.yaml`: graph lifecycle, schema (constraints/indexes),
node/relationship upserts, read/write queries, stats, and health

## Files

- `neo4j_ops.py` -- all Neo4j driver code (`Neo4jGraphService`)
- `main.py` -- FastAPI routes + Pydantic request/response models
- `prompts.py` -- system prompt text for natural-language-to-Cypher translation
- `llm.py` -- Azure OpenAI client + Cypher generation/validation, used by `/query` and `/write` when a `prompt` is sent instead of raw Cypher
- `requirements.txt`, `.env.example`

### Natural-language queries and writes

`POST /query` and `POST /write` now also accept a `prompt` field (natural
language) as an alternative to `name`/`query`. When `prompt` is sent, the
service builds a schema summary for that graph (`get_schema_context` in
`neo4j_ops.py`), asks Azure OpenAI (`gpt-4o` by default) to produce Cypher
grounded in that schema, runs a keyword-level check (`llm.py`) that rejects
disallowed operations for the mode (e.g. no `CREATE`/`MERGE`/`DELETE` from
`/query`, no `DROP DATABASE`/`LOAD CSV`/`dbms.*` calls from either), and then
executes it exactly like a normal `query`/`write` call. The response includes
an extra `generated_cypher` field so you can see what actually ran -- this is
additive and outside the original OpenAPI schema, so strict spec validators
may ignore or reject that field depending on how they're configured.

## Design note

Neo4j Community Edition only has one database. Rather than requiring
Enterprise multi-database, each "graph" from the spec is a **logical
namespace** inside your single `NEO4J_DB` database: every node gets an extra
label `Graph_<uuid>` so it can be scoped/queried/counted cheaply. Constraints
and indexes are real Neo4j schema objects (which are inherently global to a
label in Neo4j), tracked per-graph via metadata nodes so `GET /schema` and
`GET /stats` report correctly per graph.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your real NEO4J_PASSWORD / API_BEARER_TOKEN

# Neo4j must be running and reachable at NEO4J_URI, e.g. via Docker:
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password neo4j:5

uvicorn main:app --reload --port 8000
```

Docs at http://localhost:8000/docs once it's running.

All requests below assume `API_BEARER_TOKEN=dev-token` (the default) and
`BASE=http://localhost:8000/api/v2`.



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
export GID=$(curl -s -X POST "http://localhost:8000/api/v2/graphs" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"name":"atlas-code-knowledge-2","description":"demo"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo $GID
```

## 2. Get graph status

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GID" -H "Authorization: Bearer dev-token"
```

## 3. Create constraints (single)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/schema/constraints" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"name":"unique_program_program_id","label":"Program","properties":["program_id"],"type":"unique"}'
```

## 4. Create constraints (multiple)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/schema/constraints" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "constraints": [
      {"label": "Program", "properties": ["program_id"], "type": "unique"},
      {"label": "BusinessRule", "properties": ["rule_id"], "type": "unique"}
    ]
  }'
```

## 5. Create indexes (single)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/schema/indexes" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"idx_program_name",
    "label":"Program",
    "properties":["name"],
    "type":"btree"
  }'
```

## 6. Create indexes (Multiple)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/schema/indexes" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "indexes": [
      { "label": "Program", "properties": ["name"], "type": "btree" },
      { "label": "BusinessRule", "properties": ["category"], "type": "btree" }
    ]
  }'
```

## 7. Get current schema

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GID/schema" -H "Authorization: Bearer dev-token"
```

## 8. Upsert a single node

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/nodes" \
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

## 9. Upsert multiple nodes

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/nodes" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "nodes": [
      {
        "id": "PROGRAM::CUSTMGR",
        "labels": ["Program"],
        "properties": {"program_id": "CUSTMGR", "name": "CUSTMGR", "language": "COBOL", "source_path": "src/cobol/CUSTMGR.cbl", "enterprise_context": "retail-banking"}
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

## 10. Upsert a single relationship

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/relationships" \
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

## 11. Upsert multiple relationships (auto-create missing endpoints)

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/relationships" \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{
    "upsert_missing_nodes": true,
    "relationships": [
      {
        "type": "ENFORCED_BY",
        "start_node": {"label": "BusinessRule", "key": { "id": "BR::VAL::001" }},
        "end_node": {"label": "Program", "key": {"id": "PROGRAM::CUSTMGR"}},
        "properties": {"enforcement_type": "legacy-program", "legacy-program": 0.91}
      },
      {
        "type": "CALLS",
        "start_node": {"label": "Program", "key": { "id": "PROGRAM::CUSTMGR" }},
        "end_node": {"label": "Program", "key": {"id": "PROGRAM::ACCTUPD"}},
        "properties": {"call_type": "static", "source_file": "CUSTMGR.cbl"}
      }
    ]
  }'
```

## 12. Execute a named read query

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/query" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "rules_enforced_by_program",
    "parameters": {
      "program_name": "CUSTMGR"
    }
  }'
```



## 13. Execute a freeform read query

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/query" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "MATCH (br:BusinessRule)-[:ENFORCED_BY]->(p:Program {program_id:$program_id}) RETURN br.name AS businessRuleName, br.rule_id AS ruleId, br.enterprise_context AS enterpriseContext, br.lifecycle_status AS lifecycleStatus, br.category AS category",
    "parameters": {
      "program_id": "CUSTMGR"
    }
  }'
```



## 14. Execute a parameterized write query

```bash
curl -s -X POST "http://localhost:8000/api/v2/graphs/$GID/write" \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "MERGE (br:BusinessRule {id:$rule_id}) SET br.rule_id=$rule_id, br.name=$name, br.category=$category, br.enterprise_context=$enterprise_context, br.lifecycle_status=$lifecycle_status RETURN br",
    "parameters": {
      "rule_id": "BR::VAL::002",
      "name": "Validate transaction amount",
      "category": "Validation",
      "enterprise_context": "retail-banking",
      "lifecycle_status": "DISCOVERED"
    }
  }'
```

## 15. Get graph statistics

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GID/stats" -H "Authorization: Bearer dev-token"
```

## 16. Health check

```bash
curl -s "http://localhost:8000/api/v2/graphs/$GID/health" -H "Authorization: Bearer dev-token"
```

## Error cases to try

```bash
# 401 - bad/missing token
curl -s -i "http://localhost:8000/api/v2/graphs/$GID" -H "Authorization: Bearer wrong-token"

# 404 - unknown graph id
curl -s -i "http://localhost:8000/api/v2/graphs/00000000-0000-0000-0000-000000000000" -H "Authorization: Bearer dev-token"

# 409 - duplicate graph name
curl -s -i -X POST "http://localhost:8000/api/v2/graphs" -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"name": "atlas-code-knowledge-2"}'

# 422 - relationship endpoint missing, upsert_missing_nodes not set
curl -s -i -X POST "http://localhost:8000/api/v2/graphs/$GID/relationships" -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"relationship": {"type": "CALLS", "start_node": {"label": "Program", "key": {"id": "PROGRAM::DOES_NOT_EXIST"}}, "end_node": {"label": "Program", "key": {"id": "PROGRAM::CUSTMGR"}}}}'
```

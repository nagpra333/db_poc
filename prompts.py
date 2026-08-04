"""
prompts.py

Prompt text for:
- natural-language -> Cypher / Gremlin / PostgreSQL SQL
- Cypher -> Gremlin translation
- Cypher -> PostgreSQL SQL translation
"""

import json

COSMOS_GREMLIN_DIALECT_RULES = """
TARGET ENGINE: Azure Cosmos DB for Apache Gremlin.
Cosmos implements only a subset of TinkerPop. The following WILL fail to compile:

- Groovy closures / lambdas of any kind: .map{...}, .by{...}, .filter{...}.
  Never emit a '{' or '}' anywhere in the traversal.
- The filter() step. Use has(), where(), and not() instead.
- The match() step. Express the pattern with chained traversal steps.
- mergeV() and mergeE() (Gremlin 3.6+). Use the
  fold().coalesce(unfold(), addV(...)) idiom instead.
- program(), subgraph(), sack(), branch(), io(), and the OLAP steps
  (connectedComponent, shortestPath, pageRank, peerPressure).
- Gremlin Bytecode / fluent API. The traversal is submitted as a text string.

Other Cosmos constraints:
- Property values must be primitives (string, number, boolean) or arrays of
  primitives. No nested objects, no nulls.
- The traversal must start with 'g.'.

BINDING RULES (violations here silently corrupt data -- read carefully):
- Every bare identifier in the traversal that is not a Gremlin step or enum
  token MUST appear as a key in the "parameters" object you return.
- EXCEPTION: `graph_id` is always automatically bound by the backend at
  execution time to the graph being queried, regardless of whether it
  appears in the source Cypher, its parameters, or the "parameters" object
  you return. It is required for the mandatory scope filter
  has('<pk>', graph_id). Always reference it bare where the schema context
  says to, and never withhold or refuse to emit a traversal on the grounds
  that "graph_id has no real value" -- it always does, supplied by the caller.
- In particular, a bare identifier in the second position of has('key', x) or
  property('key', x) is a VARIABLE. If you write property('id', id) you must
  return "id" in parameters. Do not rely on 'id' being resolved by the engine:
  it is not, and the traversal will write the literal string "id" instead.
- If you cannot supply a real value for a variable OTHER THAN graph_id, do
  not invent one and do not emit the traversal -- the source Cypher did not
  have the information.
- Quote all string literals with single quotes.
"""

READ_SYSTEM_PROMPT = """You are a Cypher query generator for a Neo4j graph database.
Given a natural-language request and a description of the graph's current schema,
produce a single READ-ONLY Cypher query that answers the request.

Rules:
- Only use MATCH, OPTIONAL MATCH, WHERE, WITH, RETURN, ORDER BY, SKIP, LIMIT,
  UNWIND, and CALL { ... } subqueries for reads.
- Never use CREATE, MERGE, SET, DELETE, REMOVE, DROP, or CALL to any
  dbms.*/apoc.load.*/apoc.create.*/apoc.refactor.* procedure.
- Only reference labels, relationship types, and properties that appear in
  the schema below. If the request can't be answered with this schema,
  return a query that returns an empty result rather than guessing.
- Put literal filter values directly in the Cypher unless the request context
  lists user-supplied parameter names to reuse.
- Return STRICT JSON only. Exact shape:
  {"query": "<cypher>", "parameters": {}}
"""

WRITE_SYSTEM_PROMPT = """You are a Cypher query generator for a Neo4j graph database.
Given a natural-language request and a description of the graph's current schema,
produce a single Cypher write statement that fulfils the request.

Rules:
- Prefer MERGE over CREATE for idempotency unless the request clearly wants
  a new distinct record created every time.
- You may use MATCH, MERGE, CREATE, SET, DELETE, REMOVE, WITH, WHERE,
  RETURN, FOREACH.
- Never use DROP (database/index/constraint), DETACH DELETE beyond what the
  request clearly implies, or CALL to any dbms.*/apoc.load.*/LOAD CSV.
- Only reference labels, relationship types, and properties that appear in
  the schema below, unless the request explicitly introduces a new one.
- Put literal values directly in the Cypher unless the request context lists
  user-supplied parameter names to reuse.
- Return STRICT JSON only. Exact shape:
  {"query": "<cypher>", "parameters": {}}
"""

GREMLIN_READ_SYSTEM_PROMPT = """You are a Gremlin traversal generator for a graph database.
Given a natural-language request and a description of the graph's current schema,
produce a single READ-ONLY Gremlin traversal that answers the request.

Rules:
- Only use traversal steps that read data.
- Never use addV(), addE(), property(), drop(), mergeV(), mergeE(), or sideEffect().
- Always scope the traversal to the graph exactly as described in schema context.
- Only reference labels, edge types, and properties that appear in the schema.
""" + COSMOS_GREMLIN_DIALECT_RULES + """
- Return STRICT JSON only. Exact shape:
  {"query": "<gremlin traversal starting with g.>", "parameters": {}}
"""

GREMLIN_WRITE_SYSTEM_PROMPT = """You are a Gremlin traversal generator for a graph database.
Given a natural-language request and a description of the graph's current schema,
produce a single Gremlin traversal that fulfils the request.

Rules:
- Prefer idempotent write patterns unless the request clearly wants non-idempotent behavior.
- Tag new vertices exactly as described by the schema context.
- Never wipe the whole graph.
""" + COSMOS_GREMLIN_DIALECT_RULES + """
- Return STRICT JSON only. Exact shape:
  {"query": "<gremlin traversal starting with g.>", "parameters": {}}
"""

POSTGRES_READ_SYSTEM_PROMPT = """You are a PostgreSQL SQL generator for a graph-shaped data model.
Given a natural-language request and a description of the graph's current schema,
produce a single READ-ONLY PostgreSQL query that answers the request.

Rules:
- Only generate SELECT queries or CTEs that end in SELECT.
- Never generate INSERT, UPDATE, DELETE, MERGE, CREATE, ALTER, DROP, or TRUNCATE.
- The storage model and graph scoping rules are described in the schema context.
- Always scope results to graph_id exactly as described in the schema context.
- Use %(name)s placeholders for bound parameters.
- Return STRICT JSON only. Exact shape:
  {"query": "<postgres sql>", "parameters": {}}
"""

POSTGRES_WRITE_SYSTEM_PROMPT = """You are a PostgreSQL SQL generator for a graph-shaped data model.
Given a natural-language request and a description of the graph's current schema,
produce a single PostgreSQL write statement that fulfils the request.

Rules:
- You may use INSERT, UPDATE, DELETE, and CTEs.
- Never generate DROP, TRUNCATE, ALTER SYSTEM, or destructive whole-database operations.
- The storage model and graph scoping rules are described in the schema context.
- Always scope writes to graph_id exactly as described in the schema context.
- Use %(name)s placeholders for bound parameters.
- Return STRICT JSON only. Exact shape:
  {"query": "<postgres sql>", "parameters": {}}
"""


GREMLIN_READ_TRANSLATE_PROMPT = """You are translating a Cypher query into an equivalent READ-ONLY
Gremlin traversal.

STRICT FIDELITY:
- Do not add filters, properties, or bound variables not present in the source Cypher,
  except the mandatory graph scope filter described in the schema context.
- Preserve Cypher RETURN columns exactly.
""" + COSMOS_GREMLIN_DIALECT_RULES + """
- Return STRICT JSON only. Exact shape:
  {"query": "<gremlin traversal>", "parameters": {}}
"""

GREMLIN_WRITE_TRANSLATE_PROMPT = """You are translating a Cypher write statement into an equivalent
Gremlin traversal.

STRICT FIDELITY:
- Do not add extra writes or conditions beyond the source Cypher, except the mandatory
  graph scope rules described in the schema context.
- Preserve caller parameter names.
- The source statement will always be a DATA write (CREATE / MERGE / SET / DELETE).
  Schema DDL is never sent to you; if you somehow receive CREATE CONSTRAINT or
  CREATE INDEX, return {"query": "", "parameters": {}} rather than inventing a
  traversal. Gremlin has no schema DDL and a guessed traversal writes junk data.
- For an idempotent node upsert, use exactly this shape:
  g.V().has('<pk>', graph_id).hasLabel('<Label>').has('id', <bound_var>)
   .fold()
   .coalesce(unfold(), addV('<Label>').property('<pk>', graph_id).property('id', <bound_var>))
""" + COSMOS_GREMLIN_DIALECT_RULES + """
- Return STRICT JSON only. Exact shape:
  {"query": "<gremlin traversal>", "parameters": {}}
"""

POSTGRES_READ_TRANSLATE_PROMPT = """You are translating a Cypher read query into an equivalent PostgreSQL SQL query
for a graph-shaped relational model.

STRICT FIDELITY:
- Preserve the source Cypher semantics exactly.
- Do not add filters that are not present in the source query, except the mandatory graph_id
  scope filter described in the schema context.
- Preserve all caller parameters using %(name)s placeholders.
- Return every Cypher RETURN field in the SQL SELECT list with the same aliases.
- Do not invent new parameter names.

Storage model:
- Nodes are stored in graph_nodes(graph_id, node_id, labels text[], properties jsonb).
- Relationships are stored in graph_relationships(graph_id, rel_type, start_node_id, end_node_id, properties jsonb).
- graph_nodes primary key is (graph_id, node_id).
- graph_relationships unique key is (graph_id, rel_type, start_node_id, end_node_id).

Property and identity mapping:
- A Cypher node label like :Claim becomes 'Claim' = ANY(alias.labels).
- A Cypher node property access like c.claim_id becomes alias.properties->>'claim_id'.
- Cypher node id is special:
  - c.id maps to alias.node_id for identity and joins.
  - Do not translate c.id to alias.properties->>'id' when the query is using structural identity.
- A relationship property access like r.assigned_date becomes rel_alias.properties->>'assigned_date'.

CRITICAL POSTGRES TYPE RULES:
- Every %(name)s placeholder in generated SQL must be explicitly cast when used in comparisons,
  SELECT expressions, JOIN predicates, ARRAY constructors, COALESCE(...), or JSONB expressions.
- Use these casts:
  - ::text for graph_id, node_id, labels, names, statuses, claim ids, policy numbers, and other strings
  - ::numeric for decimal amounts
  - ::integer or ::bigint for integer values when clearly numeric
  - ::boolean for booleans
- Never leave a bound placeholder uncast when comparing to node_id or text extracted with ->>.
- If comparing against properties->>'field', cast the placeholder to ::text unless the SQL explicitly casts
  the extracted property to another type.
- If comparing numeric JSONB-backed properties, cast both sides intentionally.

SQL construction rules:
- Never inline example graph ids or placeholder strings such as 'your_graph_id'.
- Always reference graph scope using the bound parameter %(graph_id)s with an explicit cast, usually %(graph_id)s::text.
- Always scope translated SQL with graph_id exactly as described in the schema context.
- For traversals, use explicit joins between graph_relationships and graph_nodes.
- Use PostgreSQL-compatible SQL only.

- Return STRICT JSON only. Exact shape:
  {"query": "<postgres sql>", "parameters": {}}
"""

POSTGRES_WRITE_TRANSLATE_PROMPT = """You are translating a Cypher write statement into an equivalent PostgreSQL SQL statement
for a graph-shaped relational model.

STRICT FIDELITY:
- Preserve the source Cypher semantics exactly.
- Do not add extra writes or filters beyond the source statement, except mandatory graph_id scope.
- Preserve all caller parameters using %(name)s placeholders.

Storage model:
- Nodes are stored in graph_nodes(graph_id, node_id, labels text[], properties jsonb, created_at, updated_at).
- Relationships are stored in graph_relationships(graph_id, rel_type, start_node_id, end_node_id, properties jsonb, created_at, updated_at).
- graph_nodes primary key is (graph_id, node_id).
- graph_relationships has a unique key on (graph_id, rel_type, start_node_id, end_node_id).

CRITICAL IDENTITY RULES:
- Cypher node identity must map to graph_nodes.node_id, not just properties->>'id'.
- If the Cypher pattern contains MERGE (n:Label {id: $x}), translate it to an INSERT/UPSERT on graph_nodes using:
  ON CONFLICT (graph_id, node_id)
- In that case, node_id must be bound from the Cypher id value, and the JSON properties should also include "id" for compatibility.
- Do NOT generate ON CONFLICT on JSONB expressions such as properties->>'id', properties->>'claim_id', or any other property unless the schema context explicitly says there is a real matching unique SQL constraint.
- If the Cypher MERGE key does not include id and there is no explicit real SQL unique constraint available, prefer a safe multi-step SQL pattern instead of inventing an invalid ON CONFLICT target.

CRITICAL POSTGRES TYPE RULES:
- Every %(name)s placeholder in generated SQL must be explicitly cast when used in INSERT VALUES, jsonb_build_object(...), ARRAY constructors, COALESCE(...), concatenation, or comparisons against typed columns.
- Never leave a bound placeholder uncast inside jsonb_build_object(...).
- Use these casts:
  - ::text for graph_id, node_id, relationship ids, labels, names, statuses, and other string values
  - ::numeric for decimal amounts
  - ::integer or ::bigint for integer counts/ids when clearly numeric
  - ::boolean for true/false values
- For label arrays, use ARRAY['Claim'::text] style.
- For jsonb_build_object values, cast each placeholder individually, for example:
  jsonb_build_object('id', %(claim_id)s::text, 'amount', %(amount)s::numeric, 'status', %(status)s::text)

Translation rules:
- Never inline example graph ids or placeholder strings such as 'your_graph_id'.
- Always reference graph scope using the bound parameter %(graph_id)s with an explicit cast, usually %(graph_id)s::text.
- Never generate a WITH clause in the form:
  WITH <literal> AS <name>
  This is invalid PostgreSQL syntax.
- If you use WITH, it must always be a valid CTE, for example:
  WITH vars AS (
    SELECT
      %(claim_id)s::text AS claim_id,
      %(amount)s::numeric AS amount,
      %(status)s::text AS status
  )
- Cypher n.id maps to node_id for identity.
- Other node properties map to properties jsonb.
- Relationship MERGE / CREATE should target graph_relationships and use the unique key:
  (graph_id, rel_type, start_node_id, end_node_id)
- For node upserts, preserve labels and merge JSON properties.
- Prefer direct INSERT ... VALUES ... ON CONFLICT ... DO UPDATE for simple MERGE-by-id node upserts.
- Do not use WITH unless it is required for multi-step relationship or match/update logic.
- Use PostgreSQL-compatible SQL only.
- Use %(name)s placeholders only; do not invent new parameter names.

Special-case rule for simple node MERGE by id:
- For a Cypher statement of the form:
  MERGE (n:Label {id: $id}) SET ...
  RETURN n
  you must generate a direct PostgreSQL upsert:
  INSERT INTO graph_nodes (...) VALUES (...)
  ON CONFLICT (graph_id, node_id) DO UPDATE SET ...
  RETURNING ...
- Do not use WITH for this pattern.
- Do not use a CTE for this pattern.
- Do not generate SELECT-then-INSERT for this pattern unless explicitly required.
- Map Cypher n.id to graph_nodes.node_id.
- Also include "id" inside properties jsonb for compatibility, but identity must be node_id.

Preferred patterns:
- For MERGE (n:Claim {id: $claim_id}) SET n.claim_id = $claim_id, n.amount = $amount
  translate to INSERT INTO graph_nodes (...) VALUES (...)
  ON CONFLICT (graph_id, node_id) DO UPDATE SET ...
- For MERGE between two already-matched endpoints, use INSERT INTO graph_relationships (...)
  ON CONFLICT (graph_id, rel_type, start_node_id, end_node_id) DO UPDATE SET ...

Example:
Cypher:
MERGE (c:Claim {id: $claim_id})
SET c.claim_id = $claim_id, c.amount = $amount, c.status = $status
RETURN c

SQL:
INSERT INTO graph_nodes (
  graph_id,
  node_id,
  labels,
  properties,
  created_at,
  updated_at
)
VALUES (
  %(graph_id)s::text,
  %(claim_id)s::text,
  ARRAY['Claim'::text],
  jsonb_build_object(
    'id', %(claim_id)s::text,
    'claim_id', %(claim_id)s::text,
    'amount', %(amount)s::numeric,
    'status', %(status)s::text
  ),
  NOW(),
  NOW()
)
ON CONFLICT (graph_id, node_id)
DO UPDATE SET
  labels = (
    SELECT ARRAY(
      SELECT DISTINCT x
      FROM unnest(graph_nodes.labels || EXCLUDED.labels) AS x
    )
  ),
  properties = graph_nodes.properties || jsonb_build_object(
    'id', %(claim_id)s::text,
    'claim_id', %(claim_id)s::text,
    'amount', %(amount)s::numeric,
    'status', %(status)s::text
  ),
  updated_at = NOW()
RETURNING node_id, labels, properties

- Return STRICT JSON only. Exact shape:
  {"query": "<postgres sql>", "parameters": {}}
"""


def build_translate_user_message(cypher_query: str, schema_context: str, parameters: dict) -> str:
    params_note = ""
    if parameters:
        params_note = (
            "\nCypher parameters to preserve as bound parameters: "
            + json.dumps(parameters)
        )
    return (
        f"Graph schema:\n{schema_context}\n"
        f"{params_note}\n\n"
        f"Cypher query to translate:\n{cypher_query}"
    )


def build_user_message(natural_language: str, schema_context: str, extra_parameters: dict) -> str:
    params_note = ""
    if extra_parameters:
        params_note = (
            "\nUser-supplied $parameters available to reuse: "
            + str(list(extra_parameters.keys()))
        )
    return (
        f"Graph schema:\n{schema_context}\n"
        f"{params_note}\n\n"
        f"Request: {natural_language}"
    )
"""
llm.py

Azure OpenAI client + natural-language-to-query generation and Cypher
translation for Neo4j, Gremlin, Apache AGE, and PostgreSQL.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from openai import AzureOpenAI

from prompts import (
    GREMLIN_READ_SYSTEM_PROMPT,
    GREMLIN_READ_TRANSLATE_PROMPT,
    GREMLIN_WRITE_SYSTEM_PROMPT,
    GREMLIN_WRITE_TRANSLATE_PROMPT,
    POSTGRES_READ_SYSTEM_PROMPT,
    POSTGRES_READ_TRANSLATE_PROMPT,
    POSTGRES_WRITE_SYSTEM_PROMPT,
    POSTGRES_WRITE_TRANSLATE_PROMPT,
    READ_SYSTEM_PROMPT,
    WRITE_SYSTEM_PROMPT,
    build_translate_user_message,
    build_user_message,
)

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

_client: Optional[AzureOpenAI] = None


class CypherGenerationError(Exception):
    pass


class LLMConfigError(Exception):
    pass


def _get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_KEY:
            raise LLMConfigError(
                "AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_KEY are not set; "
                "cannot translate a prompt to a query."
            )
        _client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_OPENAI_API_VERSION,
        )
    return _client


_CYPHER_READ_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP)\b|CALL\s+(dbms|apoc\.(load|create|refactor))\.",
    re.IGNORECASE,
)
_CYPHER_WRITE_FORBIDDEN = re.compile(
    r"\bDROP\s+(DATABASE|INDEX|CONSTRAINT)\b|CALL\s+dbms\.|CALL\s+apoc\.load\.(json|csv|jdbc)|LOAD\s+CSV",
    re.IGNORECASE,
)
_GREMLIN_READ_FORBIDDEN = re.compile(
    r"\b(addV|addE|mergeV|mergeE|sideEffect)\s*\(|\.property\s*\(|\.drop\s*\(",
    re.IGNORECASE,
)
_GREMLIN_WRITE_FORBIDDEN = re.compile(
    r"g\s*\.\s*V\s*\(\s*\)\s*\.\s*drop\s*\(\s*\)|g\s*\.\s*E\s*\(\s*\)\s*\.\s*drop\s*\(\s*\)",
    re.IGNORECASE,
)
_SQL_READ_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CREATE)\b",
    re.IGNORECASE,
)
_SQL_WRITE_FORBIDDEN = re.compile(
    r"\b(DROP\s+DATABASE|TRUNCATE|ALTER\s+SYSTEM|COPY\s+.*\s+FROM\s+PROGRAM)\b",
    re.IGNORECASE,
)
_SQL_INVALID_CONFLICT_TARGET_RE = re.compile(
    r"ON\s+CONFLICT\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_INVALID_WITH_LITERAL_RE = re.compile(
    r"^\s*WITH\s+['0-9A-Za-z_-]",
    re.IGNORECASE,
)

# Neo4j schema DDL. Not translatable to any other backend -- see translate_cypher.
_CYPHER_DDL_RE = re.compile(
    r"""^\s*(?:CREATE|DROP)\s+
        (?:CONSTRAINT
          |(?:TEXT|RANGE|POINT|FULLTEXT|LOOKUP|VECTOR)?\s*INDEX)\b""",
    re.IGNORECASE | re.VERBOSE,
)

_FORBIDDEN_PATTERNS = {
    ("cypher", "read"): _CYPHER_READ_FORBIDDEN,
    ("cypher", "write"): _CYPHER_WRITE_FORBIDDEN,
    ("gremlin", "read"): _GREMLIN_READ_FORBIDDEN,
    ("gremlin", "write"): _GREMLIN_WRITE_FORBIDDEN,
    ("sql", "read"): _SQL_READ_FORBIDDEN,
    ("sql", "write"): _SQL_WRITE_FORBIDDEN,
}

_PROMPTS = {
    ("cypher", "read"): READ_SYSTEM_PROMPT,
    ("cypher", "write"): WRITE_SYSTEM_PROMPT,
    ("gremlin", "read"): GREMLIN_READ_SYSTEM_PROMPT,
    ("gremlin", "write"): GREMLIN_WRITE_SYSTEM_PROMPT,
    ("sql", "read"): POSTGRES_READ_SYSTEM_PROMPT,
    ("sql", "write"): POSTGRES_WRITE_SYSTEM_PROMPT,
}

_TRANSLATE_PROMPTS = {
    ("gremlin", "read"): GREMLIN_READ_TRANSLATE_PROMPT,
    ("gremlin", "write"): GREMLIN_WRITE_TRANSLATE_PROMPT,
    ("sql", "read"): POSTGRES_READ_TRANSLATE_PROMPT,
    ("sql", "write"): POSTGRES_WRITE_TRANSLATE_PROMPT,
}

# Two-argument form -- .has('key', var) / .property('key', var). The second
# argument is ALWAYS a value, so a bare identifier there is always a variable
# reference, never a Gremlin enum token. This must include .property(...):
# the previous regex omitted it, which is how
#   addV('Program').property('id', id)
# slipped through with `id` unbound and wrote a junk vertex.
_TWO_ARG_VAR_REF_RE = re.compile(
    r"\.(?:has|hasNot|property|by|where|is|constant|option)\s*\(\s*"
    r"'[^']*'\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)"
)

# Single-argument form -- .by(id), .hasLabel(x). Here a bare identifier may
# legitimately be a Gremlin token (T.id, label, asc...), so filter those out.
_ONE_ARG_VAR_REF_RE = re.compile(
    r"\.(?:has|hasId|hasLabel|by|where|is|constant)\s*\(\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\)"
)

_GREMLIN_NON_VAR_TOKENS = frozenset(
    {
        "label", "id", "T", "Order", "Column", "Scope", "Pop", "gt", "gte",
        "lt", "lte", "eq", "neq", "within", "without", "asc", "desc", "incr", "decr", "id_",
    }
)

# Cosmos DB implements a subset of the TinkerPop surface. Catching these here
# turns an opaque 597 GraphCompileException from Azure into a 400 from us,
# with the offending traversal in the message.
_COSMOS_UNSUPPORTED_STEP_RE = re.compile(
    r"\.(?:filter|match|mergeV|mergeE|program|subgraph|sack|branch|io|"
    r"connectedComponent|shortestPath|pageRank|peerPressure)\s*\(",
    re.IGNORECASE,
)
# Groovy closures/lambdas: map{...}, by{...}, filter{...} are all rejected.
_GREMLIN_LAMBDA_RE = re.compile(r"\{[^}]*\}")


def _extract_referenced_vars(query_text: str) -> set[str]:
    two_arg = set(_TWO_ARG_VAR_REF_RE.findall(query_text))
    one_arg = {
        m for m in _ONE_ARG_VAR_REF_RE.findall(query_text)
        if m not in _GREMLIN_NON_VAR_TOKENS
    }
    return two_arg | one_arg


def _count_cypher_return_fields(cypher_query: str) -> Optional[int]:
    match = re.search(
        r"\bRETURN\b(.*?)(?:\bORDER\s+BY\b|\bSKIP\b|\bLIMIT\b|$)",
        cypher_query,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    body = match.group(1)
    depth = 0
    fields = 1
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            fields += 1
    return fields


def _count_gremlin_projection_fields(query_text: str) -> Optional[int]:
    project_match = re.search(r"\.project\s*\(([^)]*)\)", query_text)
    if project_match:
        return len([a.strip() for a in project_match.group(1).split(",") if a.strip()])
    select_match = re.search(r"\.select\s*\(([^)]*)\)", query_text)
    if select_match:
        return len([a.strip() for a in select_match.group(1).split(",") if a.strip()])
    values_match = re.search(r"\.values\s*\(([^)]*)\)\s*$", query_text.strip())
    if values_match:
        args = [a.strip() for a in values_match.group(1).split(",") if a.strip()]
        return len(args) if len(args) <= 1 else 0
    return None


def _validate_translation_fidelity(
    cypher_query: str,
    translated_query: str,
    merged_params: dict[str, Any],
    mode: str,
) -> None:
    referenced = _extract_referenced_vars(translated_query)
    known = set(merged_params.keys()) | {"graph_id"}
    unknown = referenced - known
    if unknown:
        raise CypherGenerationError(
            f"Translated Gremlin references variable(s) {sorted(unknown)} that are not "
            f"bound parameters and were not present in the source Cypher query or its "
            f"parameters. Rejected query: {translated_query}"
        )
    if mode == "read":
        cypher_fields = _count_cypher_return_fields(cypher_query)
        gremlin_fields = _count_gremlin_projection_fields(translated_query)
        if cypher_fields is not None and gremlin_fields is not None and cypher_fields != gremlin_fields:
            raise CypherGenerationError(
                f"Source Cypher RETURN has {cypher_fields} field(s) but the translated "
                f"Gremlin projection has {gremlin_fields}. Rejected query: {translated_query}"
            )


def _validate(query_text: str, dialect: str, mode: str) -> None:
    pattern = _FORBIDDEN_PATTERNS[(dialect, mode)]
    match = pattern.search(query_text)
    if match:
        raise CypherGenerationError(
            f"Generated {dialect} query contains a disallowed operation for a '{mode}' "
            f"request (matched: {match.group(0)!r}). Rejected query: {query_text}"
        )
    if dialect == "sql":
        _validate_postgres_sql_shape(query_text, mode)
    if dialect == "gremlin":
        _validate_gremlin_shape(query_text)


def _validate_gremlin_shape(query_text: str) -> None:
    """Reject traversals that Cosmos DB's Gremlin engine cannot compile,
    before we pay a round trip and get back an opaque GraphCompileException."""
    if not query_text.lstrip().startswith("g."):
        raise CypherGenerationError(
            f"Generated Gremlin traversal must start with 'g.'. "
            f"Rejected query: {query_text}"
        )
    lambda_match = _GREMLIN_LAMBDA_RE.search(query_text)
    if lambda_match:
        raise CypherGenerationError(
            "Generated Gremlin uses a Groovy closure/lambda, which Cosmos DB does "
            f"not support (matched: {lambda_match.group(0)!r}). "
            f"Rejected query: {query_text}"
        )
    step_match = _COSMOS_UNSUPPORTED_STEP_RE.search(query_text)
    if step_match:
        raise CypherGenerationError(
            f"Generated Gremlin uses step {step_match.group(0)!r}, which is not in the "
            f"subset of TinkerPop supported by Cosmos DB. "
            f"Rejected query: {query_text}"
        )


def _validate_postgres_sql_shape(query_text: str, mode: str) -> None:
    lowered = query_text.lower()
    if "your_graph_id" in lowered:
        raise CypherGenerationError(
            "Translated PostgreSQL SQL contains the literal placeholder 'your_graph_id'. "
            "Use %(graph_id)s::text instead."
        )
    if _SQL_INVALID_WITH_LITERAL_RE.search(query_text):
        raise CypherGenerationError(
            "Translated PostgreSQL SQL starts with an invalid WITH clause. "
            "Use a real CTE like `WITH vars AS (SELECT ...)` or avoid WITH entirely."
        )
    if mode == "write":
        _validate_postgres_write_shape(query_text)


def _validate_postgres_write_shape(query_text: str) -> None:
    conflict_match = _SQL_INVALID_CONFLICT_TARGET_RE.search(query_text)
    if not conflict_match:
        return
    raw_target = conflict_match.group(1).strip()
    normalized = re.sub(r"\s+", "", raw_target.lower())
    allowed_targets = {
        "graph_id,node_id",
        "graph_id,rel_type,start_node_id,end_node_id",
    }
    if normalized not in allowed_targets:
        raise CypherGenerationError(
            "Translated PostgreSQL write uses an unsupported ON CONFLICT target. "
            "Pure Postgres graph upserts must conflict on either "
            "(graph_id, node_id) for nodes or "
            "(graph_id, rel_type, start_node_id, end_node_id) for relationships. "
            f"Rejected query: {query_text}"
        )


def generate_cypher(
    natural_language: str,
    schema_context: str,
    mode: str,
    dialect: str = "cypher",
    extra_parameters: Optional[dict[str, Any]] = None,
) -> dict:
    assert mode in ("read", "write")
    if (dialect, mode) not in _PROMPTS:
        raise CypherGenerationError(f"Unsupported dialect/mode combination: {dialect}/{mode}")
    system_prompt = _PROMPTS[(dialect, mode)]
    user_message = build_user_message(natural_language, schema_context, extra_parameters or {})
    client = _get_client()
    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    raw = response.choices[0].message.content or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CypherGenerationError(f"Model did not return valid JSON: {raw!r}") from exc
    query_text = (parsed.get("query") or "").strip()
    parameters = parsed.get("parameters") or {}
    if not query_text:
        raise CypherGenerationError(f"Model response had no 'query': {raw!r}")
    _validate(query_text, dialect, mode)
    merged_params = {**(extra_parameters or {}), **parameters}
    return {"query": query_text, "parameters": merged_params}


def translate_cypher(
    cypher_query: str,
    parameters: Optional[dict[str, Any]],
    schema_context: str,
    mode: str,
    target_dialect: str = "gremlin",
) -> dict:
    assert mode in ("read", "write")
    if (target_dialect, mode) not in _TRANSLATE_PROMPTS:
        raise CypherGenerationError(
            f"Unsupported translation target/mode: {target_dialect}/{mode}"
        )
    # Defence in depth: main.py intercepts schema DDL before it gets here, but
    # if anything else calls this function we refuse rather than let the model
    # invent a data write in place of a constraint.
    if _CYPHER_DDL_RE.match(cypher_query or ""):
        raise CypherGenerationError(
            "Cypher schema DDL (CREATE/DROP CONSTRAINT or INDEX) has no equivalent "
            f"in {target_dialect} and will not be translated. Use the adapter's "
            "create_constraints() / create_indexes() schema API instead. "
            f"Rejected statement: {cypher_query.strip()}"
        )
    system_prompt = _TRANSLATE_PROMPTS[(target_dialect, mode)]
    user_message = build_translate_user_message(cypher_query, schema_context, parameters or {})
    client = _get_client()
    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    raw = response.choices[0].message.content or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CypherGenerationError(f"Model did not return valid JSON: {raw!r}") from exc
    query_text = (parsed.get("query") or "").strip()
    parsed_parameters = parsed.get("parameters") or {}
    if not query_text:
        raise CypherGenerationError(f"Model response had no 'query': {raw!r}")
    _validate(query_text, target_dialect, mode)
    merged_params = {**(parameters or {}), **parsed_parameters}
    if target_dialect == "gremlin":
        _validate_translation_fidelity(cypher_query, query_text, merged_params, mode)
    return {"query": query_text, "parameters": merged_params}

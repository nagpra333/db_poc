"""
llm.py

Azure OpenAI client + natural-language-to-query translation, used by the
/query and /write endpoints when the caller sends a `prompt` instead of raw
query text. Prompt wording lives in prompts.py; this file owns the API call
and the safety checks on whatever comes back.

Supports two dialects, selected by the active GraphAdapter's
`query_language` ("cypher" for Neo4j, "gremlin" for Cosmos DB Gremlin) so
the same /query and /write endpoints work against either backend.
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
    """Raised when the model output can't be used: bad JSON, missing query,
    or a disallowed operation for the requested mode. Name kept for
    backwards compatibility even though it now also covers Gremlin."""
    pass


class LLMConfigError(Exception):
    """Raised when Azure OpenAI env vars are missing."""
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


# --------------------------------------------------------------------------
# Keyword-level guardrails on top of the prompt instructions -- the model
# usually follows instructions, but we don't execute anything against the
# database on instruction-following alone. Best-effort, not a full sandbox.
# --------------------------------------------------------------------------

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
# Only block the catastrophic "drop everything" pattern for writes; addV/addE/
# property/drop on specific, filtered elements is expected and allowed.
_GREMLIN_WRITE_FORBIDDEN = re.compile(
    r"g\s*\.\s*V\s*\(\s*\)\s*\.\s*drop\s*\(\s*\)|g\s*\.\s*E\s*\(\s*\)\s*\.\s*drop\s*\(\s*\)",
    re.IGNORECASE,
)

# Forbidden-operation patterns, keyed by (dialect, mode), decoupled from
# which system prompt produced the query -- shared by both the NL-generation
# path (generate_cypher) and the query-translation path (translate_cypher).
_FORBIDDEN_PATTERNS = {
    ("cypher", "read"): _CYPHER_READ_FORBIDDEN,
    ("cypher", "write"): _CYPHER_WRITE_FORBIDDEN,
    ("gremlin", "read"): _GREMLIN_READ_FORBIDDEN,
    ("gremlin", "write"): _GREMLIN_WRITE_FORBIDDEN,
}

# System prompts for natural-language -> query generation (used by
# generate_cypher, driven by the /query and /write `prompt` field).
_PROMPTS = {
    ("cypher", "read"): READ_SYSTEM_PROMPT,
    ("cypher", "write"): WRITE_SYSTEM_PROMPT,
    ("gremlin", "read"): GREMLIN_READ_SYSTEM_PROMPT,
    ("gremlin", "write"): GREMLIN_WRITE_SYSTEM_PROMPT,
}

# System prompts for Cypher -> Gremlin query translation (used by
# translate_cypher, driven by the /query and /write `query` field when the
# active backend isn't Neo4j). Cypher is the caller's canonical query
# language, so a Neo4j backend never needs this -- only non-Cypher backends do.
_TRANSLATE_PROMPTS = {
    "read": GREMLIN_READ_TRANSLATE_PROMPT,
    "write": GREMLIN_WRITE_TRANSLATE_PROMPT,
}


# --------------------------------------------------------------------------
# Translation-fidelity checks (Cypher -> Gremlin only). The prompt asks the
# model not to invent filters/variables or drop RETURN columns, but a prompt
# is not a guarantee -- these checks catch it before the query ever reaches
# the database, rather than relying on Cosmos to error (it often won't; an
# unbound Gremlin variable can just filter out every result and return 200
# with an empty, silently-wrong result set, which is what motivated this).
# --------------------------------------------------------------------------

# Matches a Gremlin step like .has('prop', someVar) or .by(someVar) where the
# second/only argument is a bare identifier (not a quoted string literal and
# not a number) -- i.e. it's meant to be a bound variable reference.
_BOUND_VAR_REF_RE = re.compile(
    r"\.(?:has|hasId|hasLabel|by|where|is)\s*\(\s*(?:'[^']*'\s*,\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\)"
)

# Gremlin/Groovy keywords and step names that can legitimately appear as a
# bare identifier in that position -- not bound variables, so exempt them
# from the "must be in bindings" check.
_GREMLIN_NON_VAR_TOKENS = frozenset({
    "label", "id", "T", "Order", "Column", "Scope", "Pop", "gt", "gte",
    "lt", "lte", "eq", "neq", "within", "without", "asc", "desc", "incr", "decr", "id_",
})


def _extract_referenced_vars(query_text: str) -> set[str]:
    return {
        m for m in _BOUND_VAR_REF_RE.findall(query_text)
        if m not in _GREMLIN_NON_VAR_TOKENS
    }


def _count_cypher_return_fields(cypher_query: str) -> Optional[int]:
    """Best-effort count of top-level RETURN fields, ignoring commas nested
    inside parentheses/brackets (e.g. function calls). Returns None if no
    RETURN clause is found (e.g. a write-only statement), in which case the
    column-count check is skipped rather than misfiring."""
    match = re.search(r"\bRETURN\b(.*?)(?:\bORDER\s+BY\b|\bSKIP\b|\bLIMIT\b|$)", cypher_query, re.IGNORECASE | re.DOTALL)
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
    """Counts .by(...) calls immediately following a .project(...) call, or
    the number of arguments to a top-level valueMap()/select() as a fallback.
    Returns None if neither pattern is found, so the check is skipped rather
    than misfiring on traversal shapes we don't specifically recognize."""
    if ".project(" in query_text:
        return len(re.findall(r"\.by\s*\(", query_text))
    select_match = re.search(r"\.select\s*\(([^)]*)\)", query_text)
    if select_match:
        args = [a for a in select_match.group(1).split(",") if a.strip()]
        return len(args) if args else None
    values_match = re.search(r"\.values\s*\(([^)]*)\)\s*$", query_text.strip())
    if values_match:
        args = [a for a in values_match.group(1).split(",") if a.strip()]
        # .values() with 2+ args streams multiple values per-vertex rather
        # than emitting one row per RETURN field the way project()/by() or
        # select() do -- it's not actually a valid shape for a multi-column
        # RETURN, so treat it as a mismatch (return 0) rather than counting
        # args as if they were equivalent to .by() clauses.
        return len(args) if len(args) <= 1 else 0
    return None


def _validate_translation_fidelity(
    cypher_query: str, translated_query: str, merged_params: dict[str, Any], mode: str
) -> None:
    # 1. Every bound variable the translated script references must be a
    #    real, known binding -- catches hallucinated filters like the
    #    unrequested `.has('item_id', iid)` this check was written to stop.
    referenced = _extract_referenced_vars(translated_query)
    known = set(merged_params.keys()) | {"graph_id"}
    unknown = referenced - known
    if unknown:
        raise CypherGenerationError(
            f"Translated Gremlin references variable(s) {sorted(unknown)} that are not "
            f"bound parameters and were not present in the source Cypher query or its "
            f"parameters. This usually means the translation added a filter that wasn't "
            f"requested. Rejected query: {translated_query}"
        )

    # 2. For reads, RETURN column count must match the translated projection
    #    -- catches silently-dropped columns.
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


def generate_cypher(
    natural_language: str,
    schema_context: str,
    mode: str,
    dialect: str = "cypher",
    extra_parameters: Optional[dict[str, Any]] = None,
) -> dict:
    """
    Translate a natural-language request into a query.

    mode: "read" (for /query) or "write" (for /write).
    dialect: "cypher" (Neo4j) or "gremlin" (Cosmos DB Gremlin) -- normally
        passed straight from the active GraphAdapter's `query_language`.
    Returns: {"query": str, "parameters": dict}
    Raises: LLMConfigError, CypherGenerationError
    """
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
    except json.JSONDecodeError as e:
        raise CypherGenerationError(f"Model did not return valid JSON: {raw!r}") from e

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
) -> dict:
    """
    Translate a raw Cypher query the caller supplied (via the `query` field,
    not `prompt`) into the active backend's native dialect, used when that
    backend isn't Neo4j. Cypher is treated as the API's canonical query
    language: callers always write `query` in Cypher, and this function
    handles converting it for non-Cypher backends (currently: Gremlin, for
    DB_TYPE=gremlin / Cosmos DB) so a Neo4j-shaped `query` body works
    unchanged against either backend.

    mode: "read" (for /query) or "write" (for /write).
    Returns: {"query": str, "parameters": dict}
    Raises: LLMConfigError, CypherGenerationError
    """
    assert mode in ("read", "write")
    if mode not in _TRANSLATE_PROMPTS:
        raise CypherGenerationError(f"Unsupported translation mode: {mode}")

    system_prompt = _TRANSLATE_PROMPTS[mode]
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
    except json.JSONDecodeError as e:
        raise CypherGenerationError(f"Model did not return valid JSON: {raw!r}") from e

    query_text = (parsed.get("query") or "").strip()
    parsed_parameters = parsed.get("parameters") or {}
    if not query_text:
        raise CypherGenerationError(f"Model response had no 'query': {raw!r}")

    # Translation always targets Gremlin today (the only non-Cypher
    # backend); validated with the same forbidden-operation guardrails used
    # for LLM-generated Gremlin queries.
    _validate(query_text, "gremlin", mode)

    merged_params = {**(parameters or {}), **parsed_parameters}

    # Fidelity checks specific to translation (not applicable to
    # generate_cypher, which has no source query to be faithful to): catches
    # hallucinated filters/variables and dropped RETURN columns before this
    # query is ever sent to the database.
    _validate_translation_fidelity(cypher_query, query_text, merged_params, mode)

    return {"query": query_text, "parameters": merged_params}

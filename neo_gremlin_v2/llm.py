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
    GREMLIN_WRITE_SYSTEM_PROMPT,
    READ_SYSTEM_PROMPT,
    WRITE_SYSTEM_PROMPT,
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

_PROMPTS = {
    ("cypher", "read"): (READ_SYSTEM_PROMPT, _CYPHER_READ_FORBIDDEN),
    ("cypher", "write"): (WRITE_SYSTEM_PROMPT, _CYPHER_WRITE_FORBIDDEN),
    ("gremlin", "read"): (GREMLIN_READ_SYSTEM_PROMPT, _GREMLIN_READ_FORBIDDEN),
    ("gremlin", "write"): (GREMLIN_WRITE_SYSTEM_PROMPT, _GREMLIN_WRITE_FORBIDDEN),
}


def _validate(query_text: str, dialect: str, mode: str) -> None:
    _, pattern = _PROMPTS[(dialect, mode)]
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

    system_prompt, _ = _PROMPTS[(dialect, mode)]
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

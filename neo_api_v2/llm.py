"""
llm.py

Azure OpenAI client + natural-language-to-Cypher translation, used by the
/query and /write endpoints when the caller sends a `prompt` instead of raw
Cypher. Prompt wording lives in prompts.py; this file owns the API call and
the safety checks on whatever Cypher comes back.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from openai import AzureOpenAI

from prompts import READ_SYSTEM_PROMPT, WRITE_SYSTEM_PROMPT, build_user_message

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

_client: Optional[AzureOpenAI] = None


class CypherGenerationError(Exception):
    """Raised when the model output can't be used: bad JSON, missing query,
    or a disallowed operation for the requested mode."""
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
                "cannot translate a prompt to Cypher."
            )
        _client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_OPENAI_API_VERSION,
        )
    return _client


# Keyword-level guardrails on top of the prompt instructions -- the model
# usually follows instructions, but we don't execute anything against Neo4j
# on instruction-following alone.
_READ_FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP)\b|CALL\s+(dbms|apoc\.(load|create|refactor))\.",
    re.IGNORECASE,
)
_WRITE_FORBIDDEN = re.compile(
    r"\bDROP\s+(DATABASE|INDEX|CONSTRAINT)\b|CALL\s+dbms\.|CALL\s+apoc\.load\.(json|csv|jdbc)|LOAD\s+CSV",
    re.IGNORECASE,
)


def _validate(cypher: str, mode: str) -> None:
    pattern = _READ_FORBIDDEN if mode == "read" else _WRITE_FORBIDDEN
    match = pattern.search(cypher)
    if match:
        raise CypherGenerationError(
            f"Generated Cypher contains a disallowed operation for a '{mode}' request "
            f"(matched: {match.group(0)!r}). Rejected query: {cypher}"
        )


def generate_cypher(
    natural_language: str,
    schema_context: str,
    mode: str,
    extra_parameters: Optional[dict[str, Any]] = None,
) -> dict:
    """
    Translate a natural-language request into Cypher.

    mode: "read" (for /query) or "write" (for /write).
    Returns: {"query": str, "parameters": dict}
    Raises: LLMConfigError, CypherGenerationError
    """
    assert mode in ("read", "write")
    system_prompt = READ_SYSTEM_PROMPT if mode == "read" else WRITE_SYSTEM_PROMPT
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

    cypher = (parsed.get("query") or "").strip()
    parameters = parsed.get("parameters") or {}
    if not cypher:
        raise CypherGenerationError(f"Model response had no 'query': {raw!r}")

    _validate(cypher, mode)

    merged_params = {**(extra_parameters or {}), **parameters}
    return {"query": cypher, "parameters": merged_params}

"""
graph_adapter.py

The adapter interface both backends implement:
  - neo4j_ops.py   -> Neo4jGraphService  (DB_TYPE=neo4j)
  - gremlin_ops.py -> GremlinGraphService (DB_TYPE=gremlin, e.g. Cosmos DB)

main.py and llm.py depend only on GraphAdapter (this file) -- they never
import a concrete backend directly. Swapping DB_TYPE in .env is the only
thing that changes which class main.py instantiates.

Shared exceptions and small utilities also live here so both backends raise
the same error types and main.py's exception handlers don't need to know
which backend is running.
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional


# --------------------------------------------------------------------------
# Errors -- raised by either backend, translated to HTTP responses in main.py
# --------------------------------------------------------------------------

class GraphNotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class BadRequestError(Exception):
    pass


class NodeReferenceError(Exception):
    """A relationship endpoint node does not exist and upsert_missing_nodes=False."""
    pass


# --------------------------------------------------------------------------
# Shared utilities
# --------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_ident(value: str, kind: str) -> str:
    """Validate a string that will be interpolated into a query as a label,
    relationship/edge type, or property key. Neither Cypher nor Gremlin
    support parameters in these positions, so only safe identifier
    characters are allowed."""
    if not value or not _IDENT_RE.match(value):
        raise BadRequestError(f"Invalid {kind}: {value!r}. Must match ^[A-Za-z_][A-Za-z0-9_]*$")
    return value


def is_valid_ident(value: str) -> bool:
    return bool(value) and bool(_IDENT_RE.match(value))


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Adapter interface
# --------------------------------------------------------------------------

class GraphAdapter(ABC):
    """Backend-agnostic graph operations consumed by main.py."""

    #: "cypher" or "gremlin" -- lets llm.py pick the right prompt/dialect
    #: and lets main.py know which query language raw `query` bodies are in.
    query_language: str

    @abstractmethod
    def close(self) -> None: ...

    # -- Graph lifecycle --------------------------------------------------
    @abstractmethod
    def create_graph(self, name: str, description: Optional[str]) -> dict: ...

    @abstractmethod
    def get_graph(self, graph_id: str) -> dict: ...

    # -- Schema -------------------------------------------------------------
    @abstractmethod
    def get_schema(self, graph_id: str) -> dict: ...

    @abstractmethod
    def create_constraints(self, graph_id: str, constraints: list[dict]) -> list[dict]: ...

    @abstractmethod
    def create_indexes(self, graph_id: str, indexes: list[dict]) -> list[dict]: ...

    # -- Nodes / relationships ----------------------------------------------
    @abstractmethod
    def upsert_nodes(self, graph_id: str, nodes: list[dict]) -> list[dict]: ...

    @abstractmethod
    def upsert_relationships(
        self, graph_id: str, relationships: list[dict], upsert_missing_nodes: bool
    ) -> list[dict]: ...

    # -- Query / write --------------------------------------------------------
    @abstractmethod
    def execute_query(
        self, graph_id: str, name: Optional[str], query: Optional[str], parameters: Optional[dict]
    ) -> dict: ...

    @abstractmethod
    def execute_write(self, graph_id: str, query: str, parameters: Optional[dict]) -> dict: ...

    # -- Stats / health / LLM grounding --------------------------------------
    @abstractmethod
    def get_stats(self, graph_id: str) -> dict: ...

    @abstractmethod
    def check_health(self, graph_id: str) -> dict: ...

    @abstractmethod
    def get_schema_context(self, graph_id: str) -> str: ...

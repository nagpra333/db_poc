"""
neo4j_ops.py

FastAPI route handlers (main.py) never talk
to the driver directly -- they call methods on Neo4jGraphService.

Design notes / simplifications (Neo4j Community edition has one database):
- A "graph" from the API spec is a logical namespace inside the single
  configured Neo4j database (NEO4J_DB), not a separate physical database.
- Every domain node created through /nodes or /relationships is tagged with
  an extra label `Graph_<uuid-without-dashes>` so it can be cheaply scoped
  back to its owning graph. This is the standard Neo4j multi-tenant-by-label
  pattern (no APOC required).
- Graph metadata itself is stored on `:__Graph__` nodes.
- Constraints and indexes are real Neo4j schema objects (global to a label,
  as Neo4j requires) but we also record which graph asked for them on a
  `:__SchemaObject__` metadata node so GET /schema can report per-graph.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError


# --------------------------------------------------------------------------
# Errors -- raised by the service layer, translated to HTTP responses in main.py
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


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize_ident(value: str, kind: str) -> str:
    """Validate a string that will be interpolated into Cypher as a label,
    relationship type, or property key. Cypher does not support parameters
    for these positions, so we allow only safe identifier characters."""
    if not value or not _IDENT_RE.match(value):
        raise BadRequestError(f"Invalid {kind}: {value!r}. Must match ^[A-Za-z_][A-Za-z0-9_]*$")
    return value


def _graph_label(graph_id: str) -> str:
    return "Graph_" + graph_id.replace("-", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Named (server-defined) read queries -- referenced via {"name": "..."} in
# POST /graphs/{id}/query
# --------------------------------------------------------------------------

NAMED_QUERIES: dict[str, str] = {
    "rules_enforced_by_program": """
        MATCH (br:BusinessRule:{graph_label})-[:ENFORCED_BY]->(p:Program:{graph_label} {{name: $program_name}})
        RETURN br.rule_id AS rule_id, br.name AS rule_name
    """,
    "programs_called_by": """
        MATCH (p:Program:{graph_label} {{name: $program_name}})-[:CALLS]->(callee:Program:{graph_label})
        RETURN callee.program_id AS program_id, callee.name AS name
    """,
}


class Neo4jGraphService:
    def __init__(self, uri: str, user: str, password: str, database: str):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def _session(self):
        return self._driver.session(database=self._database)

    # ------------------------------------------------------------------
    # Graph lifecycle
    # ------------------------------------------------------------------

    def create_graph(self, name: str, description: Optional[str]) -> dict:
        with self._session() as session:
            existing = session.run(
                "MATCH (g:__Graph__ {name: $name}) RETURN g LIMIT 1", name=name
            ).single()
            if existing:
                raise ConflictError(f"A graph named '{name}' already exists.")

            graph_id = str(uuid.uuid4())
            created_at = _now_iso()
            record = session.run(
                """
                CREATE (g:__Graph__ {
                    id: $id, name: $name, description: $description,
                    status: 'ready', created_at: $created_at
                })
                RETURN g
                """,
                id=graph_id,
                name=name,
                description=description,
                created_at=created_at,
            ).single()
            return dict(record["g"])

    def get_graph(self, graph_id: str) -> dict:
        with self._session() as session:
            record = session.run(
                "MATCH (g:__Graph__ {id: $id}) RETURN g", id=graph_id
            ).single()
            if not record:
                raise GraphNotFoundError(graph_id)
            return dict(record["g"])

    def _ensure_graph_exists(self, session, graph_id: str) -> None:
        record = session.run(
            "MATCH (g:__Graph__ {id: $id}) RETURN g.id AS id", id=graph_id
        ).single()
        if not record:
            raise GraphNotFoundError(graph_id)

    # ------------------------------------------------------------------
    # Schema: constraints & indexes
    # ------------------------------------------------------------------

    def get_schema(self, graph_id: str) -> dict:
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)

            meta = session.run(
                """
                MATCH (:__Graph__ {id: $graph_id})-[:HAS_SCHEMA_OBJECT]->(s:__SchemaObject__)
                RETURN s.kind AS kind, s.name AS name, s.label AS label,
                       s.properties AS properties, s.type AS type
                """,
                graph_id=graph_id,
            ).data()

            constraints = []
            indexes = []
            constraint_names = [m["name"] for m in meta if m["kind"] == "constraint"]
            index_names = [m["name"] for m in meta if m["kind"] == "index"]

            if constraint_names:
                live = {r["name"]: r for r in session.run("SHOW CONSTRAINTS").data()}
                for m in meta:
                    if m["kind"] == "constraint" and m["name"] in live:
                        constraints.append(
                            {
                                "name": m["name"],
                                "label": m["label"],
                                "properties": m["properties"],
                                "type": "unique",
                            }
                        )
            if index_names:
                live_idx = {r["name"]: r for r in session.run("SHOW INDEXES").data()}
                for m in meta:
                    if m["kind"] == "index" and m["name"] in live_idx:
                        state = live_idx[m["name"]].get("state", "online").lower()
                        indexes.append(
                            {
                                "name": m["name"],
                                "label": m["label"],
                                "properties": m["properties"],
                                "type": m["type"],
                                "state": state,
                            }
                        )

            return {"constraints": constraints, "indexes": indexes}

    def create_constraints(self, graph_id: str, constraints: list[dict]) -> list[dict]:
        results = []
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            for c in constraints:
                label = _sanitize_ident(c["label"], "label")
                props = [_sanitize_ident(p, "property") for p in c["properties"]]
                name = c.get("name") or f"unique_{label.lower()}_{'_'.join(p.lower() for p in props)}"
                name = _sanitize_ident(name, "constraint name")
                prop_list = ", ".join(f"n.{p}" for p in props)

                try:
                    session.run(
                        f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                        f"FOR (n:{label}) REQUIRE ({prop_list}) IS UNIQUE"
                    )
                except Neo4jError as e:
                    raise ConflictError(str(e)) from e

                session.run(
                    """
                    MATCH (g:__Graph__ {id: $graph_id})
                    MERGE (s:__SchemaObject__ {name: $name, kind: 'constraint'})
                    SET s.label = $label, s.properties = $properties, s.type = 'unique'
                    MERGE (g)-[:HAS_SCHEMA_OBJECT]->(s)
                    """,
                    graph_id=graph_id,
                    name=name,
                    label=label,
                    properties=props,
                )
                results.append({"name": name, "label": label, "properties": props, "type": "unique"})
        return results

    def create_indexes(self, graph_id: str, indexes: list[dict]) -> list[dict]:
        results = []
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            for idx in indexes:
                label = _sanitize_ident(idx["label"], "label")
                props = [_sanitize_ident(p, "property") for p in idx["properties"]]
                idx_type = idx.get("type", "btree")
                name = idx.get("name") or f"idx_{label.lower()}_{'_'.join(p.lower() for p in props)}"
                name = _sanitize_ident(name, "index name")
                prop_list = ", ".join(f"n.{p}" for p in props)

                try:
                    if idx_type == "fulltext":
                        session.run(
                            f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS "
                            f"FOR (n:{label}) ON EACH [{prop_list}]"
                        )
                    else:
                        session.run(
                            f"CREATE INDEX {name} IF NOT EXISTS "
                            f"FOR (n:{label}) ON ({prop_list})"
                        )
                except Neo4jError as e:
                    raise ConflictError(str(e)) from e

                session.run(
                    """
                    MATCH (g:__Graph__ {id: $graph_id})
                    MERGE (s:__SchemaObject__ {name: $name, kind: 'index'})
                    SET s.label = $label, s.properties = $properties, s.type = $type
                    MERGE (g)-[:HAS_SCHEMA_OBJECT]->(s)
                    """,
                    graph_id=graph_id,
                    name=name,
                    label=label,
                    properties=props,
                    type=idx_type,
                )

                state_row = session.run(
                    "SHOW INDEXES YIELD name, state WHERE name = $name", name=name
                ).single()
                state = state_row["state"].lower() if state_row else "populating"
                results.append(
                    {"name": name, "label": label, "properties": props, "type": idx_type, "state": state}
                )
        return results

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_nodes(self, graph_id: str, nodes: list[dict]) -> list[dict]:
        results = []
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            g_label = _graph_label(graph_id)
            for node in nodes:
                node_id = node["id"]
                labels = [_sanitize_ident(l, "label") for l in node.get("labels", [])]
                properties = node.get("properties", {}) or {}
                label_str = ":".join([g_label] + labels) if labels else g_label

                record = session.run(
                    f"""
                    MERGE (n:{label_str} {{id: $id}})
                    ON CREATE SET n.__created__ = true
                    WITH n, coalesce(n.__created__, false) AS was_created
                    REMOVE n.__created__
                    SET n += $properties
                    SET n.id = $id
                    RETURN was_created, size(keys($properties)) AS properties_set
                    """,
                    id=node_id,
                    properties=properties,
                ).single()

                results.append(
                    {
                        "id": node_id,
                        "node_created": record["was_created"],
                        "node_updated": not record["was_created"],
                        "properties_set": record["properties_set"],
                    }
                )
        return results

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    @staticmethod
    def _build_key_where(var: str, key: dict, param_prefix: str) -> tuple[str, dict]:
        clauses = []
        params = {}
        for i, (k, v) in enumerate(key.items()):
            prop = _sanitize_ident(k, "property")
            pname = f"{param_prefix}_{i}"
            clauses.append(f"{var}.{prop} = ${pname}")
            params[pname] = v
        return " AND ".join(clauses), params

    def upsert_relationships(
        self, graph_id: str, relationships: list[dict], upsert_missing_nodes: bool
    ) -> list[dict]:
        results = []
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            g_label = _graph_label(graph_id)
            for rel in relationships:
                rel_type = _sanitize_ident(rel["type"], "relationship type")
                start = rel["start_node"]
                end = rel["end_node"]
                start_label = _sanitize_ident(start["label"], "label")
                end_label = _sanitize_ident(end["label"], "label")
                rel_props = rel.get("properties", {}) or {}

                start_where, start_params = self._build_key_where("s", start["key"], "s")
                end_where, end_params = self._build_key_where("e", end["key"], "e")

                params = {**start_params, **end_params, "rel_properties": rel_props}

                nodes_created = 0
                if upsert_missing_nodes:
                    merge_clause = f"""
                    MERGE (s:{g_label}:{start_label} {{ {self._equals_from_key(start['key'], 's')} }})
                    ON CREATE SET s.__created__ = true
                    WITH s, coalesce(s.__created__, false) AS s_created
                    REMOVE s.__created__
                    MERGE (e:{g_label}:{end_label} {{ {self._equals_from_key(end['key'], 'e')} }})
                    ON CREATE SET e.__created__ = true
                    WITH s, s_created, e, coalesce(e.__created__, false) AS e_created
                    REMOVE e.__created__
                    """
                    params.update(self._key_params(start["key"], "s"))
                    params.update(self._key_params(end["key"], "e"))
                else:
                    merge_clause = f"""
                    MATCH (s:{g_label}:{start_label}) WHERE {start_where}
                    MATCH (e:{g_label}:{end_label}) WHERE {end_where}
                    WITH s, e, false AS s_created, false AS e_created
                    """

                query = f"""
                {merge_clause}
                MERGE (s)-[r:{rel_type}]->(e)
                ON CREATE SET r.__created__ = true
                WITH r, s_created, e_created, coalesce(r.__created__, false) AS r_created
                REMOVE r.__created__
                SET r += $rel_properties
                RETURN r_created, s_created, e_created
                """

                try:
                    record = session.run(query, **params).single()
                except Neo4jError as e:
                    raise NodeReferenceError(str(e)) from e

                if record is None:
                    raise NodeReferenceError(
                        f"Start or end node not found for relationship {rel_type} "
                        f"({start_label} -> {end_label}); set upsert_missing_nodes=true to auto-create."
                    )

                nodes_created = int(record["s_created"]) + int(record["e_created"])
                results.append(
                    {
                        "relationship_created": record["r_created"],
                        "relationship_updated": not record["r_created"],
                        "nodes_created": nodes_created,
                    }
                )
        return results

    @staticmethod
    def _equals_from_key(key: dict, var: str) -> str:
        parts = []
        for k in key.keys():
            prop = _sanitize_ident(k, "property")
            parts.append(f"{prop}: ${var}_{prop}")
        return ", ".join(parts)

    @staticmethod
    def _key_params(key: dict, var: str) -> dict:
        return {f"{var}_{_sanitize_ident(k, 'property')}": v for k, v in key.items()}

    # ------------------------------------------------------------------
    # Query / Write
    # ------------------------------------------------------------------

    def execute_query(
        self,
        graph_id: str,
        name: Optional[str],
        query: Optional[str],
        parameters: Optional[dict],
    ) -> dict:
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            g_label = _graph_label(graph_id)
            params = dict(parameters or {})

            if name:
                template = NAMED_QUERIES.get(name)
                if not template:
                    raise BadRequestError(f"Unknown named query: {name}")
                cypher = template.format(graph_label=g_label)
            elif query:
                cypher = query
                params.setdefault("graph_id", graph_id)
            else:
                raise BadRequestError("Provide either 'name' or 'query'.")

            result = session.run(cypher, **params)
            columns = list(result.keys())
            rows = [dict(r) for r in result]
            return {"columns": columns, "results": rows}

    def execute_write(self, graph_id: str, query: str, parameters: Optional[dict]) -> dict:
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            params = dict(parameters or {})
            params.setdefault("graph_id", graph_id)

            summary_counters = {}

            def work(tx):
                result = tx.run(query, **params)
                rows = [dict(r) for r in result]
                summary = result.consume()
                counters = summary.counters
                return rows, counters

            rows, counters = session.execute_write(work)
            stats = {
                "nodes_created": counters.nodes_created,
                "nodes_deleted": counters.nodes_deleted,
                "relationships_created": counters.relationships_created,
                "relationships_deleted": counters.relationships_deleted,
                "properties_set": counters.properties_set,
                "labels_added": counters.labels_added,
            }
            return {"results": rows, "stats": stats}

    # ------------------------------------------------------------------
    # Stats & health
    # ------------------------------------------------------------------

    def get_stats(self, graph_id: str) -> dict:
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
            g_label = _graph_label(graph_id)

            node_count = session.run(
                f"MATCH (n:{g_label}) RETURN count(n) AS c"
            ).single()["c"]

            rel_count = session.run(
                f"MATCH (:{g_label})-[r]->(:{g_label}) RETURN count(r) AS c"
            ).single()["c"]

            label_rows = session.run(
                f"""
                MATCH (n:{g_label})
                UNWIND [l IN labels(n) WHERE l <> $g_label] AS lbl
                RETURN lbl, count(*) AS c
                """,
                g_label=g_label,
            ).data()
            label_counts = {r["lbl"]: r["c"] for r in label_rows}

            rel_type_rows = session.run(
                f"""
                MATCH (:{g_label})-[r]->(:{g_label})
                RETURN type(r) AS t, count(*) AS c
                """
            ).data()
            rel_type_counts = {r["t"]: r["c"] for r in rel_type_rows}

            meta_counts = session.run(
                """
                MATCH (:__Graph__ {id: $graph_id})-[:HAS_SCHEMA_OBJECT]->(s:__SchemaObject__)
                RETURN s.kind AS kind, count(*) AS c
                """,
                graph_id=graph_id,
            ).data()
            constraint_count = next((r["c"] for r in meta_counts if r["kind"] == "constraint"), 0)
            index_count = next((r["c"] for r in meta_counts if r["kind"] == "index"), 0)

            return {
                "node_count": node_count,
                "relationship_count": rel_count,
                "label_counts": label_counts,
                "relationship_type_counts": rel_type_counts,
                "constraint_count": constraint_count,
                "index_count": index_count,
                "generated_at": _now_iso(),
            }

    def get_schema_context(self, graph_id: str) -> str:
        """Human-readable summary of a graph's labels, relationship types,
        constraints, indexes, and a few sample properties per label. Used to
        ground the LLM when translating a natural-language prompt to Cypher."""
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)
        schema = self.get_schema(graph_id)
        stats = self.get_stats(graph_id)
        g_label = _graph_label(graph_id)

        lines: list[str] = []

        lines.append("Node labels (count):")
        if stats["label_counts"]:
            for label, count in stats["label_counts"].items():
                lines.append(f"  - {label} ({count})")
        else:
            lines.append("  (none yet)")

        lines.append("Relationship types (count):")
        if stats["relationship_type_counts"]:
            for rtype, count in stats["relationship_type_counts"].items():
                lines.append(f"  - {rtype} ({count})")
        else:
            lines.append("  (none yet)")

        if schema["constraints"]:
            lines.append("Unique constraints:")
            for c in schema["constraints"]:
                lines.append(f"  - {c['label']}.{','.join(c['properties'])}")

        if schema["indexes"]:
            lines.append("Indexes:")
            for i in schema["indexes"]:
                lines.append(f"  - {i['label']}.{','.join(i['properties'])} ({i['type']}, {i['state']})")

        sample_lines = ["Sample properties per label:"]
        with self._session() as session:
            for label in stats["label_counts"].keys():
                if not _IDENT_RE.match(label):
                    continue
                row = session.run(
                    f"MATCH (n:{g_label}:{label}) RETURN keys(n) AS ks LIMIT 1"
                ).single()
                if row:
                    props = [k for k in row["ks"] if k not in ("id",)]
                    sample_lines.append(f"  - {label}: {', '.join(props) if props else '(no properties yet)'}")
        if len(sample_lines) > 1:
            lines.extend(sample_lines)

        return "\n".join(lines)

    def check_health(self, graph_id: str) -> dict:
        # Existence check is allowed to raise GraphNotFoundError (-> 404),
        # separate from a degraded connection (-> 503 with status unhealthy).
        with self._session() as session:
            self._ensure_graph_exists(session, graph_id)

        start = time.perf_counter()
        try:
            with self._session() as session:
                session.run("RETURN 1").single()
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {"status": "healthy", "latency_ms": latency_ms, "checked_at": _now_iso()}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e), "checked_at": _now_iso()}

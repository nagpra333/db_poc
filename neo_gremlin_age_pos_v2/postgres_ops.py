"""
postgres_ops.py

Pure PostgreSQL implementation of GraphAdapter.

Storage model:
- graphs are logical namespaces tracked in `graph_metadata`
- nodes are stored in one `graph_nodes` table with labels as text[]
  and properties as jsonb
- relationships are stored in one `graph_relationships` table with
  endpoints pointing to graph_nodes and properties as jsonb

Query model:
- API callers still send Cypher to /query and /write
- main.py + llm.py are expected to translate Cypher into PostgreSQL SQL
  before calling execute_query / execute_write on this adapter
- this adapter only executes SQL, not Cypher
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from graph_adapter import (
    BadRequestError,
    ConflictError,
    GraphAdapter,
    GraphNotFoundError,
    NodeReferenceError,
    new_id,
    now_iso,
    sanitize_ident,
)


NAMED_QUERIES: dict[str, str] = {
    "rules_enforced_by_program": """
        SELECT
            br.properties->>'rule_id' AS rule_id,
            br.properties->>'name' AS rule_name
        FROM graph_relationships rel
        JOIN graph_nodes br
          ON br.graph_id = rel.graph_id
         AND br.node_id = rel.start_node_id
        JOIN graph_nodes p
          ON p.graph_id = rel.graph_id
         AND p.node_id = rel.end_node_id
        WHERE rel.graph_id = %(graph_id)s
          AND rel.rel_type = 'ENFORCED_BY'
          AND 'BusinessRule' = ANY(br.labels)
          AND 'Program' = ANY(p.labels)
          AND p.properties->>'name' = %(program_name)s
    """,
    "programs_called_by": """
        SELECT
            callee.properties->>'program_id' AS program_id,
            callee.properties->>'name' AS name
        FROM graph_nodes p
        JOIN graph_relationships rel
          ON rel.graph_id = p.graph_id
         AND rel.start_node_id = p.node_id
         AND rel.rel_type = 'CALLS'
        JOIN graph_nodes callee
          ON callee.graph_id = rel.graph_id
         AND callee.node_id = rel.end_node_id
        WHERE p.graph_id = %(graph_id)s
          AND 'Program' = ANY(p.labels)
          AND 'Program' = ANY(callee.labels)
          AND p.properties->>'name' = %(program_name)s
    """,
}


class PostgresGraphService(GraphAdapter):
    query_language = "sql"

    def __init__(
        self,
        host: str,
        port: int | str,
        database: str,
        user: str,
        password: str,
    ):
        self._conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=database,
            user=user,
            password=password,
        )
        self._conn.autocommit = True
        self._bootstrap()

    def close(self) -> None:
        self._conn.close()

    def _cursor(self, *, dict_rows: bool = False):
        if dict_rows:
            return self._conn.cursor(cursor_factory=RealDictCursor)
        return self._conn.cursor()

    def _bootstrap(self) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_metadata (
                    graph_id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(255) UNIQUE NOT NULL,
                    description TEXT,
                    status VARCHAR(20) NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_schema_objects (
                    graph_id VARCHAR(36) NOT NULL REFERENCES graph_metadata(graph_id) ON DELETE CASCADE,
                    kind VARCHAR(20) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    label_name VARCHAR(255) NOT NULL,
                    properties JSONB NOT NULL DEFAULT '[]'::jsonb,
                    object_type VARCHAR(50) NOT NULL,
                    state VARCHAR(20),
                    created_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (graph_id, kind, name)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    graph_id VARCHAR(36) NOT NULL REFERENCES graph_metadata(graph_id) ON DELETE CASCADE,
                    node_id VARCHAR(255) NOT NULL,
                    labels TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (graph_id, node_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_relationships (
                    graph_id VARCHAR(36) NOT NULL REFERENCES graph_metadata(graph_id) ON DELETE CASCADE,
                    rel_id BIGSERIAL PRIMARY KEY,
                    rel_type VARCHAR(255) NOT NULL,
                    start_node_id VARCHAR(255) NOT NULL,
                    end_node_id VARCHAR(255) NOT NULL,
                    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    CONSTRAINT uq_graph_rel UNIQUE (graph_id, rel_type, start_node_id, end_node_id),
                    CONSTRAINT fk_graph_rel_start FOREIGN KEY (graph_id, start_node_id)
                        REFERENCES graph_nodes(graph_id, node_id) ON DELETE CASCADE,
                    CONSTRAINT fk_graph_rel_end FOREIGN KEY (graph_id, end_node_id)
                        REFERENCES graph_nodes(graph_id, node_id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_graph_nodes_graph
                ON graph_nodes (graph_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_graph_nodes_labels_gin
                ON graph_nodes USING GIN (labels)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_graph_nodes_properties_gin
                ON graph_nodes USING GIN (properties)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_graph_relationships_graph
                ON graph_relationships (graph_id, rel_type)
                """
            )

    def _ensure_graph_exists(self, graph_id: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM graph_metadata WHERE graph_id = %s",
                (graph_id,),
            )
            if not cur.fetchone():
                raise GraphNotFoundError(graph_id)

    def _resolve_node(self, graph_id: str, label: str, key: dict[str, Any]) -> Optional[dict]:
        sanitize_ident(label, "label")
        if not key:
            raise BadRequestError("Node reference key cannot be empty.")

        clauses = ["graph_id = %s", "%s = ANY(labels)"]
        params: list[Any] = [graph_id, label]

        for prop, value in key.items():
            sanitize_ident(prop, "property")
            if prop == "id":
                clauses.append("node_id = %s")
                params.append(str(value))
            else:
                clauses.append("properties @> %s::jsonb")
                params.append(json.dumps({prop: value}))

        sql = f"""
            SELECT node_id, labels, properties
            FROM graph_nodes
            WHERE {' AND '.join(clauses)}
            LIMIT 1
        """
        with self._cursor(dict_rows=True) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _create_missing_node(self, graph_id: str, label: str, key: dict[str, Any]) -> str:
        node_id = str(key.get("id") or new_id())
        properties = dict(key)
        properties.setdefault("id", node_id)
        timestamp = now_iso()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO graph_nodes (
                    graph_id, node_id, labels, properties, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (graph_id, node_id) DO UPDATE
                SET labels = (
                        SELECT ARRAY(
                            SELECT DISTINCT x
                            FROM unnest(graph_nodes.labels || EXCLUDED.labels) AS x
                        )
                    ),
                    properties = graph_nodes.properties || EXCLUDED.properties,
                    updated_at = EXCLUDED.updated_at
                """,
                (graph_id, node_id, [label], Json(properties), timestamp, timestamp),
            )
        return node_id

    @staticmethod
    def _normalize_labels(labels: list[str]) -> list[str]:
        if not labels:
            raise BadRequestError("Each node must provide at least one label.")
        normalized = []
        for label in labels:
            normalized.append(sanitize_ident(label, "label"))
        return list(dict.fromkeys(normalized))

    # ------------------------------------------------------------------
    # Graph lifecycle
    # ------------------------------------------------------------------

    def create_graph(self, name: str, description: Optional[str]) -> dict:
        graph_id = new_id()
        created_at = now_iso()
        try:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO graph_metadata (graph_id, name, description, status, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (graph_id, name, description, "ready", created_at),
                )
        except psycopg2.errors.UniqueViolation as exc:
            self._conn.rollback()
            raise ConflictError(f"A graph named '{name}' already exists.") from exc
        except Exception as exc:
            self._conn.rollback()
            raise BadRequestError(f"Failed to create graph: {exc}") from exc

        return {
            "id": graph_id,
            "name": name,
            "description": description,
            "status": "ready",
            "created_at": created_at,
        }

    def get_graph(self, graph_id: str) -> dict:
        with self._cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT graph_id AS id, name, description, status, created_at
                FROM graph_metadata
                WHERE graph_id = %s
                """,
                (graph_id,),
            )
            row = cur.fetchone()
        if not row:
            raise GraphNotFoundError(graph_id)
        row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
        return dict(row)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def get_schema(self, graph_id: str) -> dict:
        self._ensure_graph_exists(graph_id)
        with self._cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT kind, name, label_name, properties, object_type, state
                FROM graph_schema_objects
                WHERE graph_id = %s
                ORDER BY kind, name
                """,
                (graph_id,),
            )
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT DISTINCT unnest(labels) AS label
                FROM graph_nodes
                WHERE graph_id = %s
                ORDER BY label
                """,
                (graph_id,),
            )
            labels = [row["label"] for row in cur.fetchall()]

            cur.execute(
                """
                SELECT rel_type, count(*) AS c
                FROM graph_relationships
                WHERE graph_id = %s
                GROUP BY rel_type
                ORDER BY rel_type
                """,
                (graph_id,),
            )
            relationship_types = [row["rel_type"] for row in cur.fetchall()]

        constraints = []
        indexes = []
        for row in rows:
            item = {
                "name": row["name"],
                "label": row["label_name"],
                "properties": row["properties"],
                "type": row["object_type"],
            }
            if row["kind"] == "constraint":
                constraints.append(item)
            else:
                item["state"] = row["state"] or "online"
                indexes.append(item)

        return {
            "constraints": constraints,
            "indexes": indexes,
            "labels": labels,
            "relationship_types": relationship_types,
        }

    def create_constraints(self, graph_id: str, constraints: list[dict]) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        created = []
        for spec in constraints:
            label = sanitize_ident(spec["label"], "label")
            properties = [sanitize_ident(p, "property") for p in spec["properties"]]
            constraint_type = (spec.get("type") or "unique").lower()
            if constraint_type != "unique":
                raise BadRequestError("Postgres backend supports only 'unique' constraints.")

            name = spec.get("name") or f"unique_{label.lower()}_{'_'.join(p.lower() for p in properties)}"
            name = sanitize_ident(name, "constraint name")

            exprs = []
            for prop in properties:
                if prop == "id":
                    exprs.append("(node_id)")
                else:
                    exprs.append(f"((properties->>'{prop}'))")

            sql = f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {name}
                ON graph_nodes (graph_id, {', '.join(exprs)})
                WHERE labels @> ARRAY['{label}']::text[]
            """
            try:
                with self._cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        """
                        INSERT INTO graph_schema_objects (
                            graph_id, kind, name, label_name, properties, object_type, state, created_at
                        )
                        VALUES (%s, 'constraint', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (graph_id, kind, name) DO UPDATE
                        SET label_name = EXCLUDED.label_name,
                            properties = EXCLUDED.properties,
                            object_type = EXCLUDED.object_type,
                            state = EXCLUDED.state
                        """,
                        (graph_id, name, label, Json(properties), "unique", "online", now_iso()),
                    )
            except Exception as exc:
                self._conn.rollback()
                raise ConflictError(f"Failed to create constraint '{name}': {exc}") from exc

            created.append({"name": name, "label": label, "properties": properties, "type": "unique"})
        return created

    def create_indexes(self, graph_id: str, indexes: list[dict]) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        created = []
        for spec in indexes:
            label = sanitize_ident(spec["label"], "label")
            properties = [sanitize_ident(p, "property") for p in spec["properties"]]
            index_type = (spec.get("type") or "btree").lower()
            if index_type != "btree":
                raise BadRequestError("Postgres backend currently supports only 'btree' indexes.")

            name = spec.get("name") or f"idx_{label.lower()}_{'_'.join(p.lower() for p in properties)}"
            name = sanitize_ident(name, "index name")

            exprs = []
            for prop in properties:
                if prop == "id":
                    exprs.append("(node_id)")
                else:
                    exprs.append(f"((properties->>'{prop}'))")

            sql = f"""
                CREATE INDEX IF NOT EXISTS {name}
                ON graph_nodes (graph_id, {', '.join(exprs)})
                WHERE labels @> ARRAY['{label}']::text[]
            """
            try:
                with self._cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        """
                        INSERT INTO graph_schema_objects (
                            graph_id, kind, name, label_name, properties, object_type, state, created_at
                        )
                        VALUES (%s, 'index', %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (graph_id, kind, name) DO UPDATE
                        SET label_name = EXCLUDED.label_name,
                            properties = EXCLUDED.properties,
                            object_type = EXCLUDED.object_type,
                            state = EXCLUDED.state
                        """,
                        (graph_id, name, label, Json(properties), index_type, "online", now_iso()),
                    )
            except Exception as exc:
                self._conn.rollback()
                raise ConflictError(f"Failed to create index '{name}': {exc}") from exc

            created.append(
                {
                    "name": name,
                    "label": label,
                    "properties": properties,
                    "type": index_type,
                    "state": "online",
                }
            )
        return created

    # ------------------------------------------------------------------
    # Nodes / relationships
    # ------------------------------------------------------------------

    def upsert_nodes(self, graph_id: str, nodes: list[dict]) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        results = []
        for node in nodes:
            node_id = str(node["id"])
            labels = self._normalize_labels(node.get("labels") or [])
            properties = dict(node.get("properties") or {})
            timestamp = now_iso()

            with self._cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT node_id, labels, properties
                    FROM graph_nodes
                    WHERE graph_id = %s AND node_id = %s
                    """,
                    (graph_id, node_id),
                )
                existing = cur.fetchone()

                if existing:
                    merged_labels = list(dict.fromkeys(list(existing["labels"]) + labels))
                    merged_properties = dict(existing["properties"] or {})
                    merged_properties.update(properties)
                    merged_properties["id"] = node_id
                    cur.execute(
                        """
                        UPDATE graph_nodes
                        SET labels = %s,
                            properties = %s,
                            updated_at = %s
                        WHERE graph_id = %s AND node_id = %s
                        """,
                        (merged_labels, Json(merged_properties), timestamp, graph_id, node_id),
                    )
                    created = False
                else:
                    payload = dict(properties)
                    payload["id"] = node_id
                    cur.execute(
                        """
                        INSERT INTO graph_nodes (
                            graph_id, node_id, labels, properties, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (graph_id, node_id, labels, Json(payload), timestamp, timestamp),
                    )
                    created = True

            results.append(
                {
                    "id": node_id,
                    "node_created": created,
                    "node_updated": not created,
                    "properties_set": len(properties),
                }
            )
        return results

    def upsert_relationships(
        self,
        graph_id: str,
        relationships: list[dict],
        upsert_missing_nodes: bool,
    ) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        results = []

        for rel in relationships:
            rel_type = sanitize_ident(rel["type"], "relationship type")
            start = rel["start_node"]
            end = rel["end_node"]
            start_label = sanitize_ident(start["label"], "label")
            end_label = sanitize_ident(end["label"], "label")
            rel_properties = dict(rel.get("properties") or {})

            start_node = self._resolve_node(graph_id, start_label, start["key"])
            end_node = self._resolve_node(graph_id, end_label, end["key"])

            nodes_created = 0
            if not start_node and upsert_missing_nodes:
                start_id = self._create_missing_node(graph_id, start_label, start["key"])
                start_node = {"node_id": start_id}
                nodes_created += 1
            if not end_node and upsert_missing_nodes:
                end_id = self._create_missing_node(graph_id, end_label, end["key"])
                end_node = {"node_id": end_id}
                nodes_created += 1

            if not start_node or not end_node:
                raise NodeReferenceError(
                    f"Start or end node not found for relationship {rel_type} "
                    f"({start_label} -> {end_label}); set upsert_missing_nodes=true to auto-create."
                )

            timestamp = now_iso()
            with self._cursor() as cur:
                cur.execute(
                    """
                    SELECT properties
                    FROM graph_relationships
                    WHERE graph_id = %s
                      AND rel_type = %s
                      AND start_node_id = %s
                      AND end_node_id = %s
                    """,
                    (graph_id, rel_type, start_node["node_id"], end_node["node_id"]),
                )
                existing = cur.fetchone()
                if existing:
                    merged = dict(existing[0] or {})
                    merged.update(rel_properties)
                    cur.execute(
                        """
                        UPDATE graph_relationships
                        SET properties = %s,
                            updated_at = %s
                        WHERE graph_id = %s
                          AND rel_type = %s
                          AND start_node_id = %s
                          AND end_node_id = %s
                        """,
                        (
                            Json(merged),
                            timestamp,
                            graph_id,
                            rel_type,
                            start_node["node_id"],
                            end_node["node_id"],
                        ),
                    )
                    rel_created = False
                else:
                    cur.execute(
                        """
                        INSERT INTO graph_relationships (
                            graph_id, rel_type, start_node_id, end_node_id, properties, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            graph_id,
                            rel_type,
                            start_node["node_id"],
                            end_node["node_id"],
                            Json(rel_properties),
                            timestamp,
                            timestamp,
                        ),
                    )
                    rel_created = True

            results.append(
                {
                    "relationship_created": rel_created,
                    "relationship_updated": not rel_created,
                    "nodes_created": nodes_created,
                }
            )

        return results

    # ------------------------------------------------------------------
    # Query / write
    # ------------------------------------------------------------------

    def execute_query(
        self,
        graph_id: str,
        name: Optional[str],
        query: Optional[str],
        parameters: Optional[dict],
    ) -> dict:
        self._ensure_graph_exists(graph_id)
        params = dict(parameters or {})
        params.setdefault("graph_id", graph_id)

        if name:
            sql = NAMED_QUERIES.get(name)
            if not sql:
                raise BadRequestError(f"Unknown named query: {name}")
        elif query:
            sql = query
        else:
            raise BadRequestError("Provide either 'name' or 'query'.")

        try:
            with self._cursor(dict_rows=True) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                columns = list(rows[0].keys()) if rows else [desc.name for desc in (cur.description or [])]
        except Exception as exc:
            self._conn.rollback()
            raise BadRequestError(f"Failed to execute SQL query: {exc}") from exc

        return {"columns": columns, "results": [dict(row) for row in rows]}

    def execute_write(self, graph_id: str, query: str, parameters: Optional[dict]) -> dict:
        self._ensure_graph_exists(graph_id)
        params = dict(parameters or {})
        params.setdefault("graph_id", graph_id)

        try:
            with self._cursor(dict_rows=True) as cur:
                cur.execute(query, params)
                if cur.description:
                    rows = cur.fetchall()
                    columns = list(rows[0].keys()) if rows else [desc.name for desc in cur.description]
                    results = [dict(row) for row in rows]
                else:
                    columns = []
                    results = []
                rowcount = cur.rowcount if cur.rowcount != -1 else 0
        except Exception as exc:
            self._conn.rollback()
            raise BadRequestError(f"Failed to execute SQL write: {exc}") from exc

        stats = {
            "rows_affected": rowcount,
            "nodes_created": 0,
            "nodes_deleted": 0,
            "relationships_created": 0,
            "relationships_deleted": 0,
            "properties_set": 0,
            "labels_added": 0,
        }
        return {"results": results, "stats": stats, "columns": columns}

    # ------------------------------------------------------------------
    # Stats / health / schema context
    # ------------------------------------------------------------------

    def get_stats(self, graph_id: str) -> dict:
        self._ensure_graph_exists(graph_id)
        with self._cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT count(*) AS c FROM graph_nodes WHERE graph_id = %s",
                (graph_id,),
            )
            node_count = cur.fetchone()["c"]

            cur.execute(
                "SELECT count(*) AS c FROM graph_relationships WHERE graph_id = %s",
                (graph_id,),
            )
            relationship_count = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT label, count(*) AS c
                FROM (
                    SELECT unnest(labels) AS label
                    FROM graph_nodes
                    WHERE graph_id = %s
                ) x
                GROUP BY label
                ORDER BY label
                """,
                (graph_id,),
            )
            label_counts = {row["label"]: row["c"] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT rel_type, count(*) AS c
                FROM graph_relationships
                WHERE graph_id = %s
                GROUP BY rel_type
                ORDER BY rel_type
                """,
                (graph_id,),
            )
            relationship_type_counts = {row["rel_type"]: row["c"] for row in cur.fetchall()}

            cur.execute(
                """
                SELECT kind, count(*) AS c
                FROM graph_schema_objects
                WHERE graph_id = %s
                GROUP BY kind
                """,
                (graph_id,),
            )
            meta = {row["kind"]: row["c"] for row in cur.fetchall()}

        return {
            "node_count": node_count,
            "relationship_count": relationship_count,
            "label_counts": label_counts,
            "relationship_type_counts": relationship_type_counts,
            "constraint_count": meta.get("constraint", 0),
            "index_count": meta.get("index", 0),
            "generated_at": now_iso(),
        }

    def check_health(self, graph_id: str) -> dict:
        self._ensure_graph_exists(graph_id)
        start = time.perf_counter()
        try:
            with self._cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {"status": "healthy", "latency_ms": latency_ms, "checked_at": now_iso()}
        except Exception as exc:
            return {"status": "unhealthy", "error": str(exc), "checked_at": now_iso()}

    def get_schema_context(self, graph_id: str) -> str:
        self._ensure_graph_exists(graph_id)
        schema = self.get_schema(graph_id)
        stats = self.get_stats(graph_id)

        lines: list[str] = []
        lines.append("Target dialect: PostgreSQL SQL")
        lines.append("Storage model:")
        lines.append("  - graph_nodes(graph_id, node_id, labels text[], properties jsonb)")
        lines.append("  - graph_relationships(graph_id, rel_type, start_node_id, end_node_id, properties jsonb)")
        lines.append("  - graph_metadata(graph_id, name, description, status, created_at)")
        lines.append("SQL rules:")
        lines.append("  - Always scope queries with WHERE graph_id = %(graph_id)s")
        lines.append("  - Node labels are stored in labels, use 'LabelName' = ANY(labels)")
        lines.append("  - Node id is stored in node_id")
        lines.append("  - Domain properties are stored in properties jsonb")
        lines.append("  - Read text properties with properties->>'property_name'")
        lines.append("  - Filter jsonb fields with properties @> '{\"key\":\"value\"}'::jsonb when appropriate")

        lines.append("Node labels (count):")
        if stats["label_counts"]:
            for label, count in stats["label_counts"].items():
                lines.append(f"  - {label} ({count})")
        else:
            lines.append("  (none yet)")

        lines.append("Relationship types (count):")
        if stats["relationship_type_counts"]:
            for rel_type, count in stats["relationship_type_counts"].items():
                lines.append(f"  - {rel_type} ({count})")
        else:
            lines.append("  (none yet)")

        if schema["constraints"]:
            lines.append("Unique constraints:")
            for item in schema["constraints"]:
                lines.append(f"  - {item['label']}.{','.join(item['properties'])}")

        if schema["indexes"]:
            lines.append("Indexes:")
            for item in schema["indexes"]:
                lines.append(f"  - {item['label']}.{','.join(item['properties'])} ({item['type']})")

        return "\n".join(lines)

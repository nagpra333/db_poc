import psycopg2
from graph_adapter import GraphAdapter
from typing import Optional
from graph_adapter import new_id, now_iso
import json
import re
import time
import ast
_now_iso = now_iso
NAMED_QUERIES = {
    "rules_enforced_by_program": """
        MATCH (br:BusinessRule:{graph_label})-[:ENFORCED_BY]->(p:Program:{graph_label} {name: $program_name})
        RETURN br.rule_id AS rule_id, br.name AS rule_name
    """,
    "programs_called_by": """
        MATCH (p:Program:{graph_label} {name: $program_name})-[:CALLS]->(callee:Program:{graph_label})
        RETURN callee.program_id AS program_id,
               callee.name AS name
    """,
}
class ApacheAgeGraphService(GraphAdapter):
    query_language = "cypher"
    def __init__(
        self,
        host: str,
        port: int,
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

        self._cursor = self._conn.cursor()

        # Load Apache AGE
        self._cursor.execute("LOAD 'age';")
        self._cursor.execute(
            "SET search_path = ag_catalog, '$user', public;"
        )
        self._cursor.execute("""
        CREATE TABLE IF NOT EXISTS graph_metadata (
            graph_id VARCHAR(36) PRIMARY KEY,
            graph_name VARCHAR(255) UNIQUE NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            status VARCHAR(20),
            created_at TIMESTAMP
        )
        """)

    def close(self) -> None:
        try:
            if self._cursor:
                self._cursor.close()
        finally:
            if self._conn:
                self._conn.close()

    def _parse_agtype(self, value):

        if value is None:
            return None

        text = str(value)

        # Remove AGE suffix
        text = re.sub(r"::(vertex|edge|path)$", "", text)

        try:
            return json.loads(text)
        except Exception:
            return text

    def _format_cypher_value(self, value):
        if value is None:
            return "null"

        if isinstance(value, bool):
            return "true" if value else "false"

        if isinstance(value, (int, float)):
            return str(value)

        if isinstance(value, (dict, list)):
            return json.dumps(value)

        return "'" + str(value).replace("'", "\\'") + "'"

    def _bind_parameters(self, cypher: str, parameters: dict) -> str:
        for key, value in parameters.items():
            cypher = cypher.replace(
                f"${key}",
                self._format_cypher_value(value)
            )

        return cypher

    def _build_return_columns(self, cypher: str) -> str:

        match = re.search(
            r"RETURN\s+(.+?)(?:\s+ORDER\s+BY|\s+LIMIT|\s+SKIP|$)",
            cypher,
            re.IGNORECASE | re.DOTALL,
        )

        if not match:
            return "result agtype"

        return_clause = match.group(1).strip()

        columns = []

        for item in return_clause.split(","):

            item = item.strip()

            #
            # Handle aliases
            #
            alias = re.search(r"\s+AS\s+(\w+)$", item, re.IGNORECASE)

            if alias:
                name = alias.group(1)
            else:
                #
                # RETURN n
                # RETURN r
                # RETURN count(n)
                #
                item = item.split(".")[0]

                if "(" in item:
                    name = "result"
                else:
                    name = item

            columns.append(f"{name} agtype")

        return ", ".join(columns)

    def _submit(self, graph_name: str, cypher: str):

        column_definition = self._build_return_columns(cypher)

        sql = f"""
        SELECT *
        FROM cypher(
            '{graph_name}',
            $$
            {cypher}
            $$
        ) AS ({column_definition});
        """

        print("\n========== EXECUTING CYPHER ==========")
        print(sql)
        print("======================================\n")

        self._cursor.execute(sql)

        return self._cursor.fetchall()

    def create_graph(self, name: str, description: Optional[str]) -> dict:
        graph_id = new_id()
        graph_name = f"Graph_{graph_id.replace('-', '')}"
        created_at = now_iso()

        try:
            # Create Apache AGE graph
            self._cursor.execute(
                "SELECT create_graph(%s);",
                (graph_name,),
            )

            # Store metadata
            self._cursor.execute(
                """
                INSERT INTO graph_metadata
                (
                    graph_id,
                    graph_name,
                    name,
                    description,
                    status,
                    created_at
                )
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    graph_id,
                    graph_name,
                    name,
                    description,
                    "ready",
                    created_at,
                ),
            )

            return {
                "id": graph_id,
                "name": name,
                "description": description,
                "status": "ready",
                "created_at": created_at,
            }

        except Exception as e:
            self._conn.rollback()
            raise RuntimeError(f"Failed to create graph: {e}")

    def get_graph(self, graph_id: str) -> dict:
        self._cursor.execute(
            """
            SELECT
                graph_id,
                graph_name,
                name,
                description,
                status,
                created_at
            FROM graph_metadata
            WHERE graph_id = %s
            """,
            (graph_id,),
        )

        row = self._cursor.fetchone()

        if row is None:
            raise ValueError(f"Graph '{graph_id}' not found")

        return {
            "id": row[0],
            "name": row[2],
            "description": row[3],
            "status": row[4],
            "created_at": row[5].isoformat() if row[5] else None,
        }

    def _get_graph_name(self, graph_id: str) -> str:
        self._cursor.execute(
            """
            SELECT graph_name
            FROM graph_metadata
            WHERE graph_id = %s
            """,
            (graph_id,),
        )

        row = self._cursor.fetchone()

        if row is None:
            raise ValueError(f"Graph '{graph_id}' not found")

        return row[0]


    def get_schema(self, graph_id: str) -> dict:

        graph_name = self._get_graph_name(graph_id)

        #
        # Labels & Relationship Types
        #
        self._cursor.execute(
            """
            SELECT name, kind
            FROM ag_catalog.ag_label
            WHERE graph = (
                SELECT graphid
                FROM ag_catalog.ag_graph
                WHERE name = %s
            )
            ORDER BY id
            """,
            (graph_name,),
        )

        labels = []
        relationship_types = []

        for name, kind in self._cursor.fetchall():

            if name.startswith("_ag_"):
                continue

            if kind == "v":
                labels.append(name)

            elif kind == "e":
                relationship_types.append(name)

        #
        # Indexes
        #
        self._cursor.execute(
            """
            SELECT
                tablename,
                indexname,
                indexdef
            FROM pg_indexes
            WHERE schemaname = %s
            ORDER BY tablename,indexname
            """,
            (graph_name,),
        )

        indexes = []

        for table, index_name, definition in self._cursor.fetchall():

            #
            # Skip PostgreSQL / Apache AGE internal indexes
            #
            if (
                index_name.startswith("_ag_")
                or index_name.endswith("_pkey")
                or index_name.endswith("_start_id_idx")
                or index_name.endswith("_end_id_idx")
            ):
                continue

            #
            # Determine type
            #
            index_type = "unique" if "UNIQUE INDEX" in definition.upper() else "btree"

            #
            # Extract indexed properties
            #
            properties = []

            matches = re.findall(
                r'agtype_access_operator\(.*?\'\\"([^"]+)\\"\'::agtype',
                definition,
            )

            if matches:
                properties = matches

            indexes.append(
                {
                    "name": index_name,
                    "label": table,
                    "properties": properties,
                    "type": index_type,
                    "state": "online",
                }
            )

        #
        # Constraints
        #
        self._cursor.execute(
            """
            SELECT
                c.conname,
                t.relname,
                pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_class t
                ON c.conrelid = t.oid
            JOIN pg_namespace n
                ON n.oid = t.relnamespace
            WHERE n.nspname = %s
            ORDER BY t.relname
            """,
            (graph_name,),
        )

        constraints = []

        for name, table, definition in self._cursor.fetchall():

            #
            # Skip PostgreSQL / AGE internal constraints
            #
            if (
                name.startswith("_ag_")
                or name.endswith("_pkey")
            ):
                continue

            constraints.append(
                {
                    "name": name,
                    "label": table,
                    "definition": definition,
                }
            )

        #
        # Property Keys
        #
        property_keys = {}

        for table in labels + relationship_types:

            keys = set()

            try:

                self._cursor.execute(
                    f'''
                    SELECT properties
                    FROM "{graph_name}"."{table}"
                    '''
                )

                rows = self._cursor.fetchall()

                for row in rows:

                    props = row[0]

                    if props is None:
                        continue

                    text = str(props)

                    try:
                        data = ast.literal_eval(text)

                        if isinstance(data, dict):
                            keys.update(data.keys())

                    except Exception:
                        pass

            except Exception:
                pass

            property_keys[table] = sorted(keys)

        return {
            "labels": sorted(labels),
            "relationship_types": sorted(relationship_types),
            "indexes": indexes,
            "constraints": constraints,
            "property_keys": property_keys,
        }

    def create_constraints(
    self,
    graph_id: str,
    constraints: list[dict],
) -> list[dict]:

        graph_name = self._get_graph_name(graph_id)

        results = []

        for constraint in constraints:

            label = constraint["label"]
            props = constraint["properties"]

            name = constraint.get("name") or (
                f"uq_{label.lower()}_" +
                "_".join(props)
            )

            expressions = []

            for prop in props:
                expressions.append(
                    f"""agtype_access_operator(properties, '"{prop}"'::agtype)"""
                )

            sql = f'''
            CREATE UNIQUE INDEX IF NOT EXISTS "{name}"
            ON "{graph_name}"."{label}"
            ({", ".join(expressions)});
            '''

            try:

                self._cursor.execute(sql)
                self._conn.commit()

                results.append(
                    {
                        "name": name,
                        "label": label,
                        "properties": props,
                        "type": "UNIQUE",
                        "state": "online",
                    }
                )

            except Exception as e:
                self._conn.rollback()
                raise RuntimeError(f"Failed to create constraint: {e}")

        return results

    def create_indexes(
    self,
    graph_id: str,
    indexes: list[dict],
) -> list[dict]:

        graph_name = self._get_graph_name(graph_id)

        results = []

        for idx in indexes:

            label = idx["label"]
            props = idx["properties"]

            idx_type = idx.get("type", "btree")

            name = idx.get("name") or (
                f"idx_{label.lower()}_" +
                "_".join(p.lower() for p in props)
            )

            expressions = []

            for prop in props:
                expressions.append(
                    f"""agtype_access_operator(properties, '"{prop}"'::agtype)"""
                )

            sql = f'''
            CREATE INDEX IF NOT EXISTS "{name}"
            ON "{graph_name}"."{label}"
            ({", ".join(expressions)});
            '''

            try:

                self._cursor.execute(sql)
                self._conn.commit()

                results.append(
                    {
                        "name": name,
                        "label": label,
                        "properties": props,
                        "type": idx_type,
                        "state": "online",
                    }
                )

            except Exception as e:
                self._conn.rollback()
                raise RuntimeError(f"Failed to create index: {e}")

        return results

    def upsert_nodes(self, graph_id: str, nodes: list[dict]) -> list[dict]:

        results = []

        for node in nodes:

            node_id = node["id"]

            labels = node.get("labels", [])
            properties = dict(node.get("properties", {}) or {})

            properties["id"] = node_id

            label_string = ":".join(labels)

            assignments = []

            for key in properties.keys():
                assignments.append(f"n.{key} = ${key}")

            set_clause = ", ".join(assignments)

            cypher = f"""
            MERGE (n:{label_string} {{id: $id}})
            SET {set_clause}
            RETURN n
            """

            self.execute_write(
                graph_id,
                cypher,
                properties,
            )


            results.append(
                {
                    "id": node_id,
                    "node_created": True,
                    "node_updated": False,
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

        results = []

        for rel in relationships:

            rel_type = rel["type"]

            start = rel["start_node"]
            end = rel["end_node"]

            start_label = start["label"]
            end_label = end["label"]

            start_key = start["key"]
            end_key = end["key"]

            rel_properties = dict(rel.get("properties", {}) or {})

            params = {}

            #
            # Build MATCH or MERGE for start node
            #
            if upsert_missing_nodes:

                start_clause = f"""
                MERGE (s:{start_label} {{id: $start_id}})
                """

                end_clause = f"""
                MERGE (e:{end_label} {{id: $end_id}})
                """

                params["start_id"] = start_key["id"]
                params["end_id"] = end_key["id"]

            else:

                start_clause = f"""
                MATCH (s:{start_label} {{id: $start_id}})
                """

                end_clause = f"""
                MATCH (e:{end_label} {{id: $end_id}})
                """

                params["start_id"] = start_key["id"]
                params["end_id"] = end_key["id"]

            #
            # Relationship properties
            #
            assignments = []

            for key, value in rel_properties.items():
                params[key] = value
                assignments.append(f"r.{key} = ${key}")

            set_clause = ""

            if assignments:
                set_clause = "SET " + ", ".join(assignments)

            cypher = f"""
            {start_clause}

            {end_clause}

            MERGE (s)-[r:{rel_type}]->(e)

            {set_clause}

            RETURN r
            """

            self.execute_write(
                graph_id,
                cypher,
                params,
            )

            results.append(
                {
                    "relationship_created": True,
                    "relationship_updated": False,
                    "nodes_created": 0 if not upsert_missing_nodes else 2,
                }
            )

        return results

    def execute_query(
    self,
    graph_id: str,
    name: Optional[str],
    query: Optional[str],
    parameters: Optional[dict],
) -> dict:

        graph_name = self._get_graph_name(graph_id)

        params = dict(parameters or {})

        if name:
            template = NAMED_QUERIES.get(name)
            if not template:
                raise ValueError(f"Unknown named query: {name}")

            # Apache AGE doesn't use graph labels
            cypher = (
                template
                .replace(":{graph_label}", "")
                .replace("{graph_label}", "")
            )

        elif query:
            cypher = query
            params.setdefault("graph_id", graph_id)

        else:
            raise ValueError("Provide either 'name' or 'query'.")

        # Replace $parameter placeholders
        cypher = self._bind_parameters(cypher, params)

        try:

            sql = f"""
            SELECT *
            FROM cypher(
                '{graph_name}',
                $$
                {cypher}
                $$
            ) AS (result agtype);
            """

            print("\n========== EXECUTING CYPHER ==========")
            print(sql)
            print("======================================\n")

            rows = self._submit(graph_name, cypher)
            columns = ["result"]

            results = []

            for row in rows:
                results.append(
                    {
                        self._parse_agtype(row[0])
                    }
                )

            return {
                "columns": columns,
                "results": results,
            }

        except Exception as e:
            self._conn.rollback()
            raise RuntimeError(f"Failed to execute query: {e}")

    def execute_write(
    self,
    graph_id: str,
    query: str,
    parameters: Optional[dict],
) -> dict:

        graph_name = self._get_graph_name(graph_id)

        params = dict(parameters or {})
        params.setdefault("graph_id", graph_id)

        # Replace $parameter placeholders
        cypher = self._bind_parameters(query, params)

        try:

            sql = f"""
            SELECT *
            FROM cypher(
                '{graph_name}',
                $$
                {cypher}
                $$
            ) AS (result agtype);
            """

            print("\n========== EXECUTING WRITE ==========")
            print(sql)
            print("=====================================\n")

            rows = self._submit(graph_name, cypher)
            self._conn.commit()
            results = []

            for row in rows:
                results.append({"result": self._parse_agtype(row[0])})

            #
            # Apache AGE doesn't expose Neo4j-like counters.
            # Return the same response structure with placeholder stats.
            #
            stats = {
                "nodes_created": 0,
                "nodes_deleted": 0,
                "relationships_created": 0,
                "relationships_deleted": 0,
                "properties_set": 0,
                "labels_added": 0,
            }

            return {
                "results": results,
                "stats": stats,
            }

        except Exception as e:
            self._conn.rollback()
            raise RuntimeError(f"Failed to execute write: {e}")

    def get_stats(self, graph_id: str) -> dict:

        graph_name = self._get_graph_name(graph_id)

        try:

            rows = self._submit(
                graph_name,
                """
                MATCH (n)
                RETURN count(n)
                """
            )

            node_count = int(str(rows[0][0]).strip('"')) if rows else 0

            rows = self._submit(
                graph_name,
                """
                MATCH ()-[r]->()
                RETURN count(r)
                """
            )

            relationship_count = int(str(rows[0][0]).strip('"')) if rows else 0

            return {
                "node_count": node_count,
                "relationship_count": relationship_count,
            }

        except Exception as e:
            raise RuntimeError(f"Failed to get stats: {e}")

    def check_health(self, graph_id: str) -> dict:
        # Ensure the graph exists (returns 404 if it doesn't)
        self._get_graph_name(graph_id)

        start = time.perf_counter()

        try:
            self._cursor.execute("SELECT 1;")
            self._cursor.fetchone()

            latency_ms = int((time.perf_counter() - start) * 1000)

            return {
                "status": "healthy",
                "latency_ms": latency_ms,
                "checked_at": _now_iso(),
            }

        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "checked_at": _now_iso(),
            }

    def get_schema_context(self, graph_id: str) -> str:

        schema = self.get_schema(graph_id)

        lines = []

        labels = schema.get("labels", [])
        relationship_types = schema.get("relationship_types", [])

        lines.append("Node Labels:")

        if labels:
            for label in labels:
                lines.append(f"  - {label}")
        else:
            lines.append("  None")

        lines.append("")
        lines.append("Relationship Types:")

        if relationship_types:
            for rel in relationship_types:
                lines.append(f"  - {rel}")
        else:
            lines.append("  None")

        return "\n".join(lines)
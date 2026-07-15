"""
gremlin_ops.py

Gremlin implementation of GraphAdapter (see graph_adapter.py), targeting
Azure Cosmos DB's Gremlin API. Selected when DB_TYPE=gremlin.

Connection pattern follows the standard Cosmos Gremlin client usage:
    client.Client(f"wss://{host}:443/", "g", username=f"/dbs/{db}/colls/{graph}",
                  password=key, message_serializer=serializer.GraphSONSerializersV2d0())

Design notes / simplifications:
- A "graph" from the API spec is a logical namespace inside the single
  configured Cosmos Gremlin collection (GREMLIN_DATABASE/GREMLIN_GRAPH), not
  a separate physical collection. Every vertex created through /nodes or
  /relationships gets a scoping property (name configurable, see below) so
  it can be scoped back to its owning graph -- same idea as the
  `Graph_<uuid>` label used in the Neo4j adapter, just expressed as a
  property instead of a label (Gremlin vertices in Cosmos have exactly one
  label).
- IMPORTANT -- partition key: Cosmos DB requires every vertex to carry the
  container's actual partition key property, or writes fail with
  "Cannot add a vertex where the partition key property has value 'null'."
  This adapter reuses its graph-scoping property AS the partition key, so
  the scoping property name MUST match your container's real partition key
  path (check Azure Portal -> your Cosmos account -> Data Explorer -> your
  graph -> Scale & Settings -> Partition Key, e.g. "/pk" or "/tenantId").
  Set it via the `partition_key` constructor arg / GREMLIN_PARTITION_KEY env
  var -- it defaults to "graph_id", which only works if your container was
  created with that exact partition key path.
- IMPORTANT -- reserved property names: Cosmos DB stores vertices/edges as
  documents with reserved top-level fields "id", "label", and "type"
  ("vertex"/"edge"). Setting a regular Gremlin *property* with one of these
  names collides with the reserved field and can silently corrupt the
  document (it stops being recognized as a valid vertex by later
  hasLabel()/has() queries). This adapter hit it twice on its own
  schema-metadata vertices during development -- once storing a constraint's
  "type" (renamed to 'schema_type'), once storing a constraint's target
  "label" (renamed to 'target_label'; setting a *property* called 'label'
  after addV() silently overwrote the vertex's real element label, so
  GET /schema's hasLabel('__SchemaObject__') query found nothing even
  though the vertices existed, now mislabeled as e.g. "Program"). Both are
  fixed. `_check_prop_name` also rejects "label"/"type" and the configured
  partition key as regular property names on /nodes and /relationships
  payloads, with a 400 explaining why. "id" is similarly reserved when used
  as an arbitrary extra property, but is explicitly allowed (via
  `_check_key_prop_name`) as a relationship endpoint's match/create key
  (`{"key": {"id": "..."}}`) -- that's the documented way to reference a
  vertex by id, and unlike label/type it's safe to set via .property() on a
  freshly-created vertex since addV() doesn't implicitly set it for you.
- A node's `labels` list maps to a single Gremlin vertex label
  (`labels[0]`); any additional labels are recorded on an `extra_labels`
  property (comma-separated) since Cosmos vertices don't support multiple
  labels the way Neo4j nodes do.
- Cosmos DB Gremlin has no user-managed unique constraints and indexes all
  properties automatically. `create_constraints` therefore records intent as
  metadata only (visible via GET /schema) but does NOT enforce uniqueness --
  enforce it yourself by using that property as the node's `id`, since `id`
  is the only property this adapter treats as a de-duplication key.
  `create_indexes` is a metadata-only no-op for the same reason: there is
  nothing to create, Cosmos already indexes everything.
- Node/relationship upserts here are multi-step (find-or-create vertex,
  find-or-create vertex, merge edge) rather than the single atomic
  statement Cypher's MERGE gives us in the Neo4j adapter. This is a known
  tradeoff of the Gremlin/Cosmos path -- fine for typical usage, but not
  transactionally atomic across steps.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from gremlin_python.driver import client, serializer
from gremlin_python.structure.graph import Edge, Vertex

from graph_adapter import (
    BadRequestError,
    ConflictError,
    GraphAdapter,
    GraphNotFoundError,
    NodeReferenceError,
    is_valid_ident,
    new_id,
    now_iso,
    sanitize_ident,
)


def _split_props(value: Optional[str]) -> list[str]:
    return [p for p in (value or "").split(",") if p]


class GremlinGraphService(GraphAdapter):
    query_language = "gremlin"

    def __init__(self, host: str, database: str, graph: str, key: str, partition_key: str = "graph_id"):
        # This MUST match your Cosmos container's actual partition key
        # property name -- see the module docstring above.
        self._pk = sanitize_ident(partition_key, "partition key")

        username = f"/dbs/{database}/colls/{graph}"
        self._client = client.Client(
            f"wss://{host}:443/",
            "g",
            username=username,
            password=key,
            message_serializer=serializer.GraphSONSerializersV2d0(),
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _submit(self, script: str, bindings: Optional[dict] = None) -> list:
        return self._client.submit(script, bindings or {}).all().result()

    # ------------------------------------------------------------------
    # Named (server-defined) read queries -- referenced via {"name": "..."}
    # in POST /graphs/{id}/query. Parallel to NAMED_QUERIES in neo4j_ops.py,
    # written in Gremlin instead of Cypher. Built per-instance (not a module
    # constant) because they embed self._pk. `graph_id` is always available
    # as a binding.
    # ------------------------------------------------------------------

    def _named_queries(self) -> dict[str, str]:
        pk = self._pk
        return {
            "rules_enforced_by_program": (
                f"g.V().has('{pk}', graph_id).hasLabel('BusinessRule')"
                ".where(__.outE('ENFORCED_BY').inV().has('name', program_name))"
                ".project('rule_id', 'rule_name').by('rule_id').by('name')"
            ),
            "programs_called_by": (
                f"g.V().has('{pk}', graph_id).hasLabel('Program').has('name', program_name)"
                ".out('CALLS').project('program_id', 'name').by('program_id').by('name')"
            ),
        }

    # ------------------------------------------------------------------
    # Helpers
    #
    # NOTE: unlike a stock TinkerPop server, Cosmos DB's Gremlin API returns
    # vertices/edges as plain untyped JSON maps (e.g.
    # {"id": ..., "label": ..., "type": "vertex", "properties": {...}})
    # rather than gremlin_python Vertex/Edge objects. Every helper below
    # accepts either shape so this adapter works against both a real
    # TinkerPop/Gremlin Server and Cosmos DB.
    # ------------------------------------------------------------------

    @staticmethod
    def _get_id(item: Any) -> Any:
        if isinstance(item, dict):
            return item.get("id")
        return getattr(item, "id", item)

    @staticmethod
    def _get_label(item: Any) -> Any:
        if isinstance(item, dict):
            return item.get("label")
        return getattr(item, "label", None)

    @staticmethod
    def _flatten_props(props: dict) -> dict:
        flat: dict[str, Any] = {}
        for k, vals in (props or {}).items():
            if isinstance(vals, list) and vals:
                first = vals[0]
                if isinstance(first, dict):
                    flat[k] = first.get("value", first)
                else:
                    flat[k] = getattr(first, "value", first)
            else:
                flat[k] = vals
        return flat

    @staticmethod
    def _vertex_to_dict(v: Any) -> dict:
        d: dict[str, Any] = {"id": GremlinGraphService._get_id(v), "label": GremlinGraphService._get_label(v)}
        if isinstance(v, dict):
            props = v.get("properties") or {}
        else:
            props = getattr(v, "properties", None) or {}
        d.update(GremlinGraphService._flatten_props(props))
        return d

    @staticmethod
    def _result_to_dict(item: Any) -> dict:
        if isinstance(item, Vertex):
            return GremlinGraphService._vertex_to_dict(item)
        if isinstance(item, Edge):
            return {
                "id": item.id,
                "label": item.label,
                "outV": GremlinGraphService._get_id(item.outV),
                "inV": GremlinGraphService._get_id(item.inV),
            }
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "vertex":
                return GremlinGraphService._vertex_to_dict(item)
            if item_type == "edge":
                return {
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "outV": item.get("outV") or item.get("outVLabel"),
                    "inV": item.get("inV") or item.get("inVLabel"),
                }
            # Plain result map, e.g. from project()/valueMap()/group() -- pass through as-is.
            return item
        return {"value": item}

    # Cosmos DB stores vertices/edges as documents with reserved top-level
    # fields "id", "label", and "type" ("vertex"/"edge"). Setting a regular
    # Gremlin *property* with one of these names (e.g. .property('type', 'unique'))
    # collides with those reserved fields and can silently corrupt the
    # document -- it stops being recognized as a valid vertex by later
    # hasLabel()/has() queries. This bit us in our own schema-metadata
    # vertices (fixed by renaming to 'schema_type'); guard against callers
    # hitting the same trap with their own node/relationship properties.
    _RESERVED_PROPS = frozenset({"id", "label", "type"})

    def _check_prop_name(self, prop: str) -> None:
        if prop in self._RESERVED_PROPS or prop == self._pk:
            raise BadRequestError(
                f"Property name '{prop}' is reserved by Cosmos DB Gremlin (used internally "
                f"for the vertex/edge id, label, or document type, or configured as this "
                f"graph's partition key) and can't be set as a regular property -- choose a "
                f"different name."
            )

    def _check_key_prop_name(self, prop: str) -> None:
        """Like _check_prop_name, but exempts 'id': relationship endpoint
        keys like {"id": "PROGRAM::CUSTMGR"} are the documented, correct way
        to match/create a vertex by its id, and setting 'id' via .property()
        on a freshly-created vertex is safe on Cosmos -- unlike 'label' and
        'type', addV() doesn't implicitly set 'id' for you, so there's no
        collision to corrupt. 'label'/'type'/the partition key remain
        blocked here since using them as a match/create key would hit the
        same corruption this adapter already ran into once."""
        if prop == "id":
            return
        self._check_prop_name(prop)

    def _build_property_chain(self, properties: dict, bindings: dict, prefix: str) -> str:
        parts = []
        for i, (k, v) in enumerate(properties.items()):
            prop = sanitize_ident(k, "property")
            self._check_prop_name(prop)
            bind_name = f"{prefix}{i}"
            bindings[bind_name] = v
            parts.append(f".property('{prop}', {bind_name})")
        return "".join(parts)

    def _ensure_graph_exists(self, graph_id: str) -> None:
        result = self._submit(
            "g.V().hasLabel('__Graph__').has('id', gid).count()", {"gid": graph_id}
        )
        if not result or result[0] == 0:
            raise GraphNotFoundError(graph_id)

    def _resolve_or_create_vertex(
        self, graph_id: str, label: str, key: dict, upsert_missing: bool
    ) -> tuple[Any, bool]:
        label = sanitize_ident(label, "label")
        for k in key.keys():
            self._check_key_prop_name(sanitize_ident(k, "property"))

        bindings: dict[str, Any] = {"gid": graph_id}
        where_parts = []
        for i, (k, v) in enumerate(key.items()):
            prop = sanitize_ident(k, "property")
            bname = f"k{i}"
            bindings[bname] = v
            where_parts.append(f".has('{prop}', {bname})")

        find_script = f"g.V().has('{self._pk}', gid).hasLabel('{label}'){''.join(where_parts)}.limit(1)"
        found = self._submit(find_script, bindings)
        if found:
            return self._get_id(found[0]), False

        if not upsert_missing:
            raise NodeReferenceError(
                f"No {label} vertex found matching {key}; set upsert_missing_nodes=true to auto-create."
            )

        create_bindings: dict[str, Any] = {"gid": graph_id}
        create_parts = []
        for i, (k, v) in enumerate(key.items()):
            prop = sanitize_ident(k, "property")
            bname = f"c{i}"
            create_bindings[bname] = v
            create_parts.append(f".property('{prop}', {bname})")
        create_script = f"g.addV('{label}').property('{self._pk}', gid){''.join(create_parts)}"
        created = self._submit(create_script, create_bindings)
        return self._get_id(created[0]), True

    # ------------------------------------------------------------------
    # Graph lifecycle
    # ------------------------------------------------------------------

    def create_graph(self, name: str, description: Optional[str]) -> dict:
        existing = self._submit(
            "g.V().hasLabel('__Graph__').has('name', name_b)", {"name_b": name}
        )
        if existing:
            raise ConflictError(f"A graph named '{name}' already exists.")

        graph_id = new_id()
        created_at = now_iso()
        # The __Graph__ vertex is scoped to itself: its own id is also its
        # partition key value, so it satisfies Cosmos's partition-key
        # requirement without needing a separate parent graph.
        script = f"""
        g.addV('__Graph__')
         .property('id', gid)
         .property('{self._pk}', gid)
         .property('name', name_b)
         .property('description', desc_b)
         .property('status', 'ready')
         .property('created_at', created_b)
        """
        result = self._submit(
            script,
            {
                "gid": graph_id,
                "name_b": name,
                "desc_b": description or "",
                "created_b": created_at,
            },
        )
        return self._vertex_to_dict(result[0])

    def get_graph(self, graph_id: str) -> dict:
        result = self._submit(
            "g.V().hasLabel('__Graph__').has('id', gid)", {"gid": graph_id}
        )
        if not result:
            raise GraphNotFoundError(graph_id)
        return self._vertex_to_dict(result[0])

    # ------------------------------------------------------------------
    # Schema: constraints & indexes (metadata-only -- see module docstring)
    # ------------------------------------------------------------------

    def get_schema(self, graph_id: str) -> dict:
        self._ensure_graph_exists(graph_id)
        rows = self._submit(
            f"g.V().has('{self._pk}', gid).hasLabel('__SchemaObject__').valueMap()",
            {"gid": graph_id},
        )
        constraints, indexes = [], []
        for vm in rows:
            flat = {k: (v[0] if isinstance(v, list) and v else v) for k, v in vm.items()}
            props = _split_props(flat.get("properties"))
            if flat.get("kind") == "constraint":
                constraints.append(
                    {
                        "name": flat.get("name"),
                        "label": flat.get("target_label"),
                        "properties": props,
                        "type": "unique",
                        "enforced": False,
                        "note": "Cosmos DB Gremlin has no native unique constraints; "
                        "this is recorded intent only. Enforce uniqueness by using "
                        "this property as the node's 'id'.",
                    }
                )
            elif flat.get("kind") == "index":
                indexes.append(
                    {
                        "name": flat.get("name"),
                        "label": flat.get("target_label"),
                        "properties": props,
                        "type": flat.get("schema_type", "automatic"),
                        "state": "automatic",
                        "note": "Cosmos DB Gremlin indexes all properties automatically; "
                        "no index creation call was made.",
                    }
                )
        return {"constraints": constraints, "indexes": indexes}

    def create_constraints(self, graph_id: str, constraints: list[dict]) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        results = []
        for c in constraints:
            label = sanitize_ident(c["label"], "label")
            props = [sanitize_ident(p, "property") for p in c["properties"]]
            name = c.get("name") or f"unique_{label.lower()}_{'_'.join(p.lower() for p in props)}"
            name = sanitize_ident(name, "constraint name")

            existing = self._submit(
                f"g.V().has('{self._pk}', gid).hasLabel('__SchemaObject__')"
                ".has('kind', 'constraint').has('name', name_b).count()",
                {"gid": graph_id, "name_b": name},
            )
            if not existing or existing[0] == 0:
                self._submit(
                    "g.addV('__SchemaObject__')"
                    f".property('{self._pk}', gid).property('kind', 'constraint')"
                    ".property('name', name_b).property('target_label', label_b)"
                    ".property('properties', props_b).property('schema_type', 'unique')",
                    {
                        "gid": graph_id,
                        "name_b": name,
                        "label_b": label,
                        "props_b": ",".join(props),
                    },
                )
            results.append(
                {
                    "name": name,
                    "label": label,
                    "properties": props,
                    "type": "unique",
                    "enforced": False,
                    "note": "Metadata only -- not enforced by Cosmos DB Gremlin.",
                }
            )
        return results

    def create_indexes(self, graph_id: str, indexes: list[dict]) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        results = []
        for idx in indexes:
            label = sanitize_ident(idx["label"], "label")
            props = [sanitize_ident(p, "property") for p in idx["properties"]]
            idx_type = idx.get("type", "automatic")
            name = idx.get("name") or f"idx_{label.lower()}_{'_'.join(p.lower() for p in props)}"
            name = sanitize_ident(name, "index name")

            existing = self._submit(
                f"g.V().has('{self._pk}', gid).hasLabel('__SchemaObject__')"
                ".has('kind', 'index').has('name', name_b).count()",
                {"gid": graph_id, "name_b": name},
            )
            if not existing or existing[0] == 0:
                self._submit(
                    "g.addV('__SchemaObject__')"
                    f".property('{self._pk}', gid).property('kind', 'index')"
                    ".property('name', name_b).property('target_label', label_b)"
                    ".property('properties', props_b).property('schema_type', type_b)",
                    {
                        "gid": graph_id,
                        "name_b": name,
                        "label_b": label,
                        "props_b": ",".join(props),
                        "type_b": idx_type,
                    },
                )
            results.append(
                {
                    "name": name,
                    "label": label,
                    "properties": props,
                    "type": idx_type,
                    "state": "automatic",
                    "note": "Cosmos DB Gremlin indexes all properties automatically.",
                }
            )
        return results

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_nodes(self, graph_id: str, nodes: list[dict]) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        results = []
        for node in nodes:
            node_id = node["id"]
            labels = [sanitize_ident(l, "label") for l in node.get("labels", [])]
            vlabel = labels[0] if labels else "Node"
            properties = node.get("properties", {}) or {}

            bindings: dict[str, Any] = {"gid": graph_id, "nid": node_id}
            update_chain = self._build_property_chain(properties, bindings, "u")
            create_chain = self._build_property_chain(properties, bindings, "c")

            if len(labels) > 1:
                bindings["extra_labels"] = ",".join(labels[1:])
                update_chain += ".property('extra_labels', extra_labels)"
                create_chain += ".property('extra_labels', extra_labels)"

            script = f"""
            g.V().has('{self._pk}', gid).has('id', nid).fold()
             .coalesce(
                __.unfold(){update_chain}.constant('updated'),
                __.addV('{vlabel}').property('id', nid).property('{self._pk}', gid){create_chain}.constant('created')
             )
            """
            result = self._submit(script, bindings)
            outcome = result[0] if result else "updated"
            results.append(
                {
                    "id": node_id,
                    "node_created": outcome == "created",
                    "node_updated": outcome == "updated",
                    "properties_set": len(properties),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Relationships (edges)
    # ------------------------------------------------------------------

    def upsert_relationships(
        self, graph_id: str, relationships: list[dict], upsert_missing_nodes: bool
    ) -> list[dict]:
        self._ensure_graph_exists(graph_id)
        results = []
        for rel in relationships:
            rel_type = sanitize_ident(rel["type"], "relationship type")
            start = rel["start_node"]
            end = rel["end_node"]
            rel_props = rel.get("properties", {}) or {}

            s_id, s_created = self._resolve_or_create_vertex(
                graph_id, start["label"], start["key"], upsert_missing_nodes
            )
            e_id, e_created = self._resolve_or_create_vertex(
                graph_id, end["label"], end["key"], upsert_missing_nodes
            )

            bindings: dict[str, Any] = {"sid": s_id, "eid": e_id}
            update_chain = self._build_property_chain(rel_props, bindings, "u")
            create_chain = self._build_property_chain(rel_props, bindings, "c")

            script = f"""
            g.V(sid).coalesce(
              __.outE('{rel_type}').where(__.inV().hasId(eid)){update_chain}.constant('updated'),
              __.addE('{rel_type}').to(__.V(eid)){create_chain}.constant('created')
            )
            """
            result = self._submit(script, bindings)
            outcome = result[0] if result else "updated"
            results.append(
                {
                    "relationship_created": outcome == "created",
                    "relationship_updated": outcome == "updated",
                    "nodes_created": int(s_created) + int(e_created),
                }
            )
        return results

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
        self._ensure_graph_exists(graph_id)
        params = dict(parameters or {})
        params.setdefault("graph_id", graph_id)

        if name:
            template = self._named_queries().get(name)
            if not template:
                raise BadRequestError(f"Unknown named query: {name}")
            script = template
        elif query:
            script = query
        else:
            raise BadRequestError("Provide either 'name' or 'query'.")

        raw_results = self._submit(script, params)
        rows = [self._result_to_dict(r) for r in raw_results]
        columns = sorted({k for row in rows for k in row.keys()}) if rows else []
        return {"columns": columns, "results": rows}

    def execute_write(self, graph_id: str, query: str, parameters: Optional[dict]) -> dict:
        self._ensure_graph_exists(graph_id)
        params = dict(parameters or {})
        params.setdefault("graph_id", graph_id)

        raw_results = self._submit(query, params)
        rows = [self._result_to_dict(r) for r in raw_results]
        stats = {
            "results_returned": len(rows),
            "note": "The Gremlin backend does not expose per-mutation counters the way "
            "Neo4j's summary.counters does; only the result count is reported here.",
        }
        return {"results": rows, "stats": stats}

    # ------------------------------------------------------------------
    # Stats, health, LLM grounding
    # ------------------------------------------------------------------

    def get_stats(self, graph_id: str) -> dict:
        self._ensure_graph_exists(graph_id)

        node_count = self._submit(
            f"g.V().has('{self._pk}', gid).count()", {"gid": graph_id}
        )[0]
        rel_count = self._submit(
            f"g.V().has('{self._pk}', gid).outE().count()", {"gid": graph_id}
        )[0]

        label_rows = self._submit(
            f"g.V().has('{self._pk}', gid).groupCount().by(label)", {"gid": graph_id}
        )
        label_counts = {
            k: v
            for k, v in (label_rows[0].items() if label_rows else {})
            if k not in ("__Graph__", "__SchemaObject__")
        }

        rel_type_rows = self._submit(
            f"g.V().has('{self._pk}', gid).outE().groupCount().by(label)", {"gid": graph_id}
        )
        rel_type_counts = rel_type_rows[0] if rel_type_rows else {}

        schema = self.get_schema(graph_id)

        return {
            "node_count": node_count,
            "relationship_count": rel_count,
            "label_counts": label_counts,
            "relationship_type_counts": rel_type_counts,
            "constraint_count": len(schema["constraints"]),
            "index_count": len(schema["indexes"]),
            "generated_at": now_iso(),
        }

    def get_schema_context(self, graph_id: str) -> str:
        """Human-readable summary of a graph's vertex/edge labels and a few
        sample properties per label. Used to ground the LLM when translating
        a natural-language prompt to a Gremlin traversal."""
        self._ensure_graph_exists(graph_id)
        schema = self.get_schema(graph_id)
        stats = self.get_stats(graph_id)

        lines: list[str] = []

        lines.append(
            f"Every vertex is scoped to this graph via the property '{self._pk}', "
            f"bound to the $graph_id parameter. Always filter with "
            f"has('{self._pk}', graph_id)."
        )

        lines.append("Vertex labels (count):")
        if stats["label_counts"]:
            for label, count in stats["label_counts"].items():
                lines.append(f"  - {label} ({count})")
        else:
            lines.append("  (none yet)")

        lines.append("Edge labels (count):")
        if stats["relationship_type_counts"]:
            for etype, count in stats["relationship_type_counts"].items():
                lines.append(f"  - {etype} ({count})")
        else:
            lines.append("  (none yet)")

        if schema["constraints"]:
            lines.append("Declared unique constraints (metadata only, not enforced by Cosmos):")
            for c in schema["constraints"]:
                lines.append(f"  - {c['label']}.{','.join(c['properties'])}")

        lines.append("Note: Cosmos DB Gremlin indexes all properties automatically.")

        sample_lines = ["Sample properties per vertex label:"]
        for label in stats["label_counts"].keys():
            if not is_valid_ident(label):
                continue
            rows = self._submit(
                f"g.V().has('{self._pk}', gid).hasLabel('{label}').limit(1).valueMap()",
                {"gid": graph_id},
            )
            if rows:
                props = [k for k in rows[0].keys() if k not in ("id", self._pk)]
                sample_lines.append(f"  - {label}: {', '.join(props) if props else '(no properties yet)'}")
        if len(sample_lines) > 1:
            lines.extend(sample_lines)

        return "\n".join(lines)

    def check_health(self, graph_id: str) -> dict:
        # Existence check is allowed to raise GraphNotFoundError (-> 404),
        # separate from a degraded connection (-> 503 with status unhealthy).
        self._ensure_graph_exists(graph_id)

        start = time.perf_counter()
        try:
            self._submit("g.inject('ok')")
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {"status": "healthy", "latency_ms": latency_ms, "checked_at": now_iso()}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e), "checked_at": now_iso()}
"""
main.py

FastAPI app implementing Graph Database API v2 (see graph-api-v2.yaml).
Run with:  uvicorn main:app --reload --port 8000

Backend-agnostic: main.py only calls methods on the GraphAdapter interface
(graph_adapter.py). Which concrete backend `service` is depends on the
DB_TYPE env var -- "neo4j" -> neo4j_ops.Neo4jGraphService, "gremlin" ->
gremlin_ops.GremlinGraphService (e.g. Azure Cosmos DB).
"""

from __future__ import annotations

import os
from typing import Any, Optional, Union

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, model_validator

from graph_adapter import (
    BadRequestError,
    ConflictError,
    GraphAdapter,
    GraphNotFoundError,
    NodeReferenceError,
)
from llm import CypherGenerationError, LLMConfigError, generate_cypher, translate_cypher

load_dotenv()

DB_TYPE = os.environ.get("DB_TYPE", "neo4j").strip().lower()

# Simple static bearer token for demo/dev purposes. The spec only requires
# `bearerAuth` (HTTP Bearer / JWT) -- swap this for real JWT verification
# before using this anywhere near production.
API_BEARER_TOKEN = "dev-token"

app = FastAPI(title="Graph Database API v2", version="2.0.0")


def _build_service() -> GraphAdapter:
    """Adapter-pattern factory: main.py only ever depends on GraphAdapter.
    DB_TYPE=neo4j -> Neo4jGraphService, DB_TYPE=gremlin -> GremlinGraphService.
    Each backend's driver package is imported lazily, inside its branch, so
    you don't need both `neo4j` and `gremlinpython` installed -- just the
    one you're actually using."""
    if DB_TYPE == "neo4j":
        from neo4j_ops import Neo4jGraphService

        password = os.environ.get("NEO4J_PASSWORD")
        if not password:
            raise RuntimeError("DB_TYPE=neo4j but missing env var: NEO4J_PASSWORD")

        return Neo4jGraphService(
            uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            user=os.environ.get("NEO4J_USERNAME", "neo4j"),
            password=password,
            database=os.environ.get("NEO4J_DB", "neo4j"),
        )
    elif DB_TYPE == "gremlin":
        from gremlin_ops import GremlinGraphService

        host = os.environ.get("GREMLIN_HOST")
        database = os.environ.get("GREMLIN_DATABASE")
        graph = os.environ.get("GREMLIN_GRAPH")
        key = os.environ.get("GREMLIN_KEY")
        partition_key = os.environ.get("GREMLIN_PARTITION_KEY", "graph_id")
        missing = [
            n
            for n, v in [
                ("GREMLIN_HOST", host),
                ("GREMLIN_DATABASE", database),
                ("GREMLIN_GRAPH", graph),
                ("GREMLIN_KEY", key),
            ]
            if not v
        ]
        if missing:
            raise RuntimeError(f"DB_TYPE=gremlin but missing env vars: {', '.join(missing)}")
        return GremlinGraphService(
            host=host, database=database, graph=graph, key=key, partition_key=partition_key
        )
    else:
        raise RuntimeError(f"Unsupported DB_TYPE={DB_TYPE!r}; expected 'neo4j' or 'gremlin'.")


service: GraphAdapter = _build_service()

_bearer_scheme = HTTPBearer()


def require_auth(creds: HTTPAuthorizationCredentials = Security(_bearer_scheme)) -> None:
    if creds.credentials != API_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Invalid bearer token."})


@app.on_event("shutdown")
def _shutdown() -> None:
    service.close()


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------

@app.exception_handler(GraphNotFoundError)
async def _graph_not_found(_, exc: GraphNotFoundError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={"error": "not_found", "message": f"Graph '{exc}' not found."})


@app.exception_handler(ConflictError)
async def _conflict(_, exc: ConflictError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=409, content={"error": "conflict", "message": str(exc)})


@app.exception_handler(BadRequestError)
async def _bad_request(_, exc: BadRequestError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"error": "bad_request", "message": str(exc)})


@app.exception_handler(NodeReferenceError)
async def _node_reference(_, exc: NodeReferenceError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={"error": "unprocessable_entity", "message": str(exc)})


@app.exception_handler(CypherGenerationError)
async def _cypher_generation_error(_, exc: CypherGenerationError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"error": "cypher_generation_failed", "message": str(exc)})


@app.exception_handler(LLMConfigError)
async def _llm_config_error(_, exc: LLMConfigError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"error": "llm_not_configured", "message": str(exc)})


# --------------------------------------------------------------------------
# Pydantic models (mirrors components.schemas in the OpenAPI spec)
# --------------------------------------------------------------------------

class CreateGraphRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class ConstraintSpec(BaseModel):
    name: Optional[str] = None
    label: str
    properties: list[str] = Field(..., min_length=1)
    type: str = "unique"


class CreateConstraintsRequest(BaseModel):
    constraints: Optional[list[ConstraintSpec]] = None
    # single-item fields (flattened) for the "one constraint" payload shape
    name: Optional[str] = None
    label: Optional[str] = None
    properties: Optional[list[str]] = None
    type: Optional[str] = "unique"

    @model_validator(mode="after")
    def _normalize(self):
        if self.constraints is None:
            if not self.label or not self.properties:
                raise ValueError("Provide either 'constraints' array or a single label/properties.")
            self.constraints = [ConstraintSpec(name=self.name, label=self.label, properties=self.properties, type=self.type or "unique")]
        return self


class IndexSpec(BaseModel):
    name: Optional[str] = None
    label: str
    properties: list[str] = Field(..., min_length=1)
    type: str = "btree"


class CreateIndexesRequest(BaseModel):
    indexes: Optional[list[IndexSpec]] = None
    name: Optional[str] = None
    label: Optional[str] = None
    properties: Optional[list[str]] = None
    type: Optional[str] = "btree"

    @model_validator(mode="after")
    def _normalize(self):
        if self.indexes is None:
            if not self.label or not self.properties:
                raise ValueError("Provide either 'indexes' array or a single label/properties.")
            self.indexes = [IndexSpec(name=self.name, label=self.label, properties=self.properties, type=self.type or "btree")]
        return self


class NodeUpsert(BaseModel):
    id: str
    labels: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class NodeCollectionRequest(BaseModel):
    nodes: list[NodeUpsert] = Field(..., min_length=1)


class NodeRef(BaseModel):
    label: str
    key: dict[str, Any]


class RelationshipUpsert(BaseModel):
    type: str
    start_node: NodeRef
    end_node: NodeRef
    properties: dict[str, Any] = Field(default_factory=dict)


class RelationshipUpsertRequest(BaseModel):
    upsert_missing_nodes: bool = False
    relationship: RelationshipUpsert


class RelationshipCollectionRequest(BaseModel):
    upsert_missing_nodes: bool = False
    relationships: list[RelationshipUpsert] = Field(..., min_length=1)


class QueryRequest(BaseModel):
    name: Optional[str] = None
    query: Optional[str] = None
    prompt: Optional[str] = None  # natural-language request -> Azure OpenAI -> Cypher
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_of(self):
        provided = [v for v in (self.name, self.query, self.prompt) if v]
        if len(provided) != 1:
            raise ValueError("Provide exactly one of 'name', 'query', or 'prompt'.")
        return self


class WriteRequest(BaseModel):
    query: Optional[str] = None
    prompt: Optional[str] = None  # natural-language request -> Azure OpenAI -> Cypher
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_of(self):
        provided = [v for v in (self.query, self.prompt) if v]
        if len(provided) != 1:
            raise ValueError("Provide exactly one of 'query' or 'prompt'.")
        return self


# --------------------------------------------------------------------------
# Routes -- base path /api/v2
# --------------------------------------------------------------------------

API_PREFIX = "/api/v2"


@app.post(f"{API_PREFIX}/graphs", status_code=status.HTTP_201_CREATED, tags=["Graph Lifecycle"])
def create_graph(body: CreateGraphRequest, _=Depends(require_auth)):
    return service.create_graph(body.name, body.description)


@app.get(f"{API_PREFIX}/graphs/{{id}}", tags=["Graph Lifecycle"])
def get_graph(id: str, _=Depends(require_auth)):
    return service.get_graph(id)


@app.get(f"{API_PREFIX}/graphs/{{id}}/schema", tags=["Schema"])
def get_schema(id: str, _=Depends(require_auth)):
    return service.get_schema(id)


@app.post(f"{API_PREFIX}/graphs/{{id}}/schema/constraints", status_code=status.HTTP_201_CREATED, tags=["Schema"])
def create_constraints(id: str, body: CreateConstraintsRequest, _=Depends(require_auth)):
    specs = [c.model_dump() for c in body.constraints]
    created = service.create_constraints(id, specs)
    if len(created) == 1 and body.constraints and len(body.constraints) == 1 and body.label:
        # single-item request shape -> single-item response shape
        return created[0]
    return {"created": len(created), "constraints": created}


@app.post(f"{API_PREFIX}/graphs/{{id}}/schema/indexes", status_code=status.HTTP_201_CREATED, tags=["Schema"])
def create_indexes(id: str, body: CreateIndexesRequest, _=Depends(require_auth)):
    specs = [i.model_dump() for i in body.indexes]
    created = service.create_indexes(id, specs)
    if len(created) == 1 and body.indexes and len(body.indexes) == 1 and body.label:
        return created[0]
    return {"created": len(created), "indexes": created}


@app.post(f"{API_PREFIX}/graphs/{{id}}/nodes", tags=["Nodes"])
def upsert_nodes(id: str, body: dict, _=Depends(require_auth)):
    if "nodes" in body:
        req = NodeCollectionRequest(**body)
        results = service.upsert_nodes(id, [n.model_dump() for n in req.nodes])
        return {
            "nodes_created": sum(r["node_created"] for r in results),
            "nodes_updated": sum(r["node_updated"] for r in results),
            "properties_set": sum(r["properties_set"] for r in results),
        }
    else:
        node = NodeUpsert(**body)
        result = service.upsert_nodes(id, [node.model_dump()])[0]
        return {
            "node_created": result["node_created"],
            "node_updated": result["node_updated"],
            "properties_set": result["properties_set"],
        }


@app.post(f"{API_PREFIX}/graphs/{{id}}/relationships", tags=["Relationships"])
def upsert_relationships(id: str, body: dict, _=Depends(require_auth)):
    if "relationships" in body:
        req = RelationshipCollectionRequest(**body)
        results = service.upsert_relationships(
            id, [r.model_dump() for r in req.relationships], req.upsert_missing_nodes
        )
        return {
            "relationships_created": sum(r["relationship_created"] for r in results),
            "relationships_updated": sum(r["relationship_updated"] for r in results),
            "nodes_created": sum(r["nodes_created"] for r in results),
        }
    else:
        req = RelationshipUpsertRequest(**body)
        result = service.upsert_relationships(
            id, [req.relationship.model_dump()], req.upsert_missing_nodes
        )[0]
        return result


@app.post(f"{API_PREFIX}/graphs/{{id}}/query", tags=["Query"])
def execute_query(id: str, body: QueryRequest, _=Depends(require_auth)):
    if body.prompt:
        schema_context = service.get_schema_context(id)
        generated = generate_cypher(
            body.prompt,
            schema_context,
            mode="read",
            dialect=service.query_language,
            extra_parameters=body.parameters,
        )
        result = service.execute_query(id, name=None, query=generated["query"], parameters=generated["parameters"])
        result["generated_cypher"] = generated["query"]  # extension beyond the base spec, handy for debugging
        return result

    if body.query and service.query_language != "cypher":
        # `query` is always written in Cypher (the API's canonical query
        # language). If db_type == neo4j, the backend already speaks Cypher
        # natively, so it's passed through without the LLM (see the plain
        # `return` below). For any other db_type, translate it into that
        # backend's native language via the LLM before executing.
        schema_context = service.get_schema_context(id)
        translated = translate_cypher(body.query, body.parameters, schema_context, mode="read")
        result = service.execute_query(id, name=None, query=translated["query"], parameters=translated["parameters"])
        result["translated_query"] = translated["query"]  # extension beyond the base spec, handy for debugging
        return result

    # db_type == neo4j (or a named query): pass through without the LLM.
    return service.execute_query(id, body.name, body.query, body.parameters)


@app.post(f"{API_PREFIX}/graphs/{{id}}/write", tags=["Write"])
def execute_write(id: str, body: WriteRequest, _=Depends(require_auth)):
    if body.prompt:
        schema_context = service.get_schema_context(id)
        generated = generate_cypher(
            body.prompt,
            schema_context,
            mode="write",
            dialect=service.query_language,
            extra_parameters=body.parameters,
        )
        result = service.execute_write(id, generated["query"], generated["parameters"])
        result["generated_cypher"] = generated["query"]
        return result

    if service.query_language != "cypher":
        # `query` is always written in Cypher. If db_type == neo4j, pass
        # through without the LLM (see the plain `return` below). For any
        # other db_type, translate it into that backend's native language
        # via the LLM before executing.
        schema_context = service.get_schema_context(id)
        translated = translate_cypher(body.query, body.parameters, schema_context, mode="write")
        result = service.execute_write(id, translated["query"], translated["parameters"])
        result["translated_query"] = translated["query"]
        return result

    # db_type == neo4j: pass through without the LLM.
    return service.execute_write(id, body.query, body.parameters)


@app.get(f"{API_PREFIX}/graphs/{{id}}/stats", tags=["Stats"])
def get_stats(id: str, _=Depends(require_auth)):
    return service.get_stats(id)


@app.get(f"{API_PREFIX}/graphs/{{id}}/health", tags=["Health"])
def check_health(id: str, _=Depends(require_auth)):
    from fastapi.responses import JSONResponse

    result = service.check_health(id)
    if result["status"] != "healthy":
        return JSONResponse(status_code=503, content=result)
    return result

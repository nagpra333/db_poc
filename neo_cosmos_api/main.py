from fastapi import FastAPI,HTTPException
from pydantic import BaseModel
from typing import List
from graph import GraphAPI
from graph import Neo4jGraph

from contextlib import asynccontextmanager
from graph import (
    GraphAPI,
    neo_url,
    neo_username,
    neo_password
)
from cosmos import CosmosGraph

graphs = {}
#db_type = "neo"
db_type = "cosmos"
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_graphs()
    yield

app = FastAPI(
    title="Graph API",
    version="1.0.0",
    lifespan=lifespan
)

def load_graphs():
    if db_type == "neo":
        neo = Neo4jGraph(
            uri=neo_url,
            user=neo_username,
            password=neo_password
        )
        for db_name in neo.list_databases():
            graphs[db_name] = GraphAPI(db_name, db_type)
        neo.close()

    elif db_type == "cosmos":
        cosmos = CosmosGraph()
        cosmos.connect()
        for db_name in cosmos.list_databases():
            graphs[db_name] = GraphAPI(db_name, db_type)
    print("graphs:", graphs)

class CreateGraphRequest(BaseModel):
    db_name:str

class Constraint(BaseModel):
    label:str
    property_key:str

class Index(BaseModel):
    label:str
    property_key:str

class Node(BaseModel):
    label:str
    properties:dict

class Relationship(BaseModel):
    from_label:str
    from_key:str
    from_value:str|int
    to_label:str
    to_key:str
    to_value:str|int
    relationship:str
    properties:dict={}

class Query(BaseModel):
    query:str

class WriteRequest(BaseModel):
    query: str


# Create and initialize a new graph instance
@app.post("/api/v1/graphs", status_code=201)
def create_graph(request: CreateGraphRequest):
    db_name = request.db_name
    if db_name in graphs:
        raise HTTPException(
            status_code=409,
            detail="Graph already exists"
        )
    api = GraphAPI(db_name, db_type)
    api.create_db()
    api.initialize_graph()
    graphs[db_name] = api
    return {
        "graphId": db_name,
        "status": api.get_graph_status()
    }

# Get status and metadata for a specific graph
@app.get("/api/v1/graphs/{db_name}", status_code=200)
def get_graph(db_name: str):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    api = graphs[db_name]
    return api.get_graph()

# Verify connectivity to the underlying graph database. 
@app.get("/api/v1/graphs/{db_name}/health", status_code=200)
def health(db_name: str):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    result = graphs[db_name].health()
    if result.get("overall_status") != "HEALTHY":
        raise HTTPException(
            status_code=503,
            detail=result
        )
    return result

# Create a unique constraint.
@app.post("/api/v1/graphs/{db_name}/schema/constraints", status_code=201)
def create_constraint(db_name: str, constraint: Constraint):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[db_name].create_unique_constraint(constraint.label, constraint.property_key)
    return {
        "message": "Constraint created"
    }

# Create multiple constraints in one call 
@app.post("/api/v1/graphs/{db_name}/schema/constraints/batch", status_code=201)
def create_constraints(
    db_name: str,
    constraints: List[Constraint]
):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    graphs[db_name].create_multiple_constraints(
        [c.model_dump() for c in constraints]
    )
    return {
        "message": "Constraints created"
    }

#Retrieve current schema
@app.get("/api/v1/graphs/{db_name}/schema", status_code=200)
def get_schema(db_name: str):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[db_name].get_schema(db_name)

# Create a single index. 
@app.post("/api/v1/graphs/{db_name}/schema/indexes", status_code=201)
def create_index(db_name: str, index: Index):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    graphs[db_name].create_index(
        index.label,
        index.property_key
    )
    return {
        "message": "Index created successfully"
    }

# Create multiple indexes.
@app.post("/api/v1/graphs/{db_name}/schema/indexes/batch", status_code=201)
def create_indexes(db_name: str, indexes: List[Index]):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    graphs[db_name].create_multiple_indexes(
        [i.model_dump() for i in indexes]
    )
    return {
        "message": "Indexes created successfully"
    }

# Retrieve graph statistics.
@app.get("/api/v1/graphs/{db_name}/stats", status_code=200)
def get_stats(db_name: str):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    return graphs[db_name].get_graph_statistics(db_name)

# Batch upsert nodes.
@app.post("/api/v1/graphs/{db_name}/nodes/batch", status_code=200)
def upsert_nodes(db_name: str, nodes: list[Node]):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    api = graphs[db_name]
    for node in nodes:
        api.upsert_node(
            node.label,
            node.properties
        )
    return {
        "message": "Nodes upserted successfully"
    }

# Batch upsert relationships relationships.
@app.post("/api/v1/graphs/{db_name}/relationships/batch", status_code=200)
def upsert_relationships(db_name: str, relationships: list[Relationship]):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    api = graphs[db_name]
    for rel in relationships:
        api.upsert_relationship(
            rel.from_label,
            rel.from_key,
            rel.from_value,
            rel.to_label,
            rel.to_key,
            rel.to_value,
            rel.relationship,
            rel.properties
        )
    return {
        "message": "Relationships upserted successfully"
    }

# Accepts a parameterized graph write operation or natural language write request, translates it into the appropriate database-specific write query, executes it, and returns execution status along with affected node/relationship counts.
@app.post("/api/v1/graphs/{db_name}/write", status_code=200)
def execute_write(db_name: str, request: WriteRequest):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    api = graphs[db_name]
    return api.execute_natural_language(request.query, "write")

# Accepts a free-form graph query (raw Cypher/Cosmos SQL) and executes it directly. Returns query results without modifying the graph.
@app.post("/api/v1/graphs/{db_name}/query", status_code=200)
def execute_query(db_name: str, request: Query):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    api = graphs[db_name]
    return api.execute_query(request.query)

# Accepts a parameterized graph read operation or natural language read request, translates it into the appropriate database-specific read query, executes it, and returns the query results.
@app.post("/api/v1/graphs/{db_name}/read", status_code=200)
def execute_read(db_name: str, request: WriteRequest):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )
    api = graphs[db_name]
    return api.execute_natural_language(request.query, "read")
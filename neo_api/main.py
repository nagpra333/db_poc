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

graphs = {}
db_type = "neo"

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
            graphs[db_name] = GraphAPI(db_name)
        print("graphs:", graphs)
        neo.close()
    elif db_type == "cosmos":
        print("loading cosmos...")

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
def create_graph(request:CreateGraphRequest):

    db_name = request.db_name
    if db_name in graphs:
        raise HTTPException(
            status_code=409,
            detail="Graph already exists"
        )

    api = GraphAPI(db_name)

    api.create_initialize()
    api.initialize_graph()

    graphs[db_name]=api

    return{
        "graphId":db_name,
        "status":api.get_graph_status()
    }


# Get status and metadata for a specific graph
@app.get("/api/v1/graphs/{db_name}")
def get_graph(db_name:str):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[db_name].get_graph_statistics()

# Verify connectivity to the underlying graph database. 
@app.get("/api/v1/graphs/{db_name}/health")
def health(db_name:str):

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
def create_constraint(db_name:str,constraint:Constraint):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[db_name].create_unique_constraint(
        constraint.label,
        constraint.property_key
    )

    return{
        "message":"Constraint created successfully"
    }

# Create multiple constraints in one call
@app.post("/api/v1/graphs/{db_name}/schema/constraints/batch", status_code=201)
def create_constraints(db_name:str,constraints:List[Constraint]):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[db_name].create_multiple_constraints(
        [
            c.model_dump()
            for c in constraints
        ]
    )

    return{
        "message":"Constraints created successfully"
    }

# Retrieve current schema
@app.get("/api/v1/graphs/{db_name}/schema")
def get_schema(db_name:str):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[db_name].get_schema()

# Create a single index. 
@app.post("/api/v1/graphs/{db_name}/schema/indexes", status_code=201)
def create_index(db_name:str,index:Index):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[db_name].create_index(
        index.label,
        index.property_key
    )

    return{
        "message":"Index created successfully"
    }

# Create multiple indexes.
@app.post("/api/v1/graphs/{db_name}/schema/indexes/batch", status_code=201)
def create_indexes(db_name:str,indexes:List[Index]):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[db_name].create_multiple_indexes(
        [
            i.model_dump()
            for i in indexes
        ]
    )

    return{
        "message":"Indexes created successfully"
    }

# Retrieve graph statistics.
@app.get("/api/v1/graphs/{db_name}/stats")
def graph_statistics(db_name:str):

    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[db_name].get_graph_statistics()

# Batch upsert nodes.
@app.post("/api/v1/graphs/{db_name}/nodes/batch")
def upsert_nodes(db_name:str,nodes:list[Node]):

    if db_name not in graphs:
        raise HTTPException(status_code=404,detail="Graph not found")

    api=graphs[db_name]

    results=[
        api.upsert_node(
            node.label,
            node.properties
        )
        for node in nodes
    ]

    return{
        "nodes":results
    }

# Batch upsert relationships
@app.post("/api/v1/graphs/{db_name}/relationships/batch")
def upsert_relationships(db_name:str,relationships:list[Relationship]):

    if db_name not in graphs:
        raise HTTPException(status_code=404,detail="Graph not found")

    api=graphs[db_name]

    results=[
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
        for rel in relationships
    ]

    return{
        "relationships":results
    }

# Accepts a free-form graph query (or natural language query) and translates it into the native query language of the underlying graph database (Cypher, Cosmos SQL, Gremlin, etc.) before execution. Returns query results without modifying the graph.
@app.post("/api/v1/graphs/{db_name}/query")
def execute_query(db_name:str,request:Query):

    if db_name not in graphs:
        raise HTTPException(status_code=404,detail="Graph not found")

    api=graphs[db_name]

    return api.execute_query(request.query)

# Accepts a parameterized graph write operation or natural language write request, translates it into the appropriate database-specific write query, executes it, and returns execution status along with affected node/relationship counts.
@app.post("/api/v1/graphs/{db_name}/write")
def execute_write(db_name: str, request: WriteRequest):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    api = graphs[db_name]

    return api.execute_natural_language(request.query, "write")

# Accepts a parameterized graph read operation or natural language read request, translates it into the appropriate database-specific read query, executes it, and returns the query results.
@app.post("/api/v1/graphs/{db_name}/read")
def execute_read(db_name: str, request: WriteRequest):
    if db_name not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    api = graphs[db_name]

    return api.execute_natural_language(request.query, "read")
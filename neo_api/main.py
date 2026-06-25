from fastapi import FastAPI,HTTPException
from pydantic import BaseModel
from typing import List
from graph import GraphAPI

app=FastAPI(title="Graph API",version="1.0.0")

graphs={}

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

@app.post("/api/v1/graphs")
def create_graph():

    graph_id="default"

    if graph_id in graphs:
        raise HTTPException(
            status_code=409,
            detail="Graph already exists"
        )

    api=GraphAPI()

    api.create_initialize()
    api.initialize_graph()

    graphs[graph_id]=api

    return{
        "graphId":graph_id,
        "status":api.get_graph_status()
    }

@app.get("/api/v1/graphs/{graph_id}")
def get_graph(graph_id:str):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    api=graphs[graph_id]

    return{
        "graphId":graph_id,
        "status":api.get_graph_status()
    }

@app.get("/api/v1/graphs/{graph_id}/health")
def health(graph_id:str):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[graph_id].health()

@app.post("/api/v1/graphs/{graph_id}/schema/constraints")
def create_constraint(graph_id:str,constraint:Constraint):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[graph_id].create_unique_constraint(
        constraint.label,
        constraint.property_key
    )

    return{
        "message":"Constraint created successfully"
    }

@app.post("/api/v1/graphs/{graph_id}/schema/constraints/batch")
def create_constraints(graph_id:str,constraints:List[Constraint]):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[graph_id].create_multiple_constraints(
        [
            c.model_dump()
            for c in constraints
        ]
    )

    return{
        "message":"Constraints created successfully"
    }

@app.get("/api/v1/graphs/{graph_id}/schema")
def get_schema(graph_id:str):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[graph_id].get_schema()

@app.post("/api/v1/graphs/{graph_id}/schema/indexes")
def create_index(graph_id:str,index:Index):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[graph_id].create_index(
        index.label,
        index.property_key
    )

    return{
        "message":"Index created successfully"
    }

@app.post("/api/v1/graphs/{graph_id}/schema/indexes/batch")
def create_indexes(graph_id:str,indexes:List[Index]):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    graphs[graph_id].create_multiple_indexes(
        [
            i.model_dump()
            for i in indexes
        ]
    )

    return{
        "message":"Indexes created successfully"
    }

@app.get("/api/v1/graphs/{graph_id}/stats")
def graph_statistics(graph_id:str):

    if graph_id not in graphs:
        raise HTTPException(
            status_code=404,
            detail="Graph not found"
        )

    return graphs[graph_id].get_graph_statistics()

@app.post("/api/v1/graphs/{graph_id}/nodes/batch")
def upsert_nodes(graph_id:str,nodes:list[Node]):

    if graph_id not in graphs:
        raise HTTPException(status_code=404,detail="Graph not found")

    api=graphs[graph_id]

    for node in nodes:
        api.upsert_node(
            node.label,
            node.properties
        )

    return{
        "message":"Nodes upserted successfully"
    }

@app.post("/api/v1/graphs/{graph_id}/relationships/batch")
def upsert_relationships(graph_id:str,relationships:list[Relationship]):

    if graph_id not in graphs:
        raise HTTPException(status_code=404,detail="Graph not found")

    api=graphs[graph_id]

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

    return{
        "message":"Relationships upserted successfully"
    }

@app.post("/api/v1/graphs/{graph_id}/query")
def execute_query(graph_id:str,request:Query):

    if graph_id not in graphs:
        raise HTTPException(status_code=404,detail="Graph not found")

    api=graphs[graph_id]

    return api.execute_query(request.query)
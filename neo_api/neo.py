from neo4j import GraphDatabase
import pandas as pd
import json
import re
import time
from neo4j.exceptions import ClientError

import json
from openai import AzureOpenAI

from pathlib import Path
PROMPTS_DIR = Path(__file__).parent / "prompts"

from prompt_loader import load_prompt
import os
from dotenv import load_dotenv

load_dotenv()

SYSTEM_PROMPT_TEMPLATE = load_prompt("write_system_prompt.txt")
# ------------------------------------------
# Azure OpenAI Configuration
# ------------------------------------------

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
neo_url = os.getenv("NEO4J_URI")
neo_username = os.getenv("NEO4J_USERNAME")
neo_password = os.getenv("NEO4J_PASSWORD")

def load_prompt(file_name: str) -> str:
        return (PROMPTS_DIR / file_name).read_text(encoding="utf-8")

def get_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE

class Neo4jGraph:

    def __init__(self, uri, user, password):

        self.driver = GraphDatabase.driver(
            uri,
            auth=(user, password)
        )

        self.aoai = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_OPENAI_API_VERSION
        )

        self.model = AZURE_OPENAI_DEPLOYMENT

    def close(self):
        self.driver.close()

    def list_databases(self):
        with self.driver.session(database="system") as session:
            result = session.run("SHOW DATABASES")
            return [
                record["name"]
                for record in result
                if record["name"] != "system"
            ]

    # --------------------------------------------------
    # /api/v1/graphs
    # --------------------------------------------------
    def create_database(self,db_name):

        try:

            with self.driver.session(database="system") as session:
                session.run(f"CREATE DATABASE {db_name} IF NOT EXISTS")

            while True:

                with self.driver.session(database="system") as session:

                    status=session.run(
                        f"SHOW DATABASE {db_name} YIELD currentStatus RETURN currentStatus"
                    ).single()

                    if status and status["currentStatus"].lower()=="online":
                        break

                time.sleep(1)

            print(f"Database '{db_name}' created successfully")

        except ClientError:

            print("Neo4j Community Edition detected. Using default database 'neo4j'.")

    # --------------------------------------------------
    # Check Database Connectivity
    # --------------------------------------------------
    def wait_for_db(self, db_name: str):
        print("Connecting to:", db_name)
        with self.driver.session(database = db_name) as session:
            result = session.run("RETURN 1 AS test")
            print(result.single())

    
    def initialize_graph(self, db_name, sample_data):
        with self.driver.session(database = db_name) as session:
            for node in sample_data.get("nodes", []):
                label=node["label"]
                props=node["properties"]
                props_str=", ".join(
                    [f"{k}: ${k}" for k in props.keys()]
                )

                cypher=f"MERGE (n:{label} {{{props_str}}})"

                session.run(cypher, **props)

            for rel in sample_data.get("relationships", []):

                from_node = rel["from"]
                to_node = rel["to"]
                rel_type = rel["type"]
                rel_props = rel.get("properties", {})

                rel_props_str = ""

                if rel_props:
                    rel_props_str="{" + ", ".join([f"{k}: ${k}" for k in rel_props.keys()]) + "}"

                cypher=f"""
                MATCH (a:{from_node['label']} {{{from_node['key']}:$from_value}})
                MATCH (b:{to_node['label']} {{{to_node['key']}:$to_value}})
                MERGE (a)-[r:{rel_type} {rel_props_str}]->(b)
                """

                params={
                    "from_value":from_node["value"],
                    "to_value":to_node["value"],
                    **rel_props
                }

                session.run(cypher,**params)

    # --------------------------------------------------
    # /api/v1/graphs/{id}/schema/constraints
    # --------------------------------------------------
    def create_unique_constraint(self, db_name, label, property_key):
        constraint_name = f"{label.lower()}_{property_key}_unique"
        cypher=f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{property_key} IS UNIQUE"
        with self.driver.session(database=db_name) as session:
            session.run(cypher)

    # --------------------------------------------------
    # /api/v1/graphs/{id}/schema/constraints/batch
    # --------------------------------------------------
    def create_multiple_constraints(self, db_name, constraints):
        with self.driver.session(database = db_name) as session:
            for constraint in constraints:
                label = constraint["label"]
                property_key = constraint["property_key"]
                constraint_name = f"{label.lower()}_{property_key}_unique"
                cypher = f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{property_key} IS UNIQUE"
                session.run(cypher)

    # --------------------------------------------------
    # /api/v1/graphs/{id}
    # --------------------------------------------------
    def get_graph_status(self, db_name):
        try:
            with self.driver.session(database = db_name) as session:
                return session.run("RETURN 'ONLINE' AS status").single()["status"]
        except Exception as e:
            return str(e)

    # --------------------------------------------------
    # Get Constraints
    # --------------------------------------------------
    def get_constraints(self, db_name: str):
        with self.driver.session(database=db_name) as session:
            result = session.run("SHOW CONSTRAINTS")
            constraints = []
            for record in result:
                constraints.append(dict(record))
            return constraints

    # --------------------------------------------------
    # Excel Ingestion
    # --------------------------------------------------
    def ingest_excel(self, db_name, input_file):
        nodes_df = pd.read_excel(input_file, sheet_name = "nodes")
        rels_df = pd.read_excel(input_file,sheet_name = "relationships")

        with self.driver.session(database = db_name) as session:
            for _,row in nodes_df.iterrows():
                label=row["label"]
                props={
                    k:v
                    for k,v in row.items()
                    if pd.notna(v)
                }

                query=f"""
                MERGE (n:{label} {{id:$id}})
                SET n += $props
                """

                session.run(query, id=row["id"], props=props)

        with self.driver.session(database = db_name) as session:

            for _,row in rels_df.iterrows():

                query=f"""
                MATCH (a:{row['from_label']} {{{row['from_key']}:$from_value}})
                MATCH (b:{row['to_label']} {{{row['to_key']}:$to_value}})
                MERGE (a)-[r:{row['type']}]->(b)
                """

                session.run(query,from_value=row["from_value"], to_value=row["to_value"])

        print("Ingestion completed")
    
    def execute_queries_from_csv(self,db_name,input_csv,output_csv):
        df=pd.read_csv(input_csv)
        results=[]

        with self.driver.session(database=db_name) as session:

            for _,row in df.iterrows():
                query_text=str(row["Query / Cypher"]).strip()
                lines=[line.strip() for line in query_text.split("\n") if line.strip()]
                lines=[line for line in lines if not line.startswith("--")]

                cypher_lines=[]

                for line in lines:

                    if(line.upper().startswith("MATCH")
                    or line.upper().startswith("OPTIONAL MATCH")
                    or line.upper().startswith("WITH")
                    or line.upper().startswith("RETURN")
                    or line.upper().startswith("CALL")
                    or line.upper().startswith("MERGE")
                    or line.upper().startswith("CREATE")
                    or line.upper().startswith("UNWIND")):

                        cypher_lines.append(line)
                query="\n".join(cypher_lines)
                try:

                    print("="*100)
                    print(query)
                    print("="*100)

                    query_result=session.run(query)

                    records=[
                        dict(record)
                        for record in query_result
                    ]

                    result_text=json.dumps(records,default=str)

                except Exception as e:

                    result_text=f"ERROR: {str(e)}"

                results.append(result_text)

        df["Result"]=results
        df.to_csv(output_csv,index=False)

        print(f"Output written to {output_csv}")
    
    # --------------------------------------------------
    # /api/v1/graphs/{db_name}/schema/indexes
    # --------------------------------------------------

    def create_index(self, db_name, label, property_key):
        index_name = f"{label.lower()}_{property_key}_idx"
        cypher=f"CREATE INDEX {index_name} IF NOT EXISTS FOR (n:{label}) ON (n.{property_key})"
        with self.driver.session(database = db_name) as session:
            session.run(cypher)
        print(f"Index created: {index_name}")

    # --------------------------------------------------
    # /api/v1/graphs/{db_name}/schema/indexes/batch
    # --------------------------------------------------

    def create_multiple_indexes(self, db_name, indexes):
        with self.driver.session(database = db_name) as session:
            for idx in indexes:
                label = idx["label"]
                property_key = idx["property_key"]
                index_name = f"{label.lower()}_{property_key}_idx"
                cypher=f"CREATE INDEX {index_name} IF NOT EXISTS FOR (n:{label}) ON (n.{property_key})"
                session.run(cypher)
                print(f"Created index: {index_name}")

    # --------------------------------------------------
    # /api/v1/graphs/{db_name}/schema
    # --------------------------------------------------
    def get_schema(self, db_name):
        with self.driver.session(database = db_name) as session:
            constraints=[dict(r) for r in session.run("SHOW CONSTRAINTS")]
            indexes=[dict(r) for r in session.run("SHOW INDEXES")]
        return {"constraints":constraints, "indexes":indexes}

    # --------------------------------------------------
    # /api/v1/graphs/{db_name}/stats
    # --------------------------------------------------
    def get_graph_statistics(self, db_name):
        with self.driver.session(database = db_name) as session:
            node_count = session.run("MATCH (n) RETURN count(n) AS cnt").single()["cnt"]
            relationship_count = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]
            labels = [record["label"] for record in session.run("CALL db.labels() YIELD label RETURN label")]
            relationship_types = [record["relationshipType"] for record in session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
            constraint_count = len(list(session.run("SHOW CONSTRAINTS")))
            index_count = len(list(session.run("SHOW INDEXES")))
        return {
            "node_count":node_count,
            "relationship_count":relationship_count,
            "labels":labels,
            "relationship_types":relationship_types,
            "constraints":constraint_count,
            "indexes":index_count
        }
    
    def health(self, db_name):
        try:
            with self.driver.session(database = db_name) as session:
                session.run("RETURN 1").single()
            return {"overall_status": "HEALTHY", "system_message": "Database health check completed successfully."}
        except Exception as e:
            return {"overall_status": "UNHEALTHY", "message": "Unable to establish database connection.", "error_code": "DB_CONNECTION_FAILED"
}
    
    def upsert_node(self, db_name, label, node):
        props={k:v for k,v in node.items() if k != "id"}
        with self.driver.session(database=db_name) as session:
            record=session.run(
                f"MERGE (n:{label} {{id:$id}}) SET n += $props RETURN n, labels(n) AS labels",
                id=node["id"],
                props=props
            ).single()
            return {
                "label":label,
                "properties":dict(record["n"]),
                "labels":record["labels"]
            }

    def upsert_relationship(self, db_name, from_label, from_key, from_value, to_label, to_key, to_value, rel_type, props={}):
        rel_props="{" + ", ".join([f"{k}:${k}" for k in props]) + "}" if props else ""
        with self.driver.session(database = db_name) as session:
            record=session.run(
                f"""
                MATCH (a:{from_label} {{{from_key}:$from_value}})
                MATCH (b:{to_label} {{{to_key}:$to_value}})
                MERGE (a)-[r:{rel_type} {rel_props}]->(b)
                RETURN a, b, r, type(r) AS rel_type
                """,
                from_value = from_value,
                to_value = to_value,
                **props
            ).single()
            return {
                "type":record["rel_type"],
                "properties":dict(record["r"]),
                "from":{
                    "label":from_label,
                    "key":from_key,
                    "value":from_value,
                    "properties":dict(record["a"])
                },
                "to":{
                    "label":to_label,
                    "key":to_key,
                    "value":to_value,
                    "properties":dict(record["b"])
                }
            }

    def execute_query(self, db_name, query):

        with self.driver.session(database = db_name) as session:

            result=session.run(query)

            return[
                dict(record)
                for record in result
            ]
    
    def get_graph_schema_text(self, db_name):

        stats = self.get_graph_statistics(db_name)

        labels = ", ".join(stats["labels"])
        relationships = ", ".join(stats["relationship_types"])

        return f"""
    Node Labels:
    {labels}

    Relationship Types:
    {relationships}
    """

    def execute_natural_language_query(self, db_name, user_prompt, operation):

        schema = self.get_graph_schema_text(db_name)

        if operation.lower() == "write":
            system_prompt = load_prompt("write_system_prompt.txt")
        else:
            system_prompt = load_prompt("read_system_prompt.txt")

        system_prompt += "\n\nSchema:\n" + schema

        response = self.aoai.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],
            response_format={"type": "json_object"}
        )

        generated = json.loads(
            response.choices[0].message.content
        )

        query = generated["query"]
        parameters = generated.get("parameters", {})

        with self.driver.session(database=db_name) as session:

            result = session.run(query, parameters)

            records = [dict(record) for record in result]

            response_data = {
                "prompt": user_prompt,
                "query": query,
                "parameters": parameters,
                "records": records
            }

            if operation.lower() == "write":

                summary = result.consume()

                response_data["counters"] = {
                    "nodes_created": summary.counters.nodes_created,
                    "relationships_created": summary.counters.relationships_created,
                    "properties_set": summary.counters.properties_set,
                    "labels_added": summary.counters.labels_added
                }

        return response_data

    
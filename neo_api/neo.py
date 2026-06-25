from neo4j import GraphDatabase
import pandas as pd
import json
import re

class Neo4jGraph:

    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth = (user, password))

    def close(self):
        self.driver.close()

    # --------------------------------------------------
    # Check Database Connectivity
    # --------------------------------------------------
    def wait_for_db(self, db_name: str):
        with self.driver.session(database = db_name) as session:
            result = session.run("RETURN 1 AS test")
            print(result.single())

    # --------------------------------------------------
    # /api/v1/graphs
    # --------------------------------------------------
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
    # /api/v1/graphs/{id}/schema/indexes
    # --------------------------------------------------

    def create_index(self, db_name, label, property_key):
        index_name = f"{label.lower()}_{property_key}_idx"
        cypher=f"CREATE INDEX {index_name} IF NOT EXISTS FOR (n:{label}) ON (n.{property_key})"
        with self.driver.session(database = db_name) as session:
            session.run(cypher)
        print(f"Index created: {index_name}")

    # --------------------------------------------------
    # /api/v1/graphs/{id}/schema/indexes/batch
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
    # /api/v1/graphs/{id}/schema
    # --------------------------------------------------
    def get_schema(self, db_name):
        with self.driver.session(database = db_name) as session:
            constraints=[dict(r) for r in session.run("SHOW CONSTRAINTS")]
            indexes=[dict(r) for r in session.run("SHOW INDEXES")]
        return {"constraints":constraints, "indexes":indexes}

    # --------------------------------------------------
    # /api/v1/graphs/{id}/stats
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
    
    def upsert_node(self, db_name, label,node):
        props={k:v for k,v in node.items() if k != "id"}
        with self.driver.session(database=db_name) as session:
            session.run(f"MERGE (n:{label} {{id:$id}}) SET n += $props",id=node["id"],props=props)

    def upsert_relationship(self, db_name, from_label, from_key, from_value, to_label, to_key, to_value, rel_type, props={}):
        rel_props="{" + ", ".join([f"{k}:${k}" for k in props]) + "}" if props else ""
        with self.driver.session(database = db_name) as session:
            session.run(
                f"""
                MATCH (a:{from_label} {{{from_key}:$from_value}})
                MATCH (b:{to_label} {{{to_key}:$to_value}})
                MERGE (a)-[r:{rel_type} {rel_props}]->(b)
                """,
                from_value = from_value,
                to_value = to_value,
                **props
            )

    def execute_query(self, db_name, query):

        with self.driver.session(database = db_name) as session:

            result=session.run(query)

            return[
                dict(record)
                for record in result
            ]
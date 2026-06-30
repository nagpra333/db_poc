import os
from dotenv import load_dotenv
from neo import *
from cosmos import CosmosGraph
load_dotenv()

sample_data = {"nodes":[{"label":"Person","properties":{"id":1,"name":"Alice"}},{"label":"Person","properties":{"id":2,"name":"Bob"}}],"relationships":[{"from":{"label":"Person","key":"id","value":1},"to":{"label":"Person","key":"id","value":2},"type":"KNOWS","properties":{"since":2024}}]}


class GraphAPI:
    def __init__(self, graph_name, db_type):
        self.graph_name = graph_name
        self.db_name = graph_name
        self.db_type = db_type

        if db_type == "neo":
            self.neo = Neo4jGraph(
                uri=neo_url,
                user=neo_username,
                password=neo_password
            )
        elif db_type == "cosmos":
            self.cosmos = CosmosGraph()
            self.cosmos.connect()

    def create_db(self):
        if self.db_type == "neo":
            self.neo.create_database(self.db_name)
            print("Using database:", self.db_name)
            self.neo.wait_for_db(self.db_name)
        elif self.db_type == "cosmos":
            self.cosmos.create_database(self.db_name)
            print("Using database:", self.db_name)

    def get_graph_statistics(self, db_name):
        if self.db_type == "neo":
            return self.neo.get_graph_statistics(self.db_name)
        elif self.db_type == "cosmos":
            return self.cosmos.get_graph_statistics(self.db_name)
    
    def initialize_graph(self):
        if self.db_type == "neo":
            self.neo.initialize_graph(self.db_name, sample_data)
        elif self.db_type == "cosmos":
            self.cosmos.initialize_graph(self.db_name, sample_data)

    def create_unique_constraint(self, label, property_key):
        if self.db_type == "neo":
            self.neo.create_unique_constraint(self.db_name, label, property_key)
        elif self.db_type == "cosmos":
            self.cosmos.create_unique_constraint(self.db_name, label, property_key)

    def create_multiple_constraints(self, constraints):
        if self.db_type == "neo":
            self.neo.create_multiple_constraints(self.db_name, constraints)

        elif self.db_type == "cosmos":
            self.cosmos.create_multiple_constraints(self.db_name, constraints)

    def get_graph_status(self):
        if self.db_type == "neo":
            return self.neo.get_graph_status(self.db_name)
        elif self.db_type == "cosmos":
            return self.cosmos.get_graph_status(self.db_name)

    def get_graph(self):
        if self.db_type == "neo":
            return {
                "graphId": self.graph_name,
                "status": self.get_graph_status()
            }
        elif self.db_type == "cosmos":
            return self.cosmos.get_graph(self.graph_name)

    def get_constraints(self):
        return self.neo.get_constraints(self.db_name)

    def create_index(self, label, property_key):
        if self.db_type == "neo":
            self.neo.create_index(self.db_name, label, property_key)
        elif self.db_type == "cosmos":
            self.cosmos.create_index(self.db_name, label, property_key)

    def create_multiple_indexes(self, indexes):
        if self.db_type == "neo":
            self.neo.create_multiple_indexes(self.db_name, indexes)

        elif self.db_type == "cosmos":
            self.cosmos.create_multiple_indexes(self.db_name, indexes)

    def get_schema(self, db_name):
        if self.db_type == "neo":
            return self.neo.get_schema(db_name)
        elif self.db_type == "cosmos":
            return self.cosmos.get_schema(db_name)

    def health(self):
        if self.db_type == "neo":
            return self.neo.health(self.db_name)
        elif self.db_type == "cosmos":
            return self.cosmos.health(self.db_name)

    def upsert_node(self, label, properties):
        if self.db_type == "neo":
            self.neo.upsert_node(self.db_name, label, properties)
        elif self.db_type == "cosmos":
            self.cosmos.upsert_node(self.db_name, label, properties)

    def upsert_relationship(self, from_label, from_key, from_value, to_label, to_key, to_value, relationship, properties):
        if self.db_type == "neo":
            self.neo.upsert_relationship(self.db_name, from_label, from_key, from_value, to_label, to_key, to_value, relationship, properties)
        elif self.db_type == "cosmos":
            self.cosmos.upsert_relationship(self.db_name, from_label, from_key, from_value, to_label, to_key, to_value, relationship, properties)

    def execute_query(self, query):

        if self.db_type == "neo":
            return self.neo.execute_query(self.db_name, query)

        elif self.db_type == "cosmos":
            return self.cosmos.execute_query(self.db_name, query) 

    def ingest_excel(self,input_file):
        self.neo.ingest_excel(self.db_name, input_file)

    def execute_queries_from_csv(self, input_csv, output_csv):
        self.neo.execute_queries_from_csv(db_name, input_csv, output_csv)

    def execute_natural_language(self, query, operation):

        if self.db_type == "neo":

            return self.neo.execute_natural_language(
                self.db_name,
                query,
                operation
            )

        elif self.db_type == "cosmos":

            return self.cosmos.execute_natural_language(
                self.db_name,
                query,
                operation
            )
        
    def close(self):
        self.neo.close()
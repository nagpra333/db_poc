import os
from dotenv import load_dotenv
from neo import *

load_dotenv()
#db_name = "neo4j"

sample_data = {"nodes":[{"label":"Person","properties":{"id":1,"name":"Alice"}},{"label":"Person","properties":{"id":2,"name":"Bob"}}],"relationships":[{"from":{"label":"Person","key":"id","value":1},"to":{"label":"Person","key":"id","value":2},"type":"KNOWS","properties":{"since":2024}}]}



class GraphAPI:

    def __init__(self, graph_name):
        self.graph_name = graph_name
        self.db_name = graph_name
        self.neo=Neo4jGraph(uri = neo_url, user = neo_username, password = neo_password)

    def create_initialize(self):

        self.neo.create_database(self.db_name)
        print("Using database:", self.db_name)
        self.neo.wait_for_db(self.db_name)

    def initialize_graph(self):
        self.neo.initialize_graph(self.db_name, sample_data)

    def create_unique_constraint(self, label, property_key):
        self.neo.create_unique_constraint(self.db_name, label, property_key)

    def create_multiple_constraints(self, constraints):
        self.neo.create_multiple_constraints(self.db_name, constraints)

    def get_graph_status(self):
        return self.neo.get_graph_status(self.db_name)

    def get_constraints(self):
        return self.neo.get_constraints(self.db_name)

    def create_index(self, label, property_key):
        self.neo.create_index(self.db_name, label, property_key)

    def create_multiple_indexes(self, indexes):
        self.neo.create_multiple_indexes(self.db_name, indexes)

    def get_schema(self):
        return self.neo.get_schema(self.db_name)

    def get_graph_statistics(self):
        return self.neo.get_graph_statistics(self.db_name)

    def health(self):
        return self.neo.health(self.db_name)

    def upsert_node(self, label, node):
        return self.neo.upsert_node(self.db_name, label, node)

    def upsert_relationship(self, from_label, from_key, from_value, to_label, to_key, to_value, rel_type, props={}):
        return self.neo.upsert_relationship(self.db_name, from_label, from_key, from_value, to_label, to_key, to_value, rel_type, props)

    def execute_query(self,query):
        return self.neo.execute_query(self.db_name, query)    

    def ingest_excel(self,input_file):
        self.neo.ingest_excel(self.db_name, input_file)

    def execute_queries_from_csv(self, input_csv, output_csv):
        self.neo.execute_queries_from_csv(db_name, input_csv, output_csv)

    def execute_natural_language(self, query, operation):
        return self.neo.execute_natural_language_query(self.graph_name, query, operation)
    
    def close(self):
        self.neo.close()
from neo import *

neo_url = "neo4j://localhost:7687"
neo_username = "neo4j"
neo_password = "password123"
db_name = "neo4j"

sample_data = {"nodes":[{"label":"Person","properties":{"id":1,"name":"Alice"}},{"label":"Person","properties":{"id":2,"name":"Bob"}}],"relationships":[{"from":{"label":"Person","key":"id","value":1},"to":{"label":"Person","key":"id","value":2},"type":"KNOWS","properties":{"since":2024}}]}

class GraphAPI:

    def __init__(self):

        self.neo = Neo4jGraph(uri=neo_url, user=neo_username, password=neo_password)

    def create_initialize(self):
        self.neo.wait_for_db(db_name)

    def initialize_graph(self):
        self.neo.initialize_graph(db_name, sample_data)

    def create_unique_constraint(self, label, property_key):
        self.neo.create_unique_constraint(db_name, label, property_key)

    def create_multiple_constraints(self, constraints):
        self.neo.create_multiple_constraints(db_name, constraints)

    def get_graph_status(self):
        return self.neo.get_graph_status(db_name)

    def get_constraints(self):
        return self.neo.get_constraints(db_name)

    def create_index(self, label, property_key):
        self.neo.create_index(db_name, label, property_key)

    def create_multiple_indexes(self, indexes):
        self.neo.create_multiple_indexes(db_name, indexes)

    def get_schema(self):
        return self.neo.get_schema(db_name)

    def get_graph_statistics(self):
        return self.neo.get_graph_statistics(db_name)

    def health(self):
        return self.neo.health(db_name)

    def upsert_node(self, label, node):
        self.neo.upsert_node(db_name, label, node)

    def upsert_relationship(self, from_label, from_key, from_value, to_label, to_key, to_value, rel_type, props={}):
        self.neo.upsert_relationship(db_name, from_label, from_key, from_value, to_label, to_key, to_value, rel_type, props)

    def execute_query(self,query):
        return self.neo.execute_query(db_name, query)    

    def ingest_excel(self,input_file):
        self.neo.ingest_excel(db_name, input_file)

    def execute_queries_from_csv(self, input_csv, output_csv):
        self.neo.execute_queries_from_csv(db_name, input_csv, output_csv)

    def close(self):
        self.neo.close()
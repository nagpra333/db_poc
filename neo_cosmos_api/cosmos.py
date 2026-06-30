from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
import os
from dotenv import load_dotenv
from azure.cosmos import PartitionKey
import json
from openai import AzureOpenAI
from prompt_loader import load_prompt

load_dotenv()
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

COSMOS_HOST = os.getenv("COSMOS_HOST")
COSMOS_KEY = os.getenv("COSMOS_KEY")
DATABASE_NAME = os.getenv("DATABASE_NAME")
CONTAINER_NAME = os.getenv("CONTAINER_NAME")

class CosmosGraph:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.client = None
            cls._instance.database = None
            cls._instance.container = None
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self.aoai = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_OPENAI_API_VERSION
        )
        self.model = AZURE_OPENAI_DEPLOYMENT
        self._initialized = True

    def initialize_graph(self, db_name, sample_data):
        for node in sample_data.get("nodes", []):
            self.upsert_node(
                db_name,
                node["label"],
                node["properties"]
            )
        # Insert relationships
        for rel in sample_data.get("relationships", []):
            self.upsert_relationship(
                db_name,
                rel["from"]["label"],
                rel["from"]["key"],
                rel["from"]["value"],
                rel["to"]["label"],
                rel["to"]["key"],
                rel["to"]["value"],
                rel["type"],
                rel.get("properties", {})
            )

    def connect(self):

        # Already connected
        if self.client is not None:
            return

        self.client = CosmosClient(
            COSMOS_HOST,
            COSMOS_KEY
        )
        if DATABASE_NAME:
            self.database = self.client.get_database_client(
                DATABASE_NAME
            )
            self.container = self.database.get_container_client(
                CONTAINER_NAME
            )
        print("=" * 80)
        print("Connected Successfully")
        print("=" * 80)
        print(f"Database : {DATABASE_NAME}")
        print(f"Container: {CONTAINER_NAME}")
        print("=" * 80)

    def create_database(self, db_name):
        self.database = self.client.create_database_if_not_exists(id = db_name)
        self.container = self.database.create_container_if_not_exists(id=CONTAINER_NAME, partition_key = PartitionKey(path="/type"))

    def list_databases(self):
        databases = []
        for db in self.client.list_databases():
            databases.append(db["id"])
        return databases

    def get_graph(self, db_name):
        return {
            "graphId": db_name,
            "status": self.get_graph_status(db_name),
            "container": CONTAINER_NAME
        }

    def get_graph_status(self, db_name):
        if self.client is None:
            return "OFFLINE"
        try:
            database = self.client.get_database_client(db_name)
            database.read()
            container = database.get_container_client(CONTAINER_NAME)
            container.read()
            return "ONLINE"
        except CosmosResourceNotFoundError:
            return "OFFLINE"
        except Exception:
            return "OFFLINE"

    def health(self, db_name):
        if self.client is None:
            return {
                "overall_status": "UNHEALTHY",
                "message": "Not connected"
            }

        try:
            database = self.client.get_database_client(db_name)
            database.read()  # raises CosmosResourceNotFoundError if db doesn't exist
            container = database.get_container_client(CONTAINER_NAME)
            container.read()  # raises CosmosResourceNotFoundError if container doesn't exist
            return {
                "overall_status": "HEALTHY",
                "system_message": "Database health check completed successfully."
            }

        except CosmosResourceNotFoundError as e:
            return {
                "overall_status": "UNHEALTHY",
                "message": f"Resource not found: {str(e)}"
            }

        except Exception as e:
            return {
                "overall_status": "UNHEALTHY",
                "message": f"Unable to establish database connection: {str(e)}"
            }

    def close(self):
        pass

    def create_unique_constraint(self, db_name, label, property_key):
        try:
            database = self.client.get_database_client(db_name)
            database.read()  # raises CosmosResourceNotFoundError if db doesn't exist
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        container.upsert_item({
            "id": f"{label}_{property_key}",
            "type": "constraint",
            "label": label,
            "property_key": property_key
        })

    def create_multiple_constraints(self, db_name, constraints):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        for constraint in constraints:
            container.upsert_item({
                "id": f"{constraint['label']}_{constraint['property_key']}",
                "type": "constraint",
                "label": constraint["label"],
                "property_key": constraint["property_key"]
            })

    def get_schema(self, db_name):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        constraints = []
        indexes = []
        for item in container.query_items(query="SELECT * FROM c", enable_cross_partition_query=True):
            doc_type = item.get("type")
            if doc_type == "constraint":
                constraints.append(item)
            elif doc_type == "index":
                indexes.append(item)
        return {
            "constraints": constraints,
            "indexes": indexes
        }

    def create_index(self, db_name, label, property_key):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        container.upsert_item({
            "id": f"{label}_{property_key}_idx",
            "type": "index",
            "label": label,
            "property_key": property_key
        })

    def create_multiple_indexes(self, db_name, indexes):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        for index in indexes:
            container.upsert_item({
                "id": f"{index['label']}_{index['property_key']}_idx",
                "type": "index",
                "label": index["label"],
                "property_key": index["property_key"]
            })
    
    def get_graph_statistics(self, db_name):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        node_count = 0
        relationship_count = 0
        for item in container.query_items(query="SELECT * FROM c", enable_cross_partition_query=True):
            doc_type = item.get("type")
            if doc_type == "node":
                node_count += 1
            elif doc_type == "relationship":
                relationship_count += 1

        return {
            "node_count": node_count,
            "relationship_count": relationship_count
        }

    def upsert_node(self, db_name, label, node):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")

        container = database.get_container_client(CONTAINER_NAME)
        item = {
            **node,
            "id": str(node["id"]),
            "type": "node",
            "label": label
        }
        container.upsert_item(item)

    def upsert_nodes(self, db_name, nodes):
        for node in nodes:
            self.upsert_node(
                db_name,
                node["label"],
                node["properties"]
            )
    
    def upsert_relationship(self, db_name, from_label, from_key, from_value, to_label, to_key, to_value, relationship, properties={}):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")
        container = database.get_container_client(CONTAINER_NAME)
        item = {
            "id": f"{from_value}_{relationship}_{to_value}",
            "type": "relationship",
            "relationship": relationship,
            "from_label": from_label,
            "from_key": from_key,
            "from_value": from_value,
            "to_label": to_label,
            "to_key": to_key,
            "to_value": to_value,
            **properties
        }
        container.upsert_item(item)

    def upsert_relationships(self, db_name, relationships):
        for rel in relationships:
            self.upsert_relationship(
                db_name,
                rel["from_label"],
                rel["from_key"],
                rel["from_value"],
                rel["to_label"],
                rel["to_key"],
                rel["to_value"],
                rel["relationship"],
                rel.get("properties",{})
            )
        
    def execute_query(self, db_name, query):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")

        container = database.get_container_client(CONTAINER_NAME)

        return list(
            container.query_items(
                query=query,
                enable_cross_partition_query=True
            )
        )

    def get_graph_schema_text(self, db_name):
        try:
            database = self.client.get_database_client(db_name)
            database.read()
        except CosmosResourceNotFoundError:
            raise ValueError(f"Database '{db_name}' does not exist.")

        container = database.get_container_client(CONTAINER_NAME)

        node_labels = set()
        relationship_types = set()

        for item in container.query_items(
            query="SELECT * FROM c",
            enable_cross_partition_query=True
        ):
            if item.get("type") == "node":
                node_labels.add(item.get("label"))
            elif item.get("type") == "relationship":
                relationship_types.add(item.get("relationship"))

        schema = self.get_schema(db_name)

        schema_text = "Node Labels:\n" + ", ".join(sorted(node_labels)) + "\n\n"
        schema_text += "Relationship Types:\n" + ", ".join(sorted(relationship_types)) + "\n\n"

        schema_text += "Constraints:\n"
        for constraint in schema["constraints"]:
            schema_text += f'- {constraint["label"]}({constraint["property_key"]})\n'

        schema_text += "\nIndexes:\n"
        for index in schema["indexes"]:
            schema_text += f'- {index["label"]}({index["property_key"]})\n'

        return schema_text
    
    def execute_natural_language(
    self,
    db_name,
    user_prompt,
    operation
):

        schema = self.get_graph_schema_text(db_name)

        system_prompt = load_prompt(
            f"cosmos_{operation}_prompt.txt"
        )

        system_prompt += "\n\nSchema:\n"
        system_prompt += schema

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

            response_format={
                "type": "json_object"
            }

        )

        generated = json.loads(
            response.choices[0].message.content
        )
        if operation == "read":
            query = generated["query"]
            try:
                database = self.client.get_database_client(db_name)
                database.read()
            except CosmosResourceNotFoundError:
                raise ValueError(f"Database '{db_name}' does not exist.")
            container = database.get_container_client(CONTAINER_NAME)
            records = list(
                container.query_items(
                    query=query,
                    enable_cross_partition_query=True
                )
            )
            relationships = []
            related_nodes = []
            node_records = [r for r in records if r.get("type") == "node"]
            rel_records = [r for r in records if r.get("type") == "relationship"]

            # Query returned nodes -> fetch relationships touching them (either direction)
            if node_records:
                node_ids = [str(r["id"]) for r in node_records]
                id_list = ", ".join(f"'{nid}'" for nid in node_ids)
                rel_query = (
                    f"SELECT * FROM c WHERE c.type = 'relationship' "
                    f"AND (c.from_value IN ({id_list}) OR c.to_value IN ({id_list}))"
                )
                relationships = list(
                    container.query_items(
                        query=rel_query,
                        enable_cross_partition_query=True
                    )
                )
            # Query returned relationships -> fetch nodes at both ends
            elif rel_records:
                endpoint_ids = set()
                for r in rel_records:
                    endpoint_ids.add(str(r["from_value"]))
                    endpoint_ids.add(str(r["to_value"]))
                id_list = ", ".join(f"'{nid}'" for nid in endpoint_ids)
                node_query = (
                    f"SELECT * FROM c WHERE c.type = 'node' AND c.id IN ({id_list})"
                )
                related_nodes = list(
                    container.query_items(
                        query=node_query,
                        enable_cross_partition_query=True
                    )
                )
                relationships = rel_records
            return {
                "prompt": user_prompt,
                "query": query,
                "records": node_records or related_nodes,
                "relationships": relationships
            }
        elif operation == "write":
            actions = generated["actions"]
            for action in actions:
                op = action["operation"]
                if op == "upsert_node":
                    self.upsert_node(
                        db_name,
                        action["label"],
                        action["properties"]
                    )
                elif op == "upsert_relationship":
                    self.upsert_relationship(
                        db_name,
                        action["from_label"],
                        action["from_key"],
                        action["from_value"],
                        action["to_label"],
                        action["to_key"],
                        action["to_value"],
                        action["relationship"],
                        action.get("properties", {})
                    )

            return {
                "prompt": user_prompt,
                "action": generated,
                "status": "SUCCESS"
            }
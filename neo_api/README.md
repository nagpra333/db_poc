## Graph API

### Prerequisites

Before starting the API, ensure you have:

- Python 3.10+
- Neo4j running locally (or update the connection details accordingly)
- An Azure OpenAI deployment

### Environment Configuration

Create a `.env` file in the root of the project.

### .env

```env
# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://decdp-msft-az-openai.cognitiveservices.azure.com
AZURE_OPENAI_KEY=YOUR_AZURE_OPENAI_KEY
AZURE_OPENAI_API_VERSION=2025-01-01-preview
AZURE_OPENAI_DEPLOYMENT=gpt-4o

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123
```

> **Note**
>
> Replace `YOUR_AZURE_OPENAI_KEY` with your Azure OpenAI API key. Avoid committing the `.env` file to source control.

---

#### Graph API - curl Commands

> Replace **newsampledb** with any graph name you want to create.

---

#### 1. Create Graph

```bash
curl -X POST http://localhost:8000/api/v1/graphs -H "Content-Type: application/json" -d '{"db_name":"newsampledb"}'
```

---

#### 2. Get Graph

```bash
curl http://localhost:8000/api/v1/graphs/newsampledb
```

---

#### 3. Health Check

```bash
curl http://localhost:8000/api/v1/graphs/newsampledb/health
```

---

#### 4. Create Single Constraint

```bash
curl -X POST http://localhost:8000/api/v1/graphs/newsampledb/schema/constraints -H "Content-Type: application/json" -d '{
    "label":"Person",
    "property_key":"id"
}'
```

---

#### 5. Create Multiple Constraints

```bash
curl -X POST http://localhost:8000/api/v1/graphs/newsampledb/schema/constraints/batch -H "Content-Type: application/json" -d '[
    {
        "label":"Person",
        "property_key":"id"
    },
    {
        "label":"Movie",
        "property_key":"id"
    }
]'
```

---

#### 6. Get Schema

```bash
curl http://localhost:8000/api/v1/graphs/newsampledb/schema
```

---

#### 7. Create Single Index

```bash
curl -X POST http://localhost:8000/api/v1/graphs/newsampledb/schema/indexes -H "Content-Type: application/json" -d '{
    "label":"Person",
    "property_key":"name"
}'
```

---

#### 8. Create Multiple Indexes

```bash
curl -X POST http://localhost:8000/api/v1/graphs/newsampledb/schema/indexes/batch -H "Content-Type: application/json" -d '[
    {
        "label":"Person",
        "property_key":"name"
    },
    {
        "label":"Movie",
        "property_key":"title"
    }
]'
```

---

#### 9. Get Graph Statistics

```bash
curl http://localhost:8000/api/v1/graphs/newsampledb/stats
```

---

#### 10. Batch Upsert Nodes

```bash
curl -X POST http://localhost:8000/api/v1/graphs/newsampledb/nodes/batch -H "Content-Type: application/json" -d '[
    {
        "label":"Person",
        "properties":{
            "id":5,
            "color": "yellow",
            "name":"Charlie"
        }
    },
    {
        "label":"Person",
        "properties":{
            "id":4,
            "name":"David"
        }
    }
]'
```

---

#### 11. Batch Upsert Relationships

```bash
curl -X POST http://localhost:8000/api/v1/graphs/newsampledb/relationships/batch -H "Content-Type: application/json" -d '[
    {
        "from_label":"Person",
        "from_key":"id",
        "from_value":3,
        "to_label":"Person",
        "to_key":"id",
        "to_value":4,
        "relationship":"KNOWS",
        "properties":{
            "since":2025
        }
    }
]'
```

---

#### 12. Read Query

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/graphs/newsampledb/read" -H "Content-Type: application/json" -d "{\"query\":\"Show all Person nodes\"}"
```

---
#### 13. Write Query
```bash
curl -X POST "http://127.0.0.1:8000/api/v1/graphs/newsampledb/write" -H "Content-Type: application/json" -d "{\"query\":\"Create a Person named john aged 25\"}"
```

#### Execution Order

1. Create Graph (`db_name = kg_db`)
2. Get Graph
3. Health Check

4. Create Single Constraint
5. Create Multiple Constraints
6. Get Schema
7. Create Single Index
8. Create Multiple Indexes
9. Get Graph Statistics
10. Batch Upsert Nodes
11. Batch Upsert Relationships
12. Read Query
13. Write Query

> **Important:** Every endpoint after the first must use the **same `db_name`** (`newsampledb` in this example). If you create `customer_graph` instead, replace every occurrence of `newsampledb` with `customer_graph`.

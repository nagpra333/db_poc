## Graph API - curl Commands

python -m uvicorn main:app --reload

> **Base URL**

```text
http://localhost:8000/api/v1
```

---

### 1. Create Graph

```bash
curl -X POST http://localhost:8000/api/v1/graphs
```

---

### 2. Get Graph

```bash
curl http://localhost:8000/api/v1/graphs/default
```

---

### 3. Health Check

```bash
curl http://localhost:8000/api/v1/graphs/default/health
```

---

### 4. Create Single Constraint

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/schema/constraints -H "Content-Type: application/json" -d '{
    "label":"Person",
    "property_key":"id"
}'
```

To check:
SHOW CONSTRAINTS

---

### 5. Create Multiple Constraints

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/schema/constraints/batch -H "Content-Type: application/json" -d '[
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

### 6. Get Schema

```bash
curl http://localhost:8000/api/v1/graphs/default/schema
```

---

### 7. Create Single Index

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/schema/indexes -H "Content-Type: application/json" -d '{
    "label":"Person",
    "property_key":"name"
}'
```

---

### 8. Create Multiple Indexes

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/schema/indexes/batch -H "Content-Type: application/json" -d '[
    {
        "label":"YO_Person",
        "property_key":"name"
    },
    {
        "label":"YO_Movie",
        "property_key":"title"
    }
]'
```

---

### 9. Get Graph Statistics

```bash
curl http://localhost:8000/api/v1/graphs/default/stats
```

---

### 10. Batch Upsert Nodes

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/nodes/batch -H "Content-Type: application/json" -d '[
    {
        "label":"Person",
        "properties":{
            "id":3,
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

# 11. Batch Upsert Relationships

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/relationships/batch -H "Content-Type: application/json" -d '[
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

### 12. Execute Query

```bash
curl -X POST http://localhost:8000/api/v1/graphs/default/query -H "Content-Type: application/json" -d '{
    "query":"MATCH (n) RETURN n"
}'
```

---

### Execution Order

1. Create Graph
2. Get Graph
3. Health Check
4. Create Constraint(s)
5. Get Schema
6. Create Index(es)
7. Get Statistics
8. Upsert Nodes
9. Upsert Relationships
10. Execute Query

> **Important:** Every time you restart the FastAPI server (`uvicorn`), the in-memory graph registry is reset. Run **Create Graph** again before invoking the other endpoints.


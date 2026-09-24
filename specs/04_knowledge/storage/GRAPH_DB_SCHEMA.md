# Kuzu / Neo4j Graph DB Schema

**Goal:** Answer structural questions Vector Search cannot:
- "Who calls `getUser`?"
- "What does `UserService` depend on?"
- "Show me the call chain from `API Route` to `Database`."

## 1. Node Labels & Properties

| Label | Properties | Description |
| :--- | :--- | :--- |
| `Repo` | `name`, `url`, `branch`, `framework_version` | Root |
| `File` | `path`, `language`, `size`, `hash` | Source file |
| `Symbol` | `name`, `type`, `signature`, `start_line`, `end_line`, `intent`, `pattern`, `complexity`, `tags[]` | Function, Class, Interface, etc. |
| `Config` | `path`, `content_hash`, `type` (package_json, tsconfig, dockerfile) | Config files |
| `Import` | `source`, `specifier`, `is_default`, `is_namespace` | Import statement details |

## 2. Relationship Types

| Type | Source | Target | Properties | Description |
| :--- | :--- | :--- | :--- | :--- |
| `CONTAINS` | `Repo` | `File` | | Repo owns file |
| `CONTAINS` | `File` | `Symbol` | | File defines symbol |
| `CONTAINS` | `File` | `Config` | | File is config |
| `IMPORTS` | `Symbol` | `Symbol` | `import_type` (default/named/namespace) | Symbol A imports Symbol B |
| `IMPORTS_FILE` | `Symbol` | `File` | `specifier` | Symbol imports whole file (side-effect) |
| `CALLS` | `Symbol` | `Symbol` | `call_type` (static/dynamic), `line` | Symbol A calls Symbol B |
| `INHERITS` | `Symbol` | `Symbol` | | Class A extends Class B |
| `IMPLEMENTS` | `Symbol` | `Symbol` | | Class implements Interface |
| `DECORATES` | `Symbol` | `Symbol` | `decorator_name` | Decorator usage |
| `USES_TYPE` | `Symbol` | `Symbol` | `context` (param/return/generic) | Type usage |

## 3. Key Cypher Queries (Kuzu Syntax)

```cypher
// 1. Find Callers of a function (Reverse Call Graph)
MATCH (caller:Symbol)-[:CALLS]-> (callee:Symbol {name: "getUser", file_path: "src/services/user.ts"})
RETURN caller.name, caller.file_path, caller.intent LIMIT 20

// 2. Get Dependencies of a File (Imports -> External Symbols)
MATCH (f:File {path: "src/api/users.ts"})-[:CONTAINS]->(s:Symbol)-[:IMPORTS]->(dep:Symbol)
RETURN DISTINCT dep.name, dep.file_path, dep.intent, dep.pattern

// 3. Architecture View: Module Dependencies
MATCH (f1:File)-[:CONTAINS]->(s1:Symbol)-[:IMPORTS|CALLS]->(s2:Symbol)<-[:CONTAINS]-(f2:File)
WHERE f1.path STARTS WITH "src/modules/" AND f2.path STARTS WITH "src/modules/"
AND f1 <> f2
RETURN f1.path AS from_module, f2.path AS to_module, count(*) AS coupling
ORDER BY coupling DESC

// 4. Find Implementation of Interface
MATCH (impl:Symbol)-[:IMPLEMENTS]->(iface:Symbol {name: "UserRepository"})
RETURN impl.name, impl.file_path, impl.intent

// 5. Impact Analysis: What breaks if I change `User` model?
MATCH (changed:Symbol {name: "User", type: "class"})<-[:USES_TYPE|CALLS|INHERITS*]-(affected:Symbol)
RETURN DISTINCT affected.name, affected.file_path, affected.type
```

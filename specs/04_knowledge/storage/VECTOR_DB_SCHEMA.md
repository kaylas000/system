# Qdrant Vector DB Schema

**Collection:** `code_chunks` (Primary)
**Sharding:** By `repo` (tenant) or `language` (performance).

## 1. Vector Configuration
```yaml
vectors:
  size: 768 # nomic-embed-text / text-embedding-3-small
  distance: Cosine
  hnsw_config:
    m: 16
    ef_construct: 100
    full_scan_threshold: 10000
quantization:
  scalar:
    type: int8
    always_ram: true
```

## 2. Payload Indexes (Critical for Filtering Speed)
```json
[
  {"field_name": "repo", "field_schema": "keyword"},
  {"field_name": "language", "field_schema": "keyword"},
  {"field_name": "symbol_type", "field_schema": "keyword"},
  {"field_name": "framework_version", "field_schema": "keyword"},
  {"field_name": "pattern", "field_schema": "keyword"},
  {"field_name": "tags", "field_schema": "keyword"},
  {"field_name": "file_path", "field_schema": "text"}, // For prefix filtering
  {"field_name": "symbol_name", "field_schema": "keyword"},
  {"field_name": "chunk_type", "field_schema": "keyword"} // symbol, file_summary, config
]
```

## 3. Payload Structure (Per Point)
```json
{
  "id": "vercel_next.js#packages/next/server/app/render.ts#renderToReadableStream#145",
  "vector": [0.1, -0.3, ...],
  "payload": {
    "content": "# renderToReadableStream (function)\n# Intent: Streams React component output...\n# Pattern: Adapter\n# Tags: streaming, react, ssr\n# File: packages/next/server/app/render.ts\n\nasync function renderToReadableStream(...) { ... }",
    "repo": "vercel_next.js",
    "repo_url": "https://github.com/vercel/next.js",
    "branch": "canary",
    "file_path": "packages/next/server/app/render.ts",
    "symbol_name": "renderToReadableStream",
    "symbol_type": "function",
    "language": "typescript",
    "framework_version": "nextjs@14.2.0",
    "chunk_type": "symbol",
    "start_line": 145,
    "end_line": 210,
    "parent": "",
    "decorators": [],
    "intent": "Streams React component output to a readable web stream for SSR.",
    "pattern": "Adapter",
    "complexity": "O(n) - proportional to component tree size",
    "side_effects": ["Network Request (streaming)"],
    "dependencies_explained": ["React DOM Server: Core rendering", "Web Streams API: Standard streaming"],
    "usage_example": "const stream = await renderToReadableStream(jsx, { ... })",
    "potential_issues": ["Backpressure handling", "Client hydration mismatch"],
    "tags": ["streaming", "ssr", "react", "server-components"]
  }
}
```

## 4. Additional Collections
- `docs_chunks`: Documentation (Markdown split by headers). Payload: `header_path`, `url`.
- `error_fixes`: Mined from Fix Loop. Payload: `error_signature`, `fix_diff`, `root_cause`.
- `patterns`: Curated Golden Examples. Payload: `pattern_name`, `quality_score`.

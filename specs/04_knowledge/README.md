# Ответ №4: Knowledge Spec — Конвейер Знаний (RAG, Graph DB, Ingestion, Retrieval)

Это **мозг** системы. Без качественного контекста агент — «джуниор, гуглящий StackOverflow». С этим слоем — «синьор, знающий твой код и лучшие практики наизусть».

Сохрани в структуру:
```text
specs/04_knowledge/
├── KNOWLEDGE_ARCH.md
├── ingestion/
│   ├── INGESTION_PIPELINE.py
│   ├── PARSERS.py
│   ├── CHUNKING_STRATEGIES.py
│   ├── ENRICHMENT.py
│   └── EMBEDDING.py
├── storage/
│   ├── VECTOR_DB_SCHEMA.md
│   ├── GRAPH_DB_SCHEMA.md
│   ├── QDRANT_CLIENT.py
│   └── KUZU_CLIENT.py
├── retrieval/
│   ├── RETRIEVAL_STRATEGIES.py
│   ├── HYBRID_SEARCH.py
│   ├── GRAPH_TRAVERSAL.py
│   └── RERANKING.py
└── cli/
    └── INGEST_CLI.py
```

---


<!-- ... файлы спецификации лежат в этой папке ... -->

### 🎯 Инструкция для Агента-Разработчика (Prompt Snippet для Ответа №4)

> **CONTEXT FOR NEXT STEP (Vertical Spec Implementation):**
> Ты реализуешь `specs/04_knowledge/`.
> 1. Создай `kernel/knowledge/` пакет: `ingestion/` (pipeline, parsers, chunking, enrichment), `storage/` (qdrant_client, kuzu_client), `retrieval/` (engine, strategies, reranking).
> 2. Настрой `Dockerfile.knowledge` для запуска инжеста (нужен `tree-sitter` libs, `kuzu`, `qdrant-client`, `sentence-transformers`).
> 3. Реализуй `MultiLanguageParser` с Tree-sitter queries для TS, Python, Go (минимальный набор).
> 4. Реализуй `ChunkingStrategy` (Symbol + File Summary + Config).
> 5. Реализуй `EnrichmentPipeline` с батчингом и `LiteLLMClient`.
> 4. Реализуй `QdrantKB` (Hybrid Search, Payload Indexes) и `KuzuGraph` (Schema + Cypher Queries).
> 5. Реализуй `RetrievalEngine` с 3 стратегиями: `coding`, `planning`, `fixing`.
> 6. **Интеграционный тест (`tests/knowledge/test_ingestion_retrieval.py`):**
>    *   Запуск `IngestionPipeline` на маленьком тестовом репо (фикстура в `tests/fixtures/sample_repo`).
>    *   Проверка: В Qdrant появились поинты с payload `intent`, `pattern`, `tags`.
>    *   Проверка: В Kuzu есть ноды `Symbol`, `File` и ребра `CALLS`, `IMPORTS`.
>    *   Тест Retrieval: Запрос "How to stream response in Next.js?" -> возвращает чанк про `renderToReadableStream` с высоким скором.
>    *   Тест Graph: `query_callers("getUser")` -> возвращает список вызовов.

---

### ✅ Чек-лист готовности Knowledge Spec (Definition of Done для Ответа №4)

- [ ] `MultiLanguageParser` извлекает символы (func, class, interface) для TS, Python, Go.
- [ ] `ChunkingStrategy` создает 3 типа чанков: Symbol, File Summary, Config.
- [ ] `EnrichmentPipeline` батчами обогащает чанки через LLM (Intent, Pattern, Tags).
- [ ] `QdrantKB` создает коллекцию с правильными HNSW/Quantization/Indexes.
- [ ] `KuzuGraph` создает схему и загружает ноды/ребра без дубликатов (MERGE).
- [ ] `RetrievalEngine` реализует 3 стратегии с правильными фильтрами по `vertical_manifest`.
- [ ] Reranking (Cross-Encoder или LLM) улучшает Precision@K.
- [ ] CLI `ingest` успешно обрабатывает тестовый репозиторий.

---

---

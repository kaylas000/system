# Knowledge Base Architecture Specification

**Goal:** Provide agents with *precise, relevant, structured* context for code generation, fixing, and architecture decisions.

## 1. Core Principles

1.  **Code-Aware Chunking > Text Chunking.** Chunk by AST nodes (Function, Class, Interface), not by characters/tokens.
2.  **Dual Storage:** Vector DB (Semantic Search) + Graph DB (Structural Relationships).
3.  **Enrichment is Mandatory.** Raw code chunks are low value. Every chunk must have: `intent`, `pattern`, `dependencies`, `complexity`, `side_effects`.
4.  **Versioned Knowledge.** Chunks tagged with `framework_version`, `language_version`. Retrieval filters by current project stack.
5.  **Continuous Ingestion.** Git Webhooks → Ingestion Pipeline → DB. Zero manual steps.

## 2. Data Flow (Mermaid)

```mermaid
graph LR
    subgraph Sources
        GH[GitHub/GitLab Repos]
        Docs[Documentation Sites]
        Local[Local Projects]
        Success[Successful Generations]
    end

    subgraph Ingestion Pipeline
        Trigger[Trigger: Webhook / Cron / CLI]
        Clone[Clone / Checkout]
        Parse[AST Parsing\nTree-sitter / LSP]
        Chunk[Semantic Chunking\nSymbol-based]
        Enrich[LLM Enrichment\nIntent, Pattern, Issues]
        Embed[Embedding\nnomic-embed-text / 3-small]
        GraphBuild[Graph Extraction\nCalls, Imports, Inherits]
    end

    subgraph Storage
        Qdrant[(Qdrant Vector DB\nPayload: Metadata + Content)]
        Kuzu[(Kuzu Graph DB\nNodes: File, Class, Func\nEdges: CALLS, IMPORTS, CONTAINS)]
        S3[(Object Store\nRaw .md Chunks for Debug)]
    end

    subgraph Retrieval
        Agent[Agent Node\n(Coder, Fixer, Planner)]
        Router{Retrieval Router}
        Hybrid[Hybrid Search\nVector + BM25 + Filters]
        GraphQ[Graph Traversal\nCallers, Callees, Deps]
        Rerank[Cross-Encoder Rerank]
    end

    Sources --> Trigger
    Trigger --> Clone --> Parse --> Chunk
    Chunk --> Enrich
    Enrich --> Embed
    Enrich --> GraphBuild
    Embed --> Qdrant
    GraphBuild --> Kuzu
    Chunk --> S3

    Agent --> Router
    Router --> Hybrid
    Router --> GraphQ
    Hybrid --> Rerank
    GraphQ --> Rerank
    Rerank --> Agent
```

## 3. Chunk Types (The Unit of Knowledge)

| Chunk Type | Source | Size | Use Case |
| :--- | :--- | :--- | :--- |
| **Symbol Chunk** | Function, Class, Interface, Method, Hook, Component | 50-500 lines | Primary retrieval unit. "How to write a Prisma model?" |
| **File Summary** | Whole File (Imports, Exports, Top-level symbols) | 20-50 lines | "What does `user_service.py` do?" |
| **Config Chunk** | `package.json`, `tsconfig.json`, `prisma/schema.prisma`, `Dockerfile` | Whole file | "Next.js 14 TS config best practices" |
| **Doc Chunk** | Markdown docs (Headings H1/H2/H3) | Section | "Vercel Deployment Guide" |
| **Pattern Chunk** | Curated "Golden Examples" (Manual/Extracted) | Variable | Few-shot prompts for Planner/Coder |
| **Error Fix Chunk** | `Error Signature` -> `Fix Diff` (Mined from successful Fix Loops) | Small | Fixer Agent: "TypeError X -> Add null check" |

## 4. Retrieval Strategies by Agent

| Agent | Primary Strategy | Secondary | Filters |
| :--- | :--- | :--- | :--- |
| **Planner** | `workspace_symbols` (Graph) + Doc Search | Pattern Chunks (Architecture) | `type: architecture`, `framework: nextjs` |
| **Coder** | Hybrid Search (Vector + BM25) on Symbol Chunks | Graph: `CALLS` / `IMPORTS` of current symbol | `language: ts`, `framework: nextjs`, `version: 14` |
| **Fixer** | Error Signature Match (Vector) + `files_to_fix` context | Graph: Dependencies of broken file | `type: fix_pattern`, `error_type: TypeError` |
| **Reviewer** | Graph Traversal (Architecture) + Convention Docs | Cross-file Symbol Search | `type: convention`, `layer: domain` |

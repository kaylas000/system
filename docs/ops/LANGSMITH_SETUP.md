# LangSmith (написано агентом; в ТЗ файла нет — MISSING_FILES #26)

Трассировка LangGraph в LangSmith включается одной переменной:

```bash
AUTOGEN_OBSERVABILITY__LANGSMITH_API_KEY=lsv2_...
AUTOGEN_OBSERVABILITY__LANGSMITH_PROJECT=autogen-prod   # по умолчанию autogen-kernel
```

При старте сервиса (`python -m kernel.main serve`) `kernel.observability.setup_langsmith` выставляет
`LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` (если они не заданы явно), и LangGraph
сам отправляет в LangSmith каждый узел графа. Код ядра для этого не меняется.

Что видно: дерево run → узлы (`planner`, `coder`, `verifier`, ...) → входы и выходы узлов. Вызовы LLM идут
через LiteLLM, а не через LangChain, поэтому в LangSmith они видны только как часть узла. Токены, цена
и задержка LLM есть в Prometheus (`autogen_llm_*`) и в трейсах OpenTelemetry (`llm.chat`).

LangSmith и OpenTelemetry независимы, можно включить оба. Для self-hosted варианта задайте `LANGSMITH_ENDPOINT`.

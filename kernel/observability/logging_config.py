"""
Structured logging (``specs/06_ops/observability/LOGGING_CONFIG.py`` — no content in the spec;
written by the agent). JSON lines with ``run_id`` / ``node`` from context variables, so logs of
concurrent runs can be separated in Loki/Elastic.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

run_id_var: ContextVar[str] = ContextVar("autogen_run_id", default="")
node_var: ContextVar[str] = ContextVar("autogen_node", default="")
request_id_var: ContextVar[str] = ContextVar("autogen_request_id", default="")

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.node = node_var.get()
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "node", "request_id"):
            value = getattr(record, key, "")
            if value:
                data[key] = value
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key not in data and key not in ("run_id", "node", "request_id"):
                data[key] = value if isinstance(value, str | int | float | bool | None) else repr(value)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_logs: bool = True, stream: Any = None) -> logging.Handler:
    """Install one handler on the root logger (idempotent: replaces a previous autogen handler)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_autogen", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler._autogen = True  # type: ignore[attr-defined]
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(run_id)s %(node)s] %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())
    return handler

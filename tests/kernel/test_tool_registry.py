from __future__ import annotations

from typing import Any, ClassVar

from kernel.protocols import ToolResult
from kernel.tools import ToolRegistry


class EchoTool:
    name = "echo"
    description = "Echo args"
    parameters_json_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, sandbox_id: str, args: dict[str, Any], state: Any) -> ToolResult:
        if args.get("crash"):
            raise RuntimeError("kaput")
        return ToolResult(success=bool(args.get("text")), data={"sandbox": sandbox_id, **args}, error=None)


async def test_registry() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())
    assert reg.names == ["echo"]
    assert reg.get_all_schemas()[0]["function"]["name"] == "echo"

    ok = await reg.execute("echo", {"text": "hi"}, {"sandbox_id": "sb1"})  # type: ignore[typeddict-item]
    assert ok.success and ok.data["sandbox"] == "sb1"
    assert not (await reg.execute("missing", {}, {})).success  # type: ignore[typeddict-item]
    assert (await reg.execute("echo", {"text": "x"}, {})).error == "No sandbox_id in state or args"  # type: ignore[typeddict-item]
    crashed = await reg.execute("echo", {"crash": True}, {}, sandbox_id="sb2")  # type: ignore[typeddict-item]
    assert not crashed.success and "kaput" in (crashed.error or "")
    await reg.execute("echo", {}, {}, sandbox_id="sb2")  # type: ignore[typeddict-item]

    m = reg.get_metrics()
    assert m["calls"] == {"echo": 3} and m["errors"] == {"echo": 2}
    assert m["avg_latency_ms"]["echo"] >= 0

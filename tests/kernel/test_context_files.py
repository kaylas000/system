from __future__ import annotations

import base64
import tarfile
from pathlib import Path
from typing import Any

from kernel.config import Settings
from kernel.graph import build_graph
from kernel.graph.nodes._common import decode_context_files
from kernel.persistence import memory_checkpointer
from kernel.protocols import GenerateRequest
from kernel.runner import start_run
from kernel.sandbox import LocalSandbox
from kernel.state import RunStatus
from tests.kernel.fakes import FakeLLM, FakeVertical, code, plan


class OpenFilesVertical(FakeVertical):
    def get_coder_prompt(self, state: Any, task: Any) -> str:
        return "open: " + ",".join(f["path"] for f in state.get("open_files", []))


def b64(text: str | bytes) -> str:
    return base64.b64encode(text.encode() if isinstance(text, str) else text).decode()


def test_decode_context_files_skips_unsafe_binary_and_invalid() -> None:
    files, warnings = decode_context_files(
        [
            {"name": "contracts/openapi.yaml", "content_base64": b64("openapi: 3.0.3\n")},
            {"name": "../etc/passwd", "content_base64": b64("x")},
            {"name": "img.png", "content_base64": b64(b"\x89PNG\xff\xfe")},
            {"name": "bad.txt", "content_base64": "not base64!!"},
        ]
    )
    assert [f.path for f in files] == ["contracts/openapi.yaml"]
    assert files[0].content == "openapi: 3.0.3\n"
    assert len(warnings) == 3


async def test_context_files_reach_workspace_planner_and_coder(tmp_path: Path, settings: Settings) -> None:
    llm = FakeLLM([plan(("t1", [])), code("a.txt", "one")])
    graph = build_graph(
        OpenFilesVertical(),
        llm=llm,
        sandbox=LocalSandbox(tmp_path / "sb"),
        settings=settings,
        checkpointer=memory_checkpointer(),
    )
    request = GenerateRequest(
        prompt="build a client",
        context_files=[{"name": "contracts/openapi.yaml", "content_base64": b64("paths: {}\n")}],
    )
    _, result = await start_run(graph, request, settings=settings)
    assert result["status"] == RunStatus.COMPLETED
    planner_user = llm.calls[0]["messages"][1].content
    assert "CONTEXT FILES" in planner_user and "--- contracts/openapi.yaml ---" in planner_user
    coder_system = llm.calls[1]["messages"][0].content
    assert coder_system == "open: contracts/openapi.yaml"
    with tarfile.open(result["final_artifact"].artifact_path) as tar:
        names = tar.getnames()
    assert any(n.endswith("contracts/openapi.yaml") for n in names)

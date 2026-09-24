"""
Generic verification gates used by verticals.

* ``CommandGate`` - one shell command (a row of ``verification/GATES.yaml``) with an
  optional output parser that extracts ``issues`` / ``files_to_fix`` for the fixer.
* ``SkillValidationGate`` - the ``validation`` commands and ``validate`` hook of the
  skill that produced the task. It is a *gate* (re-run after every fix), not a
  one-off step inside the executor as in ``SKILL_EXECUTOR.py`` - otherwise a fix
  would never be re-validated (``specs/ISSUES.md`` SK-06).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field

from ..protocols import ISandbox
from ..state import AgentState, VerificationGateResult, VerificationGateStatus
from .definition import HookContext, LoadedSkill
from .templates import TemplateEngine

Parser = Callable[[VerificationGateResult], VerificationGateResult]
TIMEOUT_EXIT = 124


class GateConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    command: str
    runs_on_every_task: bool = False
    timeout_sec: int = 120
    parser: str | None = None
    severity: str = "error"  # error -> FAILED blocks; warning/info -> reported, does not block
    tags: list[str] = Field(default_factory=list)
    workdir: str = "."  # relative to the workspace


class CommandGate:
    def __init__(self, config: GateConfig, parser: Parser | None = None) -> None:
        self.config = config
        self.id = config.id
        self.name = config.name
        self.description = config.description
        self.runs_on_every_task = config.runs_on_every_task
        self.tags = list(config.tags)
        self.parser = parser

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, state: AgentState
    ) -> VerificationGateResult:
        cfg = self.config
        workdir = workspace if cfg.workdir in ("", ".") else f"{workspace.rstrip('/')}/{cfg.workdir.strip('/')}"
        res = await sandbox.exec(sandbox_id, cfg.command.strip(), workdir=workdir, timeout_sec=cfg.timeout_sec)
        ok = res.exit_code == 0
        blocking = cfg.severity == "error"
        stderr = res.stderr
        if res.exit_code == TIMEOUT_EXIT:
            stderr = f"{stderr}\n[gate timed out after {cfg.timeout_sec}s]".strip()
        result = VerificationGateResult(
            gate_id=cfg.id,
            name=cfg.name,
            status=VerificationGateStatus.PASSED if ok or not blocking else VerificationGateStatus.FAILED,
            command=cfg.command.strip(),
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=stderr if ok or blocking else f"[{cfg.severity}, non-blocking] {stderr}",
            duration_ms=res.duration_ms,
        )
        if self.parser is not None and not ok:
            try:
                result = self.parser(result)
            except Exception:  # a broken parser must not hide the raw output
                pass
        return result


def gates_from_config(
    rows: Iterable[dict[str, Any]], parsers: dict[str, Parser] | None = None
) -> dict[str, CommandGate]:
    parsers = parsers or {}
    gates: dict[str, CommandGate] = {}
    for row in rows:
        cfg = GateConfig.model_validate(row)
        if cfg.id in gates:
            raise ValueError(f"duplicate gate id {cfg.id!r}")
        if cfg.parser and cfg.parser not in parsers:
            raise ValueError(f"gate {cfg.id!r}: unknown parser {cfg.parser!r}")
        gates[cfg.id] = CommandGate(cfg, parsers.get(cfg.parser) if cfg.parser else None)
    return gates


class SkillValidationGate:
    runs_on_every_task = False

    def __init__(self, skill: LoadedSkill, inputs: dict[str, Any], tool_registry: Any = None, timeout_sec: int = 300):
        self.skill = skill
        self.inputs = inputs
        self.tool_registry = tool_registry
        self.timeout_sec = timeout_sec
        self.id = f"skill:{skill.id}"
        self.name = f"Skill validation: {skill.id}"
        self.description = f"validation commands of skill {skill.id}"

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, state: AgentState
    ) -> VerificationGateResult:
        start = time.perf_counter()
        engine = TemplateEngine([], state.get("skill_outputs") or {})
        try:
            inputs = self.skill.validate_inputs(self.inputs)
        except ValueError:
            inputs = dict(self.inputs)
        outputs: list[str] = []
        for raw in self.skill.validation:
            cmd = engine.render_string(raw, inputs)
            res = await sandbox.exec(sandbox_id, cmd, workdir=workspace, timeout_sec=self.timeout_sec)
            outputs.append(f"$ {cmd}\n{res.stdout[-4000:]}")
            if res.exit_code != 0:
                return VerificationGateResult(
                    gate_id=self.id,
                    name=self.name,
                    status=VerificationGateStatus.FAILED,
                    command=cmd,
                    exit_code=res.exit_code,
                    stdout="\n".join(outputs),
                    stderr=res.stderr,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )
        hook = self.skill.validate_hook
        if hook is not None:
            ctx = HookContext(
                skill_id=self.skill.id,
                skill_dir=self.skill.path,
                inputs=inputs,
                sandbox=sandbox,
                sandbox_id=sandbox_id,
                workspace=workspace,
                state=dict(state),
                tool_registry=self.tool_registry,
            )
            failed = [r for r in await hook(ctx) if r.status == VerificationGateStatus.FAILED]
            if failed:
                first = failed[0]
                return first.model_copy(
                    update={"gate_id": self.id, "duration_ms": int((time.perf_counter() - start) * 1000)}
                )
        return VerificationGateResult(
            gate_id=self.id,
            name=self.name,
            status=VerificationGateStatus.PASSED,
            command=" && ".join(self.skill.validation) or "(validate hook)",
            exit_code=0,
            stdout="\n".join(outputs),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

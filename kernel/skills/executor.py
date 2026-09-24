"""
Skill executor (port of ``specs/03_skills/skill/SKILL_EXECUTOR.py``).

Lifecycle: validate inputs -> pre_render hook -> render templates -> post_render
hook -> apply to sandbox -> post_scripts. Validation commands run as a gate
(``SkillValidationGate``) so they are repeated after every fix.

Fixes vs. the spec (``specs/ISSUES.md`` SK-06):
* ``sandbox.write_files(file_changes)`` was called without ``sandbox_id``/``workdir``;
* skill dir was taken from ``state["vertical_manifest"]["skills_dir"]`` - now from the skill;
* ``StrictUndefined`` was used without import; ``outputs`` were returned as raw templates;
* a failing post_script was ignored ("let validation catch it") - with no validation
  a broken ``pnpm install`` passed silently; now it raises ``SkillExecutionError``;
* post_scripts go straight to ``ISandbox.exec`` (skill commands are trusted repo
  content, not LLM output, so the shell allowlist does not apply);
* hook inputs are copied, hooks cannot mutate the caller's dict or the state.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..protocols import ISandbox, SkillResult
from ..state import AgentState, FileChange
from .definition import HookContext, LoadedSkill
from .templates import TemplateEngine, render_skill_templates, safe_relative

POST_SCRIPT_TIMEOUT = 900


class SkillExecutionError(RuntimeError):
    pass


class ScriptResult(BaseModel):
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


class SkillRunReport(BaseModel):
    scripts: list[ScriptResult] = Field(default_factory=list)


class SkillExecutor:
    def __init__(
        self,
        skill_def: LoadedSkill,
        tool_registry: Any = None,
        shared_template_dirs: list[Path] | None = None,
        post_script_timeout: int = POST_SCRIPT_TIMEOUT,
    ) -> None:
        self.skill_def = skill_def
        self.tool_registry = tool_registry
        self.shared_template_dirs = shared_template_dirs or []
        self.post_script_timeout = post_script_timeout

    async def execute(
        self,
        sandbox: ISandbox,
        sandbox_id: str,
        workspace: str,
        inputs: dict[str, Any],
        state: AgentState | None = None,
    ) -> SkillResult:
        skill = self.skill_def
        start = time.perf_counter()
        view: dict[str, Any] = dict(state or {})
        skill_outputs: dict[str, dict[str, Any]] = dict(view.get("skill_outputs") or {})

        values = skill.validate_inputs(inputs)

        def ctx(current: dict[str, Any]) -> HookContext:
            return HookContext(
                skill_id=skill.id,
                skill_dir=skill.path,
                inputs=dict(current),
                sandbox=sandbox,
                sandbox_id=sandbox_id,
                workspace=workspace,
                state=view,
                tool_registry=self.tool_registry,
            )

        if skill.pre_render is not None:
            extra = await skill.pre_render(ctx(values))
            if extra:
                values = {**values, **extra}

        changes = render_skill_templates(
            skill.template_path,
            values,
            skill_outputs,
            extra_dirs=self.shared_template_dirs,
            assets_dir=skill.path / "assets",
        )
        if skill.post_render is not None:
            changes = list(await skill.post_render(ctx(values), list(changes)))
        changes = [c.model_copy(update={"path": safe_relative(c.path)}) for c in changes]
        changes = await self._mark_updates(sandbox, sandbox_id, workspace, changes)

        if changes:
            await sandbox.write_files(sandbox_id, changes, workspace)

        engine = TemplateEngine([], skill_outputs)
        report = SkillRunReport()
        for raw in skill.post_scripts:
            cmd = engine.render_string(raw, values).strip()
            res = await sandbox.exec(
                sandbox_id, cmd, workdir=workspace, env=dict(skill.env) or None, timeout_sec=self.post_script_timeout
            )
            report.scripts.append(
                ScriptResult(
                    command=cmd,
                    exit_code=res.exit_code,
                    stdout=res.stdout[-4000:],
                    stderr=res.stderr[-4000:],
                    duration_ms=res.duration_ms,
                )
            )
            if res.exit_code != 0:
                raise SkillExecutionError(
                    f"skill {skill.id}: post_script `{cmd}` failed with exit code {res.exit_code}: "
                    f"{(res.stderr or res.stdout).strip()[-1500:]}"
                )

        outputs = {
            k: engine.render_string(v, values) if isinstance(v, str) and ("{{" in v or "{%" in v) else v
            for k, v in skill.outputs.items()
        }
        return SkillResult(
            file_changes=changes,
            outputs=outputs,
            metadata={
                "skill_id": skill.id,
                "skill_version": skill.version,
                "inputs": values,
                "duration_ms": int((time.perf_counter() - start) * 1000),
                "scripts": [s.model_dump() for s in report.scripts],
                "files_changed": len(changes),
            },
        )

    @staticmethod
    async def _mark_updates(
        sandbox: ISandbox, sandbox_id: str, workspace: str, changes: list[FileChange]
    ) -> list[FileChange]:
        """Set ``action=update`` for files that already exist in the sandbox (one exec for all)."""
        candidates = [c for c in changes if c.action == "create"]
        if not candidates:
            return changes
        script = "; ".join(f"test -e {shlex.quote('./' + c.path)} && echo {shlex.quote(c.path)}" for c in candidates)
        res = await sandbox.exec(sandbox_id, script + "; true", workdir=workspace)
        existing = set(res.stdout.splitlines())
        return [c.model_copy(update={"action": "update"}) if c.path in existing else c for c in changes]

# specs/03_skills/skill/SKILL_EXECUTOR.py
"""
Skill Executor: Orchestrates the full Skill Lifecycle.
Input: SkillDef, Inputs, Context (Sandbox, State, Tools)
Output: SkillOutput (FileChanges, Outputs, VerificationResults)
"""

from __future__ import annotations
import time
from typing import Dict, Any, List, Optional
from pathlib import Path

from kernel.protocols import ISandbox, ISkillExecutor, SkillDef, VerificationGateResult, VerificationGateStatus
from kernel.state import FileChange, AgentState
from kernel.tools.registry import ToolRegistry
from .TEMPLATE_ENGINE import render_skill_templates
from .SKILL_DEFINITION import HookContext


class SkillExecutor(ISkillExecutor):
    def __init__(self, skill_def: SkillDef, tool_registry: ToolRegistry):
        self.skill_def = skill_def
        self.tool_registry = tool_registry

    async def execute(
        self, sandbox: ISandbox, sandbox_id: str, workspace: str, inputs: Dict[str, Any], state: AgentState
    ) -> Dict[str, Any]:  # Returns dict matching SkillOutput

        start_time = time.time()
        workspace_path = Path(workspace)
        skill_dir = Path(state["vertical_manifest"]["skills_dir"]) / self.skill_def.id

        # 1. Validate Inputs
        validated_inputs = self.skill_def.validate_inputs(inputs)

        # 2. Prepare Context for Hooks
        hook_ctx = HookContext(
            skill_id=self.skill_def.id,
            skill_dir=skill_dir,
            inputs=validated_inputs,
            sandbox=sandbox,
            sandbox_id=sandbox_id,
            workspace=workspace,
            state=state,
            tool_registry=self.tool_registry,
        )

        # 3. PRE_RENDER HOOK
        if self.skill_def._pre_render:
            extra_inputs = await self.skill_def._pre_render(hook_ctx)
            validated_inputs.update(extra_inputs)

        # 4. RENDER TEMPLATES
        # Collect outputs from previous skills for `skill_output()` global
        prev_outputs = state.get("skill_outputs", {})

        file_changes = await render_skill_templates(
            skill_dir=skill_dir, inputs=validated_inputs, skill_outputs=prev_outputs, workspace_root=workspace_path
        )

        # 5. POST_RENDER HOOK
        if self.skill_def._post_render:
            file_changes = await self.skill_def._post_render(hook_ctx, file_changes)

        # 6. APPLY FILE CHANGES TO SANDBOX
        if file_changes:
            await sandbox.write_files(file_changes)

        # 7. RUN POST_SCRIPTS (Shell commands)
        script_results = []
        for script in self.skill_def.post_scripts:
            # Interpolate inputs into script
            rendered_script = self._interpolate(script, validated_inputs)
            shell_tool = self.tool_registry.get("shell")
            res = await shell_tool.execute(
                sandbox_id,
                {"command": rendered_script, "workdir": workspace, "env": self.skill_def.env, "timeout": 300},
                state,
            )
            script_results.append({"script": script, "result": res.data})
            if not res.success:
                # Don't fail fast here, let validation gates catch it
                pass

        # 8. VALIDATION (Shell commands from skill.yaml)
        validation_results = []
        for val_cmd in self.skill_def.validation:
            rendered_cmd = self._interpolate(val_cmd, validated_inputs)
            shell_tool = self.tool_registry.get("shell")
            res = await shell_tool.execute(
                sandbox_id, {"command": rendered_cmd, "workdir": workspace, "timeout": 120}, state
            )
            vr = VerificationGateResult(
                gate_id=f"{self.skill_def.id}_validation",
                name=f"Validation: {rendered_cmd[:50]}",
                status=VerificationGateStatus.PASSED if res.success else VerificationGateStatus.FAILED,
                command=rendered_cmd,
                exit_code=res.data.get("exit_code", -1),
                stdout=res.data.get("stdout", ""),
                stderr=res.data.get("stderr", ""),
                duration_ms=res.data.get("duration_ms", 0),
            )
            validation_results.append(vr)

        # 9. CUSTOM VALIDATION HOOK
        if self.skill_def._validate:
            custom_results = await self.skill_def._validate(hook_ctx)
            validation_results.extend(custom_results)

        # 10. Prepare Outputs
        outputs = self.skill_def.outputs_schema.copy()
        # Allow hooks to inject outputs via context? Or just static.
        # For dynamic outputs, hook could write to a known file and we read it.

        duration = int((time.time() - start_time) * 1000)

        return {
            "file_changes": file_changes,
            "outputs": outputs,
            "verification_results": validation_results,
            "metadata": {
                "duration_ms": duration,
                "scripts_run": len(self.skill_def.post_scripts),
                "files_changed": len(file_changes),
            },
        }

    def _interpolate(self, template: str, inputs: Dict) -> str:
        """Simple {{var}} interpolation for shell commands."""
        from jinja2 import Environment, BaseLoader

        env = Environment(loader=BaseLoader, undefined=StrictUndefined)
        return env.from_string(template).render(**inputs)

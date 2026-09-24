# specs/05_vertical_saas_web/VERTICAL_IMPL.py
"""
Concrete Vertical Implementation for SaaS Web.
Implements IVertical protocol with SaaS-specific logic.
"""

from __future__ import annotations
from typing: Dict, List, Any, Optional
from pathlib import Path

from kernel.protocols import IVertical, VerticalManifest, SkillDef, IVerificationGate, IVertical, ISkillExecutor
from kernel.state import AgentState, Task, VerificationGateResult, VerificationGateStatus, FileChange
from kernel.tools.registry import ToolRegistry
from kernel.skills.registry import SkillRegistry
from kernel.skills.engine import TemplateEngine
from .verification.GATES import SAAS_GATES, SaaSGateParser
from .prompts.compiler import PromptCompiler

class SaaSWebVertical(IVertical):
    def __init__(self, manifest: VerticalManifest, tool_registry: ToolRegistry, skill_registry: SkillRegistry):
        self.manifest = manifest
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry
        self.prompt_compiler = PromptCompiler(Path(manifest.prompts_dir))
        # Gate Instances
        self.gates = {g.id: g for g in SAAS_GATES}
        self.gate_parser = SaaSGateParser()

    @property
    def skills(self) -> Dict[str, SkillDef]:
        return self.skill_registry._skills

    async def initialize_state(self, request: "GenerateRequest") -> Dict[str, Any]:
        """Called by Kernel `initialize_node`."""
        # 1. Create Sandbox (handled by Kernel, but we can specify env)
        # 2. Return initial state delta
        return {
            "vertical_manifest": self.manifest.model_dump(),
            "available_skills": {k: v.model_dump() for k, v in self.skills.items()},
            "tech_stack_hints": self.manifest.tech_stack,
            "sandbox_env": self.manifest.runtime.env_vars,
            "metadata": {
                "project_structure": {
                    "src/app/(auth)": "Auth pages (login, register)",
                    "src/app/(dashboard)": "Protected dashboard layout",
                    "src/app/api/trpc": "tRPC HTTP handler",
                    "src/server/api/routers": "tRPC Routers (Procedures)",
                    "src/server/db": "Prisma Client & Schema",
                    "src/lib/auth": "NextAuth Config",
                    "src/components/ui": "shadcn/ui Components",
                }
            }
        }

    def get_planner_prompt(self, state: AgentState) -> str:
        return self.prompt_compiler.render("PLANNER.j2", {
            "state": state,
            "manifest": self.manifest,
            "skills": self.skills,
            "tech_stack": self.manifest.tech_stack,
        })

    def get_coder_prompt(self, state: AgentState, task: Task) -> str:
        # Retrieve RAG context for this specific task
        rag_tool = self.tool_registry.get("rag")
        # In real impl, we'd call rag_tool.execute here or have it pre-fetched in state
        # For prompt template, we pass the retrieved context from state
        return self.prompt_compiler.render("CODER.j2", {
            "state": state,
            "task": task,
            "skills": self.skills,
            "retrieved_context": state.get("rag_context", ""),
            "file_structure": state.get("metadata", {}).get("project_structure", {}),
        })

    def get_fixer_prompt(self, state: AgentState, task: Task) -> str:
        return self.prompt_compiler.render("FIXER.j2", {
            "state": state,
            "task": task,
            "failed_gates": state.get("current_gate_results", []),
            "files_to_fix": state.get("metadata", {}).get("files_to_fix", []),
        })

    def get_verification_gates(self, state: AgentState, task: Optional[Task]) -> List[IVerificationGate]:
        """Select gates based on task type or run all project gates at end."""
        if task is None:
            # Final Project Verification (Run ALL gates)
            return list(self.gates.values())
        
        # Task-Level Gates (Fast feedback)
        skill = self.skills.get(task.skill_id) if task.skill_id else None
        tags = skill.tags if skill else []
        
        # Mapping: Skill Tags -> Required Gates
        gate_map = {
            "typescript": ["lint_ts", "typecheck_ts"],
            "prisma": ["prisma_validate", "prisma_generate"],
            "trpc": ["typecheck_ts"], # tRPC types checked by TS
            "ui": ["lint_ts"], # Component linting
            "auth": ["typecheck_ts", "lint_ts"],
            "billing": ["typecheck_ts", "lint_ts"],
            "docker": ["docker_build"],
            "ci": ["yaml_lint"],
        }
        
        required_gate_ids = set()
        for tag in tags:
            required_gate_ids.update(gate_map.get(tag, []))
        
        # Always run lint/typecheck on any code change
        required_gate_ids.update(["lint_ts", "typecheck_ts"])
        
        return [self.gates[gid] for gid in required_gate_ids if gid in self.gates]

    def get_skill_executor(self, skill_id: str) -> ISkillExecutor:
        return self.skill_registry.get_executor(skill_id)

    async def on_task_complete(self, state: AgentState, task: Task):
        """Hook: Update derived artifacts after task."""
        # Example: If Prisma schema changed -> Regenerate tRPC types? 
        # Handled by `prisma generate` in skill post_scripts usually.
        # But if we added a tRPC router, we might want to update root router file.
        if task.skill_id == "add_trpc_router":
            await self._update_root_router(state, task)

    async def _update_root_router(self, state: AgentState, task: Task):
        """Append new router import to root app router."""
        router_name = task.inputs.get("router_name")
        if not router_name: return
        
        fs_tool = self.tool_registry.get("filesystem")
        sandbox_id = state["sandbox_id"]
        workspace = state["workspace_path"]
        file_path = "src/server/api/root.ts"
        
        read_res = await fs_tool.execute(sandbox_id, {"action": "read", "path": file_path}, {})
        if not read_res.success: return
        
        content = read_res.data["content"]
        import_line = f"import {{ {router_name}Router }} from \"./routers/{router_name}\";"
        router_entry = f"  {router_name}: {router_name}Router,"
        
        # Simple string manipulation (Tree-sitter better for production)
        if import_line not in content:
            # Find last import
            lines = content.splitlines()
            insert_idx = 0
            for i, line in enumerate(lines):
                if line.startswith("import ") and "routers/" in line:
                    insert_idx = i + 1
            lines.insert(insert_idx, import_line)
            content = "\n".join(lines)
        
        if router_entry not in content:
            # Find merge call
            content = content.replace("mergeRouters({", f"mergeRouters({{\n  {router_entry}")
            
        await fs_tool.execute(sandbox_id, {
            "action": "write", "path": file_path, "content": content, "mode": "update"
        }, {})

    async def finalize(self, state: AgentState) -> AgentState:
        """Generate final docs: README, ARCHITECTURE, API.md, CHANGELOG."""
        doc_tool = self.tool_registry.get("shell") # Or dedicated documenter agent
        # Documenter Node handles this, but Vertical can prep data.
        state["metadata"]["final_docs"] = {
            "readme_sections": ["Features", "Tech Stack", "Getting Started", "Deployment", "Env Variables"],
            "architecture_md": "Generated by Documenter Node",
            "api_md": "Generated from tRPC OpenAPI export",
        }
        return state

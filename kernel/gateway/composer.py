"""
Cross-vertical composition — "The Composer" (``specs/07_gateway/orchestrator/CROSS_VERTICAL.py``,
``GATEWAY_ARCH.md`` §3; rewritten by the agent, spec defects: ISSUES G-xx).

Flow of one composition:

1. **Plan** — the composer LLM (``gateway.composer_model``) splits the request into sub-projects of
   installed verticals and writes the **shared contracts** (OpenAPI, SQL, protobuf, event schemas) as
   complete files under ``shared/``. A caller may also send a ready plan (no LLM call). The plan is
   validated: known verticals, unique slug names, known dependencies, no cycles, safe contract paths.
2. **Execute** — sub-projects run as ordinary kernel runs (registered with ``parent_id`` = composition id,
   same tenant, visible in ``/v1/runs``) level by level of the dependency DAG; runs of one level execute
   in parallel. Contracts reach every sub-project as ``GenerateRequest.context_files`` (written into its
   workspace, shown to the planner as fixed files). A sub-project that *provides* a contract may refine it;
   the refined file (read from its artifact) is what later levels receive.
3. **HITL** — a sub-project paused for a human decision puts the composition into ``waiting_human``;
   the decision is sent to the sub-project run (``/v1/runs/{id}/interrupt``) and the composition continues.
4. **Aggregate** — one archive ``<composition_id>.tar.gz``: every sub-project under ``<name>/``,
   ``shared/`` contracts, and generated glue: ``README.md``, ``ARCHITECTURE.md``, ``docker-compose.yml``
   (sub-projects with a root ``Dockerfile``), ``Makefile``.

Not implemented (ISSUES G-xx): running the integration tests (``docker compose up`` + contract tests)
and ``skaffold.yaml``; they are listed in the generated README as a checklist.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import re
import tarfile
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from kernel.config import Settings
from kernel.protocols import GenerateRequest, ILLMClient, LLMMessage, VerticalManifest
from kernel.runner import new_run_id
from kernel.service import RunEvent, RunManager

from .auth import QuotaExceeded, RateLimiter
from .router import manifest_summary
from .store import GatewayStore, Principal, RunRecord

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_CONTRACT_BYTES = 256 * 1024
MAX_SUBPROJECTS = 8
SKIP_DIRS = {"node_modules", ".git", ".next", "__pycache__", ".venv", "dist", "build", ".turbo"}


# --- plan model (LLM structured output) ------------------------------------------------------------
class ContractFile(BaseModel):
    path: str = Field(description="Relative path under shared/, e.g. shared/openapi.yaml")
    kind: Literal["openapi", "asyncapi", "sql", "protobuf", "json_schema", "graphql", "env", "other"] = "other"
    description: str = ""
    content: str = Field(description="Complete file content (not a summary or placeholder)")

    @field_validator("path")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != "shared" or len(p.parts) < 2:
            raise ValueError(f"contract path must be relative and under shared/: {v!r}")
        return str(p)

    @field_validator("content")
    @classmethod
    def _size(cls, v: str) -> str:
        if len(v.encode()) > MAX_CONTRACT_BYTES:
            raise ValueError(f"contract larger than {MAX_CONTRACT_BYTES} bytes")
        return v


class SubProjectPlan(BaseModel):
    name: str = Field(description="Slug: lowercase letters, digits, dashes; also the directory name")
    vertical_id: str
    role: str = Field("", description="frontend | api | worker | infra | ...")
    prompt: str = Field(description="Self-contained PRD for this sub-project")
    tech_stack_hints: dict[str, str] = Field(default_factory=dict)
    contracts: list[str] = Field(default_factory=list, description="Contract paths this sub-project consumes")
    provides: list[str] = Field(default_factory=list, description="Contract paths this sub-project implements/owns")
    depends_on: list[str] = Field(default_factory=list, description="Sub-projects that must be generated first")
    port: int | None = Field(None, description="HTTP port of the service in docker-compose, if it is a service")

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"invalid sub-project name {v!r} (slug: [a-z][a-z0-9-]{{0,39}})")
        return v


class CompositionPlan(BaseModel):
    project_name: str
    summary: str = ""
    shared_contracts: list[ContractFile] = Field(default_factory=list)
    sub_projects: list[SubProjectPlan]
    integration_tests: list[str] = Field(default_factory=list)


class PlanError(ValueError):
    pass


def topo_levels(subs: list[SubProjectPlan]) -> list[list[str]]:
    """Kahn's algorithm by levels; raises PlanError on unknown dependencies or cycles."""
    names = {s.name for s in subs}
    deps = {s.name: set(s.depends_on) for s in subs}
    for name, d in deps.items():
        unknown = d - names
        if unknown:
            raise PlanError(f"{name}: unknown depends_on {sorted(unknown)}")
        if name in d:
            raise PlanError(f"{name} depends on itself")
    levels: list[list[str]] = []
    done: set[str] = set()
    while len(done) < len(names):
        ready = sorted(n for n in names - done if deps[n] <= done)
        if not ready:
            raise PlanError(f"dependency cycle among {sorted(names - done)}")
        levels.append(ready)
        done.update(ready)
    return levels


def validate_plan(plan: CompositionPlan, installed: Mapping[str, Any], allowed: Callable[[str], bool]) -> None:
    if not plan.sub_projects:
        raise PlanError("plan has no sub-projects")
    if len(plan.sub_projects) > MAX_SUBPROJECTS:
        raise PlanError(f"too many sub-projects ({len(plan.sub_projects)} > {MAX_SUBPROJECTS})")
    names = [s.name for s in plan.sub_projects]
    if len(set(names)) != len(names):
        raise PlanError(f"duplicate sub-project names: {names}")
    contract_paths = [c.path for c in plan.shared_contracts]
    if len(set(contract_paths)) != len(contract_paths):
        raise PlanError("duplicate contract paths")
    owners: dict[str, str] = {}
    for s in plan.sub_projects:
        if s.vertical_id not in installed:
            raise PlanError(f"{s.name}: vertical {s.vertical_id!r} is not installed ({sorted(installed)})")
        if not allowed(s.vertical_id):
            raise PlanError(f"{s.name}: no access to vertical {s.vertical_id!r}")
        for path in [*s.contracts, *s.provides]:
            if path not in contract_paths:
                raise PlanError(f"{s.name}: unknown contract {path!r}")
        for path in s.provides:
            if path in owners:
                raise PlanError(f"contract {path!r} provided by both {owners[path]} and {s.name}")
            owners[path] = s.name
    topo_levels(plan.sub_projects)


COMPOSER_SYSTEM = """You are the lead architect of a code-generation platform. Split the user's request into \
sub-projects, each generated independently by one vertical (a generator for one kind of project), and write the \
shared contracts that let them work together.

Installed verticals (use only these ids; the same vertical may be used for several sub-projects):
{catalog}

Rules:
- 2-{max_subprojects} sub-projects; use a single sub-project only if the request really is one project.
- name: slug ([a-z][a-z0-9-]*), also the directory name. prompt: a complete, self-contained PRD for that part \
(the generator sees only this prompt and the contract files), including which contracts it implements or consumes.
- shared_contracts: COMPLETE files under shared/ (OpenAPI 3.1 YAML for HTTP APIs, SQL DDL for shared databases, \
protobuf for gRPC, JSON Schema/AsyncAPI for events, shared/env.example for configuration). No placeholders.
- provides: contracts a sub-project implements (at most one owner per contract); contracts: those it consumes.
- depends_on: a sub-project that needs another one's final artifacts (e.g. infrastructure that deploys the \
services); consumers of a contract do NOT need to depend on its provider — the contract is fixed up front.
- port: the HTTP port of each service (distinct), null for non-services (infrastructure, libraries).
- integration_tests: short descriptions of end-to-end checks across sub-projects."""


class Composer:
    """Plans and executes compositions. One instance per gateway process."""

    def __init__(
        self,
        settings: Settings,
        store: GatewayStore,
        limiter: RateLimiter,
        manager: Callable[[], RunManager],
        verticals: Callable[[], Mapping[str, VerticalManifest]],
        llm: ILLMClient | None = None,
        limits_for: Callable[[Principal], Any] | None = None,
        webhook: Callable[[str, dict[str, Any]], Any] | None = None,
        slot_poll_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.store = store
        self.limiter = limiter
        self._manager = manager
        self._verticals = verticals
        self.llm = llm
        self.limits_for = limits_for
        self.webhook = webhook
        self.slot_poll_seconds = slot_poll_seconds
        self._queues: dict[str, asyncio.Queue[tuple[RunEvent, RunRecord]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def manager(self) -> RunManager:
        return self._manager()

    # --- planning ----------------------------------------------------------------------------
    async def plan(
        self, prompt: str, principal: Principal, tech_stack_hints: Mapping[str, str] | None = None
    ) -> CompositionPlan:
        if self.llm is None:
            raise PlanError("composer LLM is not configured: send a ready plan")
        installed = {k: v for k, v in self._verticals().items() if principal.can_use_vertical(k)}
        if not installed:
            raise PlanError("no verticals available")
        catalog = "\n".join(
            f"- {vid}: {yaml.safe_dump(manifest_summary(m), sort_keys=False, allow_unicode=True).strip()}"
            for vid, m in sorted(installed.items())
        )
        messages = [
            LLMMessage(role="system", content=COMPOSER_SYSTEM.format(catalog=catalog, max_subprojects=MAX_SUBPROJECTS)),
            LLMMessage(role="user", content=f"Request:\n{prompt}\n\nTech hints: {dict(tech_stack_hints or {})}"),
        ]
        last_error = ""
        for _attempt in range(2):
            try:
                resp = await self.llm.achat(
                    messages, model=self.settings.gateway.composer_model, response_model=CompositionPlan
                )
                parsed = resp.parsed
                plan = (
                    parsed if isinstance(parsed, CompositionPlan) else CompositionPlan.model_validate_json(resp.content)
                )
                validate_plan(plan, installed, principal.can_use_vertical)
                return plan
            except (PlanError, ValidationError) as exc:
                last_error = str(exc)
                logger.warning("composition plan rejected: %s", last_error)
                messages.append(
                    LLMMessage(role="user", content=f"The plan is invalid: {last_error}\nReturn a corrected plan.")
                )
        raise PlanError(f"composer could not produce a valid plan: {last_error}")

    # --- execution ---------------------------------------------------------------------------
    async def start(
        self,
        plan: CompositionPlan,
        principal: Principal,
        base: GenerateRequest,
        composition_id: str | None = None,
        webhook_url: str | None = None,
    ) -> str:
        validate_plan(plan, self._verticals(), principal.can_use_vertical)
        cid = composition_id or f"cmp_{uuid.uuid4().hex[:12]}"
        levels = topo_levels(plan.sub_projects)
        state: dict[str, Any] = {
            "composition_id": cid,
            "status": "running",
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "webhook_url": webhook_url,
            "levels": levels,
            "current_level": 0,
            "sub_projects": {
                s.name: {"vertical_id": s.vertical_id, "run_id": None, "status": "pending", "error": None}
                for s in plan.sub_projects
            },
            "contracts": {c.path: c.content for c in plan.shared_contracts},
            "artifact_path": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        await self.store.save_composition(cid, plan.model_dump(mode="json"), state)
        self._queues[cid] = asyncio.Queue()
        self._tasks[cid] = asyncio.create_task(self._execute(cid, plan, principal, base, state), name=f"cmp:{cid}")
        return cid

    async def on_run_stopped(self, event: RunEvent, rec: RunRecord) -> None:
        """GatewayServices stop listener: route sub-project events to their composition."""
        if rec.parent_id and rec.parent_id in self._queues:
            await self._queues[rec.parent_id].put((event, rec))

    async def cancel(self, cid: str) -> bool:
        task = self._tasks.get(cid)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def wait(self, cid: str) -> None:
        task = self._tasks.get(cid)
        if task is not None:
            await asyncio.shield(task)

    def is_running(self, cid: str) -> bool:
        task = self._tasks.get(cid)
        return task is not None and not task.done()

    async def get(self, cid: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        return await self.store.get_composition(cid)

    async def recover(self) -> int:
        """After a restart compositions in progress have no driver: mark them failed (runs keep checkpoints)."""
        n = 0
        for rec in await self.store.list_runs(None, limit=10_000):
            if rec.kind != "composition" or self.is_running(rec.run_id):
                continue
            found = await self.store.get_composition(rec.run_id)
            if found and found[1].get("status") in ("running", "waiting_human"):
                plan, state = found
                state.update(status="failed", error="interrupted by a gateway restart", updated_at=time.time())
                await self.store.save_composition(rec.run_id, plan, state)
                n += 1
        return n

    def _publish(self, cid: str, type_: str, **data: Any) -> None:
        self.manager.bus.publish(RunEvent(type=type_, run_id=cid, data=data))

    async def _save(self, cid: str, plan: CompositionPlan, state: dict[str, Any]) -> None:
        state["updated_at"] = time.time()
        await self.store.save_composition(cid, plan.model_dump(mode="json"), state)

    async def _execute(
        self, cid: str, plan: CompositionPlan, principal: Principal, base: GenerateRequest, state: dict[str, Any]
    ) -> None:
        subs = {s.name: s for s in plan.sub_projects}
        queue = self._queues[cid]
        try:
            for idx, level in enumerate(state["levels"]):
                state["current_level"] = idx
                await self._save(cid, plan, state)
                self._publish(cid, "composition", status="running", level=idx, sub_projects=level)
                for name in level:
                    await self._start_sub(cid, plan, subs[name], principal, base, state)
                await self._wait_level(cid, plan, level, state, queue)
                for name in level:
                    await self._absorb_contracts(subs[name], state)
            state["artifact_path"] = await asyncio.to_thread(self._aggregate, cid, plan, state)
            state["status"] = "completed"
            await self._save(cid, plan, state)
            self._publish(cid, "done", status="completed", artifact_path=state["artifact_path"])
        except asyncio.CancelledError:
            state["status"] = "cancelled"
            await self._cancel_children(state)
            await self._save(cid, plan, state)
            self._publish(cid, "cancelled", status="cancelled")
            raise
        except Exception as exc:
            logger.exception("composition %s failed", cid)
            state["status"] = "failed"
            state["error"] = state.get("error") or f"{type(exc).__name__}: {exc}"
            await self._cancel_children(state)
            await self._save(cid, plan, state)
            self._publish(cid, "error", status="failed", error=state["error"])
        finally:
            self._queues.pop(cid, None)
            if state.get("webhook_url") and self.webhook is not None:
                self.webhook(
                    state["webhook_url"],
                    {"event": state["status"], "composition_id": cid, "artifact_path": state.get("artifact_path")},
                )

    async def _start_sub(
        self,
        cid: str,
        plan: CompositionPlan,
        sub: SubProjectPlan,
        principal: Principal,
        base: GenerateRequest,
        state: dict[str, Any],
    ) -> None:
        run_id = new_run_id()
        wanted = set(sub.contracts) | set(sub.provides) or set(state["contracts"])
        files = [
            {"name": path, "content_base64": base64.b64encode(content.encode()).decode()}
            for path, content in sorted(state["contracts"].items())
            if path in wanted
        ]
        others = [
            f"- {s.name} ({s.vertical_id}, {s.role or 'component'})" for s in plan.sub_projects if s.name != sub.name
        ]
        note = (
            f"\n\n---\nThis is sub-project `{sub.name}` ({sub.role or 'component'}) of the composed system "
            f"`{plan.project_name}`. {plan.summary}\nOther sub-projects:\n" + "\n".join(others) + "\n"
            "Shared contracts are files under shared/ in the workspace. "
            + (f"You implement (own): {', '.join(sub.provides)}. " if sub.provides else "")
            + (f"You consume: {', '.join(sub.contracts)}. " if sub.contracts else "")
            + "Follow the contracts exactly; do not change contracts you do not own."
        )
        request = GenerateRequest(
            prompt=sub.prompt + note,
            vertical_id=sub.vertical_id,
            tech_stack_hints={**base.tech_stack_hints, **sub.tech_stack_hints},
            constraints=[*base.constraints, f"Part of composition {plan.project_name} ({cid})"],
            context_files=[*base.context_files, *files],
            max_budget_usd=base.max_budget_usd,
        )
        entry = state["sub_projects"][sub.name]
        entry.update(run_id=run_id, status="queued")
        await self.store.add_run(
            RunRecord(
                run_id=run_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                vertical_id=sub.vertical_id,
                parent_id=cid,
                routing={"method": "composition", "composition_id": cid, "sub_project": sub.name},
            )
        )
        await self._save(cid, plan, state)
        limits = self.limits_for(principal) if self.limits_for else None
        announced = False
        while limits is not None:  # wait for a free concurrent slot of the tenant
            try:
                await self.limiter.acquire(principal.tenant_id, run_id, limits)
                break
            except QuotaExceeded:
                if not announced:
                    self._publish(cid, "subproject", name=sub.name, run_id=run_id, status="waiting_for_slot")
                    announced = True
                await asyncio.sleep(self.slot_poll_seconds)
        try:
            await self.manager.start(request, run_id=run_id)
        except Exception:
            await self.limiter.release(principal.tenant_id, run_id)
            raise
        entry["status"] = "running"
        await self._save(cid, plan, state)
        self._publish(cid, "subproject", name=sub.name, run_id=run_id, status="running")

    async def _wait_level(
        self,
        cid: str,
        plan: CompositionPlan,
        level: list[str],
        state: dict[str, Any],
        queue: asyncio.Queue[tuple[RunEvent, RunRecord]],
    ) -> None:
        by_run = {state["sub_projects"][n]["run_id"]: n for n in level}
        pending = set(level)
        while pending:
            event, _rec = await queue.get()
            name = by_run.get(event.run_id)
            if name is None:
                continue
            entry = state["sub_projects"][name]
            if event.type == "interrupt":
                entry["status"] = "waiting_human"
                entry["interrupt_type"] = event.data.get("interrupt_type")
                state["status"] = "waiting_human"
            elif event.type == "done" and str(event.data.get("status")) == "completed":
                artifact = (event.data.get("final_artifact") or {}).get("artifact_path")
                entry.update(status="completed", interrupt_type=None, artifact_path=artifact)
                pending.discard(name)
            else:
                error = event.data.get("error") or f"run ended with {event.type}/{event.data.get('status')}"
                entry.update(status="failed", error=error)
                state["error"] = f"sub-project {name} failed: {error}"
                await self._save(cid, plan, state)
                raise RuntimeError(state["error"])
            if state["status"] == "waiting_human" and not any(
                state["sub_projects"][n]["status"] == "waiting_human" for n in level
            ):
                state["status"] = "running"
            await self._save(cid, plan, state)
            self._publish(
                cid,
                "subproject",
                name=name,
                run_id=event.run_id,
                status=entry["status"],
                interrupt_type=entry.get("interrupt_type"),
            )

    async def _cancel_children(self, state: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            for entry in state["sub_projects"].values():
                if entry.get("run_id") and self.manager.is_running(entry["run_id"]):
                    await self.manager.cancel(entry["run_id"])
                    entry["status"] = "cancelled"

    async def _absorb_contracts(self, sub: SubProjectPlan, state: dict[str, Any]) -> None:
        """Contracts owned by ``sub`` as they are in its artifact (the generator may have refined them)."""
        if not sub.provides:
            return
        archive = _existing(state["sub_projects"][sub.name].get("artifact_path"))
        if archive is None:
            return
        updated = await asyncio.to_thread(_read_members, archive, set(sub.provides))
        for path, content in updated.items():
            if content != state["contracts"].get(path):
                logger.info("contract %s refined by %s", path, sub.name)
                state["contracts"][path] = content

    # --- aggregation -------------------------------------------------------------------------
    def _aggregate(self, cid: str, plan: CompositionPlan, state: dict[str, Any]) -> str:
        out_dir = self.settings.artifacts.local_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{cid}.tar.gz"
        root = _slug(plan.project_name) or cid
        dockerized: set[str] = set()
        archives: dict[str, Path] = {}
        for sub in plan.sub_projects:
            path = _existing(state["sub_projects"][sub.name].get("artifact_path"))
            if path is not None:
                archives[sub.name] = path
                if _has_member(path, "Dockerfile"):
                    dockerized.add(sub.name)
        glue = render_glue(plan, state, dockerized)
        with tarfile.open(dest, "w:gz") as out:
            for name, archive in archives.items():
                _copy_tree(archive, out, f"{root}/{name}")
            for path, content in {**state["contracts"], **glue}.items():
                data = content.encode()
                info = tarfile.TarInfo(f"{root}/{path}")
                info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
                out.addfile(info, io.BytesIO(data))
        return str(dest)


def _existing(path: str | None) -> Path | None:
    return Path(path) if path and Path(path).is_file() else None


def _norm(name: str) -> str | None:
    p = PurePosixPath(name)
    parts = [x for x in p.parts if x not in (".", "")]
    if p.is_absolute() or ".." in parts or not parts:
        return None
    return "/".join(parts)


def _read_members(archive: Path, wanted: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar:
            rel = _norm(m.name)
            if rel in wanted and m.isfile() and m.size <= MAX_CONTRACT_BYTES:
                f = tar.extractfile(m)
                if f is not None:
                    with contextlib.suppress(UnicodeDecodeError):
                        out[rel] = f.read().decode()
    return out


def _has_member(archive: Path, name: str) -> bool:
    with tarfile.open(archive, "r:gz") as tar:
        return any(_norm(m.name) == name and m.isfile() for m in tar)


def _copy_tree(archive: Path, out: tarfile.TarFile, prefix: str) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar:
            rel = _norm(m.name)
            if rel is None or not (m.isfile() or m.isdir()) or SKIP_DIRS & set(rel.split("/")):
                continue
            info = tarfile.TarInfo(f"{prefix}/{rel}")
            info.type, info.mode, info.mtime, info.size = m.type, m.mode & 0o755, m.mtime, m.size if m.isfile() else 0
            out.addfile(info, tar.extractfile(m) if m.isfile() else None)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def render_glue(plan: CompositionPlan, state: Mapping[str, Any], dockerized: set[str]) -> dict[str, str]:
    """Root files of the composed repository (deterministic, no LLM)."""
    subs = plan.sub_projects
    files: dict[str, str] = {}
    rows = "\n".join(
        f"| `{s.name}/` | {s.vertical_id} | {s.role or '-'} | {s.port or '-'} | "
        f"{state['sub_projects'][s.name]['status']} |"
        for s in subs
    )
    contracts = "\n".join(f"- `{c.path}` ({c.kind}) {c.description}".rstrip() for c in plan.shared_contracts)
    tests = "\n".join(f"- [ ] {t}" for t in plan.integration_tests) or "- [ ] start everything and smoke-test"
    run = (
        "```bash\nmake up      # docker compose up --build -d\nmake logs\nmake down\n```"
        if dockerized
        else "Each sub-project has its own README with run instructions."
    )
    files["README.md"] = f"""# {plan.project_name}

{plan.summary}

Generated by the AutoGen Platform composer (composition `{state["composition_id"]}`).

| Directory | Vertical | Role | Port | Status |
| --- | --- | --- | --- | --- |
{rows}

## Shared contracts

{contracts or "- none"}

Contracts are the source of truth between sub-projects; change them first, then regenerate or update the
implementations.

## Run

{run}

## Integration checks (not run by the platform)

{tests}
"""
    edges = "\n".join(f"    {d.replace('-', '_')} --> {s.name.replace('-', '_')}" for s in subs for d in s.depends_on)
    nodes = "\n".join(f'    {s.name.replace("-", "_")}["{s.name} ({s.vertical_id})"]' for s in subs)
    uses = "\n".join(
        f"- **{s.name}**: provides {', '.join(s.provides) or '-'}; consumes {', '.join(s.contracts) or '-'}"
        for s in subs
    )
    files["ARCHITECTURE.md"] = (
        f"# {plan.project_name} — architecture\n\n{plan.summary}\n\n```mermaid\ngraph LR\n{nodes}\n{edges}\n```\n\n"
        f"## Contracts\n\n{uses}\n"
    )
    if dockerized:
        services: dict[str, Any] = {}
        env_file = "shared/env.example" if "shared/env.example" in state["contracts"] else None
        for s in subs:
            if s.name not in dockerized:
                continue
            svc: dict[str, Any] = {"build": f"./{s.name}"}
            if s.port:
                svc["ports"] = [f"{s.port}:{s.port}"]
                svc["environment"] = {"PORT": str(s.port)}
            if env_file:
                svc["env_file"] = [env_file]
            deps = [d for d in s.depends_on if d in dockerized]
            if deps:
                svc["depends_on"] = deps
            services[s.name] = svc
        files["docker-compose.yml"] = yaml.safe_dump({"services": services}, sort_keys=False)
    targets = [
        "up:\n\tdocker compose up --build -d",
        "down:\n\tdocker compose down",
        "logs:\n\tdocker compose logs -f",
        "ps:\n\tdocker compose ps",
    ]
    phony = "up down logs ps" if dockerized else "help"
    body = "\n\n".join(targets) if dockerized else "help:\n\t@echo 'see README.md; each sub-project builds on its own'"
    files["Makefile"] = f".PHONY: {phony}\n\n{body}\n"
    return files

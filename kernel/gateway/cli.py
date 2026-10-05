"""
``autogen`` — command-line client of the Gateway API (``specs/07_gateway/cli/AUTOGEN_CLI.py``; rewritten by
the agent: the spec hard-coded ``ws://localhost``, sent ``X-API-Key: None`` and used the removed
``websockets`` ``extra_headers`` argument — ISSUES G-07). Live progress uses the SSE endpoints, which pass
through proxies and need no extra dependency.

    export AUTOGEN_API_URL=https://api.example.com AUTOGEN_API_KEY=agk_...   # or profiles in
    #   ~/.config/autogen/config.yaml (AUTOGEN_CONFIG), selected with --profile / AUTOGEN_PROFILE
    autogen verticals
    autogen generate "Build a SaaS with Stripe" --vertical saas_web --watch --download ./out
    autogen generate -f prd.md --hint database=postgres --context-file openapi.yaml
    autogen status <run_id> | runs | logs <run_id> --follow | interrupt <run_id>
    autogen approve <run_id> --action approve --comment "lgtm"
    autogen cancel <run_id> | download <run_id> -o project.tar.gz
    autogen compose "Next.js frontend + FastAPI billing + Terraform" --dry-run
    autogen compose -f prd.md --plan plan.json --watch
    autogen composition status|watch|cancel|download <composition_id>

Exit codes: 0 ok, 1 API/usage error, 2 run finished unsuccessfully, 3 waiting for a human decision
(non-interactive ``--watch``).
"""

from __future__ import annotations

import base64
import json
import sys
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.table import Table

console = Console()
err = Console(stderr=True)

ACTIONS = ["approve", "reject", "edit", "abort", "retry", "skip_gate"]


class ApiError(click.ClickException):
    def __init__(self, resp: httpx.Response) -> None:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        super().__init__(f"HTTP {resp.status_code}: {detail}")


class Api:
    def __init__(
        self, url: str, api_key: str | None = None, token: str | None = None, client: httpx.Client | None = None
    ) -> None:
        base = url.rstrip("/").removesuffix("/v1")
        headers = {"User-Agent": "autogen-cli"}
        if api_key:
            headers["X-API-Key"] = api_key
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        if client is None:
            client = httpx.Client(base_url=base, timeout=httpx.Timeout(60.0, read=None))
        client.headers.update(headers)
        self.client = client

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = self.client.request(method, f"/v1{path}", **kw)
        if resp.status_code >= 400:
            raise ApiError(resp)
        return resp

    def json(self, method: str, path: str, **kw: Any) -> Any:
        return self.request(method, path, **kw).json()

    def sse(self, path: str, headers: dict[str, str] | None = None) -> Generator[tuple[str, Any], None, None]:
        """(event, data) pairs; comments/keep-alives are skipped."""
        hdrs = {"Accept": "text/event-stream", **(headers or {})}
        with self.client.stream("GET", f"/v1{path}", headers=hdrs) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise ApiError(resp)
            event, data = "message", ""
            for line in resp.iter_lines():
                if not line:
                    if data:
                        try:
                            yield event, json.loads(data)
                        except ValueError:
                            yield event, data
                    event, data = "message", ""
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()

    def download(self, path: str, dest: Path) -> Path:
        with self.client.stream("GET", f"/v1{path}") as resp:
            if resp.status_code >= 400:
                resp.read()
                raise ApiError(resp)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return dest


def _api(ctx: click.Context) -> Api:
    api: Api = ctx.obj["api"]
    return api


def _out(ctx: click.Context, data: Any) -> bool:
    """Print JSON in --json mode; returns True if printed."""
    if ctx.obj.get("json"):
        click.echo(json.dumps(data, indent=2, default=str))
        return True
    return False


def _hints(values: tuple[str, ...]) -> dict[str, str]:
    out = {}
    for item in values:
        if "=" not in item:
            raise click.BadParameter(f"expected key=value, got {item!r}", param_hint="--hint")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _context_files(paths: tuple[str, ...]) -> list[dict[str, str]]:
    files = []
    for p in paths:
        path = Path(p)
        files.append({"name": path.name, "content_base64": base64.b64encode(path.read_bytes()).decode()})
    return files


def _prompt_text(prompt: str | None, prompt_file: str | None) -> str:
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")
    if prompt == "-":
        return sys.stdin.read()
    if not prompt:
        raise click.UsageError("give PROMPT, '-' for stdin, or --file")
    return prompt


DEFAULT_URL = "http://localhost:8000"


def config_path() -> Path:
    import os

    env = os.environ.get("AUTOGEN_CONFIG")
    if env:
        return Path(env)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autogen" / "config.yaml"


def load_config(profile: str | None) -> dict[str, Any]:
    """``cli/CONFIG.yaml`` (no content in the spec; agent's format)::

    default_profile: prod
    profiles:
      prod: {url: https://api.example.com, key: agk_acme_...}
      local: {url: http://localhost:8000, token: eyJ...}
    """
    import yaml

    path = config_path()
    if not path.is_file():
        if profile:
            raise click.UsageError(f"profile {profile!r} requested but {path} does not exist")
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = profile or data.get("default_profile")
    if not name:
        return {}
    profiles = data.get("profiles") or {}
    if name not in profiles:
        raise click.UsageError(f"profile {name!r} not found in {path}")
    return dict(profiles[name] or {})


@click.group()
@click.option("--url", envvar="AUTOGEN_API_URL", help=f"gateway URL (default: profile or {DEFAULT_URL})")
@click.option("--key", envvar="AUTOGEN_API_KEY", help="API key (agk_...)")
@click.option("--token", envvar="AUTOGEN_TOKEN", help="JWT bearer token (if no API key)")
@click.option("--profile", "-p", envvar="AUTOGEN_PROFILE", help="profile from ~/.config/autogen/config.yaml")
@click.option("--json", "as_json", is_flag=True, help="print raw JSON")
@click.pass_context
def cli(
    ctx: click.Context, url: str | None, key: str | None, token: str | None, profile: str | None, as_json: bool
) -> None:
    """AutoGen Platform client."""
    obj = ctx.ensure_object(dict)
    obj.setdefault("api", None)
    if obj["api"] is None:
        conf = load_config(profile)
        creds_given = bool(key or token)
        obj["api"] = Api(
            url or conf.get("url") or DEFAULT_URL,
            key if creds_given else conf.get("key"),
            token if creds_given else conf.get("token"),
        )
    obj["json"] = as_json


# --- verticals -----------------------------------------------------------------------------------
@cli.group(invoke_without_command=True)
@click.pass_context
def verticals(ctx: click.Context) -> None:
    """List verticals (``verticals show ID`` for details)."""
    if ctx.invoked_subcommand is not None:
        return
    data = _api(ctx).json("GET", "/verticals")
    if _out(ctx, data):
        return
    table = Table(title="Verticals")
    for col in ("ID", "Name", "Version", "Stack"):
        table.add_column(col)
    for v in data:
        stack = ", ".join(f"{k}={val}" for k, val in v.get("tech_stack", {}).items())
        table.add_row(v["id"], v["name"], v["version"], stack)
    console.print(table)


@verticals.command("list")
@click.pass_context
def verticals_list(ctx: click.Context) -> None:
    """List verticals."""
    ctx.invoke(verticals)


@verticals.command("show")
@click.argument("vertical_id")
@click.pass_context
def verticals_show(ctx: click.Context, vertical_id: str) -> None:
    data = _api(ctx).json("GET", f"/verticals/{vertical_id}")
    if not _out(ctx, data):
        console.print_json(json.dumps(data, default=str))


# --- runs ----------------------------------------------------------------------------------------
@cli.command()
@click.argument("prompt", required=False)
@click.option("--file", "-f", "prompt_file", type=click.Path(exists=True, dir_okay=False), help="PRD file")
@click.option("--vertical", "-v", help="vertical id (auto-routing if omitted)")
@click.option("--hint", multiple=True, help="tech stack hint key=value (repeat)")
@click.option("--constraint", multiple=True, help="constraint (repeat)")
@click.option("--context-file", multiple=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--budget", type=float, help="max budget USD for the run")
@click.option("--webhook", help="URL notified when the run stops")
@click.option("--idempotency-key", help="safe retries (default: random)")
@click.option("--watch", "-w", is_flag=True, help="follow progress; answer HITL prompts interactively")
@click.option("--download", type=click.Path(file_okay=False), help="with --watch: save the artifact into DIR")
@click.pass_context
def generate(
    ctx: click.Context,
    prompt: str | None,
    prompt_file: str | None,
    vertical: str | None,
    hint: tuple[str, ...],
    constraint: tuple[str, ...],
    context_file: tuple[str, ...],
    budget: float | None,
    webhook: str | None,
    idempotency_key: str | None,
    watch: bool,
    download: str | None,
) -> None:
    """Start a project generation."""
    body: dict[str, Any] = {
        "prompt": _prompt_text(prompt, prompt_file),
        "tech_stack_hints": _hints(hint),
        "constraints": list(constraint),
        "context_files": _context_files(context_file),
    }
    if vertical:
        body["vertical_id"] = vertical
    if budget is not None:
        body["max_budget_usd"] = budget
    if webhook:
        body["webhook_url"] = webhook
    headers = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
    data = _api(ctx).json("POST", "/generate", json=body, headers=headers)
    if not _out(ctx, data):
        r = data.get("routing") or {}
        console.print(f"[green]started[/green] {data['run_id']}  vertical=[cyan]{data['vertical_id']}[/cyan]"
                      f" ({r.get('method')}, confidence {r.get('confidence')})")  # fmt: skip
        if len(r.get("candidates") or []) > 1:
            console.print(f"[yellow]several verticals match {r['candidates']}: consider `autogen compose`[/yellow]")
    if watch:
        _watch_run(ctx, data["run_id"], download)


@cli.command()
@click.argument("run_id")
@click.pass_context
def status(ctx: click.Context, run_id: str) -> None:
    """Run status."""
    data = _api(ctx).json("GET", f"/runs/{run_id}")
    if _out(ctx, data):
        return
    progress = float(data.get("progress") or 0) * 100
    tasks = f"{data.get('tasks_done', 0)}/{data.get('tasks_total', 0)}"
    console.print(
        f"[bold]{run_id}[/bold]  status=[cyan]{data.get('status')}[/cyan]  progress={progress:.0f}%  "
        f"tasks={tasks}  running={data.get('running')}"
    )
    if data.get("interrupt_type") not in (None, "none"):
        console.print(
            f"[yellow]waiting for a human decision:[/yellow] {data['interrupt_type']}  (autogen interrupt {run_id})"
        )
    if data.get("error"):
        console.print(f"[red]error:[/red] {data['error']}")
    usage = data.get("token_usage") or {}
    if usage:
        console.print(f"tokens={usage.get('total_tokens')}  cost=${usage.get('cost_usd', 0)}")
    if data.get("final_artifact"):
        console.print(f"[green]artifact ready[/green]: autogen download {run_id}")


@cli.command()
@click.option("--limit", default=20, show_default=True)
@click.option("--parent", help="only sub-projects of this composition")
@click.pass_context
def runs(ctx: click.Context, limit: int, parent: str | None) -> None:
    """Runs of your tenant (newest first)."""
    params: dict[str, Any] = {"limit": limit}
    if parent:
        params["parent_id"] = parent
    data = _api(ctx).json("GET", "/runs", params=params)
    if _out(ctx, data):
        return
    table = Table()
    table.add_column("Run", no_wrap=True)
    for col in ("Vertical", "Kind", "Status", "Progress", "User", "Parent"):
        table.add_column(col)
    for r in data:
        prog = "" if r.get("progress") is None else f"{r['progress'] * 100:.0f}%"
        state = (r.get("status") or "") + (" (running)" if r.get("running") else "")
        table.add_row(r["run_id"], r["vertical_id"], r["kind"], state, prog, r["user_id"], r.get("parent_id") or "")
    console.print(table)


@cli.command()
@click.argument("run_id")
@click.option("--follow", "-f", is_flag=True, help="stream new lines until the run stops")
@click.option("--offset", default=0, show_default=True)
@click.pass_context
def logs(ctx: click.Context, run_id: str, follow: bool, offset: int) -> None:
    """Run log."""
    api = _api(ctx)
    if not follow:
        for line in api.json("GET", f"/runs/{run_id}/logs", params={"offset": offset, "limit": 2000})["logs"]:
            click.echo(line)
        return
    headers = {"Last-Event-ID": str(offset - 1)} if offset else {}
    for event, data in api.sse(f"/runs/{run_id}/logs", headers):
        if event == "log":
            click.echo(data["line"])
        elif event == "end":
            err.print(f"[dim]-- run stopped: {data.get('status')} {data.get('interrupt_type') or ''}[/dim]")


@cli.command()
@click.argument("run_id")
@click.pass_context
def interrupt(ctx: click.Context, run_id: str) -> None:
    """Show the pending human-in-the-loop request."""
    data = _api(ctx).json("GET", f"/runs/{run_id}/interrupt")
    if not _out(ctx, data):
        _show_interrupt(data)


@cli.command()
@click.argument("run_id")
@click.option("--action", "-a", type=click.Choice(ACTIONS), required=True)
@click.option("--comment", "-m")
@click.option("--edit-json", type=click.Path(exists=True, dir_okay=False), help="edited_data for action=edit")
@click.pass_context
def approve(ctx: click.Context, run_id: str, action: str, comment: str | None, edit_json: str | None) -> None:
    """Submit a human decision for a paused run."""
    edited = json.loads(Path(edit_json).read_text(encoding="utf-8")) if edit_json else None
    data = _decide(_api(ctx), run_id, action, comment, edited)
    if not _out(ctx, data):
        console.print(f"[green]submitted[/green] {action}: run resumed")


@cli.command()
@click.argument("run_id")
@click.pass_context
def cancel(ctx: click.Context, run_id: str) -> None:
    """Cancel a running run."""
    data = _api(ctx).json("DELETE", f"/runs/{run_id}")
    if not _out(ctx, data):
        console.print(f"[yellow]cancelled[/yellow] {run_id}")


@cli.command()
@click.argument("run_id")
@click.option("--output", "-o", type=click.Path(dir_okay=False), help="default: <run_id>.tar.gz")
@click.pass_context
def download(ctx: click.Context, run_id: str, output: str | None) -> None:
    """Download the artifact of a completed run."""
    path = _api(ctx).download(f"/runs/{run_id}/artifact", Path(output or f"{run_id}.tar.gz"))
    console.print(f"[green]saved[/green] {path}")


@cli.command()
@click.argument("run_id")
@click.option("--download", type=click.Path(file_okay=False))
@click.pass_context
def watch(ctx: click.Context, run_id: str, download: str | None) -> None:
    """Follow a run; answer HITL prompts interactively."""
    _watch_run(ctx, run_id, download)


# --- compositions --------------------------------------------------------------------------------
@cli.command()
@click.argument("prompt", required=False)
@click.option("--file", "-f", "prompt_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--plan", "plan_file", type=click.Path(exists=True, dir_okay=False), help="ready plan (JSON/YAML)")
@click.option("--hint", multiple=True)
@click.option("--constraint", multiple=True)
@click.option("--context-file", multiple=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--budget", type=float, help="max budget USD per sub-project")
@click.option("--webhook")
@click.option("--dry-run", is_flag=True, help="only print the plan")
@click.option("--save-plan", type=click.Path(dir_okay=False), help="with --dry-run: write the plan to a file")
@click.option("--watch", "-w", is_flag=True)
@click.option("--download", type=click.Path(file_okay=False))
@click.pass_context
def compose(
    ctx: click.Context,
    prompt: str | None,
    prompt_file: str | None,
    plan_file: str | None,
    hint: tuple[str, ...],
    constraint: tuple[str, ...],
    context_file: tuple[str, ...],
    budget: float | None,
    webhook: str | None,
    dry_run: bool,
    save_plan: str | None,
    watch: bool,
    download: str | None,
) -> None:
    """Generate a multi-vertical system (frontend + services + infra) with shared contracts."""
    import yaml

    body: dict[str, Any] = {
        "prompt": _prompt_text(prompt, prompt_file) if (prompt or prompt_file) else "(ready plan)",
        "tech_stack_hints": _hints(hint),
        "constraints": list(constraint),
        "context_files": _context_files(context_file),
        "dry_run": dry_run,
    }
    if plan_file:
        body["plan"] = yaml.safe_load(Path(plan_file).read_text(encoding="utf-8"))
    elif not (prompt or prompt_file):
        raise click.UsageError("give PROMPT, --file or --plan")
    if budget is not None:
        body["max_budget_usd"] = budget
    if webhook:
        body["webhook_url"] = webhook
    headers = {} if dry_run else {"Idempotency-Key": uuid.uuid4().hex}
    data = _api(ctx).json("POST", "/compositions", json=body, headers=headers)
    if dry_run:
        if save_plan:
            Path(save_plan).write_text(json.dumps(data["plan"], indent=2), encoding="utf-8")
        if not _out(ctx, data):
            _show_plan(data["plan"])
            if save_plan:
                console.print(
                    f"[green]plan saved[/green] {save_plan}: edit it and run `autogen compose --plan {save_plan}`"
                )
        return
    if not _out(ctx, data):
        console.print(f"[green]composition started[/green] {data['composition_id']}  levels={data['levels']}")
    if watch:
        _watch_composition(ctx, data["composition_id"], download)


@cli.group()
def composition() -> None:
    """Manage compositions."""


@composition.command("status")
@click.argument("composition_id")
@click.pass_context
def composition_status(ctx: click.Context, composition_id: str) -> None:
    data = _api(ctx).json("GET", f"/compositions/{composition_id}")
    if not _out(ctx, data):
        _show_composition(data)


@composition.command("watch")
@click.argument("composition_id")
@click.option("--download", type=click.Path(file_okay=False))
@click.pass_context
def composition_watch(ctx: click.Context, composition_id: str, download: str | None) -> None:
    _watch_composition(ctx, composition_id, download)


@composition.command("cancel")
@click.argument("composition_id")
@click.pass_context
def composition_cancel(ctx: click.Context, composition_id: str) -> None:
    data = _api(ctx).json("DELETE", f"/compositions/{composition_id}")
    if not _out(ctx, data):
        console.print(f"[yellow]cancelled[/yellow] {composition_id}")


@composition.command("download")
@click.argument("composition_id")
@click.option("--output", "-o", type=click.Path(dir_okay=False))
@click.pass_context
def composition_download(ctx: click.Context, composition_id: str, output: str | None) -> None:
    dest = Path(output or f"{composition_id}.tar.gz")
    path = _api(ctx).download(f"/compositions/{composition_id}/artifact", dest)
    console.print(f"[green]saved[/green] {path}")


# --- helpers -------------------------------------------------------------------------------------
def _decide(api: Api, run_id: str, action: str, comment: str | None, edited: Any = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if comment:
        body["comment"] = comment
    if edited is not None:
        body["edited_data"] = edited
    return api.json("POST", f"/runs/{run_id}/interrupt", json=body)


def _show_interrupt(data: dict[str, Any]) -> None:
    console.print(f"[yellow]human input required:[/yellow] [bold]{data.get('interrupt_type')}[/bold]")
    payload = data.get("payload") or {}
    if data.get("interrupt_type") == "plan_review" and payload.get("task_graph"):
        table = Table(title="Proposed plan")
        for col in ("Task", "Name", "Depends on"):
            table.add_column(col)
        for t in payload["task_graph"]:
            table.add_row(t.get("id", ""), t.get("name", ""), ", ".join(t.get("depends_on") or []))
        console.print(table)
    else:
        console.print_json(json.dumps(payload, default=str)[:20000])
    console.print(f"actions: {', '.join(data.get('actions') or [])}")


def _ask_decision(api: Api, run_id: str) -> bool:
    """Interactive decision; False if not interactive (caller exits with 3)."""
    try:
        data = api.json("GET", f"/runs/{run_id}/interrupt")
    except ApiError:
        return True  # already resumed by someone else
    _show_interrupt(data)
    if not sys.stdin.isatty():
        err.print(f"[yellow]waiting for a decision: autogen approve {run_id} --action <action>[/yellow]")
        return False
    action = click.prompt("action", type=click.Choice(list(data.get("actions") or ACTIONS)))
    comment = click.prompt("comment", default="", show_default=False) or None
    edited = None
    if action == "edit":
        text = click.edit(json.dumps(data.get("payload") or {}, indent=2, default=str), extension=".json")
        edited = json.loads(text) if text else None
    _decide(api, run_id, action, comment, edited)
    console.print(f"[green]submitted[/green] {action}")
    return True


def _watch_run(ctx: click.Context, run_id: str, download_dir: str | None) -> None:
    api = _api(ctx)
    final: dict[str, Any] = {}
    while True:
        paused = False
        stream = api.sse(f"/runs/{run_id}/events")
        for event, data in stream:
            payload = data.get("data", data) if isinstance(data, dict) else {}
            if event == "state":
                final = data
                console.print(
                    f"[dim]status: {data.get('status')} progress {float(data.get('progress') or 0) * 100:.0f}%[/dim]"
                )
                if data.get("interrupt_type") not in (None, "none") and not data.get("running"):
                    paused = True
                    break
            elif event == "node":
                for line in payload.get("logs") or []:
                    console.print(line, markup=False, highlight=False)
            elif event == "interrupt":
                paused = True
                break
            elif event in ("done", "error", "cancelled"):
                final = payload
                break
        stream.close()
        if paused:
            if not _ask_decision(api, run_id):
                ctx.exit(3)
            continue
        break
    final = api.json("GET", f"/runs/{run_id}")
    if final.get("status") == "completed":
        console.print(f"[green]completed[/green] {run_id}")
        if download_dir:
            path = api.download(f"/runs/{run_id}/artifact", Path(download_dir) / f"{run_id}.tar.gz")
            console.print(f"[green]saved[/green] {path}")
        return
    err.print(f"[red]run ended: {final.get('status')}[/red] {final.get('error') or ''}")
    ctx.exit(2)


def _show_plan(plan: dict[str, Any]) -> None:
    console.print(f"[bold]{plan.get('project_name')}[/bold] — {plan.get('summary', '')}")
    table = Table(title="Sub-projects")
    for col in ("Name", "Vertical", "Role", "Provides", "Consumes", "Depends on", "Port"):
        table.add_column(col)
    for s in plan.get("sub_projects", []):
        table.add_row(
            s["name"],
            s["vertical_id"],
            s.get("role") or "",
            ", ".join(s.get("provides") or []),
            ", ".join(s.get("contracts") or []),
            ", ".join(s.get("depends_on") or []),
            str(s.get("port") or ""),
        )
    console.print(table)
    for c in plan.get("shared_contracts", []):
        console.print(f"  contract [cyan]{c['path']}[/cyan] ({c.get('kind')}, {len(c.get('content', ''))} chars)")


def _show_composition(data: dict[str, Any]) -> None:
    console.print(f"[bold]{data['composition_id']}[/bold] status=[cyan]{data.get('status')}[/cyan] "
                  f"level={data.get('current_level')} of {len(data.get('levels') or [])}")  # fmt: skip
    table = Table()
    for col in ("Sub-project", "Vertical", "Run", "Status"):
        table.add_column(col)
    for name, s in (data.get("sub_projects") or {}).items():
        state = s.get("status", "") + (f" ({s['interrupt_type']})" if s.get("interrupt_type") else "")
        table.add_row(name, s.get("vertical_id", ""), s.get("run_id") or "", state)
    console.print(table)
    if data.get("error"):
        console.print(f"[red]error:[/red] {data['error']}")


def _watch_composition(ctx: click.Context, cid: str, download_dir: str | None) -> None:
    api = _api(ctx)
    stream = api.sse(f"/compositions/{cid}/events")
    for event, data in stream:
        payload = data.get("data", data) if isinstance(data, dict) else {}
        if event == "state":
            console.print(f"[dim]status: {data.get('status')}[/dim]")
        elif event == "subproject":
            console.print(f"  {payload.get('name')}: {payload.get('status')} {payload.get('interrupt_type') or ''}")
            if payload.get("status") == "waiting_human" and payload.get("run_id"):
                if not _ask_decision(api, payload["run_id"]):
                    ctx.exit(3)
        elif event == "composition":
            console.print(f"[bold]level {payload.get('level')}[/bold]: {', '.join(payload.get('sub_projects') or [])}")
        elif event in ("done", "error", "cancelled"):
            break
    stream.close()
    view = api.json("GET", f"/compositions/{cid}")
    _show_composition(view)
    if view.get("status") != "completed":
        ctx.exit(2)
    if download_dir:
        path = api.download(f"/compositions/{cid}/artifact", Path(download_dir) / f"{cid}.tar.gz")
        console.print(f"[green]saved[/green] {path}")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()

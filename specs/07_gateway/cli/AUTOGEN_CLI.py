# specs/07_gateway/cli/AUTOGEN_CLI.py
"""
CLI Client for AutoGen Platform.
Usage:
  autogen generate "Build a SaaS with Stripe" --vertical saas_web --watch
  autogen status <run_id>
  autogen approve <run_id> --action approve
  autogen verticals list
"""

import asyncio
import click
import httpx
import websockets
import json
import os
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.markdown import Markdown
from pathlib import Path

console = Console()


class AutoGenCLI:
    def __init__(self, base_url: str = None, api_key: str = None):
        self.base_url = base_url or os.getenv("AUTOGEN_API_URL", "http://localhost:8000/v1")
        self.api_key = api_key or os.getenv("AUTOGEN_API_KEY")
        self.client = httpx.AsyncClient(base_url=self.base_url, headers={"X-API-Key": self.api_key}, timeout=300)

    async def generate(self, prompt: str, vertical: str = None, watch: bool = False, **kwargs) -> str:
        """Start generation, optionally watch progress."""
        payload = {"prompt": prompt, **kwargs}
        if vertical:
            payload["vertical_id"] = vertical

        resp = await self.client.post("/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        run_id = data["run_id"]

        console.print(f"[green]Generation started:[/green] {run_id}")
        console.print(f"[dim]Stream URL:[/dim] {data['stream_url']}")

        if watch:
            await self.watch(run_id)
        return run_id

    async def watch(self, run_id: str):
        """Watch progress via WebSocket."""
        ws_url = f"ws://localhost:8000/v1/ws/runs/{run_id}".replace("http://", "ws://").replace("https://", "wss://")
        try:
            async with websockets.connect(ws_url, extra_headers={"X-API-Key": self.api_key}) as ws:
                with Live(console=console, refresh_per_second=4) as live:
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if data["type"] == "log":
                            log = data["data"]
                            live.update(self._render_log(log))
                        elif data["type"] == "state":
                            # Update status table
                            pass
                        elif data["type"] == "interrupt":
                            console.print(
                                f"\n[yellow]⚠️ HUMAN INPUT REQUIRED:[/yellow] {data['data']['interrupt_type']}"
                            )
                            action = click.prompt(
                                "Action", type=click.Choice(["approve", "reject", "edit", "abort", "skip_gate"])
                            )
                            await self.approve(run_id, action)
                            # Resume watching...
        except KeyboardInterrupt:
            console.print("\n[red]Stopped watching.[/red]")

    def _render_log(self, log: dict):
        from rich.text import Text

        t = Text()
        t.append(f"[{log.get('timestamp', '')}] ", style="dim")
        t.append(f"{log.get('node', '')}: ", style="cyan")
        t.append(log.get("message", ""), style="white" if log.get("level") == "info" else "yellow")
        return t

    async def status(self, run_id: str):
        resp = await self.client.get(f"/runs/{run_id}")
        resp.raise_for_status()
        data = resp.json()
        console.print(
            Markdown(f"## Run: {run_id}\n**Status:** {data['status']}\n**Progress:** {data['progress'] * 100:.0f}%")
        )
        if data.get("artifact"):
            console.print(f"[green]Artifact:[/green] {data['artifact']['artifact_url']}")

    async def approve(self, run_id: str, action: str, data: dict = None):
        resp = await self.client.post(f"/runs/{run_id}/interrupt", json={"action": action, "edited_data": data})
        resp.raise_for_status()
        console.print(f"[green]Submitted:[/green] {action}")

    async def list_verticals(self):
        resp = await self.client.get("/verticals")
        resp.raise_for_status()
        verticals = resp.json()
        table = Table(title="Available Verticals")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("Stack")
        for v in verticals:
            table.add_row(v["id"], v["name"], v["description"][:50], str(v.get("tech_stack", {})))
        console.print(table)


@click.group()
@click.option("--url", envvar="AUTOGEN_API_URL", default="http://localhost:8000/v1")
@click.option("--key", envvar="AUTOGEN_API_KEY")
@click.pass_context
def cli(ctx, url, key):
    ctx.obj = AutoGenCLI(url, key)


@cli.command()
@click.argument("prompt")
@click.option("--vertical", "-v", help="Vertical ID (auto-detect if omitted)")
@click.option("--watch", "-w", is_flag=True, help="Watch progress in real-time")
@click.option("--budget", type=float, help="Max budget USD")
@click.pass_context
def generate(ctx, prompt, vertical, watch, budget):
    """Generate a new project."""
    asyncio.run(ctx.obj.generate(prompt, vertical, watch, max_budget_usd=budget))


@cli.command()
@click.argument("run_id")
@click.pass_context
def status(ctx, run_id):
    """Check run status."""
    asyncio.run(ctx.obj.status(run_id))


@cli.command()
@click.argument("run_id")
@click.option("--action", type=click.Choice(["approve", "reject", "edit", "abort", "skip_gate"]), required=True)
@click.pass_context
def approve(ctx, run_id, action):
    """Submit human approval for interrupted run."""
    asyncio.run(ctx.obj.approve(run_id, action))


@cli.command()
@click.pass_context
def verticals(ctx):
    """List available verticals."""
    asyncio.run(ctx.obj.list_verticals())


if __name__ == "__main__":
    cli()

# specs/04_knowledge/cli/INGEST_CLI.py
"""
CLI Entry Point for Ingestion.
Usage:
  autogen-knowledge ingest --repo https://github.com/vercel/next.js --branch canary --tags nextjs,react,streaming
  autogen-knowledge ingest --local ./my-project --framework-version "nextjs@14.2.0"
  autogen-knowledge reindex --vertical saas_web
"""

import asyncio
import click
from typing: List, Optional
from ..ingestion.INGESTION_PIPELINE import IngestionPipeline, IngestionConfig
from kernel.config import settings

@click.group()
def cli():
    pass

@cli.command()
@click.option("--repo", required=True, help="Git URL or Local Path")
@click.option("--branch", default="main")
@click.option("--name", default="", help="Repo name (auto from URL)")
@click.option("--framework", default="", help="Framework version tag (e.g. nextjs@14.2.0)")
@click.option("--tags", default="", help="Comma-separated tags for filtering")
@click.option("--no-enrich", is_flag=True, help="Skip LLM Enrichment (Fast mode)")
@click.option("--include", multiple=True, help="Glob patterns to include")
@click.option("--exclude", multiple=True, help="Glob patterns to exclude")
def ingest(repo, branch, name, framework, tags, no_enrich, include, exclude):
    """Ingest a repository into Knowledge Base."""
    config = IngestionConfig(
        repo_url=repo,
        branch=branch,
        repo_name=name,
        framework_version=framework,
        vertical_tags=tags.split(",") if tags else [],
        include_patterns=list(include) or None,
        exclude_patterns=list(exclude) or None,
        enrich=not no_enrich
    )
    
    click.echo(f"Starting ingestion for: {repo} ({branch})")
    pipeline = IngestionPipeline(config)
    stats = asyncio.run(pipeline.run())
    click.echo(f"✅ Done. Stats: {stats}")

@cli.command()
@click.option("--vertical", required=True, help="Vertical ID to re-index from its configured sources")
def reindex(vertical):
    """Re-index all sources defined in a Vertical's manifest."""
    # Load Vertical Manifest -> get sources -> run pipeline for each
    click.echo(f"Re-indexing vertical: {vertical} (Not implemented in spec)")

@cli.command()
@click.option("--repo", required=True)
def delete(repo):
    """Delete all data for a repository."""
    # vector_db.delete_repo(repo_name)
    click.echo(f"Deleting repo: {repo} (Not implemented in spec)")

if __name__ == "__main__":
    cli()

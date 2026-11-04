"""Command-line interface: ``signal collect | summarize | digest | serve | validate-sources | reindex``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from signal_news.context import AppContext

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Signal: local news collector and daily digest.",
)
console = Console()

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to config.yaml (default: ./config.yaml)")
]


def _ctx(config: Path | None) -> AppContext:
    if sys.platform == "win32":
        # Make emoji in log output safe on older Windows consoles.
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    return AppContext.load(config)


@app.command()
def collect(
    config: ConfigOpt = None,
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Only these source names")
    ] = None,
    summarize: Annotated[bool, typer.Option(help="Summarise new articles afterwards")] = False,
) -> None:
    """Fetch all enabled sources and store new articles in the vault."""
    from signal_news.jobs import run_collect

    ctx = _ctx(config)
    res = run_collect(ctx, only=source)
    if res is None:
        raise typer.Exit(1)
    c = res.counts
    console.print(
        f"[green]Collected {c['new_articles']} new articles[/green] from {c['sources_ok']}/{c['sources']} sources"
        f" ({c['new_kev']} new KEV, {c['affects_me']} affect your gear)."
    )
    for err in res.errors[:30]:
        console.print(f"  [yellow]![/yellow] {err}")
    if res.auto_disabled:
        console.print(f"[red]Auto-disabled:[/red] {', '.join(res.auto_disabled)}")
    if summarize:
        from signal_news.jobs import run_summarize

        run_summarize(ctx)


@app.command()
def summarize(
    config: ConfigOpt = None,
    limit: Annotated[
        int | None, typer.Option(help="Max articles to summarise (default: all pending)")
    ] = None,
) -> None:
    """Summarise articles that don't have a summary yet (incremental)."""
    from signal_news.jobs import run_summarize

    ctx = _ctx(config)
    res = run_summarize(ctx, limit=limit)
    if res is None:
        raise typer.Exit(1)
    console.print(
        f"[green]Summarised {res.done}[/green], failed {res.failed}, remaining {res.remaining}."
        + (" [yellow]Model unreachable; stopped early.[/yellow]" if res.unreachable else "")
    )


@app.command()
def digest(
    config: ConfigOpt = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Regenerate even if today's digest exists")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Build a headline-only digest without a model")
    ] = False,
    notify: Annotated[bool, typer.Option(help="Send notifications after generating")] = True,
) -> None:
    """Generate today's digest from everything collected since the previous digest."""
    from signal_news.jobs import run_digest

    ctx = _ctx(config)
    res = run_digest(
        ctx,
        force=force,
        use_llm=not no_llm,
        notify=notify,
    )
    if res is None:
        raise typer.Exit(1)
    console.print(f"[green]Digest written:[/green] {res.path} ({res.article_count} articles)")


@app.command()
def serve(
    config: ConfigOpt = None,
    host: Annotated[str | None, typer.Option(help="Bind address (default from config)")] = None,
    port: Annotated[int | None, typer.Option(help="Port (default from config)")] = None,
    no_scheduler: Annotated[
        bool, typer.Option("--no-scheduler", help="Web UI only, no scheduled jobs")
    ] = False,
) -> None:
    """Run the web UI and the scheduler (catch-up collection, hourly collect, daily digest)."""
    import uvicorn

    from signal_news.web.app import create_app

    ctx = _ctx(config)
    h = host or ctx.cfg.web.host
    p = port or ctx.cfg.web.port
    application = create_app(ctx, start_scheduler=not no_scheduler, host=h)
    console.print(
        f"Signal running at [bold]http://{'127.0.0.1' if h in ('0.0.0.0', '::') else h}:{p}[/bold]"
    )
    uvicorn.run(application, host=h, port=p, log_level="warning", access_log=False)


@app.command("validate-sources")
def validate_sources(
    config: ConfigOpt = None,
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Only these source names")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report only; don't edit sources.yaml")
    ] = False,
) -> None:
    """Check every feed; repair moved feeds, fall back to Google News, or disable dead ones."""
    import asyncio

    from signal_news.collector.validate import apply_results, report_markdown, validate_async

    ctx = _ctx(config)
    console.print("Checking sources (this can take a minute)...")
    results = asyncio.run(validate_async(ctx.cfg, source))
    table = Table(title="Source validation")
    for col in ("Source", "Topic", "Status", "Items", "Newest", "Result"):
        table.add_column(col)
    for r in sorted(results, key=lambda r: (r.ok, r.source.topic, r.source.name)):
        newest = r.newest.astimezone(ctx.cfg.tz).strftime("%Y-%m-%d %H:%M") if r.newest else "-"
        if r.ok and not r.action:
            result = "[green]OK[/green]"
        elif r.ok:
            result = f"[yellow]{r.action}[/yellow] {r.new_url or r.new_query or ''}"
        else:
            result = f"[red]{r.action or 'failed'}[/red] {r.error or ''}"
        table.add_row(
            r.source.name, r.source.topic, str(r.status or "-"), str(r.items), newest, result
        )
    console.print(table)
    report = report_markdown(ctx.cfg, results)
    out = ctx.cfg.logs / f"validate-sources-{ctx.cfg.now():%Y%m%d-%H%M}.md"
    out.write_text(report, encoding="utf-8")
    console.print(f"Report saved to {out}")
    if not dry_run:
        apply_results(ctx.cfg, results)
        changed = [r for r in results if r.action and not r.source.generated]
        console.print(
            f"Updated sources.yaml for {len(changed)} source(s)."
            if changed
            else "No changes needed."
        )


@app.command()
def reindex(config: ConfigOpt = None) -> None:
    """Rebuild the SQLite index from the Markdown vault."""
    from signal_news.storage.reindex import reindex as do_reindex

    ctx = _ctx(config)
    res = do_reindex(ctx.db, ctx.vault)
    console.print(
        f"Indexed {res.articles} articles and {res.digests} digests ({res.errors} errors)."
    )


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question about the collected news")],
    config: ConfigOpt = None,
) -> None:
    """Ask a question about the collected news (answers cite articles)."""
    from signal_news.summarize.ask import answer_stream

    ctx = _ctx(config)
    for chunk in answer_stream(ctx, question):
        console.print(chunk, end="", markup=False, highlight=False)
    console.print()


@app.command("notify-test")
def notify_test(config: ConfigOpt = None) -> None:
    """Send a test notification to every configured target."""
    from signal_news.notify import send_test

    ctx = _ctx(config)
    ok, msg = send_test(ctx.cfg)
    console.print(("[green]" if ok else "[red]") + msg)


@app.command()
def cleanup(config: ConfigOpt = None) -> None:
    """Delete old articles that never made it into a digest (only if retention is configured)."""
    from signal_news.jobs import run_cleanup

    ctx = _ctx(config)
    n = run_cleanup(ctx)
    console.print(f"Removed {n} articles.")


@app.command("embed")
def embed(
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option(help="Max articles to embed")] = 500,
) -> None:
    """Compute embeddings for articles (needs qa.embeddings_enabled)."""
    from signal_news.summarize.ask import embed_pending

    ctx = _ctx(config)
    n = embed_pending(ctx, limit=limit)
    console.print(f"Embedded {n} articles.")


if __name__ == "__main__":
    app()

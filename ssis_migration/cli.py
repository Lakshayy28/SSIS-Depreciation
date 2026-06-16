"""
CLI entry point: ssis-migrate

Commands:
  assess    Phase 0 — scan packages, build inventory & wave plan
  convert   Run full pipeline on a single .dtsx file
  pipeline  Run full pipeline on a directory (all or by wave)
  compare   Run all three modes on one file and print a side-by-side diff
  config    Print current config (from .env / environment)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

console = Console()


def _setup_logging(verbose: bool) -> None:
    from ssis_migration.config import cfg
    level = logging.DEBUG if verbose else getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """SSIS-to-PySpark Migration Framework."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


# ─── config ──────────────────────────────────────────────────────────────────

@main.command("config")
def show_config() -> None:
    """Print current configuration (resolved from .env + environment)."""
    from ssis_migration.config import cfg
    table = Table(title="Active Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    table.add_row("COPILOT_MODEL", cfg.copilot_model)
    table.add_row("COPILOT_REVIEWER_MODEL", cfg.copilot_reviewer_model)
    table.add_row("CONVERSION_MODE", cfg.conversion_mode)
    table.add_row("SPARK_VERSION", cfg.spark_version)
    table.add_row("COPILOT_TEMPERATURE", str(cfg.copilot_temperature))
    table.add_row("COPILOT_MAX_TOKENS", str(cfg.copilot_max_tokens))
    table.add_row("COPILOT_MAX_REVIEW_ITERATIONS", str(cfg.copilot_max_review_iterations))
    table.add_row("FUNCTIONAL_VALIDATION_MAX_ITERATIONS", str(cfg.functional_validation_max_iterations))
    table.add_row("LLM_CONFIDENCE_THRESHOLD", str(cfg.llm_confidence_threshold))
    table.add_row("OUTPUT_DIR", str(cfg.output_dir))
    table.add_row("LOG_LEVEL", cfg.log_level)
    token_val = "[green]SET[/green]" if cfg.has_llm_token else "[red]NOT SET — LLM phases will fail[/red]"
    table.add_row("GITHUB_TOKEN", token_val)
    console.print(table)


# ─── assess ──────────────────────────────────────────────────────────────────

@main.command()
@click.argument("dtsx_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
def assess(dtsx_dir: Path, output: Path | None) -> None:
    """Phase 0: Scan packages and generate inventory report + wave plan."""
    from ssis_migration.config import cfg
    from ssis_migration.inventory import build_inventory, save_inventory

    out = output or cfg.output_dir
    console.print(f"[bold]Scanning:[/bold] {dtsx_dir}")
    inventory = build_inventory(dtsx_dir)
    save_inventory(inventory, out)

    table = Table(title="Complexity Summary")
    table.add_column("Complexity", style="cyan")
    table.add_column("Count", justify="right")
    for level, count in inventory["complexity_summary"].items():
        table.add_row(level, str(count))
    console.print(table)
    console.print(f"[green]Saved:[/green] {out}/inventory_report.json, wave_plan.json")


# ─── convert ─────────────────────────────────────────────────────────────────

@main.command()
@click.argument("dtsx_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option(
    "--mode", "-m",
    type=click.Choice(["deterministic", "hybrid", "llm", "auto"]),
    default=None,
    help="Conversion mode (default: from CONVERSION_MODE in .env). "
         "auto = deterministic + risk-aware per-item routing.",
)
@click.option("--spark-version", default=None, help="Target PySpark version")
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None,
              help="GitHub Copilot token (overrides .env / GITHUB_TOKEN env var)")
def convert(
    dtsx_file: Path, output: Path | None, mode: str | None,
    spark_version: str | None, github_token: str | None,
) -> None:
    """Run the full migration pipeline on a single .dtsx file."""
    from ssis_migration.config import cfg
    from ssis_migration.pipeline import ConversionMode, MigrationPipeline, PipelineConfig

    resolved_mode = ConversionMode(mode or cfg.conversion_mode)
    console.print(
        f"[bold]Converting:[/bold] {dtsx_file.name}  "
        f"[dim]mode={resolved_mode.value}  model={cfg.copilot_model}[/dim]"
    )

    if resolved_mode != ConversionMode.DETERMINISTIC and not (github_token or cfg.has_llm_token):
        console.print("[yellow]⚠ GITHUB_TOKEN not set — LLM phases will be skipped.[/yellow]")
        console.print("[dim]  Add GITHUB_TOKEN to .env or export it in your shell.[/dim]")

    config = PipelineConfig(
        output_dir=output or cfg.output_dir,
        mode=resolved_mode,
        github_token=github_token or cfg.github_token or None,
        spark_version=spark_version or cfg.spark_version,
    )
    pipeline = MigrationPipeline(config)
    result = pipeline.run(dtsx_file)

    _print_result(result)
    sys.exit(0 if not result.error else 1)


# ─── compare ─────────────────────────────────────────────────────────────────

@main.command()
@click.argument("dtsx_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None)
def compare(dtsx_file: Path, output: Path | None, github_token: str | None) -> None:
    """
    Run deterministic, hybrid, and llm modes on the same file and
    print a side-by-side comparison table.
    """
    from ssis_migration.config import cfg
    from ssis_migration.pipeline import ConversionMode, MigrationPipeline, PipelineConfig

    token = github_token or cfg.github_token or None
    base_out = output or cfg.output_dir

    results = {}
    for m in ConversionMode:
        mode_out = base_out / f"compare_{m.value}"
        config = PipelineConfig(
            output_dir=mode_out,
            mode=m,
            github_token=token,
            spark_version=cfg.spark_version,
        )
        console.print(f"\n[bold cyan]Running mode: {m.value}[/bold cyan]")
        pipeline = MigrationPipeline(config)
        results[m.value] = pipeline.run(dtsx_file)

    # ── comparison table ──────────────────────────────────────────────────────
    console.print("\n")
    table = Table(title=f"Mode Comparison: {dtsx_file.name}", show_lines=True)
    table.add_column("Metric", style="cyan", width=30)
    for m in ConversionMode:
        table.add_column(m.value, justify="center", width=18)

    def _row(label: str, fn) -> None:
        table.add_row(label, *[str(fn(results[m.value])) for m in ConversionMode])

    _row("Pipeline error", lambda r: r.error or "—")
    _row("Validation passed", lambda r: "✓" if (r.validation_report and r.validation_report.passed) else "✗")
    _row("Errors", lambda r: str(len(r.validation_report.errors)) if r.validation_report else "—")
    _row("Warnings", lambda r: str(len(r.validation_report.warnings)) if r.validation_report else "—")
    _row("Det. coverage", lambda r: (
        f"{r.cir.conversion_metadata.deterministic_coverage*100:.0f}%"
        if r.cir else "—"
    ))
    _row("LLM items", lambda r: (
        str(len(r.cir.conversion_metadata.llm_required_items))
        if r.cir else "—"
    ))
    _row("Human review items", lambda r: (
        str(len(r.cir.conversion_metadata.human_review_required))
        if r.cir else "—"
    ))
    _row("Output module", lambda r: r.module_path.name if r.module_path else "—")

    console.print(table)

    # Save comparison JSON
    compare_path = base_out / f"compare_{dtsx_file.stem}.json"
    compare_path.parent.mkdir(parents=True, exist_ok=True)
    compare_data = {}
    for m, r in results.items():
        compare_data[m] = {
            "error": r.error,
            "validation_passed": r.validation_report.passed if r.validation_report else None,
            "errors": len(r.validation_report.errors) if r.validation_report else None,
            "warnings": len(r.validation_report.warnings) if r.validation_report else None,
            "deterministic_coverage": (
                r.cir.conversion_metadata.deterministic_coverage if r.cir else None
            ),
            "llm_required_items": (
                len(r.cir.conversion_metadata.llm_required_items) if r.cir else None
            ),
            "human_review_items": (
                len(r.cir.conversion_metadata.human_review_required) if r.cir else None
            ),
            "output_module": str(r.module_path) if r.module_path else None,
        }
    compare_path.write_text(json.dumps(compare_data, indent=2), encoding="utf-8")
    console.print(f"\n[green]Comparison JSON:[/green] {compare_path}")


# ─── pipeline (batch) ────────────────────────────────────────────────────────

@main.command("pipeline")
@click.argument("dtsx_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--wave", type=click.Choice(["all", "simple", "medium", "high", "very_high"]),
              default="all")
@click.option("--mode", "-m", type=click.Choice(["deterministic", "hybrid", "llm", "auto"]), default=None)
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None)
def run_pipeline(
    dtsx_dir: Path, output: Path | None, wave: str,
    mode: str | None, github_token: str | None,
) -> None:
    """Run the migration pipeline on all .dtsx files in a directory."""
    from ssis_migration.config import cfg
    from ssis_migration.inventory import build_inventory
    from ssis_migration.pipeline import ConversionMode, MigrationPipeline, PipelineConfig

    resolved_mode = ConversionMode(mode or cfg.conversion_mode)
    inventory = build_inventory(dtsx_dir)

    wave_map = {
        "simple": ["simple"], "medium": ["medium"],
        "high": ["high"], "very_high": ["very_high"],
        "all": ["simple", "medium", "high", "very_high"],
    }
    target_packages = [
        p for p in inventory["packages"]
        if p.get("complexity", "unknown") in wave_map[wave]
    ]

    console.print(
        f"[bold]Pipeline:[/bold] {len(target_packages)} packages "
        f"[dim]wave={wave}  mode={resolved_mode.value}[/dim]"
    )

    config = PipelineConfig(
        output_dir=output or cfg.output_dir,
        mode=resolved_mode,
        github_token=github_token or cfg.github_token or None,
        spark_version=cfg.spark_version,
    )
    pipeline = MigrationPipeline(config)

    counts = {"pass": 0, "fail": 0, "error": 0}
    for pkg in target_packages:
        dtsx_path = dtsx_dir / pkg["file"]
        if not dtsx_path.exists():
            continue
        result = pipeline.run(dtsx_path)
        if result.error:
            counts["error"] += 1
            console.print(f"  [red]ERROR[/red] {dtsx_path.name}: {result.error}")
        elif result.success:
            counts["pass"] += 1
            console.print(f"  [green]PASS[/green]  {dtsx_path.name}")
        else:
            counts["fail"] += 1
            console.print(f"  [yellow]WARN[/yellow]  {dtsx_path.name}")

    console.print(
        f"\nSummary: [green]{counts['pass']} passed[/green], "
        f"[yellow]{counts['fail']} with warnings[/yellow], "
        f"[red]{counts['error']} errors[/red]"
    )


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _print_result(result) -> None:
    if result.error:
        console.print(f"[red]FAILED:[/red] {result.error}")
        return

    if result.cir:
        cov = result.cir.conversion_metadata.deterministic_coverage
        llm_n = len(result.cir.conversion_metadata.llm_required_items)
        hr_n = len(result.cir.conversion_metadata.human_review_required)
        console.print(
            f"  det. coverage=[cyan]{cov*100:.0f}%[/cyan]  "
            f"llm_items=[yellow]{llm_n}[/yellow]  "
            f"human_review=[red]{hr_n}[/red]"
        )

    if result.routing_plan is not None:
        counts = result.routing_plan.counts()
        console.print(
            f"  AUTO routing: deterministic=[cyan]{counts['deterministic']}[/cyan]  "
            f"llm=[yellow]{counts['llm']}[/yellow]  "
            f"human_review=[red]{counts['human_review']}[/red]"
        )

    if result.validation_report:
        status = "[green]PASS[/green]" if result.validation_report.passed else "[red]FAIL[/red]"
        console.print(f"  Validation: {status}")
        for f in result.validation_report.errors[:5]:
            console.print(f"    [red]ERR[/red]  [{f.code}] {f.message}")
        for f in result.validation_report.warnings[:3]:
            console.print(f"    [yellow]WARN[/yellow] [{f.code}] {f.message}")

    if result.module_path:
        console.print(f"  [green]Output:[/green] {result.module_path}")


if __name__ == "__main__":
    main()

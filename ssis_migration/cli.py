"""
CLI entry point: ssis-migrate

Commands:
  assess    Phase 0 — scan packages, build inventory & wave plan
  convert   Run full pipeline on a single .dtsx file
  pipeline  Run full pipeline on a directory (all or by wave)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
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


# ─── assess ──────────────────────────────────────────────────────────────────

@main.command()
@click.argument("dtsx_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=Path("output"),
              help="Output directory for inventory_report.json and wave_plan.json")
def assess(dtsx_dir: Path, output: Path) -> None:
    """Phase 0: Scan packages and generate inventory report + wave plan."""
    from ssis_migration.inventory import build_inventory, save_inventory

    console.print(f"[bold]Scanning:[/bold] {dtsx_dir}")
    inventory = build_inventory(dtsx_dir)
    save_inventory(inventory, output)

    # Pretty-print summary
    table = Table(title="Complexity Summary")
    table.add_column("Complexity", style="cyan")
    table.add_column("Count", justify="right")
    for level, count in inventory["complexity_summary"].items():
        table.add_row(level, str(count))
    console.print(table)
    console.print(f"[green]Saved:[/green] {output}/inventory_report.json, wave_plan.json")


# ─── convert ─────────────────────────────────────────────────────────────────

@main.command()
@click.argument("dtsx_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=Path("output"),
              help="Output directory")
@click.option("--no-llm", is_flag=True, help="Disable LLM augmentation phase")
@click.option("--spark-version", default="3.3", help="Target PySpark version (default: 3.3)")
@click.option("--github-token", envvar="GITHUB_TOKEN",
              help="GitHub Copilot token (or set GITHUB_TOKEN env var)")
def convert(
    dtsx_file: Path, output: Path, no_llm: bool,
    spark_version: str, github_token: str | None,
) -> None:
    """Run the full migration pipeline on a single .dtsx file."""
    from ssis_migration.pipeline import MigrationPipeline, PipelineConfig

    config = PipelineConfig(
        output_dir=output,
        enable_llm=not no_llm,
        github_token=github_token,
        spark_version=spark_version,
    )
    pipeline = MigrationPipeline(config)

    console.print(f"[bold]Converting:[/bold] {dtsx_file.name}")
    result = pipeline.run(dtsx_file)

    if result.error:
        console.print(f"[red]FAILED:[/red] {result.error}")
        sys.exit(1)

    if result.validation_report:
        status = "[green]PASS[/green]" if result.validation_report.passed else "[red]FAIL[/red]"
        console.print(f"Validation: {status}")
        for finding in result.validation_report.errors:
            console.print(f"  [red]ERROR[/red] [{finding.code}] {finding.message}")
        for finding in result.validation_report.warnings[:5]:
            console.print(f"  [yellow]WARN[/yellow]  [{finding.code}] {finding.message}")

    if result.module_path:
        console.print(f"[green]Output:[/green] {result.module_path}")


# ─── pipeline ────────────────────────────────────────────────────────────────

@main.command("pipeline")
@click.argument("dtsx_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=Path("output"))
@click.option("--wave", type=click.Choice(["all", "simple", "medium", "high", "very_high"]),
              default="all", help="Run only packages of the specified complexity")
@click.option("--no-llm", is_flag=True)
@click.option("--spark-version", default="3.3")
@click.option("--github-token", envvar="GITHUB_TOKEN")
def run_pipeline(
    dtsx_dir: Path, output: Path, wave: str, no_llm: bool,
    spark_version: str, github_token: str | None,
) -> None:
    """Run the migration pipeline on all .dtsx files in a directory."""
    from ssis_migration.inventory import build_inventory
    from ssis_migration.pipeline import MigrationPipeline, PipelineConfig

    inventory = build_inventory(dtsx_dir)

    # Filter by wave/complexity
    wave_map = {
        "simple": ["simple"],
        "medium": ["medium"],
        "high": ["high"],
        "very_high": ["very_high"],
        "all": ["simple", "medium", "high", "very_high"],
    }
    target_complexities = wave_map[wave]

    target_packages = [
        p for p in inventory["packages"]
        if p.get("complexity", "unknown") in target_complexities
    ]

    console.print(f"[bold]Running pipeline:[/bold] {len(target_packages)} packages (wave={wave})")

    config = PipelineConfig(
        output_dir=output, enable_llm=not no_llm,
        github_token=github_token, spark_version=spark_version,
    )
    pipeline = MigrationPipeline(config)

    results = {"pass": 0, "fail": 0, "error": 0}
    for pkg in target_packages:
        dtsx_path = dtsx_dir / pkg["file"]
        if not dtsx_path.exists():
            continue
        result = pipeline.run(dtsx_path)
        if result.error:
            results["error"] += 1
            console.print(f"  [red]ERROR[/red] {dtsx_path.name}: {result.error}")
        elif result.success:
            results["pass"] += 1
            console.print(f"  [green]PASS[/green]  {dtsx_path.name}")
        else:
            results["fail"] += 1
            console.print(f"  [yellow]WARN[/yellow]  {dtsx_path.name}: validation issues")

    console.print(
        f"\nSummary: [green]{results['pass']} passed[/green], "
        f"[yellow]{results['fail']} with warnings[/yellow], "
        f"[red]{results['error']} errors[/red]"
    )


if __name__ == "__main__":
    main()

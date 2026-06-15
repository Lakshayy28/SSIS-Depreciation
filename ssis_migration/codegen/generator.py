"""
Code Generator — renders CIR objects to PySpark .py modules via Jinja2.

Outputs per package:
  - {package_name}.py      — the PySpark module
  - test_{package_name}.py — test scaffold

Post-processing (when tools are available):
  - black (formatting)
  - isort (import ordering)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ssis_migration.cir.models import CIR

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _python_identifier(name: str) -> str:
    """Convert a string to a valid Python identifier (snake_case)."""
    name = re.sub(r'[^a-z0-9]+', '_', name.lower())
    name = name.strip('_')
    if name and name[0].isdigit():
        name = 'flow_' + name
    return name or 'unnamed'


def _pascal_case(name: str) -> str:
    return ''.join(word.capitalize() for word in re.split(r'[^a-z0-9]+', name.lower()) if word)


def _truncate(text: str, length: int = 80) -> str:
    return (text[:length] + '...') if len(text) > length else text


def _tojson(value) -> str:
    if value is None:
        return 'None'
    return json.dumps(value)


def _enumerate_filter(iterable):
    return enumerate(iterable)


def _selectattr(iterable, attr, *args):
    """Minimal selectattr implementation for use in templates."""
    for item in iterable:
        if hasattr(item, attr):
            if len(args) == 2 and args[0] == "equalto":
                if getattr(item, attr) == args[1]:
                    yield item
            else:
                if getattr(item, attr):
                    yield item


def _map_attr(iterable, attr):
    for item in iterable:
        if hasattr(item, attr):
            yield getattr(item, attr)


class CodeGenerator:
    """
    Renders a resolved CIR to a PySpark module.

    Usage:
        gen = CodeGenerator(output_dir=Path("./output"))
        paths = gen.generate(cir)
        # paths = {"module": Path("output/customer_load.py"), "test": Path(...)}
    """

    def __init__(self, output_dir: Path | str = Path("output")) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape([]),   # Python files — no HTML escaping
            keep_trailing_newline=True,
        )
        # Register custom filters
        self._env.filters["python_identifier"] = _python_identifier
        self._env.filters["pascal_case"] = _pascal_case
        self._env.filters["truncate"] = _truncate
        self._env.filters["tojson"] = _tojson

        # Register global functions
        self._env.globals["enumerate"] = _enumerate_filter

    def generate(self, cir: CIR) -> dict[str, Path]:
        module_name = _python_identifier(
            cir.metadata.source_file.removesuffix(".dtsx")
        )

        context = {
            **cir.model_dump(by_alias=True),
            # Re-inject typed objects so templates can access methods/enums
            "metadata": cir.metadata,
            "parameters": cir.parameters,
            "variables": cir.variables,
            "connections": cir.connections,
            "control_flow": cir.control_flow,
            "data_flows": cir.data_flows,
            "conversion_metadata": cir.conversion_metadata,
            "module_name": module_name,
        }

        module_path = self._output_dir / f"{module_name}.py"
        test_path = self._output_dir / f"test_{module_name}.py"

        # Render module
        module_code = self._render("module.py.j2", context)
        module_path.write_text(module_code, encoding="utf-8")
        logger.info("Generated module: %s", module_path)

        # Render test scaffold
        test_code = self._render("test_module.py.j2", context)
        test_path.write_text(test_code, encoding="utf-8")
        logger.info("Generated test scaffold: %s", test_path)

        # Post-process with black + isort
        self._format(module_path)
        self._format(test_path)

        return {"module": module_path, "test": test_path}

    def _render(self, template_name: str, context: dict) -> str:
        template = self._env.get_template(template_name)
        return template.render(**context)

    def _format(self, path: Path) -> None:
        """Run black and isort on the generated file (best-effort)."""
        for tool in ("black", "isort"):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", tool, str(path)],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    logger.debug("%s returned non-zero for %s: %s", tool, path.name, result.stderr[:200])
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                logger.debug("%s not available or timed out: %s", tool, exc)


class AirflowDAGGenerator:
    """
    Generates Airflow DAGs for a cluster of related packages.
    """

    def __init__(self, output_dir: Path | str = Path("output/dags")) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape([]),
        )
        self._env.filters["python_identifier"] = _python_identifier

    def generate(
        self,
        cluster_name: str,
        packages: list[dict],
        dependencies: list[dict],
        wave: str = "wave1",
        schedule_interval: str = "0 2 * * *",
    ) -> Path:
        from datetime import datetime
        context = {
            "cluster_name": cluster_name,
            "packages": packages,
            "dependencies": dependencies,
            "wave": wave,
            "schedule_interval": schedule_interval,
            "generated_at": datetime.utcnow().isoformat(),
        }
        dag_path = self._output_dir / f"{_python_identifier(cluster_name)}_dag.py"
        template = self._env.get_template("airflow_dag.py.j2")
        dag_path.write_text(template.render(**context), encoding="utf-8")
        logger.info("Generated Airflow DAG: %s", dag_path)
        return dag_path

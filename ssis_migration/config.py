"""
Central configuration loader.

Reads `.env` (if present) then falls back to environment variables.
All other modules import from here instead of reading os.environ directly.

Usage:
    from ssis_migration.config import cfg
    print(cfg.copilot_model)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env file from repo root (or any parent directory).
# python-dotenv does NOT override already-set environment variables.
def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        # Walk up from CWD looking for .env
        here = Path.cwd()
        for candidate in [here, *here.parents]:
            env_file = candidate / ".env"
            if env_file.exists():
                load_dotenv(env_file, override=False)
                return
        # Also try the package source root
        pkg_root = Path(__file__).parent.parent
        env_file = pkg_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
    except ImportError:
        pass  # python-dotenv not installed; rely on shell environment


_load_dotenv()


@dataclass(frozen=True)
class Config:
    # ── GitHub Copilot ────────────────────────────────────────────────────────
    github_token: str = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN", "")
    )
    copilot_model: str = field(
        default_factory=lambda: os.environ.get("COPILOT_MODEL", "gpt-4o-mini")
    )
    copilot_temperature: float = field(
        default_factory=lambda: float(os.environ.get("COPILOT_TEMPERATURE", "0.1"))
    )
    copilot_max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("COPILOT_MAX_TOKENS", "4096"))
    )
    copilot_max_review_iterations: int = field(
        default_factory=lambda: int(os.environ.get("COPILOT_MAX_REVIEW_ITERATIONS", "3"))
    )

    # ── Pipeline ──────────────────────────────────────────────────────────────
    conversion_mode: str = field(
        default_factory=lambda: os.environ.get("CONVERSION_MODE", "hybrid")
    )
    llm_confidence_threshold: float = field(
        default_factory=lambda: float(os.environ.get("LLM_CONFIDENCE_THRESHOLD", "0.50"))
    )

    # ── Output ────────────────────────────────────────────────────────────────
    output_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("OUTPUT_DIR", "output"))
    )
    spark_version: str = field(
        default_factory=lambda: os.environ.get("SPARK_VERSION", "3.3")
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO")
    )

    @property
    def has_llm_token(self) -> bool:
        return bool(self.github_token)

    def summary(self) -> str:
        token_status = "SET" if self.has_llm_token else "NOT SET"
        return (
            f"model={self.copilot_model}  mode={self.conversion_mode}  "
            f"spark={self.spark_version}  token={token_status}"
        )


# Singleton — import this everywhere
cfg = Config()

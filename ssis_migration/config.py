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
        default_factory=lambda: os.environ.get("COPILOT_MODEL", "claude-haiku-4.5")
    )
    # Reviewer model. claude-haiku-4.5 has proven stronger than gpt-4o for this
    # review/critique task, so it is the default for BOTH generation and review.
    # Override with COPILOT_REVIEWER_MODEL in .env to use a different reviewer.
    copilot_reviewer_model: str = field(
        default_factory=lambda: os.environ.get("COPILOT_REVIEWER_MODEL", "claude-haiku-4.5")
    )
    copilot_temperature: float = field(
        default_factory=lambda: float(os.environ.get("COPILOT_TEMPERATURE", "0.1"))
    )
    copilot_max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("COPILOT_MAX_TOKENS", "4096"))
    )
    # Max review→regen iterations per component (each is one generation + one review call)
    copilot_max_review_iterations: int = field(
        default_factory=lambda: int(os.environ.get("COPILOT_MAX_REVIEW_ITERATIONS", "4"))
    )
    # Max outer functional-validation passes per package (each re-converts all LLM items)
    functional_validation_max_iterations: int = field(
        default_factory=lambda: int(os.environ.get("FUNCTIONAL_VALIDATION_MAX_ITERATIONS", "2"))
    )

    # Max LLM syntax-edit attempts per artifact (chunk / item / whole file).
    # Deterministic repair (fences, whitespace, unicode) always runs first.
    syntax_fix_max_iterations: int = field(
        default_factory=lambda: int(os.environ.get("SYNTAX_FIX_MAX_ITERATIONS", "2"))
    )

    # ── Resilience / NFRs (Copilot API) ───────────────────────────────────────
    # Per-request timeout in seconds.
    copilot_request_timeout: float = field(
        default_factory=lambda: float(os.environ.get("COPILOT_REQUEST_TIMEOUT", "120"))
    )
    # Max HTTP attempts per call (transport errors, 429, 5xx). 4xx never retried.
    copilot_max_retries: int = field(
        default_factory=lambda: int(os.environ.get("COPILOT_MAX_RETRIES", "3"))
    )
    # Circuit breaker: open after this many consecutive failed calls.
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.environ.get("COPILOT_CIRCUIT_BREAKER_THRESHOLD", "5"))
    )
    # Circuit breaker: seconds to stay open before allowing a half-open probe.
    circuit_breaker_cooldown: float = field(
        default_factory=lambda: float(os.environ.get("COPILOT_CIRCUIT_BREAKER_COOLDOWN", "30"))
    )
    # Client-side rate limit in requests/minute (0 = unlimited).
    copilot_rate_limit_per_min: float = field(
        default_factory=lambda: float(os.environ.get("COPILOT_RATE_LIMIT_PER_MIN", "0"))
    )

    # ── Pipeline ──────────────────────────────────────────────────────────────
    conversion_mode: str = field(
        default_factory=lambda: os.environ.get("CONVERSION_MODE", "auto")
    )
    llm_confidence_threshold: float = field(
        default_factory=lambda: float(os.environ.get("LLM_CONFIDENCE_THRESHOLD", "0.50"))
    )
    # Composite scorecard threshold (parsing × functional) a package must clear.
    migration_pass_threshold: float = field(
        default_factory=lambda: float(os.environ.get("MIGRATION_PASS_THRESHOLD", "0.75"))
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

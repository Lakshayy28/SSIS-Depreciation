"""Validation report model — aggregates findings from all validation stages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Finding:
    stage: str           # static | semantic | data | network
    severity: Severity
    code: str            # short identifier e.g. "SYNTAX_ERROR"
    message: str
    location: str = ""   # file path, component id, column name, etc.
    detail: str = ""


@dataclass
class ValidationReport:
    source_file: str
    module_path: str
    findings: list[Finding] = field(default_factory=list)
    acceptable_divergences: list[str] = field(default_factory=list)

    # Aggregate counts
    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def error(self, code: str, message: str, stage: str = "static",
              location: str = "", detail: str = "") -> None:
        self.add(Finding(stage, Severity.ERROR, code, message, location, detail))

    def warn(self, code: str, message: str, stage: str = "static",
             location: str = "", detail: str = "") -> None:
        self.add(Finding(stage, Severity.WARNING, code, message, location, detail))

    def info(self, code: str, message: str, stage: str = "static",
             location: str = "", detail: str = "") -> None:
        self.add(Finding(stage, Severity.INFO, code, message, location, detail))

    def save(self, path: Path | str) -> None:
        data = {
            "source_file": self.source_file,
            "module_path": self.module_path,
            "passed": self.passed,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "acceptable_divergences": self.acceptable_divergences,
            "findings": [
                {
                    "stage": f.stage,
                    "severity": f.severity.value,
                    "code": f.code,
                    "message": f.message,
                    "location": f.location,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] {self.source_file}: "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings"
        )

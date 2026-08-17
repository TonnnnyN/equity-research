from __future__ import annotations

import os
from dataclasses import dataclass

from equity_research.config import ProjectConfig


@dataclass(slots=True)
class PreflightCheck:
    name: str
    ok: bool
    message: str
    blocking: bool = True


@dataclass(slots=True)
class PreflightSummary:
    checks: list[PreflightCheck]

    @property
    def ready(self) -> bool:
        return all(check.ok or not check.blocking for check in self.checks)

    def to_lines(self) -> list[str]:
        lines = [f"Preflight: {'READY' if self.ready else 'BLOCKED'}"]
        for check in self.checks:
            state = "OK" if check.ok else ("WARN" if not check.blocking else "FAIL")
            lines.append(f"- [{state}] {check.name}: {check.message}")
        return lines


def _build_api_key_checks(config: ProjectConfig) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    key_value = os.environ.get("SEC_USER_AGENT")
    if key_value and key_value.strip():
        checks.append(
            PreflightCheck(
                "SEC_USER_AGENT",
                True,
                f"configured (value: {key_value[:50]}...)",
                blocking=True,
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "SEC_USER_AGENT",
                False,
                "missing or empty — set in environment",
                blocking=True,
            )
        )
    return checks


def build_preflight_summary(config: ProjectConfig) -> PreflightSummary:
    checks: list[PreflightCheck] = []
    checks.extend(_build_api_key_checks(config))
    return PreflightSummary(checks=checks)

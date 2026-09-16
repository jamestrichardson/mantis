"""Deterministic scoring: evaluate a scenario's expectations against a
completed EvalResult. No LLM-as-judge — see mantis.eval.expectations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mantis.eval.expectations import Expectation
    from mantis.eval.results import EvalResult


@dataclass(frozen=True)
class CheckResult:
    """One expectation's outcome against a single run."""

    label: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScoreReport:
    """Every expectation's outcome for one run, in the order they were
    declared on the scenario."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [c.to_dict() for c in self.checks],
            "passed": self.passed_count,
            "total": self.total_count,
        }


def score_result(expectations: list["Expectation"], result: "EvalResult") -> ScoreReport:
    """Evaluate every expectation against `result`, in order.

    Never raises for an individual expectation's own check() logic being
    wrong for this particular result — a check simply fails (passed=False)
    with whatever detail it returns. A genuine bug in a check()
    implementation (e.g. an AttributeError) does propagate, same
    reasoning as mantis.eval.runner.run_scenario: a Mantis bug must never
    be silently absorbed as if it were a legitimate finding about the
    model.
    """
    checks = []
    for expectation in expectations:
        passed, detail = expectation.check(result)
        checks.append(CheckResult(label=expectation.display_label(), passed=passed, detail=detail))
    return ScoreReport(checks=checks)

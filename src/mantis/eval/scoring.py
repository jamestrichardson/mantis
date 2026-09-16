"""Deterministic scoring: evaluate a scenario's expectations against a
completed EvalResult. No LLM-as-judge, no network calls, no subjective
model-generated grading — see mantis.eval.expectations.

The central design decision: hard requirements gate pass/fail
independently of the numeric score. A model that fabricates a root
cause but passes every other check must not "average out" to a passing
grade (e.g. 9/10 -> 90% -> PASS). See ``Evaluation.passed``.
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

    name: str
    passed: bool
    hard: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Evaluation:
    """Every expectation's outcome for one run, in declaration order.

    ``passed`` is governed solely by ``hard_failures`` — a run with a
    perfect numeric score but a hard failure is not passing, and a run
    that missed several quality checks but has zero hard failures is.
    ``score``/``max_score`` are a separate, uniformly-weighted (1 point
    per check) signal for ranking/comparison, independent of pass/fail.
    """

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[str]:
        return [c.name for c in self.checks if c.hard and not c.passed]

    @property
    def passed(self) -> bool:
        return len(self.hard_failures) == 0

    @property
    def score(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def max_score(self) -> int:
        return len(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "max_score": self.max_score,
            "checks": [c.to_dict() for c in self.checks],
            "hard_failures": self.hard_failures,
        }


def evaluate_result(expectations: list["Expectation"], result: "EvalResult") -> Evaluation:
    """Evaluate every expectation against `result`, in declaration order.

    Names are disambiguated within this evaluation if two expectations
    resolve to the same name (e.g. two ``RequiredAnswerPattern`` checks
    without explicit names both defaulting similarly) — the second
    occurrence becomes ``"<name>#2"``, the third ``"<name>#3"``, etc., so
    every check in the output is uniquely identifiable without forcing
    every scenario author to hand-name every check.

    Never raises for an individual expectation's own check() logic being
    wrong for this particular result — a check simply fails (passed=False)
    with whatever detail it returns. A genuine bug in a check()
    implementation (e.g. an AttributeError) does propagate — same
    reasoning as mantis.eval.runner.run_scenario: a Mantis bug must never
    be silently absorbed as if it were a legitimate finding about the
    model.
    """
    seen_names: dict[str, int] = {}
    checks = []
    for expectation in expectations:
        base_name = expectation.resolved_name()
        seen_names[base_name] = seen_names.get(base_name, 0) + 1
        occurrence = seen_names[base_name]
        name = base_name if occurrence == 1 else f"{base_name}#{occurrence}"

        passed, detail = expectation.check(result)
        checks.append(CheckResult(name=name, passed=passed, hard=expectation.hard, detail=detail))
    return Evaluation(checks=checks)

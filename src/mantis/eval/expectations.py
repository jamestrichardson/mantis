"""Deterministic scoring expectations for evaluation scenarios.

An :class:`Expectation` is a small, deterministic (no LLM-as-judge) check
against a completed :class:`~mantis.eval.results.EvalResult` — "was this
tool called exactly once", "does the final answer mention X", "did the
model avoid claiming Y". A :class:`~mantis.eval.scenarios.Scenario`'s
``expectations`` list is evaluated by
:func:`mantis.eval.scoring.score_result` after a run completes.

Deliberately deterministic: substring/count matching, not semantic
judgment. "Correctly reported only one failed job exists" is checked by
looking for one of a few expected phrasings, not by asking another model
whether the claim is accurate — LLM-as-judge scoring is explicitly out of
scope (see docs/evaluation.md and issue #36).

Every concrete type here is a frozen dataclass exposing ``display_label()``
(a human-readable line for `mantis eval run`'s PASS/FAIL output — override
with the ``label`` field for a scenario-specific phrasing, as in the
`awx-no-route` scenario) and ``check(result)`` returning
``(passed, detail)``. New expectation types don't need to inherit from
anything — only satisfy this shape (see the :class:`Expectation` Protocol)
— so a scenario author can add a narrow, scenario-specific check without
touching this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mantis.eval.results import EvalResult


class Expectation(Protocol):
    """Structural shape every expectation type satisfies."""

    label: str | None

    def display_label(self) -> str: ...

    def check(self, result: "EvalResult") -> tuple[bool, str]: ...


def _as_list(patterns: str | list[str]) -> list[str]:
    return [patterns] if isinstance(patterns, str) else list(patterns)


def _find_match(
    text: str, patterns: str | list[str], *, case_sensitive: bool
) -> str | None:
    """Return the first pattern found in `text`, or None."""
    haystack = text if case_sensitive else text.lower()
    for pattern in _as_list(patterns):
        needle = pattern if case_sensitive else pattern.lower()
        if needle in haystack:
            return pattern
    return None


@dataclass(frozen=True)
class RequiredToolCall:
    """The named tool must have been called within
    ``[min_count, max_count]`` times.

    Counts both "ok" and "duplicate" outcomes — a duplicate call still
    represents the model successfully getting real data (replayed from
    cache), it just didn't re-execute the integration. Does not count
    "error"/"unknown_tool"/"bad_arguments" attempts.
    """

    tool_name: str
    min_count: int = 1
    max_count: int | None = None
    label: str | None = None

    def display_label(self) -> str:
        if self.label:
            return self.label
        if self.max_count == self.min_count:
            return f"called {self.tool_name} exactly {self.min_count} time(s)"
        if self.max_count is None:
            return f"called {self.tool_name} at least {self.min_count} time(s)"
        return f"called {self.tool_name} {self.min_count}-{self.max_count} time(s)"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        count = sum(
            1
            for tc in result.tool_calls
            if tc.tool_name == self.tool_name and tc.outcome in ("ok", "duplicate")
        )
        passed = count >= self.min_count and (self.max_count is None or count <= self.max_count)
        return passed, f"{self.tool_name} called {count} time(s)"


@dataclass(frozen=True)
class MaxToolCalls:
    """No more than ``count`` tool-call attempts total, of any tool and
    any outcome (successful, duplicate, malformed, unknown) — a broad
    guard against runaway/looping behavior, independent of any single
    tool's own count. Distinct from :class:`RequiredToolCall`'s
    ``max_count``, which only bounds one named tool.
    """

    count: int
    label: str | None = None

    def display_label(self) -> str:
        return self.label or f"made no more than {self.count} tool call(s) total"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        total = len(result.tool_calls)
        return total <= self.count, f"{total} tool call(s) total"


@dataclass(frozen=True)
class RequiredEvidence:
    """The final answer must contain at least one of ``patterns``
    (case-insensitive by default) — evidence the model actually surfaced
    to the user, not just that a tool returned it somewhere in its raw
    result.
    """

    patterns: str | list[str]
    label: str | None = None
    case_sensitive: bool = False

    def display_label(self) -> str:
        if self.label:
            return self.label
        patterns = _as_list(self.patterns)
        if len(patterns) == 1:
            return f'mentions "{patterns[0]}"'
        return f"mentions one of {patterns}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        text = result.final_answer or ""
        match = _find_match(text, self.patterns, case_sensitive=self.case_sensitive)
        if match is not None:
            return True, f'found "{match}"'
        return False, "none of the expected phrases were found in the final answer"


@dataclass(frozen=True)
class ForbiddenClaim:
    """The final answer must NOT contain any of ``patterns``
    (case-insensitive by default) — for claims that overstate the
    evidence, e.g. asserting a specific unproven root cause.
    """

    patterns: str | list[str]
    label: str | None = None
    case_sensitive: bool = False

    def display_label(self) -> str:
        if self.label:
            return self.label
        patterns = _as_list(self.patterns)
        if len(patterns) == 1:
            return f'does not claim "{patterns[0]}"'
        return f"does not claim any of {patterns}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        text = result.final_answer or ""
        match = _find_match(text, self.patterns, case_sensitive=self.case_sensitive)
        if match is not None:
            return False, f'found forbidden phrase "{match}"'
        return True, "no forbidden phrases found"


@dataclass(frozen=True)
class MustProduceFinalAnswer:
    """The run must have ended with ``outcome="ok"`` and non-empty,
    non-whitespace answer text.

    Fails for a model that errored (``outcome="error"``), or one that
    produced neither a tool call nor usable answer text — see
    ``AgentRuntime.diagnostic_raw_message`` for how that case is
    diagnosed.
    """

    label: str | None = None

    def display_label(self) -> str:
        return self.label or "produced a final answer"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        if result.outcome != "ok":
            return False, f"run outcome was {result.outcome!r}, not ok"
        if not (result.final_answer or "").strip():
            return False, "final answer was empty"
        return True, "produced a non-empty final answer"

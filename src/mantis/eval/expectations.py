"""Deterministic scoring expectations for evaluation scenarios.

An :class:`Expectation` is a small, deterministic (no LLM-as-judge, no
network calls, no subjective model-generated grading) check against a
completed :class:`~mantis.eval.results.EvalResult`. A
:class:`~mantis.eval.scenarios.Scenario`'s ``expectations`` list is
evaluated by :func:`mantis.eval.scoring.evaluate_result` after a run
completes.

Hard vs. quality
-----------------

Every expectation is either a **hard requirement** or a **quality
check** (see each type's ``hard`` default below; every type accepts an
explicit ``hard=`` override since the same check can be a hard
requirement in one scenario and a nice-to-have in another). This
distinction is what :func:`mantis.eval.scoring.evaluate_result` uses to
compute ``passed``: a scenario run only passes if **zero hard
requirements failed**, regardless of the numeric score. A model that
fabricates a root cause but otherwise gives a well-organized, mostly
correct answer must not "average out" to a passing grade — see
docs/evaluation.md for the worked example this guards against.

What this does NOT score
-------------------------

Prose quality — conciseness, tone, "sounds like good troubleshooting
advice" — is deliberately out of scope. Every check here is about
correctness and agent behavior: tool use, grounding in the fixture,
unsupported claims, stopping behavior, error-source attribution, and
evidence coverage. There is no dimension anywhere in this module for
"is this answer well-written."

Every concrete type is a frozen dataclass exposing:

- ``name`` — machine-stable identifier (auto-derived per type if not
  given explicitly; :func:`mantis.eval.scoring.evaluate_result`
  disambiguates collisions within one scenario by appending ``#2``,
  ``#3``, ...).
- ``hard`` — whether failing this expectation is a hard failure.
- ``check(result) -> (passed, detail)``.

New types don't need to inherit from anything — only satisfy that shape
(see the :class:`Expectation` Protocol) — so a scenario-specific check
never requires touching this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from mantis.eval.results import EvalResult, ToolCallSummary


class Expectation(Protocol):
    """Structural shape every expectation type satisfies."""

    name: str | None
    hard: bool

    def resolved_name(self) -> str: ...

    def check(self, result: "EvalResult") -> tuple[bool, str]: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _as_list(patterns: str | list[str]) -> list[str]:
    return [patterns] if isinstance(patterns, str) else list(patterns)


def _successful_calls(result: "EvalResult", tool_name: str | None = None) -> list["ToolCallSummary"]:
    """Tool calls that got the model real data — "ok" (freshly executed)
    or "duplicate" (replayed from cache); both mean the model received
    real evidence. Optionally filtered to one tool name."""
    return [
        tc
        for tc in result.tool_calls
        if tc.outcome in ("ok", "duplicate") and (tool_name is None or tc.tool_name == tool_name)
    ]


def _split_sentences(text: str) -> list[str]:
    """Crude, deterministic sentence splitter — good enough for the
    short, plain-prose final answers these checks run against. Not
    meant to handle arbitrary prose correctly, just consistently."""
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _tool_error_kind_present(value: Any, kind: str) -> bool:
    """Recursively search a tool result (dict/list of arbitrary JSON-safe
    data) for a ``mantis.contracts.ToolError``-shaped entry
    (``{"kind": ..., "message": ...}``) matching ``kind`` anywhere in it
    — e.g. a per-job ``stdout_retrieval_error``. Contract-aware rather
    than hardcoded to one field name, so this works for any tool that
    adopts ``mantis.contracts.ToolError`` (see docs/tools.md), not just
    AWX.
    """
    if isinstance(value, dict):
        if value.get("kind") == kind:
            return True
        return any(_tool_error_kind_present(v, kind) for v in value.values())
    if isinstance(value, list):
        return any(_tool_error_kind_present(v, kind) for v in value)
    return False


def _any_tool_error_present(value: Any) -> bool:
    """Like :func:`_tool_error_kind_present`, but matches a
    ``ToolError``-shaped entry of *any* kind — every
    ``mantis.contracts.ToolErrorKind`` value represents "Mantis failed to
    retrieve or parse evidence" (see that class's docstring), just
    classified differently (timeout vs. upstream_error vs. ...). Since #15
    introduced real per-failure classification (previously every AWX
    stdout failure was hardcoded to ``retrieval_error`` regardless of
    cause), a check for "was there a retrieval failure at all" must not
    hardcode one specific kind.
    """
    if isinstance(value, dict):
        if "kind" in value and "message" in value:
            return True
        return any(_any_tool_error_present(v) for v in value.values())
    if isinstance(value, list):
        return any(_any_tool_error_present(v) for v in value)
    return False


def _meta_truncated(value: Any) -> bool:
    """True if a tool result carries ``meta.truncated: true`` (see
    ``mantis.contracts.QueryMeta``)."""
    if isinstance(value, dict):
        meta = value.get("meta")
        if isinstance(meta, dict) and meta.get("truncated") is True:
            return True
    return False


# ---------------------------------------------------------------------------
# Tool-use checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MustProduceFinalAnswer:
    """Hard. ``outcome == "ok"`` and the final answer is non-empty/
    non-whitespace. Fails for a model that errored, or one that produced
    neither a tool call nor usable answer text — see
    ``AgentRuntime.diagnostic_raw_message``.
    """

    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or "final_answer_produced"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        if result.outcome != "ok":
            return False, f"run outcome was {result.outcome!r}, not ok"
        if not (result.final_answer or "").strip():
            return False, "final answer was empty"
        return True, "produced a non-empty final answer"


@dataclass(frozen=True)
class RequiredToolCall:
    """Hard by default. The named tool must have been called within
    ``[min_count, max_count]`` times (counting "ok" and "duplicate"
    outcomes — both mean the model got real data; a duplicate just
    replayed a cached result instead of re-executing).
    """

    tool: str
    min_count: int = 1
    max_count: int | None = None
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or f"required_tool_call:{self.tool}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        count = len(_successful_calls(result, self.tool))
        passed = count >= self.min_count and (self.max_count is None or count <= self.max_count)
        return passed, f"{self.tool} called {count} time(s)"


@dataclass(frozen=True)
class RequiredToolAttempt:
    """Hard by default. The named tool must have been *attempted* at
    least once, regardless of outcome — including a classified retrieval
    failure (``outcome == "integration_error"``) or any other non-"ok"
    result. Distinct from :class:`RequiredToolCall`, which only counts a
    successful ("ok"/"duplicate") outcome and would therefore never be
    satisfiable in a scenario where a source is *expected* to fail (see
    #11's ``system-troubleshooter-retrieval-failure`` golden scenario):
    the point there is proving the agent actually tried the failing
    source (so it can honestly report that evidence is unavailable)
    rather than silently skipping it, not that the call succeeded.
    """

    tool: str
    min_count: int = 1
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or f"required_tool_attempt:{self.tool}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        count = sum(1 for tc in result.tool_calls if tc.tool_name == self.tool)
        return count >= self.min_count, f"{self.tool} attempted {count} time(s)"


@dataclass(frozen=True)
class ForbiddenToolCall:
    """Hard by default. The named tool must never have been called."""

    tool: str
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or f"forbidden_tool_call:{self.tool}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        count = sum(1 for tc in result.tool_calls if tc.tool_name == self.tool)
        return count == 0, f"{self.tool} called {count} time(s)"


@dataclass(frozen=True)
class MaxToolCalls:
    """Quality by default. No more than ``count`` tool-call attempts
    total, of any tool and any outcome (successful, duplicate,
    malformed, unknown) — a broad guard against runaway/looping
    behavior. Distinct from :class:`RequiredToolCall`'s ``max_count``,
    which only bounds one named tool.
    """

    count: int
    name: str | None = None
    hard: bool = False

    def resolved_name(self) -> str:
        return self.name or "max_tool_calls"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        total = len(result.tool_calls)
        return total <= self.count, f"{total} tool call(s) total"


# Alias — the vocabulary discussion referred to this interchangeably as
# ToolCallCount/MaxToolCalls; MaxToolCalls is the canonical name.
ToolCallCount = MaxToolCalls


@dataclass(frozen=True)
class ToolArgumentsMatch:
    """Hard by default. At least one call to ``tool`` (any outcome that
    reached the handler — "ok" or "duplicate") had arguments containing
    every key/value in ``expected`` (subset match, not exact — a tool
    call with additional arguments beyond ``expected`` still counts).
    """

    tool: str
    expected: dict[str, Any]
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or f"tool_arguments_match:{self.tool}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        calls = _successful_calls(result, self.tool)
        for tc in calls:
            args = tc.arguments or {}
            if all(args.get(k) == v for k, v in self.expected.items()):
                return True, f"found a call with matching arguments: {args}"
        seen = [tc.arguments for tc in calls]
        return False, f"no call to {self.tool} matched {self.expected}; calls seen: {seen}"


@dataclass(frozen=True)
class MaxIterations:
    """Quality by default. The run must have taken no more than
    ``count`` model round-trips — a proxy for "stopped promptly" once
    sufficient evidence was available.
    """

    count: int
    name: str | None = None
    hard: bool = False

    def resolved_name(self) -> str:
        return self.name or "max_iterations"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        return result.iterations <= self.count, f"{result.iterations} iteration(s)"


# ---------------------------------------------------------------------------
# Answer-text checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredAnswerPattern:
    """Quality by default. The final answer must match ``patterns``
    (regex, case-insensitive unless ``case_sensitive=True``) —
    ``match="any"`` (default) requires at least one; ``match="all"``
    requires every pattern to match.

    Prefer several acceptable phrasings over one exact string — a good
    answer might say "SSH could not reach host03 on port 22" instead of
    copying the fixture's raw ``"ssh: connect to host host03 port 22:
    No route to host"`` verbatim. Plain substrings are valid regex
    patterns as-is, so this also covers the simple "must mention X" case.
    """

    patterns: str | list[str]
    match: Literal["any", "all"] = "any"
    case_sensitive: bool = False
    name: str | None = None
    hard: bool = False

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        patterns = _as_list(self.patterns)
        return f"required_answer_pattern:{patterns[0][:30]}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        text = result.final_answer or ""
        flags = 0 if self.case_sensitive else re.IGNORECASE
        patterns = _as_list(self.patterns)
        matched = [p for p in patterns if re.search(p, text, flags)]
        if self.match == "all":
            passed = len(matched) == len(patterns)
            missing = [p for p in patterns if p not in matched]
            detail = "all patterns matched" if passed else f"missing: {missing}"
        else:
            passed = len(matched) > 0
            detail = f"matched: {matched[0]!r}" if passed else f"none of {patterns} matched"
        return passed, detail


@dataclass(frozen=True)
class ForbiddenAnswerPattern:
    """Quality by default. The final answer must match NONE of
    ``patterns`` (regex, case-insensitive unless ``case_sensitive=True``).
    A generic phrase blocklist — for the specific, high-stakes case of
    an unsupported *definitive root cause* claim, prefer
    :class:`UnsupportedDefinitiveClaim` (hard by default), which requires
    both a subject and a definitive-language match rather than a bare
    phrase, and is less prone to false positives on hedged language.
    """

    patterns: str | list[str]
    case_sensitive: bool = False
    reason: str | None = None
    name: str | None = None
    hard: bool = False

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        patterns = _as_list(self.patterns)
        return f"forbidden_answer_pattern:{patterns[0][:30]}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        text = result.final_answer or ""
        flags = 0 if self.case_sensitive else re.IGNORECASE
        for pattern in _as_list(self.patterns):
            if re.search(pattern, text, flags):
                reason = f" ({self.reason})" if self.reason else ""
                return False, f"found forbidden pattern {pattern!r}{reason}"
        return True, "no forbidden patterns found"


@dataclass(frozen=True)
class UnsupportedDefinitiveClaim:
    """Hard by default. Flags a sentence in the final answer that
    combines a ``subject_patterns`` match (e.g. "firewall", "routing")
    with a ``definitive_patterns`` match (e.g. "caused", "was due to") —
    an assertion that a specific unproven cause is *the* explanation,
    rather than a hedged possibility.

    This deliberately does not attempt general natural-language
    hypothesis classification. For each golden scenario, the specific
    unsupported conclusions worth guarding against are known in advance
    (the fixture proves X, it does not prove Y) — this checks for Y
    being asserted definitively, scenario by scenario, not for some
    universal notion of "sounds like a hypothesis." A sentence merely
    mentioning a subject term without definitive language (e.g.
    "possible causes include a firewall or routing issue") does not
    trigger this — see :class:`HypothesisLabeled` for rewarding that
    phrasing instead.
    """

    subject_patterns: str | list[str]
    definitive_patterns: tuple[str, ...] = (
        r"\bcaused\b",
        r"\bthe root cause is\b",
        r"\bdue to\b",
        r"\bresulted from\b",
        r"\bthe reason (is|was)\b",
    )
    case_sensitive: bool = False
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        subjects = _as_list(self.subject_patterns)
        return f"unsupported_definitive_claim:{subjects[0][:30]}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        subjects = _as_list(self.subject_patterns)
        for sentence in _split_sentences(result.final_answer or ""):
            subject_hit = next((s for s in subjects if re.search(s, sentence, flags)), None)
            if subject_hit is None:
                continue
            definitive_hit = next(
                (d for d in self.definitive_patterns if re.search(d, sentence, flags)), None
            )
            if definitive_hit is not None:
                return False, f"definitive claim about {subject_hit!r} in: {sentence!r}"
        return True, "no unsupported definitive claims found"


@dataclass(frozen=True)
class HypothesisLabeled:
    """Quality by default. The complement of
    :class:`UnsupportedDefinitiveClaim`: rewards explicitly hedging a
    deeper cause as a possibility rather than omitting it entirely.
    Passes trivially (nothing to label) if ``subject_patterns`` never
    appear; if they do appear, at least one such sentence must also
    contain hedging language.
    """

    subject_patterns: str | list[str]
    hedge_patterns: tuple[str, ...] = (
        r"\bpossible\b",
        r"\bpossibly\b",
        r"\bmight\b",
        r"\bmay have\b",
        r"\bcould be\b",
        r"\bcould have\b",
        r"\bunclear\b",
        r"\bunconfirmed\b",
        r"\bhypothes",
        r"\bpotential\b",
    )
    case_sensitive: bool = False
    name: str | None = None
    hard: bool = False

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        subjects = _as_list(self.subject_patterns)
        return f"hypothesis_labeled:{subjects[0][:30]}"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        subjects = _as_list(self.subject_patterns)
        subject_sentences = [
            s
            for s in _split_sentences(result.final_answer or "")
            if any(re.search(subj, s, flags) for subj in subjects)
        ]
        if not subject_sentences:
            return True, "subject not mentioned; nothing to label"
        for sentence in subject_sentences:
            if any(re.search(h, sentence, flags) for h in self.hedge_patterns):
                return True, f"hedged appropriately in: {sentence!r}"
        return False, f"mentions {subjects} without hedging language: {subject_sentences}"


@dataclass(frozen=True)
class TruncationAcknowledged:
    """Quality by default. If any tool result in this run carried
    ``meta.truncated: true`` (see ``mantis.contracts.QueryMeta``), the
    final answer must acknowledge that more matching records may exist
    — otherwise passes trivially (nothing to acknowledge).
    """

    acknowledgment_patterns: tuple[str, ...] = (
        r"\bmore\b",
        r"\badditional\b",
        r"\bnot all\b",
        r"\btruncat",
        r"\bonly (a|the first)\b",
        r"\blimited to\b",
        r"\bat least\b",
    )
    case_sensitive: bool = False
    name: str | None = None
    hard: bool = False

    def resolved_name(self) -> str:
        return self.name or "truncation_acknowledged"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        truncated = any(_meta_truncated(tc.result) for tc in result.tool_calls)
        if not truncated:
            return True, "no truncated tool result in this run; nothing to acknowledge"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        text = result.final_answer or ""
        if any(re.search(p, text, flags) for p in self.acknowledgment_patterns):
            return True, "answer acknowledges more results may exist"
        return False, "tool result was truncated but the answer doesn't acknowledge it"


@dataclass(frozen=True)
class NoRetrievalErrorMisattribution:
    """Hard by default. If any tool result reports a
    ``mantis.contracts.ToolError`` of *any* kind (Mantis-side failure to
    *fetch* evidence — e.g. AWX stdout retrieval timing out, or AWX
    itself returning a server error) the final answer must not claim
    that error is *why the underlying investigated system/job failed*.
    Contract-aware (matches on the ``ToolError`` shape rather than a
    hardcoded field name or one specific ``ToolErrorKind`` value — see
    ``mantis.reliability`` for the classification that decides which
    specific kind a given failure gets, #15), so this works for any tool
    adopting the shared error taxonomy, not just AWX.

    Also treats a whole-call ``outcome in ("integration_error", "error")``
    as a retrieval failure, not just a ``ToolError`` embedded *within* an
    otherwise-successful result. AWX's per-job stdout fetch degrades into
    the latter shape (a successful call whose result carries a
    ``stdout_retrieval_error`` field alongside real evidence for other
    jobs), but a #8/#9/#10-style transport failure (network/Prometheus/
    Loki) raises a classified ``IntegrationError`` instead, which
    ``AgentRuntime`` records with ``result=None`` (see
    ``mantis.runtime.ToolCallLogEntry``) — there is no dict for
    ``_any_tool_error_present`` to find in that case, so relying on it
    alone would miss every such failure entirely. See #11's retrieval-
    failure golden scenario (``mantis.eval.fixtures.system_troubleshooter``)
    for the concrete case this covers.
    """

    retrieval_terms: tuple[str, ...] = (r"retriev", r"\bfetch", r"\bstdout\b")
    causal_terms: tuple[str, ...] = (
        r"\bcaused\b",
        r"\bresulted in\b",
        r"\bled to\b",
        r"\bis why\b",
        r"\bexplains\b",
        r"\bdue to\b",
        r"\bbecause of\b",
    )
    case_sensitive: bool = False
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or "no_retrieval_error_misattribution"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        has_retrieval_error = any(
            _any_tool_error_present(tc.result) or tc.outcome in ("integration_error", "error")
            for tc in result.tool_calls
        )
        if not has_retrieval_error:
            return True, "no retrieval error present in tool results; nothing to misattribute"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        # Sentence-scoped, like UnsupportedDefinitiveClaim: merely
        # mentioning both "failed" and "retrieve" near each other (e.g.
        # "job failed. Unable to retrieve stdout.") is completely normal
        # and must not trip this — only a genuine causal claim within the
        # same sentence counts.
        for sentence in _split_sentences(result.final_answer or ""):
            retrieval_hit = any(re.search(t, sentence, flags) for t in self.retrieval_terms)
            causal_hit = any(re.search(c, sentence, flags) for c in self.causal_terms)
            if retrieval_hit and causal_hit:
                return False, f"answer appears to blame the retrieval error for the job's failure: {sentence!r}"
        return True, "did not attribute the job's failure to the retrieval error"


@dataclass(frozen=True)
class NoUnexpectedEntities:
    """Hard by default. The final answer must not mention hosts or job
    IDs that aren't in the fixture's known set — an objective,
    structural fabrication check, since the fixtures are controlled data
    (unlike free-form claims, "did the model invent a specific host
    name" is exactly checkable).

    ``known_hosts``/``known_job_ids`` should list every value the
    fixture actually contains; anything matching ``host_pattern``/
    ``job_id_pattern`` in the answer that isn't in those sets is flagged.
    Both default patterns are deliberately narrow (``hostNN``-style
    names, "job #NNNN") — broaden them per-scenario if a fixture uses a
    different naming convention.
    """

    known_hosts: frozenset[str] = frozenset()
    known_job_ids: frozenset[str] = frozenset()
    host_pattern: str = r"\bhost[0-9]+\b"
    job_id_pattern: str = r"\bjob\s*#?\s*([0-9]{2,})\b"
    name: str | None = None
    hard: bool = True

    def resolved_name(self) -> str:
        return self.name or "no_unexpected_entities"

    def check(self, result: "EvalResult") -> tuple[bool, str]:
        text = result.final_answer or ""
        known_hosts_lower = {h.lower() for h in self.known_hosts}
        known_job_ids = set(self.known_job_ids)

        unexpected: list[str] = []
        for match in re.finditer(self.host_pattern, text, re.IGNORECASE):
            host = match.group(0)
            if host.lower() not in known_hosts_lower:
                unexpected.append(f"host {host!r}")
        for match in re.finditer(self.job_id_pattern, text, re.IGNORECASE):
            job_id = match.group(1)
            if job_id not in known_job_ids:
                unexpected.append(f"job id {job_id!r}")

        if unexpected:
            return False, f"mentions entities not in the fixture: {', '.join(unexpected)}"
        return True, "no unexpected hosts/job IDs mentioned"

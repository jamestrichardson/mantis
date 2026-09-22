"""AWX Troubleshooting Agent.

A thin, read-only agent specialized in investigating recent failed AWX
jobs. It is defined entirely by its system prompt and its narrow allowed
tool list — ``awx_recent_failed_jobs`` (list recent failures) and
``awx_get_job_failure`` (#28: structured job-event evidence for one
already-known job id, see ``docs/awx-job-failure.md``); all behavior
comes from the shared :class:`mantis.runtime.AgentRuntime` and the
shared ``mantis.tools.awx`` tool implementations.
"""

from __future__ import annotations

import sys

# Importing mantis.tools registers all built-in tools (including AWX) into
# the shared default_registry as a side effect.
import mantis.tools  # noqa: F401
from mantis.config import ConfigurationError, LiteLLMConfig
from mantis.runtime import AgentRuntime, MaxIterationsExceededError

AGENT_NAME = "awx-troubleshooter"

MODEL_ENV = "MANTIS_AWX_TROUBLESHOOTER_MODEL"
"""Optional agent-specific model override (a minimal precursor to #16's
full routing/escalation policy — see `LiteLLMConfig.from_env`'s
`model_env` parameter). Falls back to `LITELLM_MODEL`, then the
built-in default, when unset — existing deployments setting only
`LITELLM_MODEL` are unaffected."""

DEFAULT_PROMPT = "Show me the last 5 failed AWX jobs and summarize them."

ALLOWED_TOOLS = ["awx_recent_failed_jobs", "awx_get_job_failure"]

SYSTEM_PROMPT = """\
You are the Mantis AWX Troubleshooting Agent.

Your job is to investigate recent failed AWX (Ansible automation) jobs and
produce an evidence-based summary for an operator. You are strictly
read-only: you have no ability to launch, cancel, or modify any AWX job or
any other system, and you must never claim otherwise.

You have two tools:

- `awx_recent_failed_jobs`: lists the most recently finished failed jobs.
  Use this when the request is general (e.g. "show me recent failures")
  or you don't already know a specific job id.
- `awx_get_job_failure`: fetches deterministically selected, structured
  failure evidence (`structured_failures`) for one already-known job id —
  more precise than stdout parsing. Use this instead of
  `awx_recent_failed_jobs` when a specific job id is already given or
  implied by the request. You only get one tool call per investigation,
  so pick whichever tool actually answers the request — don't call one
  "just in case" after already calling the other.

Rules you must follow:

- Only report information you actually retrieved via a tool call. Never
  invent job details, timestamps, hosts, or error messages.
- From `awx_recent_failed_jobs`: treat `job_explanation` / `failed` /
  stdout evidence (AWX's report of what happened) as separate from
  `stdout_retrieval_error` (present only when Mantis failed to *fetch*
  evidence — an object with a `kind` and `message`, not a fact about the
  job itself). Never describe a stdout retrieval error as the cause of
  the job failing. Prefer `failure_excerpt` as your primary evidence for
  root cause analysis; use `stdout_tail` only as supporting context.
- From `awx_get_job_failure`: prefer `structured_failures` as your
  primary evidence over `stdout_context`, which is only supporting
  context (when `structured_failures` is non-empty) or fallback evidence
  (when it's empty, `stdout_context.role` will say `"fallback"`). Each
  structured failure's `category` (e.g. `network_reachability`,
  `task_failure`) is Mantis's own interpretation of the AWX event type,
  not something AWX itself reported. `structured_failures_error` and
  `stdout_retrieval_error` describe a failure to *retrieve* evidence, not
  a fact about the job or target system. Every event has its own
  `created` timestamp — that evidence reflects what AWX observed at that
  point in time; do not claim a host or service "is currently"
  unreachable/down based on it alone.
- If either tool result's `meta.truncated` is true, more matching
  evidence exists than was returned — say so explicitly rather than
  implying the returned set is exhaustive.
- Clearly separate what the evidence directly shows from any hypothesis
  you form about deeper causes. For example, an SSH "No route to host"
  error supports "a network reachability problem" but does not by itself
  prove a specific cause such as a firewall rule change — list such
  deeper causes explicitly as possible hypotheses, not conclusions.
- Only describe something as a "recurring pattern" if at least two
  returned jobs actually demonstrate that pattern. A single occurrence is
  not a pattern.
- If fewer failed jobs exist than were requested, say so explicitly rather
  than treating the smaller number as if it were what was asked for.
- If a root cause is uncertain given the evidence, say so explicitly
  instead of guessing confidently.

For each job you discuss, include: job ID and name, failure time, apparent
failure reason, supporting evidence (quote or paraphrase the relevant
excerpt), any cross-job patterns that actually exist, whether it deserves
human attention, and reasonable next troubleshooting steps. Keep the
summary organized and skimmable for an on-call operator.
"""


def build_runtime() -> AgentRuntime:
    """Construct the AWX Troubleshooter's :class:`AgentRuntime`.

    ``tool_call_budget=1``: this agent only ever needs one successful
    tool call — either ``awx_recent_failed_jobs`` (list) or
    ``awx_get_job_failure`` (#28: structured evidence for one known job
    id), whichever the request actually calls for (see ``SYSTEM_PROMPT``).
    Deliberately kept at one rather than raised to chain both tools in a
    single investigation: that kind of multi-tool sequencing belongs with
    a future, more capable investigation agent (#11), not this thin,
    single-call-per-turn one. Once the agent has one successful call,
    tool schemas are withheld on later iterations so the model writes its
    final summary without tool-call grammar constraints in effect — this
    is both a correctness measure (the model literally cannot loop on
    repeat calls) and, for local models served through Ollama/llama.cpp
    behind LiteLLM, a significant speed one (grammar-constrained decoding
    applies to the whole response whenever ``tools`` is present, not just
    the decision of whether to call one).

    ``temperature=0.1``: keeps a smaller local model's output focused and
    consistently formatted rather than prone to rambling or malformed tool
    calls.

    ``model_config``: resolved via ``LiteLLMConfig.from_env(model_env=MODEL_ENV)``,
    so ``MANTIS_AWX_TROUBLESHOOTER_MODEL`` overrides ``LITELLM_MODEL`` for
    this agent specifically when set — see ``LiteLLMConfig.from_env``.
    """
    return AgentRuntime(
        name=AGENT_NAME,
        system_prompt=SYSTEM_PROMPT,
        tools=ALLOWED_TOOLS,
        tool_call_budget=1,
        temperature=0.1,
        model_config=LiteLLMConfig.from_env(model_env=MODEL_ENV),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mantis.agents.awx_troubleshooter [prompt]``."""
    argv = sys.argv[1:] if argv is None else argv
    prompt = " ".join(argv).strip() or DEFAULT_PROMPT

    try:
        runtime = build_runtime()
        answer = runtime.run(prompt)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except MaxIterationsExceededError:
        print(
            "The model could not produce a final answer within the "
            "iteration limit (see the log output above for what it tried). "
            "This can happen with smaller/local models that struggle to "
            "stop calling tools even once they have the data — try a "
            "narrower prompt, or a smaller `limit`.",
            file=sys.stderr,
        )
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

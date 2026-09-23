"""Shared agent runtime: the model/tool-calling loop every agent uses.

Individual agents must not reimplement this loop. An agent is defined by
its name, system prompt, allowed tool names, and model configuration; the
runtime is responsible for talking to the model gateway (LiteLLM, via its
OpenAI-compatible API), dispatching tool calls against the shared
:class:`~mantis.registry.ToolRegistry`, and producing a final answer.

Design notes:

- ``tool_choice`` is intentionally never sent — some providers behind
  LiteLLM (e.g. Ollama) reject or mishandle it. We rely on the system
  prompt and tool descriptions to guide tool use.
- Exact duplicate tool calls (same name + same arguments, already
  executed) are detected and replayed: the *cached prior result* is
  reused instead of re-executing, to avoid hammering an integration with
  redundant calls. Smaller local models can be inconsistent about
  attending to a tool result on the first pass and re-ask for the same
  data — the cache means a repeat ask is still answered with real
  evidence (plus a note telling the model to stop asking and answer),
  rather than punished with an error that starves it of the data it
  needs. This is duplicate-call *replay* — see the budget/mechanism
  list below for why that name matters.
- Every failure mode (malformed arguments, unknown tool, integration
  exception) is caught and turned into a tool-result message rather than
  raising out of the loop, so a single bad call doesn't crash the agent.
- ``tool_call_budget`` lets an agent withhold tool schemas once it has
  enough successful calls, instead of offering them for the rest of the
  run. Beyond guaranteeing the model can't loop on tool calls, this
  matters for speed: many local model backends (e.g. Ollama/llama.cpp via
  LiteLLM) use grammar-constrained decoding for the *entire* response
  whenever ``tools`` is present in the request — not just to decide
  whether to call a function — which can make even a plain final-answer
  generation dramatically slower. See ``AgentRuntime.tool_call_budget``.

Four distinct, easy-to-conflate budget/reliability mechanisms exist —
keep them named separately in code and docs (full detail in
``mantis.reliability``'s module docstring and ``docs/reliability.md``):

1. ``tool_call_budget`` (above) — model-requested *successful* tool
   calls allowed per run.
2. retry/attempt budget (``mantis.reliability.RetryPolicy``) — transport
   attempts one logical integration read may consume, entirely inside
   one tool call.
3. duplicate-call *replay* (above) — reusing a cached result for an
   exact-repeat model request; never triggers another integration
   attempt, so it's independent of #2.
4. run-local *short circuit* (``mantis.reliability.RunLocalBreaker``) —
   after enough classified-transient failures against one integration
   *within a single run*, later calls to it in that same run fail fast
   without a request. Fresh state every ``run()`` call — never a
   persistent/global circuit breaker.

Plus two deadlines (``ReliabilityConfig.tool_timeout_seconds`` /
``.run_timeout_seconds``, also ``mantis.reliability.Deadline``): a
per-tool-call and an overall per-run wall-clock budget, bounding how
long Mantis will keep *starting* new work — not a guarantee that an
already-in-flight blocking call gets interrupted (Python cannot preempt
arbitrary synchronous code; see ``docs/reliability.md``).
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import OpenAI

from mantis.config import LiteLLMConfig, ReliabilityConfig
from mantis.contracts import ToolError
from mantis.observability import metrics
from mantis.observability.logging import bound_for_log, log_event, new_run_id
from mantis.registry import Tool, ToolRegistry, default_registry
from mantis.reliability import (
    Deadline,
    DeadlineExceededError,
    IntegrationError,
    IntegrationErrorKind,
    RunLocalBreaker,
)
from mantis.security import UNTRUSTED_TOOL_OUTPUT_POLICY, make_model_safe, redact_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8

_NO_RESULT = object()
"""Sentinel distinguishing "no result to log" from a tool genuinely
returning ``None`` as its result."""


class RuntimeError_(RuntimeError):
    """Base class for runtime-level failures (not tool failures)."""


class MaxIterationsExceededError(RuntimeError_):
    """Raised when the model/tool loop exceeds its iteration budget without
    producing a final answer."""


class RunDeadlineExceededError(RuntimeError_):
    """Raised when the overall run deadline
    (``ReliabilityConfig.run_timeout_seconds``) is exhausted before the
    runtime would start another model/tool iteration.

    Distinct from :class:`MaxIterationsExceededError` — that's a bound on
    *iteration count*; this is a bound on *wall-clock time*, and (like
    every deadline in ``mantis.reliability``) only guarantees Mantis
    doesn't *start* new work past the deadline, not that any single
    already-in-flight call gets forcibly interrupted. See
    ``docs/reliability.md``.
    """


def build_openai_client(model_config: LiteLLMConfig) -> OpenAI:
    """Build an OpenAI-compatible client pointed at LiteLLM.

    Shared by :class:`AgentRuntime` and anything else that needs to talk
    to LiteLLM directly without going through a full agent run — e.g.
    ``mantis eval list-models``, which just needs to list what LiteLLM has
    configured.
    """
    base_url = model_config.url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    return OpenAI(base_url=base_url, api_key=model_config.api_key.get_secret_value())


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    """Best-effort conversion of an OpenAI-SDK usage object to a plain
    dict. Not every LiteLLM-fronted backend returns usage data, so this
    must degrade to ``None`` rather than raise.
    """
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return dict(usage)
    return None


def _message_to_dict(message: Any) -> dict[str, Any] | None:
    """Best-effort conversion of an OpenAI-SDK message object to a plain
    dict, preserving any provider-specific extra fields (the SDK's
    ``ChatCompletionMessage`` allows extras, so a field like
    ``reasoning_content`` some backends return alongside/instead of
    ``content`` survives ``model_dump()`` even though this runtime never
    reads it directly).
    """
    if message is None:
        return None
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if isinstance(message, dict):
        return dict(message)
    return None


@dataclass
class ToolCallLogEntry:
    """A single record of a tool invocation attempt, for observability."""

    iteration: int
    tool_name: str
    arguments: dict[str, Any] | None
    outcome: str  # "ok", "duplicate", "unknown_tool", "bad_arguments", "error"
    detail: str = ""
    result: Any = None
    """The tool's return value for "ok"/"duplicate" outcomes (the cached
    value on a duplicate replay); None for outcomes with no successful
    result (bad_arguments/unknown_tool/error)."""


@dataclass
class AgentRuntime:
    """Shared model/tool loop used by every Mantis agent.

    Args:
        name: Agent name, used for logging.
        system_prompt: The agent's system prompt.
        tools: Names of tools (already registered in ``registry``) this
            agent is allowed to use. Only these tool schemas are ever sent
            to the model.
        model_config: LiteLLM connection settings. Defaults to
            ``LiteLLMConfig.from_env()`` if not provided.
        registry: Tool registry to resolve ``tools`` against. Defaults to
            the shared process-wide registry.
        max_iterations: Maximum number of model round-trips before giving
            up. Guards against runaway loops.
        tool_call_budget: Maximum number of *successful* tool calls to
            allow in a single run before tool schemas are withheld on
            subsequent iterations, forcing a final answer. ``None``
            (default) means unlimited — tools stay available for the
            whole run, which is appropriate for an agent that may
            legitimately need several different tools in sequence. Set
            this for single-purpose agents (e.g. ``1`` for an agent with
            one investigative tool): once that tool has succeeded, there
            is nothing left to look up, and withholding the tool schema
            also has a real performance benefit — many local model
            backends (e.g. Ollama/llama.cpp via LiteLLM) use
            grammar-constrained decoding for the *entire* response
            whenever ``tools`` is present in the request, not just to
            decide whether to call a function. That can make even a
            plain-text final summary dramatically slower to generate.
            Withholding ``tools`` once no further calls are needed lets
            that final answer generate at normal speed.
        temperature: Passed through to the model call when set. Lower
            values (e.g. ``0.1``) tend to produce more focused, shorter,
            more reliably-formatted output from smaller local models —
            useful for keeping tool-calling agents on task. ``None``
            (default) omits the parameter and uses the backend's default.
        reliability: The shared reliability contract's configuration
            (timeouts, retry budget, run/tool deadlines, short-circuit
            threshold — see ``mantis.reliability`` and
            ``docs/reliability.md``). Defaults to
            ``ReliabilityConfig.from_env()``. ``run_timeout_seconds``/
            ``tool_timeout_seconds`` here are a *different* concept from
            ``tool_call_budget`` above — see this module's docstring and
            ``docs/reliability.md``'s "Retry budget vs. tool-call
            budget" section; conflating them is exactly the mistake this
            issue exists to prevent.

    After a call to :meth:`run`, two attributes hold a record of what
    happened, in order: ``call_log`` (tool-call attempts and their
    outcomes) and ``usage_log`` (per-iteration token usage, when the
    backend reports it). Both accumulate across repeated ``run()`` calls
    on the same instance rather than resetting. The run-local
    short-circuit guard (:class:`~mantis.reliability.RunLocalBreaker`)
    does the opposite: a fresh instance is created at the start of every
    :meth:`run` call, never carried over from a previous run.
    """

    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    model_config: LiteLLMConfig | None = None
    registry: ToolRegistry = field(default_factory=lambda: default_registry)
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    tool_call_budget: int | None = None
    temperature: float | None = None
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig.from_env)
    clock: Callable[[], float] = time.monotonic
    """Monotonic clock used for :class:`~mantis.reliability.Deadline`
    (run/tool budgets). Injectable purely for deterministic tests — real
    usage always wants the real ``time.monotonic``, which is the
    default."""

    def __post_init__(self) -> None:
        if self.model_config is None:
            self.model_config = LiteLLMConfig.from_env()

        self._client = build_openai_client(self.model_config)
        self._resolved_tools: dict[str, Tool] = {
            tool.name: tool for tool in self.registry.subset(self.tools)
        }
        self.call_log: list[ToolCallLogEntry] = []
        # Per-iteration token usage, one entry per model call in the last
        # run(), in order. None for an iteration where the backend didn't
        # return usage data. Consumers (e.g. the eval harness) that want a
        # total can sum the "total_tokens" key across entries.
        self.usage_log: list[dict[str, Any] | None] = []
        # The backend-resolved model identity ("response.model") for each
        # iteration, in order -- distinct from model_config.model (the
        # requested LiteLLM *alias*): LiteLLM's OpenAI-compatible response
        # reports which underlying provider model actually served the
        # request, when the backend includes it. None for an iteration
        # where the response didn't carry this field. Exists primarily for
        # #13's model-qualification harness (mantis.eval.qualification),
        # which must record the resolved backend identity "when reliably
        # available" and never guess it otherwise.
        self.backend_model_log: list[str | None] = []
        # Diagnostic only: the raw final message, captured only when a run
        # ends with neither tool calls nor usable answer text — a model
        # producing no content and no tool_calls despite spending
        # completion tokens usually means something (a provider-specific
        # field like reasoning_content, or a malformed tool-call attempt)
        # landed somewhere this runtime doesn't read. Left None otherwise
        # to avoid bloating every successful run with a redundant dump of
        # data already in the returned answer.
        self.diagnostic_raw_message: dict[str, Any] | None = None
        # The run_id generated for the most recent run() call — exposed so
        # a caller wrapping this runtime (e.g. mantis.eval.runner) can
        # attach the same correlation ID to its own events for that run.
        self.last_run_id: str | None = None

    def _tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(tool.schema) for tool in self._resolved_tools.values()]

    def run(self, user_prompt: str, *, run_id: str | None = None) -> str:
        """Run the agent loop for a single user prompt and return the
        model's final textual answer.

        Emits ``mantis_run_started``/``mantis_run_completed``/
        ``mantis_run_failed`` structured events and records
        ``mantis_runs_total``/``mantis_run_duration_seconds`` under a
        fresh ``run_id`` (see :attr:`last_run_id`) — on failure, the
        event/metric are recorded and the exception still propagates
        unchanged; observability here never changes control flow.

        ``run_id``: normally left ``None`` so one is generated here, but
        a caller that must assign a stable run ID *before* invocation
        (e.g. ``mantis.api.invocation.InvocationService``, so the same ID
        it returns to an HTTP caller is the ID that correlates every
        ``mantis_run_started``/``mantis_tool_call``/``mantis_model_call``
        event this run produces) may pass one in explicitly instead of
        reading :attr:`last_run_id` only after the fact.
        """
        run_id = run_id or new_run_id()
        self.last_run_id = run_id
        model_alias = self.model_config.model
        env = metrics.environment()

        log_event(logger, "mantis_run_started", run_id=run_id, agent=self.name, model_alias=model_alias)
        run_start = time.perf_counter()
        try:
            final_answer = self._run_loop(user_prompt, run_id=run_id)
        except Exception as exc:
            duration = time.perf_counter() - run_start
            if isinstance(exc, MaxIterationsExceededError):
                outcome = "max_iterations"
            elif isinstance(exc, RunDeadlineExceededError):
                outcome = "run_deadline_exceeded"
            else:
                outcome = "error"
            log_event(
                logger,
                "mantis_run_failed",
                level=logging.WARNING,
                run_id=run_id,
                agent=self.name,
                model_alias=model_alias,
                duration_seconds=duration,
                outcome=outcome,
                error_kind=type(exc).__name__,
            )
            metrics.RUNS_TOTAL.labels(
                agent=self.name, model_alias=model_alias, result=outcome, environment=env
            ).inc()
            metrics.RUN_DURATION_SECONDS.labels(
                agent=self.name, model_alias=model_alias, result=outcome, environment=env
            ).observe(duration)
            raise

        duration = time.perf_counter() - run_start
        log_event(
            logger,
            "mantis_run_completed",
            run_id=run_id,
            agent=self.name,
            model_alias=model_alias,
            duration_seconds=duration,
            outcome="ok",
        )
        metrics.RUNS_TOTAL.labels(
            agent=self.name, model_alias=model_alias, result="ok", environment=env
        ).inc()
        metrics.RUN_DURATION_SECONDS.labels(
            agent=self.name, model_alias=model_alias, result="ok", environment=env
        ).observe(duration)
        return final_answer

    def _run_loop(self, user_prompt: str, *, run_id: str) -> str:
        # The trust-boundary instruction is appended here, at the runtime
        # level, rather than requiring every agent to include it in its
        # own SYSTEM_PROMPT — see mantis.security's module docstring and
        # docs/security.md. self.system_prompt itself is never mutated.
        system_content = f"{self.system_prompt}\n\n{UNTRUSTED_TOOL_OUTPUT_POLICY}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]
        # Cache of successful tool results by (tool_name, sorted-json-args).
        # A repeat call with identical arguments is idempotent for every
        # currently registered (read-only) tool, so we replay the cached
        # result instead of re-executing — this protects the underlying
        # integration from redundant calls without ever starving the model
        # of data it already asked for and is entitled to see again.
        tool_result_cache: dict[tuple[str, str], Any] = {}
        tool_schemas = self._tool_schemas()
        successful_tool_calls = 0

        # Overall run wall-clock budget and the run-local short-circuit
        # guard — both created fresh here, once per run() call, never
        # reused across runs. See this module's docstring and
        # docs/reliability.md.
        run_deadline = Deadline.after(self.reliability.run_timeout_seconds, clock=self.clock)
        breaker = RunLocalBreaker(threshold=self.reliability.short_circuit_threshold)

        for iteration in range(1, self.max_iterations + 1):
            if run_deadline.expired():
                # Never start another model/tool iteration once the run
                # budget is gone — a distinct, classified failure, not a
                # cooperative "the model decided to stop."
                log_event(
                    logger,
                    "mantis_run_deadline_exceeded",
                    level=logging.WARNING,
                    run_id=run_id,
                    agent=self.name,
                    iteration=iteration,
                )
                raise RunDeadlineExceededError(
                    f"Agent '{self.name}' exceeded run_timeout_seconds="
                    f"{self.reliability.run_timeout_seconds} before iteration {iteration}"
                )

            # Once tool_call_budget successful calls have happened, stop
            # offering tool schemas: there's nothing left this agent needs
            # to look up, and — for local models where `tools` triggers
            # grammar-constrained decoding for the whole response — this is
            # what keeps the final answer fast. See tool_call_budget's
            # docstring above.
            budget_exhausted = (
                self.tool_call_budget is not None
                and successful_tool_calls >= self.tool_call_budget
            )
            offer_tools = bool(tool_schemas) and not budget_exhausted

            logger.info(
                "[%s] iteration %d/%d (tools offered: %s)",
                self.name,
                iteration,
                self.max_iterations,
                offer_tools,
            )

            kwargs: dict[str, Any] = {
                "model": self.model_config.model,
                "messages": messages,
            }
            if offer_tools:
                kwargs["tools"] = tool_schemas
            if self.temperature is not None:
                kwargs["temperature"] = self.temperature

            model_alias = self.model_config.model
            env = metrics.environment()
            model_call_start = time.perf_counter()
            response = self._client.chat.completions.create(**kwargs)
            model_call_duration = time.perf_counter() - model_call_start

            usage = _usage_to_dict(getattr(response, "usage", None))
            self.usage_log.append(usage)
            self.backend_model_log.append(getattr(response, "model", None))
            total_tokens = usage.get("total_tokens") if usage else None

            log_event(
                logger,
                "mantis_model_call",
                run_id=run_id,
                agent=self.name,
                model_alias=model_alias,
                iteration=iteration,
                duration_seconds=model_call_duration,
                tokens=total_tokens,
            )
            metrics.MODEL_CALLS_TOTAL.labels(
                agent=self.name, model_alias=model_alias, environment=env
            ).inc()
            metrics.MODEL_CALL_DURATION_SECONDS.labels(
                agent=self.name, model_alias=model_alias, environment=env
            ).observe(model_call_duration)
            if total_tokens is not None:
                metrics.MODEL_TOKENS_TOTAL.labels(
                    agent=self.name, model_alias=model_alias, environment=env
                ).inc(total_tokens)

            choice = response.choices[0]
            message = choice.message

            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                final_answer = message.content or ""
                if not final_answer.strip():
                    # Empty content and no tool call, despite this being a
                    # "final answer" turn — capture what the backend
                    # actually sent so this is diagnosable without a live
                    # re-run. See diagnostic_raw_message's docstring.
                    self.diagnostic_raw_message = _message_to_dict(message)
                    logger.warning(
                        "[%s] iteration %d produced neither tool calls nor "
                        "answer text; see diagnostic_raw_message",
                        self.name,
                        iteration,
                    )
                else:
                    logger.info(
                        "[%s] final answer produced on iteration %d", self.name, iteration
                    )
                return final_answer

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tool_call in tool_calls:
                result_text, fresh_success = self._dispatch_tool_call(
                    tool_call,
                    iteration=iteration,
                    run_id=run_id,
                    tool_result_cache=tool_result_cache,
                    breaker=breaker,
                    run_deadline=run_deadline,
                )
                if fresh_success:
                    successful_tool_calls += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    }
                )

        raise MaxIterationsExceededError(
            f"Agent '{self.name}' exceeded max_iterations={self.max_iterations} "
            "without producing a final answer"
        )

    def _dispatch_tool_call(
        self,
        tool_call: Any,
        *,
        iteration: int,
        run_id: str,
        tool_result_cache: dict[tuple[str, str], Any],
        breaker: RunLocalBreaker,
        run_deadline: Deadline,
    ) -> tuple[str, bool]:
        """Execute (or replay/reject/short-circuit) one tool call.

        Returns ``(tool_message_content, fresh_success)`` — ``fresh_success``
        is True only when this call newly executed a tool's handler
        successfully (not on a cache replay, error, rejection, or
        short-circuit), which is what counts against ``tool_call_budget``.

        Emits one ``mantis_tool_call`` event and records
        ``mantis_tool_calls_total`` (plus, for an executed call,
        ``mantis_tool_call_duration_seconds`` and — on failure —
        ``mantis_tool_errors_total``) for every outcome, including
        rejections that never reach a tool handler.

        The reliability contract (see ``mantis.reliability`` and
        ``docs/reliability.md``) is applied here: ``breaker`` is checked
        before the handler runs and updated after (success resets it,
        only classified-transient failures count toward it); a
        per-tool-call deadline — bounded by both
        ``reliability.tool_timeout_seconds`` and whatever remains of
        ``run_deadline`` — is derived and passed to any handler that
        declares a ``_deadline`` parameter (the same keyword-only,
        leading-underscore convention as ``_client``, so it's never
        something a model's JSON arguments could set); a classified
        :class:`~mantis.reliability.IntegrationError` from the handler is
        turned into a well-formed tool-facing result instead of the
        generic catch-all below, which remains as the last-resort safety
        net for a genuinely unexpected bug. A handler that instead
        degrades a per-item integration failure into partial evidence
        (never raising) can still report it into the breaker via an
        injected ``_reliability_report`` callback, using the same
        keyword-only/underscore convention — see
        ``mantis.tools.awx.awx_recent_failed_jobs`` for the pattern, and
        this method's ``degraded_within_call`` handling of it.
        """
        tool_name = tool_call.function.name
        raw_arguments = tool_call.function.arguments or "{}"
        env = metrics.environment()

        def emit(
            outcome: str,
            *,
            arguments: dict[str, Any] | None = None,
            result: Any = _NO_RESULT,
            duration: float | None = None,
            error_kind: str | None = None,
        ) -> None:
            log_event(
                logger,
                "mantis_tool_call",
                level=(
                    logging.WARNING
                    if outcome
                    in (
                        "bad_arguments",
                        "unknown_tool",
                        "error",
                        "integration_error",
                        "budget_exceeded",
                        "short_circuited",
                    )
                    else logging.INFO
                ),
                run_id=run_id,
                agent=self.name,
                iteration=iteration,
                tool=tool_name,
                outcome=outcome,
                duration_seconds=duration,
                error_kind=error_kind,
                bound_arguments=bound_for_log(arguments) if arguments is not None else None,
                bound_result=bound_for_log(result) if result is not _NO_RESULT else None,
            )
            metrics.TOOL_CALLS_TOTAL.labels(
                agent=self.name, tool=tool_name, result=outcome, environment=env
            ).inc()
            if duration is not None:
                metrics.TOOL_CALL_DURATION_SECONDS.labels(
                    agent=self.name, tool=tool_name, environment=env
                ).observe(duration)
            if error_kind is not None:
                metrics.TOOL_ERRORS_TOTAL.labels(
                    agent=self.name, tool=tool_name, error_kind=error_kind, environment=env
                ).inc()

        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments must decode to a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            detail = f"Malformed arguments for tool '{tool_name}': {exc}"
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, None, "bad_arguments", detail)
            )
            emit("bad_arguments")
            return json.dumps({"error": detail}), False

        dedupe_key = (tool_name, json.dumps(arguments, sort_keys=True))
        if dedupe_key in tool_result_cache:
            detail = (
                f"Duplicate call to '{tool_name}' with identical arguments; "
                "replayed the cached result instead of re-executing."
            )
            logger.info("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(
                    iteration, tool_name, arguments, "duplicate", detail,
                    result=tool_result_cache[dedupe_key],
                )
            )
            emit("duplicate", arguments=arguments, result=tool_result_cache[dedupe_key])
            # Never withhold data the model is asking for, even on a repeat
            # call — a small/local model may not attend well to a tool
            # result on the first pass and re-ask for it. Returning an error
            # here instead of the real data starves it of evidence and, in
            # practice, causes it to give up and echo the error back as its
            # "final answer" rather than ever producing a real summary.
            #
            # The cached value is the tool's raw result — a duplicate reply
            # must go through the exact same model-input safety pipeline as
            # a fresh call, not reintroduce it unprotected.
            duplicate_tool = self._resolved_tools[tool_name]
            safe_result = make_model_safe(
                tool_result_cache[dedupe_key],
                contains_untrusted_text=duplicate_tool.contains_untrusted_text,
            )
            payload = {
                "note": (
                    "You already called this tool with these exact "
                    "arguments. This is the same result as before — it will "
                    "not change on another call. Use this data now to write "
                    "your final answer instead of calling this tool again."
                ),
                "result": safe_result,
            }
            return json.dumps(payload, default=str), False

        if tool_name not in self._resolved_tools:
            detail = f"Unknown tool: '{tool_name}'"
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "unknown_tool", detail)
            )
            emit("unknown_tool", arguments=arguments)
            return json.dumps({"error": detail}), False

        tool = self._resolved_tools[tool_name]

        if breaker.is_open(tool.category):
            detail = (
                f"'{tool.category}' has failed repeatedly this run; short-circuiting "
                f"'{tool_name}' without making another request. See "
                "mantis.reliability.RunLocalBreaker."
            )
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "short_circuited", detail)
            )
            emit("short_circuited", arguments=arguments, error_kind="run_local_short_circuit")
            safe_result = make_model_safe(
                {"error": detail}, contains_untrusted_text=tool.contains_untrusted_text
            )
            return json.dumps(safe_result, default=str), False

        if run_deadline.expired():
            detail = (
                f"Run deadline (run_timeout_seconds={self.reliability.run_timeout_seconds}) "
                f"already exhausted; not starting '{tool_name}'."
            )
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "budget_exceeded", detail)
            )
            emit("budget_exceeded", arguments=arguments, error_kind="run_deadline_exceeded")
            safe_result = make_model_safe(
                {"error": detail}, contains_untrusted_text=tool.contains_untrusted_text
            )
            return json.dumps(safe_result, default=str), False

        # Bounded by both the named per-tool-call budget and whatever
        # remains of the run budget — a tool call late in a long run
        # never gets a full fresh tool_timeout_seconds when the run
        # itself is nearly out of time. See docs/reliability.md.
        #
        # Always strictly positive here: reliability.tool_timeout_seconds
        # is validated to be > 0 at ReliabilityConfig construction (see
        # mantis.config.ReliabilityConfig.__post_init__), and
        # run_deadline.remaining() is strictly positive too — a zero (or
        # negative) remaining budget was already handled above by the
        # run_deadline.expired() check, which returns before reaching
        # here. There is deliberately no further "insufficient tool
        # budget" branch below: it would be unreachable dead code.
        tool_budget_seconds = min(self.reliability.tool_timeout_seconds, run_deadline.remaining())
        tool_deadline = Deadline.after(tool_budget_seconds, clock=self.clock)

        # A tool handler that degrades a per-item integration failure into
        # partial evidence (e.g. one AWX job's stdout retrieval, see
        # mantis.tools.awx._summarize_job) never raises IntegrationError,
        # so the except-branch below never sees it and the breaker would
        # otherwise stay blind to it — then get reset to zero anyway by
        # the unconditional record_success below once the handler returns
        # "successfully". _reliability_report is the handler's escape
        # hatch to still report that failure into this run's breaker
        # state; degraded_within_call tracks whether that happened so the
        # success path (below) knows not to wipe it back out.
        #
        # It returns whether the breaker is now open *for this call's
        # category* so a handler iterating over several items (e.g. one
        # job per stdout fetch) can stop issuing further requests within
        # the same logical tool call once the threshold is crossed,
        # instead of only affecting the *next* tool call — see
        # mantis.tools.awx.awx_recent_failed_jobs.
        degraded_within_call = False

        def _reliability_report(kind: IntegrationErrorKind) -> bool:
            nonlocal degraded_within_call
            degraded_within_call = True
            breaker.record_failure(tool.category, kind)
            return breaker.is_open(tool.category)

        handler_kwargs = dict(arguments)
        handler_params = inspect.signature(tool.handler).parameters
        if "_deadline" in handler_params:
            handler_kwargs["_deadline"] = tool_deadline
        if "_reliability_report" in handler_params:
            handler_kwargs["_reliability_report"] = _reliability_report

        handler_start = time.perf_counter()
        try:
            result = tool.handler(**handler_kwargs)
        except IntegrationError as exc:
            # A classified integration failure — the shared reliability
            # taxonomy (mantis.reliability), not a generic/unexpected bug.
            # Recorded against the run-local breaker (only a
            # classified-transient kind actually counts, see
            # RunLocalBreaker.record_failure) and turned into a
            # well-formed tool-facing ToolError result rather than an
            # opaque "raised an error" string.
            handler_duration = time.perf_counter() - handler_start
            breaker.record_failure(tool.category, exc.kind)
            detail = str(exc)
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "integration_error", detail)
            )
            emit(
                "integration_error",
                arguments=arguments,
                duration=handler_duration,
                error_kind=exc.kind.value,
            )
            safe_result = make_model_safe(
                {
                    "error": ToolError(kind=exc.to_tool_error_kind(), message=detail).to_dict(),
                    "note": (
                        "Mantis could not retrieve evidence from this integration. This "
                        "does not establish the target system's current state — it means "
                        "the retrieval attempt itself failed."
                    ),
                },
                contains_untrusted_text=tool.contains_untrusted_text,
            )
            return json.dumps(safe_result, default=str), False
        except DeadlineExceededError as exc:
            # The tool-call deadline was already gone before the handler
            # (specifically: before its retry policy) could make even one
            # attempt — a budget failure, not a classified integration
            # failure, so it's kept separate from the branch above and
            # never counted against the breaker.
            handler_duration = time.perf_counter() - handler_start
            detail = str(exc)
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "budget_exceeded", detail)
            )
            emit(
                "budget_exceeded",
                arguments=arguments,
                duration=handler_duration,
                error_kind=f"{exc.scope}_deadline_exceeded",
            )
            safe_result = make_model_safe(
                {"error": detail}, contains_untrusted_text=tool.contains_untrusted_text
            )
            return json.dumps(safe_result, default=str), False
        except Exception as exc:  # noqa: BLE001 — deliberately broad: any
            # other bug must fail cleanly back into the conversation, not
            # crash the runtime. This is the last-resort safety net, not
            # the primary classification path — see the IntegrationError
            # branch above for that.
            handler_duration = time.perf_counter() - handler_start
            detail = f"Tool '{tool_name}' raised an error: {exc}"
            logger.warning("[%s] %s", self.name, redact_text(detail))
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "error", detail)
            )
            emit(
                "error",
                arguments=arguments,
                duration=handler_duration,
                error_kind=type(exc).__name__,
            )
            return json.dumps({"error": detail}), False

        handler_duration = time.perf_counter() - handler_start
        # Only reset the breaker on a call that had no degraded-partial
        # failures reported via _reliability_report — a tool call that
        # swallowed one or more per-item integration failures into
        # partial evidence already recorded those against the breaker
        # above, and an unconditional reset here would silently wipe
        # that out on every single logical success, defeating the guard.
        if not degraded_within_call:
            breaker.record_success(tool.category)
        # The cache and call_log keep the tool's raw result — internal
        # diagnostics/eval-log fidelity — but the value actually
        # serialized into the model-facing tool message must go through
        # the safety pipeline first, same as a duplicate-call replay.
        tool_result_cache[dedupe_key] = result
        self.call_log.append(
            ToolCallLogEntry(iteration, tool_name, arguments, "ok", result=result)
        )
        emit("ok", arguments=arguments, result=result, duration=handler_duration)
        safe_result = make_model_safe(result, contains_untrusted_text=tool.contains_untrusted_text)
        return json.dumps(safe_result, default=str), True

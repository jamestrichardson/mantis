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
  executed) are detected and short-circuited: the *cached prior result* is
  replayed instead of re-executing, to avoid hammering an integration with
  redundant calls. Smaller local models can be inconsistent about
  attending to a tool result on the first pass and re-ask for the same
  data — the cache means a repeat ask is still answered with real
  evidence (plus a note telling the model to stop asking and answer),
  rather than punished with an error that starves it of the data it
  needs.
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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from mantis.config import LiteLLMConfig
from mantis.registry import Tool, ToolRegistry, default_registry

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8


class RuntimeError_(RuntimeError):
    """Base class for runtime-level failures (not tool failures)."""


class MaxIterationsExceededError(RuntimeError_):
    """Raised when the model/tool loop exceeds its iteration budget without
    producing a final answer."""


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

    After a call to :meth:`run`, two attributes hold a record of what
    happened, in order: ``call_log`` (tool-call attempts and their
    outcomes) and ``usage_log`` (per-iteration token usage, when the
    backend reports it). Both accumulate across repeated ``run()`` calls
    on the same instance rather than resetting.
    """

    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    model_config: LiteLLMConfig | None = None
    registry: ToolRegistry = field(default_factory=lambda: default_registry)
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    tool_call_budget: int | None = None
    temperature: float | None = None

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
        # Diagnostic only: the raw final message, captured only when a run
        # ends with neither tool calls nor usable answer text — a model
        # producing no content and no tool_calls despite spending
        # completion tokens usually means something (a provider-specific
        # field like reasoning_content, or a malformed tool-call attempt)
        # landed somewhere this runtime doesn't read. Left None otherwise
        # to avoid bloating every successful run with a redundant dump of
        # data already in the returned answer.
        self.diagnostic_raw_message: dict[str, Any] | None = None

    def _tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(tool.schema) for tool in self._resolved_tools.values()]

    def run(self, user_prompt: str) -> str:
        """Run the agent loop for a single user prompt and return the
        model's final textual answer.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
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

        for iteration in range(1, self.max_iterations + 1):
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

            response = self._client.chat.completions.create(**kwargs)
            self.usage_log.append(_usage_to_dict(getattr(response, "usage", None)))
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
                    tool_call, iteration=iteration, tool_result_cache=tool_result_cache
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
        tool_result_cache: dict[tuple[str, str], Any],
    ) -> tuple[str, bool]:
        """Execute (or replay/reject) one tool call.

        Returns ``(tool_message_content, fresh_success)`` — ``fresh_success``
        is True only when this call newly executed a tool's handler
        successfully (not on a cache replay, error, or rejection), which is
        what counts against ``tool_call_budget``.
        """
        tool_name = tool_call.function.name
        raw_arguments = tool_call.function.arguments or "{}"

        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments must decode to a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            detail = f"Malformed arguments for tool '{tool_name}': {exc}"
            logger.warning("[%s] %s", self.name, detail)
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, None, "bad_arguments", detail)
            )
            return json.dumps({"error": detail}), False

        dedupe_key = (tool_name, json.dumps(arguments, sort_keys=True))
        if dedupe_key in tool_result_cache:
            detail = (
                f"Duplicate call to '{tool_name}' with identical arguments; "
                "replayed the cached result instead of re-executing."
            )
            logger.info("[%s] %s", self.name, detail)
            self.call_log.append(
                ToolCallLogEntry(
                    iteration, tool_name, arguments, "duplicate", detail,
                    result=tool_result_cache[dedupe_key],
                )
            )
            # Never withhold data the model is asking for, even on a repeat
            # call — a small/local model may not attend well to a tool
            # result on the first pass and re-ask for it. Returning an error
            # here instead of the real data starves it of evidence and, in
            # practice, causes it to give up and echo the error back as its
            # "final answer" rather than ever producing a real summary.
            payload = {
                "note": (
                    "You already called this tool with these exact "
                    "arguments. This is the same result as before — it will "
                    "not change on another call. Use this data now to write "
                    "your final answer instead of calling this tool again."
                ),
                "result": tool_result_cache[dedupe_key],
            }
            return json.dumps(payload, default=str), False

        if tool_name not in self._resolved_tools:
            detail = f"Unknown tool: '{tool_name}'"
            logger.warning("[%s] %s", self.name, detail)
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "unknown_tool", detail)
            )
            return json.dumps({"error": detail}), False

        tool = self._resolved_tools[tool_name]
        try:
            result = tool.handler(**arguments)
        except Exception as exc:  # noqa: BLE001 — deliberately broad: any
            # integration exception must fail cleanly back into the
            # conversation, not crash the runtime.
            detail = f"Tool '{tool_name}' raised an error: {exc}"
            logger.warning("[%s] %s", self.name, detail)
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "error", detail)
            )
            return json.dumps({"error": detail}), False

        tool_result_cache[dedupe_key] = result
        self.call_log.append(
            ToolCallLogEntry(iteration, tool_name, arguments, "ok", result=result)
        )
        return json.dumps(result, default=str), True

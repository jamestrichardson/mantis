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
  executed) are detected and short-circuited with a message back to the
  model instead of re-executing, to avoid pathological repeat-call loops
  with smaller local models.
- Every failure mode (malformed arguments, unknown tool, integration
  exception) is caught and turned into a tool-result message rather than
  raising out of the loop, so a single bad call doesn't crash the agent.
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


@dataclass
class ToolCallLogEntry:
    """A single record of a tool invocation attempt, for observability."""

    iteration: int
    tool_name: str
    arguments: dict[str, Any] | None
    outcome: str  # "ok", "duplicate", "unknown_tool", "bad_arguments", "error"
    detail: str = ""


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
    """

    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    model_config: LiteLLMConfig | None = None
    registry: ToolRegistry = field(default_factory=lambda: default_registry)
    max_iterations: int = DEFAULT_MAX_ITERATIONS

    def __post_init__(self) -> None:
        if self.model_config is None:
            self.model_config = LiteLLMConfig.from_env()

        base_url = self.model_config.url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"

        self._client = OpenAI(base_url=base_url, api_key=self.model_config.api_key)
        self._resolved_tools: dict[str, Tool] = {
            tool.name: tool for tool in self.registry.subset(self.tools)
        }
        self.call_log: list[ToolCallLogEntry] = []

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
        executed_calls: set[tuple[str, str]] = set()
        tool_schemas = self._tool_schemas()

        for iteration in range(1, self.max_iterations + 1):
            logger.info("[%s] iteration %d/%d", self.name, iteration, self.max_iterations)

            kwargs: dict[str, Any] = {
                "model": self.model_config.model,
                "messages": messages,
            }
            if tool_schemas:
                kwargs["tools"] = tool_schemas

            response = self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message

            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                logger.info("[%s] final answer produced on iteration %d", self.name, iteration)
                return message.content or ""

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
                result_text = self._dispatch_tool_call(
                    tool_call, iteration=iteration, executed_calls=executed_calls
                )
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
        executed_calls: set[tuple[str, str]],
    ) -> str:
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
            return json.dumps({"error": detail})

        dedupe_key = (tool_name, json.dumps(arguments, sort_keys=True))
        if dedupe_key in executed_calls:
            detail = (
                f"Duplicate call to '{tool_name}' with identical arguments "
                "was skipped; reuse the prior result instead of calling again."
            )
            logger.warning("[%s] %s", self.name, detail)
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "duplicate", detail)
            )
            return json.dumps({"error": detail})

        if tool_name not in self._resolved_tools:
            detail = f"Unknown tool: '{tool_name}'"
            logger.warning("[%s] %s", self.name, detail)
            self.call_log.append(
                ToolCallLogEntry(iteration, tool_name, arguments, "unknown_tool", detail)
            )
            return json.dumps({"error": detail})

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
            return json.dumps({"error": detail})

        executed_calls.add(dedupe_key)
        self.call_log.append(
            ToolCallLogEntry(iteration, tool_name, arguments, "ok")
        )
        return json.dumps(result, default=str)

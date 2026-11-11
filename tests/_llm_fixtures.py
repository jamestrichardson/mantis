"""A real, local, OpenAI-compatible mock LiteLLM gateway (#16 end-to-end
routing evidence).

Built on ``tests/_http_fixtures.py``'s real ``http.server``-based
``HTTPTestServer`` — this is a genuine local HTTP boundary (real TCP
socket, real HTTP/1.1 framing, real JSON body), not a monkeypatch of
``openai.OpenAI.chat.completions.create``. A test using this fixture
exercises the full, real chain:

    AgentRuntime -> openai.OpenAI SDK -> real local HTTP -> this gateway

The gateway understands just enough of ``POST /v1/chat/completions`` to
serve scripted responses per requested ``model`` alias, and records
every incoming request body (unredacted) so a test can assert exactly
what ``AgentRuntime`` sent -- messages, tools, and the ``extra_body``
attribution metadata -- through the real HTTP boundary rather than
inspecting in-process call kwargs.

Not a test file itself (no ``test_`` functions) — imported by the real
test modules, mirroring ``tests/_http_fixtures.py``'s/
``tests/_tls_fixtures.py``'s own convention.
"""

from __future__ import annotations

import itertools
import json
import threading
from typing import Any

from _http_fixtures import HTTPTestServer, read_json_body, send_simple

_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

_id_counter = itertools.count(1)


def tool_call_response(tool_name: str, arguments: dict[str, Any], *, call_id: str | None = None) -> dict[str, Any]:
    """A real OpenAI chat-completion JSON response requesting one tool
    call — the wire-format equivalent of the in-process
    ``FakeResponse``/``FakeToolCall`` helpers used by
    ``tests/test_runtime.py``'s monkeypatched-client tests, for a test
    that instead goes through the real HTTP boundary."""
    return {
        "id": f"chatcmpl-mock-{next(_id_counter)}",
        "object": "chat.completion",
        "created": 0,
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id or f"call_{next(_id_counter)}",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def final_message_response(text: str) -> dict[str, Any]:
    """A real OpenAI chat-completion JSON response with a plain final
    answer, no tool call."""
    return {
        "id": f"chatcmpl-mock-{next(_id_counter)}",
        "object": "chat.completion",
        "created": 0,
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class MockLLMGateway:
    """A real local OpenAI-compatible ``/v1/chat/completions`` server.

    ``scripts`` maps a requested model *alias* to an ordered list of
    scripted responses for that alias, consumed one at a time (mirroring
    ``tests/test_runtime.py``'s in-process ``FakeCompletions``' own
    queue-popping convention, but served over real HTTP here). Each
    scripted entry is either:

    - a ``dict`` (from :func:`tool_call_response`/
      :func:`final_message_response`) — served as a ``200`` with that
      JSON body, or
    - an ``int`` HTTP status code — served as that status with a
      minimal OpenAI-shaped error JSON body, which the real
      ``openai`` SDK raises as the matching exception
      (``503`` -> ``openai.InternalServerError`` -> classified
      ``ModelCallFailureKind.SERVER_ERROR``, eligible for fallback).

    Every request actually received is recorded verbatim (the full
    decoded JSON body) in ``received_requests``, in arrival order, for
    a test to assert against — this is the real request Mantis sent
    over the wire, not a reconstruction of in-process call kwargs.
    """

    def __init__(self, scripts: dict[str, list[dict[str, Any] | int]]) -> None:
        self._scripts = {alias: list(queue) for alias, queue in scripts.items()}
        self.received_requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

        def _respond(handler: Any, status: int, payload: dict[str, Any]) -> None:
            # Force the connection closed after this one response --
            # without this, the real openai SDK's httpx connection pool
            # keeps this HTTP/1.1 keep-alive connection open expecting
            # to reuse it, which blocks HTTPTestServer's (single-
            # threaded, one-connection-at-a-time) shutdown() forever
            # waiting on that idle socket. A real production LiteLLM
            # deployment serves many concurrent connections and doesn't
            # have this problem; this local single-threaded test
            # gateway does, so every response is deliberately one-shot.
            handler.close_connection = True
            send_simple(
                handler,
                status,
                headers={"Content-Type": "application/json", "Connection": "close"},
                body=json.dumps(payload).encode(),
            )

        def _handle_chat_completions(handler: Any) -> None:
            body = read_json_body(handler)
            with self._lock:
                self.received_requests.append(body)
                model = body.get("model") if isinstance(body, dict) else None
                queue = self._scripts.get(model)

            if not queue:
                _respond(
                    handler,
                    500,
                    {"error": {"message": f"mock gateway: no scripted response left for model {model!r}"}},
                )
                return

            item = queue.pop(0)
            if isinstance(item, int):
                _respond(handler, item, {"error": {"message": "mock gateway: scripted failure", "type": "server_error"}})
                return

            payload = dict(item)
            payload["model"] = model
            _respond(handler, 200, payload)

        self._server = HTTPTestServer({_CHAT_COMPLETIONS_PATH: _handle_chat_completions})

    @property
    def url(self) -> str:
        """The root URL for ``LiteLLMConfig(url=...)`` -- deliberately
        *without* a ``/v1`` suffix, matching how a real ``LITELLM_URL``
        is normally configured; ``build_openai_client`` appends
        ``/v1`` itself, landing on this gateway's one registered route,
        ``/v1/chat/completions``."""
        return f"http://127.0.0.1:{self._server.port}"

    def close(self) -> None:
        self._server.close()

    def __enter__(self) -> "MockLLMGateway":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

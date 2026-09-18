"""HTTP client for the Mantis API (#83), used exclusively by
``mantis.cli``.

The official CLI must never construct :class:`~mantis.runtime.AgentRuntime`,
execute a tool, or call LiteLLM directly — every supported agent
invocation goes through this module's real HTTP calls to the persistent
Mantis service (``mantis serve``, #21). There is no local-execution
fallback anywhere in this module: every failure mode (unreachable,
timed out, unauthenticated, rejected, server error) is surfaced as a
distinct, typed exception for the CLI to report, never swallowed into a
silent retry-in-process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from mantis.config import ApiClientConfig


class ApiClientError(Exception):
    """Base class for a client-observable failure talking to the Mantis API."""


class ApiUnavailableError(ApiClientError):
    """The Mantis API could not be reached at all (connection refused,
    DNS failure, TLS failure, ...) — never treated as "run it locally
    instead"."""


class ApiTimeoutError(ApiClientError):
    """The Mantis API did not respond within the configured client
    timeout (``MANTIS_API_CLIENT_CONNECT_TIMEOUT_SECONDS``/
    ``MANTIS_API_CLIENT_READ_TIMEOUT_SECONDS``)."""


class ApiAuthError(ApiClientError):
    """The configured ``MANTIS_API_TOKEN`` was rejected (HTTP 401)."""


class ApiRequestError(ApiClientError):
    """The API returned a well-formed rejection (HTTP 4xx other than
    401) — carries the parsed, safe error type/message from the
    response body (see ``mantis.api.schemas.ErrorResponse``).

    ``run_id`` is set when the server had already assigned one before
    rejecting the request (e.g. a 429 overload rejection — see
    ``mantis.api.invocation.ConcurrencyLimitExceededError``) — Mantis
    deliberately generates that ID so even a rejected attempt is
    correlatable in server-side logs; this client must not discard it.
    """

    def __init__(self, status_code: int, error_type: str, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.run_id = run_id


class ApiServerError(ApiClientError):
    """The API returned an HTTP 5xx, or a response this client could not
    parse as the expected shape."""


@dataclass(frozen=True)
class AgentInfo:
    """One entry from ``GET /api/v1/agents`` — mirrors
    ``mantis.api.schemas.AgentSummary`` field-for-field."""

    id: str
    display_name: str
    description: str
    read_only: bool
    available: bool
    unavailable_reason: str | None


@dataclass(frozen=True)
class RunResult:
    """The client-side view of a ``POST /api/v1/runs`` response —
    mirrors ``mantis.api.schemas.RunResponse``."""

    run_id: str
    agent: str
    outcome: str
    output: str | None
    error_kind: str | None
    error_message: str | None
    started_at: str
    finished_at: str
    duration_ms: int


_DEFAULT_ERROR_TYPE = "request_error"
_DEFAULT_ERROR_MESSAGE = "The request was rejected."


def _parse_error_body(response: httpx.Response) -> tuple[str, str, str | None]:
    """Parse ``mantis.api.schemas.ErrorResponse``'s
    ``{"error": {"type", "message", "run_id"}}`` shape defensively —
    the response is coming from the network, not a value this client
    controls. A non-JSON body, or JSON that isn't the expected object
    shape (e.g. a bare list or string, which a misbehaving proxy could
    return instead of the real API), falls back to a safe default
    rather than raising ``AttributeError``/``TypeError`` out of this
    function and escaping the typed :class:`ApiClientError` hierarchy
    entirely.
    """
    try:
        body = response.json()
    except ValueError:
        return _DEFAULT_ERROR_TYPE, _DEFAULT_ERROR_MESSAGE, None
    if not isinstance(body, dict):
        return _DEFAULT_ERROR_TYPE, _DEFAULT_ERROR_MESSAGE, None
    error = body.get("error")
    if not isinstance(error, dict):
        return _DEFAULT_ERROR_TYPE, _DEFAULT_ERROR_MESSAGE, None
    error_type = error.get("type")
    message = error.get("message")
    run_id = error.get("run_id")
    return (
        error_type if isinstance(error_type, str) else _DEFAULT_ERROR_TYPE,
        message if isinstance(message, str) else _DEFAULT_ERROR_MESSAGE,
        run_id if isinstance(run_id, str) else None,
    )


class MantisApiClient:
    """Thin HTTP client for the versioned Mantis API. Constructs a fresh
    ``httpx.Client`` per call — this is a low-frequency, interactive CLI
    tool, not a high-throughput service client, so there's no need for
    connection-pool lifetime management beyond one request."""

    def __init__(self, config: ApiClientConfig | None = None) -> None:
        self._config = config or ApiClientConfig.from_env()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._config.token is not None:
            headers["Authorization"] = f"Bearer {self._config.token.get_secret_value()}"
        return headers

    def _request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=self._config.read_timeout_seconds,
            write=self._config.read_timeout_seconds,
            pool=self._config.connect_timeout_seconds,
        )
        try:
            with httpx.Client(base_url=self._config.base_url, timeout=timeout) as client:
                return client.request(method, path, json=json, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ApiTimeoutError(
                f"The Mantis API at {self._config.base_url} did not respond in time: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(f"Could not reach the Mantis API at {self._config.base_url}: {exc}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise ApiAuthError("Authentication with the Mantis API failed -- check MANTIS_API_TOKEN.")
        if 400 <= response.status_code < 500:
            error_type, message, run_id = _parse_error_body(response)
            raise ApiRequestError(response.status_code, error_type, message, run_id=run_id)
        if response.status_code >= 500:
            raise ApiServerError(
                f"The Mantis API returned an unexpected server error (HTTP {response.status_code})."
            )

    def list_agents(self) -> list[AgentInfo]:
        response = self._request("GET", "/api/v1/agents")
        self._raise_for_status(response)
        try:
            data = response.json()
            return [AgentInfo(**entry) for entry in data["agents"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiServerError("The Mantis API returned an unexpected response shape.") from exc

    def create_run(self, agent: str, prompt: str) -> RunResult:
        response = self._request("POST", "/api/v1/runs", json={"agent": agent, "prompt": prompt})
        self._raise_for_status(response)
        try:
            data = response.json()
            error = data.get("error")
            return RunResult(
                run_id=data["run_id"],
                agent=data["agent"],
                outcome=data["outcome"],
                output=data.get("output"),
                error_kind=error["kind"] if error else None,
                error_message=error["message"] if error else None,
                started_at=data["started_at"],
                finished_at=data["finished_at"],
                duration_ms=data["duration_ms"],
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiServerError("The Mantis API returned an unexpected response shape.") from exc

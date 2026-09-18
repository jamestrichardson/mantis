"""Pydantic request/response models for the versioned Mantis API (#83).

These are the only place the HTTP-facing shape of a request/response is
defined — route handlers build these from
``mantis.api.catalog``/``mantis.api.invocation`` objects, they never
hand-serialize a dict. FastAPI derives ``/openapi.json``/``/docs``
directly from these models (field descriptions/examples below are part
of the supported developer experience, not incidental).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mantis.api.catalog import AgentCatalogEntry
from mantis.api.invocation import MAX_PROMPT_CHARS, RunResult

MAX_AGENT_ID_CHARS = 128


class AgentSummary(BaseModel):
    """Public, safe metadata for one invokable agent — never a system
    prompt, provider model ID, integration configuration, credential,
    filesystem path, or internal Python class name (see #83)."""

    id: str = Field(..., description="Stable canonical agent ID, used in `POST /api/v1/runs`.")
    display_name: str = Field(..., description="Human-readable agent name.")
    description: str = Field(..., description="What this agent investigates and how.")
    read_only: bool = Field(..., description="True if this agent cannot mutate any external system.")
    available: bool = Field(..., description="False if the agent is currently unable to run (see `unavailable_reason`).")
    unavailable_reason: str | None = Field(
        None,
        description="A stable, low-cardinality reason code when `available` is false (e.g. `misconfigured`). Never a raw error message.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "system-troubleshooter",
                "display_name": "System Troubleshooter",
                "description": "Correlates AWX, network, Prometheus, and Loki evidence to investigate a system-level issue.",
                "read_only": True,
                "available": True,
                "unavailable_reason": None,
            }
        }
    )

    @classmethod
    def from_entry(cls, entry: AgentCatalogEntry) -> "AgentSummary":
        available, reason = entry.probe_availability()
        return cls(
            id=entry.id,
            display_name=entry.display_name,
            description=entry.description,
            read_only=entry.read_only,
            available=available,
            unavailable_reason=reason,
        )


class AgentsResponse(BaseModel):
    """Response body for ``GET /api/v1/agents``."""

    agents: list[AgentSummary]


class RunRequest(BaseModel):
    """Request body for ``POST /api/v1/runs``.

    Deliberately narrow: no tool allowlist override, no provider model
    ID override, no mutation/policy override, and no integration
    credentials — those all remain server-side configuration (see #83's
    hard architectural rules). Unknown fields are rejected outright
    rather than silently ignored.
    """

    agent: str = Field(
        ...,
        min_length=1,
        max_length=MAX_AGENT_ID_CHARS,
        description="Canonical agent ID from `GET /api/v1/agents`.",
    )
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PROMPT_CHARS,
        description="The investigation request to send to the agent.",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "agent": "system-troubleshooter",
                "prompt": "Investigate why payment-api is unhealthy",
            }
        },
    )


class RunErrorBody(BaseModel):
    """A safe, bounded description of why a run did not succeed."""

    kind: str = Field(..., description="Stable machine-readable failure category.")
    message: str = Field(..., description="Safe, human-readable summary. Never a raw stack trace or provider body.")


class RunResponse(BaseModel):
    """Response body for a request `POST /api/v1/runs` actually
    attempted (as opposed to one rejected before execution — see
    `ErrorResponse`). `outcome="error"` still returns HTTP 200: the API
    request itself succeeded in performing the invocation attempt: the
    *agent run* is what failed. See `docs/api.md`."""

    run_id: str = Field(..., description="Stable ID for this run, present in correlated server-side logs.")
    agent: str
    outcome: str = Field(..., description='"success" or "error".')
    output: str | None = Field(None, description="The agent's final answer, when `outcome` is `success`.")
    error: RunErrorBody | None = Field(None, description="Set when `outcome` is `error`.")
    started_at: str
    finished_at: str
    duration_ms: int

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "run_id": "3f9a1c2e4b6d4f0aa2c8e6d1b7a90123",
                    "agent": "system-troubleshooter",
                    "outcome": "success",
                    "output": "AWX previously observed...",
                    "error": None,
                    "started_at": "2026-09-18T12:00:00+00:00",
                    "finished_at": "2026-09-18T12:00:04+00:00",
                    "duration_ms": 4231,
                },
                {
                    "run_id": "8b2d4e6f1a3c4d5e9f0a1b2c3d4e5f60",
                    "agent": "system-troubleshooter",
                    "outcome": "error",
                    "output": None,
                    "error": {
                        "kind": "max_iterations",
                        "message": "The agent could not produce a final answer within its iteration limit.",
                    },
                    "started_at": "2026-09-18T12:05:00+00:00",
                    "finished_at": "2026-09-18T12:05:30+00:00",
                    "duration_ms": 30000,
                },
            ]
        }
    )

    @classmethod
    def from_result(cls, result: RunResult) -> "RunResponse":
        return cls(
            run_id=result.run_id,
            agent=result.agent,
            outcome=result.outcome,
            output=result.output,
            error=RunErrorBody(kind=result.error.kind, message=result.error.message) if result.error else None,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
        )


class ErrorDetail(BaseModel):
    type: str = Field(..., description="Stable machine-readable error category.")
    message: str = Field(..., description="Safe, human-readable message. Never a raw stack trace or credential.")
    run_id: str | None = Field(None, description="Set when a run ID was already assigned before this rejection.")


class ErrorResponse(BaseModel):
    """Body for a request rejected *before* an agent was invoked:
    validation failure (422), authentication failure (401), unknown/
    unavailable agent (404/409), or overload (429). Distinct from
    `RunResponse`'s `outcome="error"`, which describes an agent run that
    was actually attempted."""

    error: ErrorDetail

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"error": {"type": "unknown_agent", "message": "Unknown agent: 'nope'", "run_id": None}}
        }
    )


class HealthResponse(BaseModel):
    status: str = Field(..., description='Always "ok" when this endpoint responds at all.')


class ReadyResponse(BaseModel):
    status: str = Field(..., description='"ready" or "not_ready".')
    reason: str | None = Field(None, description="Safe, low-cardinality reason when not ready.")

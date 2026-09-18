"""The real, production Mantis FastAPI application (#21/#83).

::

    HTTP route -> InvocationService -> AgentCatalog -> real AgentRuntime

Every route below is wired to the exact objects :func:`create_app`
constructs — there is no parallel demo/fake app, and no route
implements agent-specific logic of its own (see #83's hard
architectural rules). ``mantis.api.server`` hosts this app with uvicorn
as the ``mantis serve`` persistent process (#21); tests
(``tests/test_api_app.py``) exercise this exact factory via FastAPI's
``TestClient``.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mantis.api.auth import build_auth_dependency
from mantis.api.catalog import AgentCatalog, AgentCatalogError, build_default_catalog
from mantis.api.invocation import InvocationError, InvocationService
from mantis.api.schemas import (
    AgentsResponse,
    AgentSummary,
    ErrorResponse,
    HealthResponse,
    ReadyResponse,
    RunRequest,
    RunResponse,
)
from mantis.config import ApiServerConfig
from mantis.observability.logging import log_event

logger = logging.getLogger(__name__)

API_TITLE = "Mantis API"
API_DESCRIPTION = (
    "HTTP interface for invoking Mantis agents. See docs/api.md in the "
    "Mantis repository for authentication, run semantics, concurrency/"
    "overload behavior, and the CLI relationship."
)


def _error_response(status_code: int, error_type: str, message: str, *, run_id: str | None = None) -> JSONResponse:
    body = ErrorResponse.model_validate({"error": {"type": error_type, "message": message, "run_id": run_id}})
    return JSONResponse(status_code=status_code, content=body.model_dump())


def create_app(
    *,
    server_config: ApiServerConfig | None = None,
    catalog: AgentCatalog | None = None,
) -> FastAPI:
    """Build the real Mantis FastAPI application.

    ``server_config``/``catalog`` are overridable only so deterministic
    tests can inject a fixed config/catalog without touching the real
    environment or the real agent modules — production code always
    calls this with no arguments, resolving
    ``ApiServerConfig.from_env()``/``build_default_catalog()``.
    """
    resolved_config = server_config or ApiServerConfig.from_env()
    resolved_catalog = catalog or build_default_catalog()
    invocation_service = InvocationService(
        resolved_catalog, max_concurrent_runs=resolved_config.max_concurrent_runs
    )
    auth_dependency = build_auth_dependency(resolved_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: mandatory local initialization only -- config, the
        # agent catalog, and the invocation service were already
        # constructed above (closed over here, not deferred), so this
        # step just marks the process ready. No model/agent/integration
        # call happens here or anywhere in /readyz -- see #21.
        app.state.startup_complete = True
        log_event(logger, "mantis_api_startup_complete")
        yield
        # Shutdown: flip readiness to not-ready as the very first
        # action, before any other cleanup -- #21 requires readiness to
        # transition to not-ready *before* new work is rejected, and
        # both derive from this one flag (see create_run below), so
        # there is no window where they disagree.
        app.state.shutting_down = True
        log_event(logger, "mantis_api_shutdown_started")

    app = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version="1",
        lifespan=lifespan,
    )
    app.state.startup_complete = False
    app.state.shutting_down = False
    app.state.invocation_service = invocation_service
    app.state.catalog = resolved_catalog
    app.state.server_config = resolved_config

    @app.middleware("http")
    async def _observability_middleware(request: Request, call_next: Any) -> Any:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - start) * 1000)
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or request.url.path
        log_event(
            logger,
            "mantis_api_request",
            invocation_source="http",
            route=route_path,
            method=request.method,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, "validation_error", "The request did not match the expected shape.")

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        # Every HTTPException raised anywhere in this app (auth, or
        # FastAPI's own routing errors) is reshaped into the one
        # ErrorResponse envelope -- see mantis.api.auth._unauthorized for
        # the {"type", "message", "run_id"} shape this expects.
        detail = exc.detail
        if isinstance(detail, dict) and "type" in detail and "message" in detail:
            body = ErrorResponse.model_validate({"error": detail})
        else:
            body = ErrorResponse.model_validate({"error": {"type": "http_error", "message": str(detail), "run_id": None}})
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(), headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        log_event(
            logger,
            "mantis_api_unhandled_error",
            level=logging.ERROR,
            error_kind=type(exc).__name__,
            route=request.url.path,
        )
        return _error_response(500, "internal_error", "An unexpected error occurred.")

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        tags=["health"],
        summary="Liveness probe",
        description="Cheap, bounded liveness check. Never calls a model, agent, or integration.",
    )
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/readyz",
        response_model=ReadyResponse,
        tags=["health"],
        summary="Readiness probe",
        description=(
            "Reports whether mandatory local startup has completed and the process is "
            "still accepting new runs. Never calls a model, agent, or integration."
        ),
        responses={503: {"model": ReadyResponse, "description": "Not yet started, or shutting down"}},
    )
    async def readyz(request: Request) -> JSONResponse:
        if request.app.state.startup_complete and not request.app.state.shutting_down:
            return JSONResponse(status_code=200, content=ReadyResponse(status="ready").model_dump())
        reason = "shutting_down" if request.app.state.shutting_down else "starting_up"
        return JSONResponse(
            status_code=503, content=ReadyResponse(status="not_ready", reason=reason).model_dump()
        )

    @app.get(
        "/api/v1/agents",
        response_model=AgentsResponse,
        tags=["agents"],
        summary="List invokable agents",
        dependencies=[Depends(auth_dependency)],
        responses={401: {"model": ErrorResponse}},
    )
    async def list_agents() -> AgentsResponse:
        return AgentsResponse(agents=[AgentSummary.from_entry(entry) for entry in resolved_catalog.list()])

    @app.post(
        "/api/v1/runs",
        response_model=RunResponse,
        tags=["runs"],
        summary="Invoke an agent",
        description=(
            "Executes the named agent through the real Mantis agent/runtime/tool stack and "
            "returns its final answer. `outcome=\"error\"` still returns HTTP 200: the "
            "invocation attempt itself completed, the agent run is what failed."
        ),
        dependencies=[Depends(auth_dependency)],
        responses={
            401: {"model": ErrorResponse, "description": "Missing or invalid bearer token"},
            404: {"model": ErrorResponse, "description": "Unknown agent"},
            409: {"model": ErrorResponse, "description": "Agent currently unavailable"},
            422: {"model": ErrorResponse, "description": "Request validation failure"},
            429: {"model": ErrorResponse, "description": "Server is at its concurrent-run limit"},
            503: {"model": ErrorResponse, "description": "Service is starting up or shutting down"},
        },
    )
    async def create_run(payload: RunRequest, request: Request) -> JSONResponse:
        if not request.app.state.startup_complete or request.app.state.shutting_down:
            return _error_response(503, "not_ready", "The service is not currently accepting new runs.")
        try:
            result = await invocation_service.invoke(payload.agent, payload.prompt)
        except AgentCatalogError as exc:
            return _error_response(exc.http_status, exc.kind, str(exc))
        except InvocationError as exc:
            return _error_response(exc.http_status, exc.kind, str(exc), run_id=exc.run_id)
        return JSONResponse(status_code=200, content=RunResponse.from_result(result).model_dump())

    return app

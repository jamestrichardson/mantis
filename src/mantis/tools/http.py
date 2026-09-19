"""Semantic HTTP probe tool exposed to agents (#110).

Provides ``http_probe(target_alias, path="/", method="GET")``: a
bounded, read-only GET/HEAD request against one server-side-configured
HTTP(S) target, answering "what does this origin's endpoint currently
return?" as current-state evidence — a received response (any status
code) is successful retrieval; only a transport-level failure (DNS/
connect/TLS/timeout/malformed response) is a retrieval failure. See
``docs/http-probe.md`` for the full design.

All input validation (target alias, method, path) happens here, before
any request is attempted (see ``mantis.integrations.http`` for the
request/streaming mechanics this gates) — the same reasoning
``mantis.tools.network``/``.dns`` document: invalid input is untrusted,
model-supplied data (#14) and must flow through
``mantis.security.make_model_safe()`` like any other tool result, never
through the runtime's generic last-resort exception path.

Adopts the shared result contract from ``mantis.contracts`` (``meta`` /
``QueryMeta``), the same pattern every other Mantis tool established.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mantis.config import HTTPProfilesConfig, ReliabilityConfig
from mantis.contracts import QueryMeta
from mantis.integrations.http import (
    HTTPMethodValidationError,
    HTTPPathValidationError,
    HTTPProbeResult,
    probe_http,
    validate_http_method,
    validate_http_path,
)
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline

logger = logging.getLogger(__name__)

# Every field below is either the original request or a direct
# pass-through of the target's own reported data -- nothing here is
# Mantis-computed interpretation. See mantis.contracts.QueryMeta.derived_fields.
DERIVED_RESULT_FIELDS: tuple[str, ...] = ()


def _get_http_config() -> HTTPProfilesConfig:
    return HTTPProfilesConfig.from_env()


def _invalid_input_result(target_alias: Any, path: Any, method: Any, message: str) -> dict[str, Any]:
    # Deliberately never interpolates `message`/target_alias/path/method
    # into the log line -- see mantis.tools.dns's identical fix (PR #117
    # review): a validator's exception text is designed for the
    # *returned* "message" field, which goes through
    # mantis.security.make_model_safe() like any other tool result, not
    # for a raw logger.info(..., message) call that would write it
    # straight to container stdout/Loki unredacted.
    logger.info("http_probe rejected invalid input")
    meta = QueryMeta(source_system="http", derived_fields=list(DERIVED_RESULT_FIELDS))
    return {
        "meta": meta.to_dict(),
        "target_alias": target_alias,
        "method": method,
        "path": path,
        "scheme": None,
        "host": None,
        "port": None,
        "status_code": None,
        "reason": None,
        "latency_ms": None,
        "headers": {},
        "body_excerpt": None,
        "body_bytes_observed": 0,
        "redirect_location": None,
        "error": {"type": "invalid_input", "message": message},
    }


def http_probe(
    target_alias: Any,
    path: Any = "/",
    method: Any = "GET",
    *,
    _deadline: Deadline | None = None,
    _config: HTTPProfilesConfig | None = None,
    _probe_fn: Callable[..., HTTPProbeResult] = probe_http,
) -> dict[str, Any]:
    """Send one bounded GET/HEAD request to a server-side-configured
    HTTP(S) target and return the response as structured evidence.

    Args:
        target_alias: The *name* of a server-side-configured HTTP
            target (e.g. ``"grafana"``) — see
            ``mantis.config.HTTPProfilesConfig``. This is the **only**
            target-selecting input a caller may supply: never a
            scheme, host, port, URL, or proxy directly. An alias with
            no matching configured target is rejected as invalid input
            *before* any request is attempted — no network access
            happens for an unknown alias.
        path: A path relative to the configured target's origin (and
            optional configured base path), e.g. ``"/api/health"``.
            Validated (see
            ``mantis.integrations.http.validate_http_path``):
            must start with a single ``/``, bounded length, no absolute
            URL, no ``//host`` escape, no query string or fragment, no
            CR/LF/control characters, no embedded credentials. Can
            never change the configured scheme/host/port, and can never
            escape the configured origin.
        method: ``"GET"`` or ``"HEAD"`` only (case-insensitive) — see
            ``mantis.integrations.http.SUPPORTED_HTTP_METHODS``.
            Anything else (POST/PUT/PATCH/DELETE/CONNECT/...) is
            rejected. There is no way to send a body, custom headers,
            cookies, or arbitrary auth through this tool.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime`` — see ``mantis.reliability.Deadline`` and
            ``docs/reliability.md``. Same keyword-only/underscore
            convention as every other Mantis tool.
        _config: Test/evaluation-only :class:`~mantis.config.HTTPProfilesConfig`
            override — same convention as the AWX/Prometheus/Loki/
            Kubernetes/DNS tools' ``_client``/``_config``.
        _probe_fn: Test/evaluation-only override for
            :func:`mantis.integrations.http.probe_http` — same
            convention as ``mantis.tools.network``'s ``_connect_fn``.

    Returns:
        A dict with ``meta`` (provenance — ``source_system="http"``),
        the request echoed back (``target_alias``/``method``/``path``),
        the resolved origin (``scheme``/``host``/``port`` — the
        target's *configured* values, never caller-supplied),
        ``status_code``/``reason`` (any HTTP status, 100-599, is normal
        successful evidence — never raised as an error merely for
        being non-2xx), ``latency_ms``, ``headers`` (a small explicit
        allowlist only — see
        ``mantis.integrations.http._ALLOWED_RESPONSE_HEADERS`` — never
        ``set-cookie``/``authorization``/``proxy-authorization``/
        ``cookie``, never every header), ``body_excerpt`` (bounded,
        decoded best-effort as UTF-8; ``None`` for ``HEAD``, which never
        attempts to read a body at all), ``body_bytes_observed``, and
        ``redirect_location`` (the bounded ``Location`` header for a
        3xx response — the redirect is never automatically followed,
        same-origin or not).

        If ``target_alias``/``path``/``method`` fail validation, the
        result carries ``"error": {"type": "invalid_input", "message":
        ...}`` and every network-observation field is ``None``/empty —
        returned as a normal result, not raised, since the rejected
        text is untrusted, model-supplied data (#14). A successful
        probe always has ``"error": None``.
    """
    try:
        safe_method = validate_http_method(method)
    except HTTPMethodValidationError as exc:
        return _invalid_input_result(target_alias, path, method, str(exc))

    try:
        safe_path = validate_http_path(path)
    except HTTPPathValidationError as exc:
        return _invalid_input_result(target_alias, path, method, str(exc))

    config = _config or _get_http_config()
    target = config.resolve_target(target_alias)
    if target is None:
        return _invalid_input_result(
            target_alias, path, method, f"Unknown HTTP target alias: {target_alias!r}"
        )

    reliability = ReliabilityConfig.from_env()
    result = _probe_fn(
        scheme=target.scheme,
        host=target.host,
        port=target.port,
        base_path=target.base_path,
        verify_ssl=target.verify_ssl,
        path=safe_path,
        method=safe_method,
        connect_timeout_seconds=reliability.http_connect_timeout_seconds,
        read_timeout_seconds=reliability.http_read_timeout_seconds,
        deadline=_deadline,
    )

    meta = QueryMeta(
        source_system="http",
        query_time=result.observed_at,
        observation_time=result.observed_at,
        truncated=result.truncated,
        derived_fields=list(DERIVED_RESULT_FIELDS),
    )

    return {
        "meta": meta.to_dict(),
        "target_alias": target_alias,
        "method": result.method,
        "path": result.path,
        "scheme": result.scheme,
        "host": result.host,
        "port": result.port,
        "status_code": result.status_code,
        "reason": result.reason,
        "latency_ms": result.latency_ms,
        "headers": result.headers,
        "body_excerpt": result.body_excerpt,
        "body_bytes_observed": result.body_bytes_observed,
        "redirect_location": result.redirect_location,
        "error": None,
    }


HTTP_PROBE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "http_probe",
        "description": (
            "Send a single bounded GET or HEAD request to a "
            "server-side-configured HTTP(S) target and report the "
            "response as current-state evidence. Any HTTP status code "
            "(200, 301, 404, 500, ...) is a normal, successful result "
            "-- not an error. Redirects are never automatically "
            "followed; a 3xx response's Location is reported instead. "
            "You may only select a target by its configured alias -- "
            "you cannot supply a host, port, URL, or scheme directly. "
            "No request body, custom headers, cookies, or "
            "authentication of any kind. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_alias": {
                    "type": "string",
                    "description": (
                        "Name of a server-side-configured HTTP target "
                        "(e.g. \"grafana\"). Never a host/URL/port directly."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to the configured target's origin, "
                        "e.g. \"/api/health\". Must start with '/'. No "
                        "absolute URLs, no query strings, no fragments."
                    ),
                },
                "method": {
                    "type": "string",
                    "description": "HTTP method. Defaults to GET.",
                    "enum": ["GET", "HEAD"],
                },
            },
            "required": ["target_alias"],
        },
    },
}


default_registry.register(
    Tool(
        name="http_probe",
        schema=HTTP_PROBE_SCHEMA,
        handler=http_probe,
        category="http",
        mutating=False,
        # Response bodies/headers/redirect Location are external,
        # Mantis-uncontrolled data -- same untrusted-output treatment
        # as every other evidence tool. See mantis.security and
        # docs/security.md.
        contains_untrusted_text=True,
        description=(
            "Send a bounded GET/HEAD request to a server-side-configured "
            "HTTP(S) target and report the response as current-state evidence."
        ),
    )
)

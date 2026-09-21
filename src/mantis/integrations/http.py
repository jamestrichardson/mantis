"""Bounded, read-only HTTP probe mechanics (#110): a single GET/HEAD
request against a server-side-configured origin, with the response
(any status code) treated as successful evidence retrieval.

This module knows how to send one bounded HTTP request and read its
response with hard caps on bytes/headers, never how to pick a target —
see ``mantis.tools.http`` for the semantic, LLM-facing layer that
resolves a target *alias* (via ``mantis.config.HTTPProfilesConfig``)
into the concrete scheme/host/port/base_path this module receives.

Reliability posture (#15): mirrors ``mantis.integrations.network``/``.dns``
exactly — this is a current-state *observation*, not an idempotent
read that should be retried, so :func:`probe_http` is never wrapped in
``mantis.reliability.retry_call()``. Connect/read timeouts reuse
``mantis.config.ReliabilityConfig``'s shared HTTP timeout fields (the
same ones AWX/Prometheus/Loki use) rather than inventing a second
timeout family — the deliberate difference from those integrations is
that this one is never retried and never raises merely for a non-2xx
status (see :func:`probe_http`'s docstring).

Uses ``httpx``, but with its automatic redirect following, environment
proxy trust, and cookie jar all **explicitly disabled** — see
:func:`_build_client`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

from mantis.reliability import Deadline, IntegrationError, IntegrationErrorKind, classify_httpx_exception

SOURCE_SYSTEM = "http"

SUPPORTED_HTTP_METHODS = ("GET", "HEAD")
"""#110's initial method allowlist. No body-carrying method (POST/PUT/
PATCH/DELETE) and no CONNECT — see #110's non-goals."""

MAX_PATH_CHARS = 512
"""Hard cap on the model-facing ``path`` argument's length."""

MAX_BODY_BYTES_READ = 65_536
"""Hard cap on raw response-body bytes ever read off the wire, for any
single request — enforced by streaming and stopping, never by reading
the full body and truncating afterward (see :func:`_read_bounded_body`).
64 KiB is comfortably larger than :data:`MAX_BODY_EXCERPT_CHARS` needs
(accounting for multi-byte UTF-8 and decode overhead) while bounding
worst-case memory/time regardless of how large the real response is."""

MAX_BODY_EXCERPT_CHARS = 2_000
"""Cap on the model-facing decoded body excerpt, in characters — well
below ``mantis.security.MODEL_TOOL_RESULT_MAX_CHARS`` (this is one
field of one tool result, not the whole thing), generous enough for a
typical health-check JSON/HTML error payload."""

_ALLOWED_RESPONSE_HEADERS = ("content-type", "content-length", "server", "date", "location", "retry-after")
"""Explicit safe allowlist — never blindly return every response
header. Excludes (among everything else) ``set-cookie``,
``authorization``/``proxy-authorization``, and ``cookie`` by
construction, since only names in this tuple are ever copied into a
result."""

MAX_HEADERS_RETURNED = len(_ALLOWED_RESPONSE_HEADERS)
"""Derived from :data:`_ALLOWED_RESPONSE_HEADERS` — the allowlist
itself is the actual bound; named here for #110's explicit "named
constant" requirement."""

MAX_HEADER_VALUE_CHARS = 500
"""Cap on each individual allowed header's value length."""

MAX_LOCATION_CHARS = 2_000
"""Cap on the ``redirect_location`` field's length — a `Location`
header is attacker/server-controlled text and could otherwise be
arbitrarily long."""


class HTTPProbeError(IntegrationError):
    """Raised for an HTTP transport-level failure: DNS/connect/TLS/
    timeout/malformed-response. Never raised merely because a response
    was received with a non-2xx status — every status code (200-599)
    that a server actually returns is normal, successful evidence
    retrieval (see :func:`probe_http`'s docstring and #110's "HTTP
    result semantics")."""

    def __init__(self, message: str, *, kind: IntegrationErrorKind) -> None:
        super().__init__(message, kind=kind, source_system=SOURCE_SYSTEM)


class HTTPPathValidationError(ValueError):
    """Raised by :func:`validate_http_path` for a ``path`` that is not
    a plain, bounded, origin-relative path — a caller/model mistake,
    not an HTTP observation."""


class HTTPMethodValidationError(ValueError):
    """Raised by :func:`validate_http_method` for a method outside
    :data:`SUPPORTED_HTTP_METHODS`."""


def _has_dot_segment(path: str) -> bool:
    """True if any ``/``-separated segment of ``path`` is a literal
    ``.``/``..`` traversal segment. Only meaningful for *literal* dots —
    :func:`validate_http_path` rejects every ``%`` character outright
    (see its docstring), so this never needs to reason about
    percent-decoding at all."""
    return any(segment in (".", "..") for segment in path.split("/"))


def validate_http_method(method: object) -> str:
    """Validate ``method`` is ``GET`` or ``HEAD`` (case-insensitive on
    input, normalized to uppercase). Anything else — POST, PUT, PATCH,
    DELETE, CONNECT, OPTIONS, TRACE, or garbage — is rejected; this
    alone is what keeps this tool read-only and body-free."""
    if not isinstance(method, str):
        raise HTTPMethodValidationError(f"method must be a string, got {type(method).__name__}")
    normalized = method.strip().upper()
    if normalized not in SUPPORTED_HTTP_METHODS:
        raise HTTPMethodValidationError(f"method must be one of {SUPPORTED_HTTP_METHODS}, got {method!r}")
    return normalized


def validate_http_path(path: object) -> str:
    """Validate ``path`` is a plain, bounded, origin-relative HTTP
    path.

    Rejects (deliberately, mirroring
    ``mantis.integrations.network.validate_host``'s allowlist
    approach): non-string input, empty/oversized strings
    (:data:`MAX_PATH_CHARS`), anything not starting with a single
    ``/``, a leading ``//`` (protocol-relative/host-escape --
    ``//evil.example/`` would otherwise be interpreted by some HTTP
    stacks as switching host entirely), an embedded absolute URL
    (``://``), embedded credentials (``@``), a query string (``?`` --
    #110 deliberately rejects query strings in this first
    implementation rather than validating/bounding them), a fragment
    (``#`` — fragments are never sent, and rejecting one outright here
    is simpler than silently stripping it), whitespace/control
    characters (including CR/LF — a path cannot be used to smuggle
    extra header lines into the request), a backslash (some HTTP
    stacks/proxies treat ``\\`` as a path separator equivalent to
    ``/``), a literal ``.``/``..`` path-traversal segment (see
    :func:`_has_dot_segment`), and **any** ``%`` character at all.

    These last two checks are what keep the configured target's
    ``base_path`` genuinely immutable: without the dot-segment check, a
    caller-supplied ``path`` like ``"/../admin"`` would, once
    concatenated onto a configured ``base_path`` of ``"/grafana"`` and
    handed to ``httpx.URL``'s own path normalization, actually request
    ``/admin`` — escaping the origin's configured prefix even though
    ``base_path`` itself was never touched. Rejecting every ``%``
    outright — rather than trying to decode and recognize specific
    encoded forms (``%2e``, or an encoded path separator like ``%2f``/
    ``%5c`` that reassembles into ``..`` only *after* some downstream
    proxy or server decodes it, never at this layer) — closes the same
    escape for every encoding scheme a downstream component might
    apply, without this tool needing to model any of them. #110's
    "path" was never meant to carry percent-encoding in the first
    place (it is a plain, already-decoded logical path, not a raw URL
    component), so this has no legitimate use case to preserve.
    """
    if not isinstance(path, str):
        raise HTTPPathValidationError(f"path must be a string, got {type(path).__name__}")
    if not path:
        raise HTTPPathValidationError("path must not be empty")
    if len(path) > MAX_PATH_CHARS:
        raise HTTPPathValidationError(f"path must be at most {MAX_PATH_CHARS} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise HTTPPathValidationError("path must not contain whitespace or control characters")
    if any(ch.isspace() for ch in path):
        raise HTTPPathValidationError("path must not contain whitespace")
    if not path.startswith("/"):
        raise HTTPPathValidationError("path must start with '/'")
    if path.startswith("//"):
        raise HTTPPathValidationError("path must not start with '//' (protocol-relative host escape)")
    if "://" in path:
        raise HTTPPathValidationError("path must not contain an absolute URL")
    if "@" in path:
        raise HTTPPathValidationError("path must not contain embedded credentials")
    if "?" in path:
        raise HTTPPathValidationError("path must not contain a query string")
    if "#" in path:
        raise HTTPPathValidationError("path must not contain a fragment")
    if "\\" in path:
        raise HTTPPathValidationError("path must not contain a backslash")
    if _has_dot_segment(path):
        raise HTTPPathValidationError("path must not contain a '.' or '..' path-traversal segment")
    if "%" in path:
        raise HTTPPathValidationError(
            "path must not contain '%' -- percent-encoding of any kind is rejected outright, "
            "since a downstream proxy/server decoding it (e.g. %2f/%5c reassembling into a "
            "path separator, or %2e into a dot) could otherwise reconstruct a path-traversal "
            "segment this layer never sees literally"
        )
    return path


def _join_path(base_path: str, path: str) -> str:
    """Combine the configured, immutable ``base_path`` with a
    validated, caller-supplied ``path``. ``base_path`` never ends in
    ``/`` (normalized at config-parse time — see
    ``mantis.config._parse_http_target``); ``path`` always starts with
    exactly one ``/`` (enforced by :func:`validate_http_path`) — so
    plain concatenation can never produce a double slash or drop the
    base path, and never re-parses the result through a URL join that
    could reinterpret a leading ``/`` as origin-absolute."""
    return f"{base_path}{path}"


def _bounded_header_value(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_HEADER_VALUE_CHARS:
        return value, False
    return value[:MAX_HEADER_VALUE_CHARS], True


def _extract_headers(headers: "httpx.Headers") -> tuple[dict[str, str], bool]:
    """Copy only the explicitly allowlisted headers
    (:data:`_ALLOWED_RESPONSE_HEADERS`) — never all response headers,
    and never ``set-cookie``/``authorization``/``proxy-authorization``/
    ``cookie``, which are not in the allowlist by construction."""
    result: dict[str, str] = {}
    truncated = False
    for name in _ALLOWED_RESPONSE_HEADERS:
        value = headers.get(name)
        if value is None:
            continue
        bounded, was_truncated = _bounded_header_value(value)
        result[name] = bounded
        truncated = truncated or was_truncated
    return result, truncated


def _extract_location(headers: "httpx.Headers") -> tuple[str | None, bool]:
    value = headers.get("location")
    if value is None:
        return None, False
    if len(value) <= MAX_LOCATION_CHARS:
        return value, False
    return value[:MAX_LOCATION_CHARS], True


def _decode_excerpt(raw: bytes) -> tuple[str, bool]:
    """Best-effort decode of raw response bytes into a bounded, safe
    text excerpt. Never raises on non-UTF-8/binary content — malformed
    sequences are replaced (``errors="replace"``), which is exactly the
    honest behavior for arbitrary response bodies (attacker/server
    controlled, #14's untrusted-evidence path handles the rest)."""
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= MAX_BODY_EXCERPT_CHARS:
        return text, False
    return text[:MAX_BODY_EXCERPT_CHARS], True


def _read_bounded_body(response: "httpx.Response", *, deadline: Deadline | None) -> tuple[bytes, bool]:
    """Stream the response body, stopping at :data:`MAX_BODY_BYTES_READ`
    or the deadline, whichever comes first — the network acquisition
    itself is bounded, never "read everything, then truncate." Returns
    ``(raw_bytes_read, truncated)``. The caller is responsible for
    using ``response`` inside its own ``client.stream(...)`` context
    manager, which closes the underlying connection on exit even if the
    body was never fully consumed.
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in response.iter_bytes():
        if deadline is not None and deadline.expired():
            truncated = True
            break
        remaining = MAX_BODY_BYTES_READ - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


@dataclass(frozen=True)
class HTTPProbeResult:
    """The full outcome of one :func:`probe_http` call."""

    scheme: str
    host: str
    port: int
    method: str
    path: str
    status_code: int
    reason: str
    latency_ms: float
    headers: dict[str, str] = field(default_factory=dict)
    body_excerpt: str | None = None
    body_bytes_observed: int = 0
    redirect_location: str | None = None
    truncated: bool = False
    observed_at: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_client(*, verify_ssl: bool, connect_timeout: float, read_timeout: float) -> "httpx.Client":
    """The one place an ``httpx.Client`` is constructed for a probe —
    deliberately different from every other Mantis HTTP integration
    (AWX/Prometheus/Loki) in two ways, both required by #110:

    - ``trust_env=False``: never silently honor ``HTTP_PROXY``/
      ``HTTPS_PROXY``/``ALL_PROXY`` or any other environment proxy
      configuration. A current-state network observation must be
      deterministic — routing it through whatever proxy happens to be
      set in the process environment would make "can Mantis reach this
      origin" actually mean "can Mantis reach this origin *through
      whatever proxy is configured right now*," a materially different
      and non-obvious question.
    - ``follow_redirects=False`` (httpx's own default, set explicitly
      here for clarity): a 3xx response is returned as evidence, never
      chased automatically — see ``mantis.tools.http``'s "Redirects"
      section.
    """
    return httpx.Client(
        verify=verify_ssl,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout),
    )


def probe_http(
    *,
    scheme: str,
    host: str,
    port: int,
    base_path: str,
    verify_ssl: bool,
    path: str,
    method: str,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    deadline: Deadline | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> HTTPProbeResult:
    """Send one bounded ``method`` request to
    ``scheme://host:port{base_path}{path}`` and return the response as
    structured evidence.

    Callers must validate ``path``/``method`` first (see
    :func:`validate_http_path`/:func:`validate_http_method`) and
    resolve a target *alias* to these concrete values themselves (see
    ``mantis.tools.http``) — this function trusts its arguments, knows
    nothing about aliases/config, and never shells out.

    **A received HTTP response is successful evidence retrieval,
    regardless of status code.** 200, 204, 301, 401, 403, 404, 429,
    500, and 503 are all returned identically as a normal
    :class:`HTTPProbeResult` — never raised as an :class:`HTTPProbeError`
    merely for being non-2xx. Only a *transport*-level failure (DNS/
    connect/TLS/timeout/malformed response — anything that happens
    *before* a response is received at all, or while it cannot be
    parsed as one) raises :class:`HTTPProbeError`, classified via the
    same :func:`~mantis.reliability.classify_httpx_exception` every
    other Mantis HTTP integration uses.

    **No automatic retry**: this is a current-state observation, not an
    idempotent API read — see this module's docstring. **No automatic
    redirect following**: a 3xx response's ``Location`` header (if
    present, bounded) is returned as ``redirect_location``; the
    redirect itself is never chased.

    ``connect_timeout_seconds``/``read_timeout_seconds`` are further
    capped by ``deadline.remaining()`` when given, exactly like every
    other Mantis integration's deadline handling — and the same
    deadline also bounds the streamed body read (see
    :func:`_read_bounded_body`), so a slow-but-still-sending response
    can never itself exceed the caller's remaining budget.

    For ``method="HEAD"``, no body is read at all — ``body_excerpt`` is
    ``None`` and ``body_bytes_observed`` is ``0``, never an attempted
    (and necessarily empty) read.
    """
    observed_at = _utc_now_iso()
    connect_timeout = connect_timeout_seconds
    read_timeout = read_timeout_seconds
    if deadline is not None:
        remaining = deadline.remaining()
        if remaining <= 0:
            raise HTTPProbeError(
                "remaining budget exhausted before the request could start", kind=IntegrationErrorKind.TIMEOUT
            )
        connect_timeout = min(connect_timeout, remaining)
        read_timeout = min(read_timeout, remaining)

    full_path = _join_path(base_path, path)
    # Never hand-format the authority: an IPv6 literal host (e.g.
    # "::1") requires bracket syntax ("http://[::1]:8080/") that plain
    # f-string interpolation does not produce, which httpx then rejects
    # outright as an invalid URL. httpx.URL's constructor brackets an
    # IPv6 host automatically.
    url = httpx.URL(scheme=scheme, host=host, port=port, path=full_path)

    start = clock()
    try:
        with _build_client(verify_ssl=verify_ssl, connect_timeout=connect_timeout, read_timeout=read_timeout) as client:
            with client.stream(method, url) as response:
                if method == "HEAD":
                    body_bytes, body_truncated = b"", False
                else:
                    body_bytes, body_truncated = _read_bounded_body(response, deadline=deadline)
                latency_ms = (clock() - start) * 1000.0
                headers, headers_truncated = _extract_headers(response.headers)
                redirect_location, location_truncated = _extract_location(response.headers)
                if method == "HEAD":
                    body_excerpt, excerpt_truncated = None, False
                else:
                    body_excerpt, excerpt_truncated = _decode_excerpt(body_bytes)
                return HTTPProbeResult(
                    scheme=scheme,
                    host=host,
                    port=port,
                    method=method,
                    path=path,
                    status_code=response.status_code,
                    reason=response.reason_phrase,
                    latency_ms=round(latency_ms, 3),
                    headers=headers,
                    body_excerpt=body_excerpt,
                    body_bytes_observed=len(body_bytes),
                    redirect_location=redirect_location,
                    truncated=body_truncated or headers_truncated or location_truncated or excerpt_truncated,
                    observed_at=observed_at,
                )
    except HTTPProbeError:
        raise
    except httpx.HTTPError as exc:
        kind = classify_httpx_exception(exc)
        raise HTTPProbeError(f"HTTP request failed: {type(exc).__name__}: {exc}", kind=kind) from exc

"""Semantic network tools exposed to agents.

Provides ``check_tcp_connectivity(host, port)``: Mantis's first
current-state network evidence tool (#8), answering one narrow
question — can Mantis establish a TCP connection to this host/port
right now — as a structured, provenance-tagged observation. See
``docs/network-tcp-connectivity.md`` for the full design (status
vocabulary, multi-address semantics, deadline handling, and how this
combines with #28's historical AWX evidence).

Adopts the shared result contract from ``mantis.contracts`` (``meta`` /
``QueryMeta``), the same pattern ``mantis.tools.awx`` established.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mantis.contracts import QueryMeta
from mantis.integrations.network import (
    AddressAttempt,
    HostValidationError,
    PortValidationError,
    TCPConnectResult,
    check_tcp_connect,
    validate_host,
    validate_port,
)
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline

logger = logging.getLogger(__name__)

# "status" is Mantis's own normalized summary derived from the raw
# per-address attempt evidence (see mantis.integrations.network's
# failure-precedence rule) -- every other top-level field is either the
# original request (target) or a direct pass-through of one successful
# attempt's own data. See mantis.contracts.QueryMeta.derived_fields.
DERIVED_RESULT_FIELDS = ("status",)


def _attempt_to_dict(attempt: AddressAttempt) -> dict[str, Any]:
    return attempt.to_dict()


def check_tcp_connectivity(
    host: Any,
    port: Any,
    *,
    _deadline: Deadline | None = None,
    _connect_fn: Callable[..., TCPConnectResult] = check_tcp_connect,
) -> dict[str, Any]:
    """Check whether Mantis can establish a TCP connection to
    ``host``:``port`` right now.

    This is a **current-state** observation from Mantis's own network
    vantage point at query time — it proves nothing about *why* a
    connection succeeded or failed, does not prove the application
    behind the port is healthy, and says nothing about historical
    behavior (compare with #28's ``awx_get_job_failure``, which reports
    *historical* AWX-observed evidence — the two are deliberately
    different kinds of evidence, never conflate them). See
    ``docs/network-tcp-connectivity.md``'s status-vocabulary table for
    exactly what each ``status`` value does and does not prove.

    Args:
        host: Hostname or IP literal (IPv4 or IPv6). Rejected (see
            ``mantis.integrations.network.validate_host``): URLs/schemes,
            paths, embedded credentials, whitespace/control characters,
            or anything else that isn't a plain hostname or IP literal.
            Private/internal addresses are always allowed — Mantis is
            built to troubleshoot private infrastructure; see
            ``docs/network-tcp-connectivity.md``'s SSRF-posture section.
        port: TCP port, an integer in ``[1, 65535]``.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime`` — see ``mantis.reliability.Deadline`` and
            ``docs/reliability.md``. Same keyword-only/underscore
            convention as the AWX tools' ``_deadline``. There is
            deliberately no model-facing timeout argument (see
            ``docs/network-tcp-connectivity.md``) — every socket timeout
            is derived from this deadline (or a fixed, documented
            default when it's ``None``, e.g. a direct/manual call).
        _connect_fn: Test/evaluation-only override for
            ``mantis.integrations.network.check_tcp_connect`` — same
            keyword-only/underscore convention and purpose as the AWX
            tools' ``_client``. Used by ``mantis.eval.fixtures.network``
            to run this exact production code path against canned
            :class:`~mantis.integrations.network.TCPConnectResult` data
            instead of a real socket.

    Returns:
        A dict with ``meta`` (provenance — ``source_system="network"``,
        a point-in-time ``observation_time`` since this is a single
        current-state observation, and ``truncated`` when more resolved
        addresses existed than were attempted), ``target`` (the
        requested host/port), ``status`` (the normalized vocabulary —
        see the module docstring), ``connected`` (bool), the successful
        attempt's ``resolved_address``/``address_family``/``latency_ms``
        when ``connected`` is true (``None`` otherwise), and ``attempts``
        (bounded, per-address evidence — see
        ``mantis.integrations.network.AddressAttempt``).

        If ``host``/``port`` fail validation, ``status`` is
        ``"invalid_input"`` — a distinct value from the eight
        network-observation statuses (see
        ``mantis.integrations.network.ConnectStatus``): this represents
        a caller/model mistake, not a network outcome, and no network
        activity was attempted (``attempts`` is empty). This is returned
        as a normal result rather than raised, deliberately: the invalid
        host text is untrusted, model-supplied data (#14) and must flow
        through the standard model-input safety pipeline
        (``mantis.security.make_model_safe()``) like any other tool
        result, not through the runtime's generic last-resort exception
        path, which does not apply that pipeline.
    """
    try:
        safe_host = validate_host(host)
        safe_port = validate_port(port)
    except (HostValidationError, PortValidationError) as exc:
        logger.info("check_tcp_connectivity rejected invalid input: %s", exc)
        meta = QueryMeta(source_system="network", derived_fields=list(DERIVED_RESULT_FIELDS))
        return {
            "meta": meta.to_dict(),
            "target": {"host": host, "port": port},
            "status": "invalid_input",
            "connected": False,
            "resolved_address": None,
            "address_family": None,
            "latency_ms": None,
            "attempts": [],
            "message": str(exc),
        }

    result = _connect_fn(safe_host, safe_port, deadline=_deadline)

    meta = QueryMeta(
        source_system="network",
        query_time=result.observed_at,
        observation_time=result.observed_at,
        truncated=result.truncated,
        derived_fields=list(DERIVED_RESULT_FIELDS),
    )

    return {
        "meta": meta.to_dict(),
        "target": {"host": result.host, "port": result.port},
        "status": result.status,
        "connected": result.connected,
        "resolved_address": result.resolved_address,
        "address_family": result.address_family,
        "latency_ms": result.latency_ms,
        "attempts": [_attempt_to_dict(attempt) for attempt in result.attempts],
    }


CHECK_TCP_CONNECTIVITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "check_tcp_connectivity",
        "description": (
            "Check whether Mantis can establish a TCP connection to a "
            "specific host and port right now. This is a current-state "
            "observation from Mantis's own network vantage point at "
            "query time -- it does not explain WHY a connection "
            "succeeded or failed, does not prove the application "
            "behind the port is healthy, and says nothing about past "
            "behavior. TCP-only: no ping/ICMP, no UDP, no port "
            "scanning, no HTTP/TLS probing. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": (
                        "Hostname or IP literal (IPv4 or IPv6) to connect "
                        "to. No URLs, paths, or schemes."
                    ),
                },
                "port": {
                    "type": "integer",
                    "description": "TCP port to connect to (1-65535).",
                    "minimum": 1,
                    "maximum": 65535,
                },
            },
            "required": ["host", "port"],
        },
    },
}


default_registry.register(
    Tool(
        name="check_tcp_connectivity",
        schema=CHECK_TCP_CONNECTIVITY_SCHEMA,
        handler=check_tcp_connectivity,
        category="network",
        mutating=False,
        # Resolver/OS diagnostic text (attempt messages, the host the
        # model supplied) is external, Mantis-uncontrolled data -- same
        # untrusted-output treatment as the AWX tools. See
        # mantis.security and docs/security.md.
        contains_untrusted_text=True,
        description=(
            "Check current-state TCP connectivity to a host/port from "
            "Mantis's own network vantage point."
        ),
    )
)

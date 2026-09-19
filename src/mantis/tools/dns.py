"""Semantic DNS tools exposed to agents (#109).

Provides ``dns_lookup(name, record_type="A", resolver_alias="internal")``:
a bounded, read-only DNS query against exactly one server-side-configured
resolver *profile*, answering "what does this specific resolver
perspective report for this name?" — deliberately not "what is the
global truth about this name" (there isn't one; see
``docs/dns-lookup.md``'s split-horizon discussion). This is Mantis's
first DNS evidence tool, distinct from #8's current-state TCP evidence,
#9's time-series Prometheus evidence, #10's log evidence, and #18's
Kubernetes cluster evidence.

All input validation (record type, query name, resolver alias) happens
here, before any query is attempted (see ``mantis.integrations.dns`` for
the query/failover mechanics this gates) — the same reasoning
``mantis.tools.network``/``.prometheus``/``.loki`` document: invalid
input is untrusted, model-supplied data (#14) and must flow through
``mantis.security.make_model_safe()`` like any other tool result, never
through the runtime's generic last-resort exception path.

Adopts the shared result contract from ``mantis.contracts`` (``meta`` /
``QueryMeta``), the same pattern every other Mantis tool established.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import dns.reversename

from mantis.config import DNSConfig
from mantis.contracts import QueryMeta
from mantis.integrations.dns import (
    DNSAnswer,
    DNSLookupResult,
    DNSNameValidationError,
    DNSServerAttempt,
    PTRTargetValidationError,
    RecordTypeValidationError,
    SUPPORTED_RECORD_TYPES,
    resolve_dns,
    validate_dns_name,
    validate_ptr_target,
    validate_record_type,
)
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline

logger = logging.getLogger(__name__)

# "status" is Mantis's own normalized summary derived from the raw
# per-server attempt evidence (see mantis.integrations.dns's failover
# rule) -- every other top-level field is either the original request
# or a direct pass-through of the resolver's own reported data. See
# mantis.contracts.QueryMeta.derived_fields.
DERIVED_RESULT_FIELDS = ("status",)


def _get_dns_config() -> DNSConfig:
    return DNSConfig.from_env()


def _answer_to_dict(answer: DNSAnswer) -> dict[str, Any]:
    return answer.to_dict()


def _attempt_to_dict(attempt: DNSServerAttempt) -> dict[str, Any]:
    return attempt.to_dict()


def _invalid_input_result(
    name: Any, record_type: Any, resolver_alias: Any, message: str
) -> dict[str, Any]:
    logger.info("dns_lookup rejected invalid input: %s", message)
    meta = QueryMeta(source_system="dns", derived_fields=list(DERIVED_RESULT_FIELDS))
    return {
        "meta": meta.to_dict(),
        "name": name,
        "record_type": record_type,
        "resolver_alias": resolver_alias,
        "resolver_addresses": [],
        "responding_resolver": None,
        "status": "invalid_input",
        "answers": [],
        "server_attempts": [],
        "message": message,
    }


def dns_lookup(
    name: Any,
    record_type: Any = "A",
    resolver_alias: Any = "internal",
    *,
    _deadline: Deadline | None = None,
    _config: DNSConfig | None = None,
    _resolve_fn: Callable[..., DNSLookupResult] = resolve_dns,
) -> dict[str, Any]:
    """Look up a DNS record from one server-side-configured resolver
    profile's perspective.

    This deliberately answers "what does the *resolver_alias*
    perspective report for this name right now" — never "what is THE
    answer for this name." The same name can legitimately resolve
    differently (or not at all) from different resolver perspectives
    (split-horizon DNS) — see ``docs/dns-lookup.md``. One call queries
    exactly one configured profile; it never fans out to every
    configured resolver to compare answers.

    Args:
        name: Domain name to query for ``A``/``AAAA``/``CNAME``. For
            ``PTR``, the IPv4/IPv6 address to reverse-look-up instead
            (see :func:`mantis.integrations.dns.validate_ptr_target`).
            Always echoed back exactly as given, even on rejection —
            never transformed into the internal ``*.in-addr.arpa``/
            ``*.ip6.arpa`` reverse-lookup name a ``PTR`` query actually
            sends on the wire, which is implementation detail, not
            evidence.
        record_type: One of :data:`~mantis.integrations.dns.SUPPORTED_RECORD_TYPES`
            (``A``, ``AAAA``, ``CNAME``, ``PTR``). Case-insensitive.
            Anything else (``TXT``, ``MX``, ``SRV``, ``NS``, ``SOA``,
            ``ANY``, ...) is rejected — see #109's non-goals.
        resolver_alias: The *name* of a server-side-configured resolver
            profile (e.g. ``"internal"``) — see ``mantis.config.DNSConfig``.
            This is the **only** resolver-selecting input a caller may
            supply: never a resolver IP/hostname/port, and never an
            arbitrary DNS endpoint. An alias with no matching configured
            profile is rejected as invalid input *before* any query is
            attempted — no network access happens for an unknown alias.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime`` — see ``mantis.reliability.Deadline`` and
            ``docs/reliability.md``. Same keyword-only/underscore
            convention as every other Mantis tool. There is deliberately
            no model-facing timeout argument; every query timeout is
            derived from this deadline (or a fixed, documented default
            when it's ``None``).
        _config: Test/evaluation-only :class:`~mantis.config.DNSConfig`
            override — same keyword-only/underscore convention as the
            AWX/Prometheus/Loki/Kubernetes tools' ``_client``.
        _resolve_fn: Test/evaluation-only override for
            :func:`mantis.integrations.dns.resolve_dns` — same
            convention as ``mantis.tools.network``'s ``_connect_fn``.

    Returns:
        A dict with ``meta`` (provenance — ``source_system="dns"``, a
        point-in-time ``observation_time`` since this is a single
        current-state observation, and ``truncated`` when more servers/
        answers/CNAME hops existed than were attempted/returned),
        ``name``/``record_type``/``resolver_alias`` (the request, echoed
        back), ``resolver_addresses`` (the profile's *configured*
        servers — safe, Mantis-controlled provenance, never a secret),
        ``responding_resolver`` (which configured server actually
        produced the returned ``status`` — ``None`` only when no server
        could be reached at all, see below), ``status`` (``ok`` /
        ``nxdomain`` / ``no_data`` / ``servfail`` / ``refused`` /
        ``budget_exceeded`` / ``invalid_input`` — never ``timeout`` or a
        transport failure, which propagate as a classified
        :class:`~mantis.integrations.dns.DNSError` instead, handled by
        ``AgentRuntime`` generically like any other integration failure
        — see ``docs/dns-lookup.md``'s status-vocabulary table), and
        ``answers`` (bounded ``{"value", "ttl", "type"}`` records — the
        resolver's own data, never Mantis interpretation).

        ``server_attempts`` additionally preserves bounded, structured
        per-server evidence (mirroring
        ``mantis.tools.network``'s ``attempts``) — which servers were
        tried, in what order, and each one's own outcome — even though
        ``status``/``responding_resolver`` alone already answer the
        common case.

        If ``name``/``record_type``/``resolver_alias`` fail validation,
        ``status`` is ``"invalid_input"`` and no query was attempted
        (``resolver_addresses``/``answers``/``server_attempts`` are all
        empty) — returned as a normal result, not raised, since the
        rejected text is untrusted, model-supplied data (#14).
    """
    try:
        safe_record_type = validate_record_type(record_type)
    except RecordTypeValidationError as exc:
        return _invalid_input_result(name, record_type, resolver_alias, str(exc))

    try:
        if safe_record_type == "PTR":
            safe_name = validate_ptr_target(name)
            query_name = str(dns.reversename.from_address(safe_name))
        else:
            safe_name = validate_dns_name(name)
            query_name = safe_name
    except (DNSNameValidationError, PTRTargetValidationError) as exc:
        return _invalid_input_result(name, record_type, resolver_alias, str(exc))

    config = _config or _get_dns_config()
    servers = config.resolve_profile(resolver_alias)
    if servers is None:
        return _invalid_input_result(
            name, record_type, resolver_alias, f"Unknown resolver alias: {resolver_alias!r}"
        )

    result = _resolve_fn(query_name, safe_record_type, servers, deadline=_deadline)

    meta = QueryMeta(
        source_system="dns",
        query_time=result.observed_at,
        observation_time=result.observed_at,
        truncated=result.truncated,
        derived_fields=list(DERIVED_RESULT_FIELDS),
    )

    return {
        "meta": meta.to_dict(),
        "name": safe_name,
        "record_type": safe_record_type,
        "resolver_alias": resolver_alias,
        "resolver_addresses": list(servers),
        "responding_resolver": result.responding_resolver,
        "status": result.status,
        "answers": [_answer_to_dict(answer) for answer in result.answers],
        "server_attempts": [_attempt_to_dict(attempt) for attempt in result.attempts],
    }


DNS_LOOKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dns_lookup",
        "description": (
            "Look up a DNS record from one specific, server-side-configured "
            "resolver perspective (resolver_alias) -- e.g. an \"internal\" "
            "view vs. a configured public resolver profile. Useful for "
            "split-horizon diagnostics: the same name can legitimately "
            "resolve differently (or not at all) from different resolver "
            "perspectives -- this never proves one perspective is more "
            "'correct' than another. Supports A, AAAA, CNAME, and PTR "
            "records only. You may only select a resolver by its "
            "configured alias name -- you cannot supply a resolver IP, "
            "hostname, port, or any other DNS endpoint. Read-only; "
            "queries exactly one resolver profile per call, never every "
            "configured profile at once."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Domain name to query for A/AAAA/CNAME. For a PTR "
                        "query, the IPv4/IPv6 address to reverse-look-up "
                        "instead."
                    ),
                },
                "record_type": {
                    "type": "string",
                    "description": "DNS record type to query. Defaults to A.",
                    "enum": list(SUPPORTED_RECORD_TYPES),
                },
                "resolver_alias": {
                    "type": "string",
                    "description": (
                        "Name of a server-side-configured resolver profile "
                        "(e.g. \"internal\"). Never a resolver IP, hostname, "
                        "or port -- unknown aliases are rejected. Defaults "
                        "to \"internal\"."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}


default_registry.register(
    Tool(
        name="dns_lookup",
        schema=DNS_LOOKUP_SCHEMA,
        handler=dns_lookup,
        category="dns",
        mutating=False,
        # Resolver-reported answer values (a CNAME/PTR target, a raw
        # attempt diagnostic message) are external, Mantis-uncontrolled
        # data -- same untrusted-output treatment as every other
        # evidence tool. See mantis.security and docs/security.md.
        contains_untrusted_text=True,
        description=(
            "Look up a DNS record (A/AAAA/CNAME/PTR) from one "
            "server-side-configured resolver profile's perspective."
        ),
    )
)

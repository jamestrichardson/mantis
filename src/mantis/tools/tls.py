"""Semantic TLS certificate inspection tool exposed to agents (#111).

Provides ``tls_certificate_inspect(target_alias)``: inspects the TLS
certificate presented at one server-side-configured direct TLS
endpoint, independent of whether it would verify — see
``docs/tls-certificate-inspection.md`` for the full design (the
"inspect != verify" requirement, the two-handshake mechanism, and why
chain trust/hostname match/time validity stay independent dimensions
rather than one collapsed boolean).

All input validation (target alias) happens here, before any handshake
is attempted (see ``mantis.integrations.tls`` for the handshake/
certificate-parsing mechanics this gates) — the same reasoning
``mantis.tools.network``/``.dns``/``.http`` document: invalid input is
untrusted, model-supplied data (#14) and must flow through
``mantis.security.make_model_safe()`` like any other tool result, never
through the runtime's generic last-resort exception path.

Adopts the shared result contract from ``mantis.contracts`` (``meta`` /
``QueryMeta``), the same pattern every other Mantis tool established.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mantis.config import TLSProfilesConfig
from mantis.contracts import QueryMeta
from mantis.integrations.tls import TLSInspectionResult, inspect_tls
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline

logger = logging.getLogger(__name__)

# Every field below is either the original request, the target's own
# configuration (host/port/server_name), or a direct pass-through of
# what the peer actually presented -- "status" is the one Mantis-
# derived summary. See mantis.contracts.QueryMeta.derived_fields.
DERIVED_RESULT_FIELDS: tuple[str, ...] = ("verification.status",)


def _get_tls_config() -> TLSProfilesConfig:
    return TLSProfilesConfig.from_env()


def _invalid_input_result(target_alias: Any, message: str) -> dict[str, Any]:
    # Deliberately never interpolates `message`/target_alias into the
    # log line -- see mantis.tools.dns/.http's identical fix (PR #117
    # review): a validator's exception text is designed for the
    # *returned* "message" field, which goes through
    # mantis.security.make_model_safe() like any other tool result, not
    # for a raw logger.info(..., message) call that would write it
    # straight to container stdout/Loki unredacted.
    logger.info("tls_certificate_inspect rejected invalid input")
    meta = QueryMeta(source_system="tls", derived_fields=[])
    return {
        "meta": meta.to_dict(),
        "target_alias": target_alias,
        "host": None,
        "port": None,
        "server_name": None,
        "connected_address": None,
        "tls_version": None,
        "cipher": None,
        "certificate": None,
        "verification": None,
        "error": {"type": "invalid_input", "message": message},
    }


def tls_certificate_inspect(
    target_alias: Any,
    *,
    _deadline: Deadline | None = None,
    _config: TLSProfilesConfig | None = None,
    _inspect_fn: Callable[..., TLSInspectionResult] = inspect_tls,
) -> dict[str, Any]:
    """Inspect the TLS certificate presented at one server-side-
    configured direct TLS endpoint.

    This deliberately reports what certificate *was presented*, even
    when it wouldn't pass verification — a self-signed, untrusted-
    issuer, expired, not-yet-valid, or hostname-mismatched certificate
    is still returned with full metadata, never collapsed into "no
    certificate information available." See ``docs/tls-certificate-inspection.md``.

    Args:
        target_alias: The *name* of a server-side-configured TLS target
            (e.g. ``"grafana"``) — see
            ``mantis.config.TLSProfilesConfig``. This is the **only**
            target-selecting input a caller may supply: never a host,
            port, SNI/server name, CA bundle, or verification mode
            directly. An alias with no matching configured target is
            rejected as invalid input *before* any handshake is
            attempted — no network access happens for an unknown
            alias.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime`` — see ``mantis.reliability.Deadline`` and
            ``docs/reliability.md``. Both handshakes (inspection and
            verification — see ``mantis.integrations.tls.inspect_tls``)
            draw from this same budget.
        _config: Test/evaluation-only :class:`~mantis.config.TLSProfilesConfig`
            override — same convention as every other Mantis tool's
            ``_client``/``_config``.
        _inspect_fn: Test/evaluation-only override for
            :func:`mantis.integrations.tls.inspect_tls` — same
            convention as ``mantis.tools.network``'s ``_connect_fn``.

    Returns:
        A dict with ``meta`` (provenance — ``source_system="tls"``),
        the request echoed back (``target_alias``), the resolved
        endpoint (``host``/``port``/``server_name`` — the target's
        *configured* values, never caller-supplied — and
        ``connected_address``, the specific resolved address that
        actually presented the certificate), ``tls_version``/``cipher``,
        ``certificate`` (bounded ``subject``/``issuer``/``serial_number``/
        ``sha256_fingerprint``/``not_before``/``not_after``/``san_dns``/
        ``san_ip`` — never full PEM/DER, never arbitrary extensions),
        and ``verification`` (``chain_trusted``/``hostname_matches``/
        ``time_valid`` as **independent** dimensions, plus a clearly-
        derived ``status`` summary — see
        ``mantis.integrations.tls.VerificationInfo``).

        If ``target_alias`` fails validation, the result carries
        ``"error": {"type": "invalid_input", "message": ...}`` and
        every other field is ``None`` — returned as a normal result,
        not raised, since the rejected text is untrusted, model-supplied
        data (#14). A successful inspection always has ``"error": None``.
    """
    config = _config or _get_tls_config()
    target = config.resolve_target(target_alias)
    if target is None:
        return _invalid_input_result(target_alias, f"Unknown TLS target alias: {target_alias!r}")

    result = _inspect_fn(
        target.host,
        target.port,
        target.server_name,
        ca_file=target.ca_file,
        deadline=_deadline,
    )

    meta = QueryMeta(
        source_system="tls",
        query_time=result.observed_at,
        observation_time=result.observed_at,
        truncated=result.truncated,
        derived_fields=list(DERIVED_RESULT_FIELDS),
    )

    return {
        "meta": meta.to_dict(),
        "target_alias": target_alias,
        "host": result.host,
        "port": result.port,
        "server_name": result.server_name,
        "connected_address": result.connected_address,
        "tls_version": result.tls_version,
        "cipher": result.cipher,
        "certificate": result.certificate.to_dict(),
        "verification": result.verification.to_dict(),
        "error": None,
    }


TLS_CERTIFICATE_INSPECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tls_certificate_inspect",
        "description": (
            "Inspect the TLS certificate presented at a "
            "server-side-configured direct TLS endpoint, independent "
            "of whether it would pass verification. Reports "
            "certificate metadata (subject, issuer, serial, SHA-256 "
            "fingerprint, validity dates, SANs) plus three "
            "*independent* verification dimensions: chain_trusted, "
            "hostname_matches, time_valid -- a self-signed, expired, "
            "or hostname-mismatched certificate still returns full "
            "metadata, never 'no certificate available'. You may only "
            "select a target by its configured alias -- you cannot "
            "supply a host, port, SNI, CA bundle, or verification mode "
            "directly. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_alias": {
                    "type": "string",
                    "description": (
                        "Name of a server-side-configured TLS target "
                        "(e.g. \"grafana\"). Never a host/port/SNI directly."
                    ),
                },
            },
            "required": ["target_alias"],
        },
    },
}


default_registry.register(
    Tool(
        name="tls_certificate_inspect",
        schema=TLS_CERTIFICATE_INSPECT_SCHEMA,
        handler=tls_certificate_inspect,
        category="tls",
        mutating=False,
        # Certificate subject/issuer/SAN entries are external,
        # presenter-controlled data -- same untrusted-output treatment
        # as every other evidence tool. See mantis.security and
        # docs/security.md.
        contains_untrusted_text=True,
        description=(
            "Inspect the TLS certificate presented at a server-side-configured "
            "direct TLS endpoint, independent of whether it verifies."
        ),
    )
)

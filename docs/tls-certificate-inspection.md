# TLS certificate inspection (`tls_certificate_inspect`)

Mantis's first TLS evidence tool (#111). The core question it answers
is deliberately narrow:

> What certificate did *this specific, server-side-configured direct TLS
> endpoint* actually present, right now — and how does it stand up
> against chain trust, hostname match, and validity period,
> **independently**?

Not:

> ~~Did this handshake verify: yes or no?~~

A single verified/unverified boolean is exactly the wrong shape for a
troubleshooting tool — see "Inspect != verify" below, which is the
entire reason this tool exists as a distinct capability from a plain
TLS handshake check.

## Layering

Following the existing Mantis architecture (see
[docs/architecture.md](architecture.md)):

```
src/mantis/config.py
    TLSProfilesConfig -- server-side TLS target profile configuration
    (alias -> host/port/server_name(SNI)/optional ca_file). Parsing
    only; no network access.

src/mantis/integrations/tls.py
    Handshake/parsing mechanics: address resolution, the two
    independent handshakes (inspection and verification), certificate
    parsing via `cryptography.x509`, hostname/time-validity
    computation, SAN bounding.

src/mantis/tools/tls.py
    Semantic result shaping, provenance (QueryMeta), target-alias
    resolution against TLSProfilesConfig, registry registration,
    untrusted-output handling.
```

No TLS mechanics live in an agent; no new reliability abstraction was
created — this reuses #15's `Deadline`/`IntegrationError` directly, the
same way `check_tcp_connectivity`/`dns_lookup`/`http_probe` do.

`TLSProfilesConfig` is deliberately a **separate** config class and
alias namespace from `HTTPProfilesConfig` (#110), not a shared "network
endpoint" abstraction: TLS inspection is meaningful for any direct TLS
endpoint (an HTTPS origin, but also e.g. a bare TLS-wrapped TCP service),
and forcing it to be HTTP-specific would be exactly the premature
generalization both tickets warn against.

## Target profiles

A caller (model or API client) selects a target only by its
**configured alias name** — `target_alias` is the only target-selecting
input `tls_certificate_inspect` accepts. It can never supply a host,
port, SNI/server name, CA bundle, or verification mode directly.

Configure one or more profiles via environment variables:

```bash
MANTIS_TLS_TARGET_GRAFANA_HOST=grafana.example.net
MANTIS_TLS_TARGET_RAWIP_HOST=172.30.10.20
MANTIS_TLS_TARGET_RAWIP_SERVER_NAME=grafana.internal
MANTIS_TLS_TARGET_INTERNAL_CA_FILE=/etc/mantis/internal-ca.pem
```

`<ALIAS>` (case-insensitive) becomes the `target_alias` a caller may
request. See [docs/configuration.md](configuration.md#tls) for the full
reference.

`TLSProfilesConfig.from_env()` performs **no network access**:

- The host must be a valid hostname or IP literal; the port must be in
  `1..65535` (default `443`).
- `server_name` (SNI) defaults to `host` **only when `host` is itself a
  hostname**. When `host` is an IP literal and no `server_name` is
  configured, `from_env()` raises `ConfigurationError` at startup — SNI
  is never guessed, because there's no hostname to default it from.
  This is a deliberate fail-at-startup design: an operator configuring
  an IP-literal target must make an explicit SNI decision, not
  discover a silently-wrong one at request time.
- `ca_file` (if configured) is stored as a path only — its existence on
  disk is never checked at config-parse time, only when a handshake
  actually runs (and never exposed in any tool result — see "Trust
  store" below).

An unrecognized `target_alias` is rejected as invalid input
(`error.type="invalid_input"`) *before* any handshake is attempted —
see "Input validation" below.

## Inspect != verify

**This is #111's central design requirement.** A verified TLS handshake
failing must never mean "no certificate information available" — that
would defeat the entire purpose of a troubleshooting tool. A
self-signed certificate, an untrusted issuer, an expired or
not-yet-valid certificate, or a hostname mismatch are all exactly the
*kinds of problems an operator wants this tool to describe* — not
conditions that make the tool go blind.

Why the naive approach fails: `ssl.SSLSocket.getpeercert()` (the dict
form) returns an **empty dict** whenever the handshake didn't verify —
per its own documented behavior. A tool built on that call alone would
report "no certificate" for exactly the cases where certificate detail
matters most.

`tls_certificate_inspect` avoids this by performing **two explicitly
bounded handshakes**, both against the same successfully-connected
address, both drawing from the same overall `Deadline` budget:

1. **Inspection handshake** (`ssl.SSLContext(PROTOCOL_TLS_CLIENT)` with
   `verify_mode=CERT_NONE`, `check_hostname=False`): completes and
   yields a certificate *regardless of whether it would verify*.
   `getpeercert(binary_form=True)` returns the raw DER bytes
   unconditionally (as long as a certificate was presented at all) —
   this is the call that actually makes the certificate retrievable.
2. **Verification handshake** (a separate `ssl.SSLContext` with
   `verify_mode=CERT_REQUIRED`, `check_hostname=False`): against the
   *same* address, determines chain trust **alone** — using the
   system/default trust store, or a configured `ca_file` — with
   hostname checking explicitly turned off so this handshake's
   pass/fail reflects chain trust only, never conflated with a
   hostname-match failure.

`hostname_matches` and `time_valid` are **not** derived from either
handshake's pass/fail at all — they're computed directly, in Python,
from the certificate already parsed in phase 1 (SAN entries vs.
`server_name`; not-before/not-after vs. the current time). This is what
keeps all three verification dimensions genuinely independent, rather
than all being downstream of one opaque OpenSSL verification error (see
"Verification dimensions" below).

## Certificate parsing

Certificate parsing uses the `cryptography` library
(`cryptography.x509.load_der_x509_certificate`), not any private or
undocumented stdlib helper, and never by shelling out to `openssl`
(there is no `subprocess` import anywhere in this tool's code — see
`tests/test_tls_tools.py::test_tool_never_imports_subprocess`, which
checks this structurally by inspecting the module's own imports).

**Why `cryptography`, not stdlib alone**: as explained above,
`ssl.SSLSocket.getpeercert()`'s dict form is empty for an unverified
handshake, which is exactly the case this tool needs to handle well.
`getpeercert(binary_form=True)` gives raw DER bytes; parsing DER into
structured fields (subject, issuer, serial, SANs, validity dates) with
correct ASN.1 handling is substantial, security-sensitive work that
`cryptography` already does correctly and is already a well-maintained,
widely-used dependency in the Python ecosystem — adding it here is a
narrow, well-justified addition (one module, `mantis.integrations.tls`,
uses it), not a general parsing framework.

## Verification dimensions

**Never collapsed into one boolean.** `VerificationInfo` carries three
independent fields, all visible in every result:

| Field | Meaning | Independent of |
|---|---|---|
| `chain_trusted` | Does the certificate chain to a trusted root (system/default store, or configured `ca_file`)? `None` only if the verification handshake couldn't be attempted at all (deadline exhausted after phase 1 succeeded) — an honest "not determined," never coerced to `True`/`False`. | Hostname, validity period. |
| `hostname_matches` | Does the certificate's SAN (DNS or IP) actually cover `server_name`? Computed via RFC 6125 leftmost-single-label wildcard matching (`*.example.com` matches `foo.example.com`, not `foo.bar.example.com`), hand-implemented rather than relying on the deprecated `ssl.match_hostname`. | Chain trust, validity period. |
| `time_valid` | Is "now" within `[not_before, not_after]`? | Chain trust, hostname. |

A clearly-**derived** `status` summary is also included
(`mantis.integrations.tls._derive_status`), but never as a replacement
for the three dimensions above — precedence is time validity first (the
most fundamental problem, independent of signer or name), then chain
trust, then hostname:

| `status` | When |
|---|---|
| `valid` | `time_valid=True`, `chain_trusted=True`, `hostname_matches=True`. |
| `hostname_mismatch` | `time_valid=True`, `chain_trusted=True`, `hostname_matches=False`. |
| `untrusted` | `time_valid=True`, `chain_trusted=False` (regardless of hostname match). |
| `expired_or_not_yet_valid` | `time_valid=False` (regardless of the other two). |
| `unknown` | `chain_trusted=None` (verification handshake not attempted — deadline exhausted after phase 1 succeeded). |

Concrete worked examples (all real, verified outcomes — see
`tests/test_tls.py`):

```text
self-signed certificate, correct name:
    chain_trusted=false, hostname_matches=true, time_valid=true -> status="untrusted"

certificate for a different hostname, trusted issuer:
    chain_trusted=true, hostname_matches=false, time_valid=true -> status="hostname_mismatch"

expired certificate, otherwise trusted and correctly named:
    chain_trusted=true, hostname_matches=true, time_valid=false -> status="expired_or_not_yet_valid"
```

## SNI and IP-literal targets

`server_name` (SNI) is always a **deterministic, config-time** decision
— never guessed at request time. A target configured with a hostname
naturally defaults `server_name` to that hostname. A target configured
with a bare IP literal has no hostname to default from, so
`TLSProfilesConfig.from_env()` **requires** an explicit
`MANTIS_TLS_TARGET_<ALIAS>_SERVER_NAME` and raises `ConfigurationError`
at startup if it's missing — this fails loudly at configuration time,
never silently at request time with a wrong or empty SNI. See "Target
profiles" above.

## Failure semantics

**Failures before a certificate is obtained are retrieval/handshake
failures** (reusing #15's `IntegrationError` taxonomy via
`mantis.integrations.tls.TLSError`), handled by `AgentRuntime`
generically, exactly like every other Mantis integration's transport
failures:

- DNS resolution failure for the configured host.
- Every candidate address's TCP connect failing.
- Every candidate address's *inspection* handshake failing (e.g. a
  non-TLS endpoint, a protocol mismatch) — before any certificate was
  presented at all.
- A handshake completing but the presented bytes not parsing as a
  well-formed X.509 certificate at all (a malformed-response-shaped
  failure, not "no certificate available").

**Once a certificate has been obtained (phase 1 succeeds), nothing
about that certificate's content is ever raised as an error** — an
untrusted issuer, an expired/not-yet-valid period, and a hostname
mismatch are all verification *evidence*, returned as a normal result
via the independent `VerificationInfo` dimensions above, never
exceptions. If the *verification* handshake (phase 2) itself fails at
the transport level (e.g. the address became unreachable between
phases), `chain_trusted` becomes `None` rather than discarding the
certificate metadata already obtained in phase 1.

## Address handling

Address resolution and selection mirror `check_tcp_connectivity`'s
bounded, deterministic multi-address philosophy (reimplemented locally
in `mantis.integrations.tls`, not imported — matching the convention
that no integration cross-imports another integration's private
internals):

- Resolve `host`/`port`, deduplicate `(family, sockaddr)` pairs, cap at
  `MAX_ADDRESSES_ATTEMPTED` (**4**).
- Try candidates strictly in order; stop at the first address where the
  *inspection* handshake actually succeeds (a certificate was obtained)
  — never tried further to compare across addresses.
- The verification handshake (phase 2) is always performed against
  that **same** address that answered phase 1.
- `connected_address` in the result names the specific resolved address
  that actually presented the certificate.

## Trust store

Chain verification (phase 2) uses the **system/default trust store**
(`ssl.SSLContext.load_default_certs()`) unless a target is configured
with `ca_file`, in which case that CA is trusted instead
(`load_verify_locations(cafile=...)`). A model input can **never**
select `verify=false`, a CA file, a trust store, or a client
certificate — `ca_file` is deployment-config-only, set by an operator
in `TLSProfilesConfig`, and its path is **never exposed in any tool
result** (see `tests/test_tls_tools.py::test_ca_file_path_never_appears_in_any_result`).

## Certificate fields

`certificate` in a result carries bounded, structured metadata — never
full PEM/DER, and never a dump of arbitrary certificate extensions
beyond what's listed here:

| Field | Source |
|---|---|
| `subject` | `cert.subject.rfc4514_string()`, bounded to `MAX_SUBJECT_CHARS` (500). |
| `issuer` | `cert.issuer.rfc4514_string()`, bounded to `MAX_ISSUER_CHARS` (500). |
| `serial_number` | `str(cert.serial_number)`. |
| `sha256_fingerprint` | `cert.fingerprint(hashes.SHA256()).hex()`. |
| `not_before` / `not_after` | `cert.not_valid_before_utc`/`not_valid_after_utc`, ISO 8601. |
| `san_dns` | DNS-typed Subject Alternative Name entries, each bounded to `MAX_SAN_STRING_CHARS` (253, RFC 1035's own domain-name limit), capped at `MAX_SAN_ENTRIES` (25) entries. |
| `san_ip` | IP-typed SAN entries, same bounds. |

Where practical, the result also reports `tls_version`
(`ssl.SSLSocket.version()`) and `cipher` (`ssl.SSLSocket.cipher()`'s
first element) from the inspection handshake, and
`connected_address` — the specific resolved address that presented the
certificate.

`meta.truncated=true` is set only when SAN entries or a subject/issuer
string were **actually** cut by the bounds above — never merely because
a bound exists.

## Result shape

```json
{
  "meta": {
    "source_system": "tls",
    "query_time": "2026-09-19T00:00:00+00:00",
    "observation_time": "2026-09-19T00:00:00+00:00",
    "query_window": null,
    "truncated": false,
    "derived_fields": ["verification.status"],
    "contract_version": "1.0"
  },
  "target_alias": "grafana",
  "host": "grafana.example.net",
  "port": 443,
  "server_name": "grafana.example.net",
  "connected_address": "172.30.10.20",
  "tls_version": "TLSv1.3",
  "cipher": "TLS_AES_256_GCM_SHA384",
  "certificate": {
    "subject": "CN=grafana.example.net",
    "issuer": "CN=Example CA",
    "serial_number": "123456",
    "sha256_fingerprint": "ab...ab",
    "not_before": "2026-01-01T00:00:00+00:00",
    "not_after": "2027-01-01T00:00:00+00:00",
    "san_dns": ["grafana.example.net"],
    "san_ip": []
  },
  "verification": {
    "chain_trusted": true,
    "hostname_matches": true,
    "time_valid": true,
    "status": "valid"
  },
  "error": null
}
```

(Real, verified output shape — generated by running
`tls_certificate_inspect` against a canned/mocked inspection seam; not a
hand-written mockup.)

Adopts the same `mantis.contracts.QueryMeta` contract as every other
Mantis tool: `meta.source_system` is always `"tls"`;
`meta.observation_time` is populated (this call is a single
point-in-time observation); `meta.derived_fields` names
`verification.status` as Mantis's own normalized summary — every other
field is either the original request, the target's own configuration
(`host`/`port`/`server_name`), or a direct pass-through of what the peer
actually presented.

`host`/`port`/`server_name` echo the target's **configured** values,
never caller-supplied — proof that a caller cannot redirect the tool to
a different endpoint or SNI even indirectly.

## Input validation

`target_alias` is validated before any handshake is attempted: it must
name a configured target — an alias with no matching target is
rejected the same way, and this check happens without ever contacting
an endpoint (`TLSProfilesConfig.resolve_target` performs no network
access).

Invalid input returns `"error": {"type": "invalid_input", "message":
...}` as a **normal tool result**, not a raised exception — deliberately,
because the rejected text is untrusted, model-supplied data (#14) and
must flow through the standard model-input safety pipeline
(`mantis.security.make_model_safe()`) like any other tool result. Every
other field (`certificate`, `verification`, ...) is `None` in this case.

`tls_certificate_inspect`'s own logging never writes the raw rejected
`target_alias` to a log line — only a fixed string
(`"tls_certificate_inspect rejected invalid input"`). See
`tests/test_tls_tools.py::test_invalid_input_never_writes_the_raw_untrusted_value_to_the_log`.

## Security and bounds

`tls_certificate_inspect` is registered with `mutating=False` and
`contains_untrusted_text=True` (a certificate's subject, issuer, and SAN
entries are external, presenter-controlled data — same treatment as
every other evidence tool).

**Why target selection is alias-only, never an arbitrary
host/port/SNI**: allowing a caller to name an endpoint directly would
turn this tool into a general-purpose TLS probe against *whatever
endpoint the model chooses*. Restricting selection to a fixed,
server-side-configured alias set keeps the tool's entire capability
surface auditable from `mantis.config.TLSProfilesConfig` alone — the
same reasoning `dns_lookup`/`http_probe` apply to their own target
selection.

Malicious certificate text (a crafted subject/issuer/SAN string) is
preserved as evidence, not stripped — see
`tests/test_tls_tools.py::test_malicious_certificate_subject_remains_untrusted_not_stripped`,
and [docs/security.md](security.md)'s "Why prompt-like text is
preserved as evidence" for the rationale.

Every named bound:

| Bound | Value | Protects against |
|---|---|---|
| `MAX_ADDRESSES_ATTEMPTED` | 4 | Unbounded worst-case latency from a host resolving to many addresses. |
| `MAX_SAN_ENTRIES` | 25 | A malicious/misbehaving certificate flooding the model with SAN entries. |
| `MAX_SAN_STRING_CHARS` | 253 | An oversized individual SAN entry. |
| `MAX_SUBJECT_CHARS` / `MAX_ISSUER_CHARS` | 500 each | An oversized subject/issuer distinguished name. |
| `DEFAULT_TLS_TIMEOUT_SECONDS` | 5.0s | An unbounded wait on a slow/unresponsive endpoint per handshake attempt (further capped by the caller's remaining `Deadline`). |

## Non-goals

Deliberately excluded from #111 (candidates for a distinct, later,
separately-scoped issue if ever needed):

- **mTLS / client certificates.** No model-facing or per-call parameter
  exists for presenting a client certificate.
- **Cipher suite enumeration or a TLS vulnerability scanner.** This tool
  reports the negotiated `tls_version`/`cipher` from one handshake, not
  an exhaustive scan of what an endpoint supports.
- **OCSP/CRL revocation checking.** Chain trust verification uses the
  standard trust-store validation only; revocation status is not
  checked or reported.
- **STARTTLS or any protocol-upgrade TLS.** Direct TLS endpoints only —
  the same connect-then-handshake model `check_tcp_connectivity` uses.
- **Raw certificate output (full PEM/DER) or arbitrary extension
  dumping.** Only the bounded fields in "Certificate fields" above are
  ever returned.
- **Arbitrary host/port/SNI/CA input.** `target_alias` only; see
  "Security and bounds".
- **Agent-specific TLS logic.** All mechanics live in
  `mantis.integrations.tls`/`mantis.tools.tls`, reusable by any current
  or future agent that declares `tls_certificate_inspect` in its
  `ALLOWED_TOOLS` — see [docs/agents.md](agents.md). No agent declares
  it yet.

## Observability

No new telemetry system. Tool invocation, duration, success/failure, and
budget/deadline exhaustion are already visible through the existing
`AgentRuntime` structured logs/metrics (`mantis_tool_call`/
`mantis_tool_calls_total`, labeled by tool name — see
[docs/observability.md](observability.md)) the same way every other
tool's calls are. No TLS-specific structured log event was added; target
aliases/hosts/certificate text never appear in a Prometheus label (the
only label involving this tool is the fixed string
`tool="tls_certificate_inspect"`).

## Correlating with other evidence

`tls_certificate_inspect` is a **current-state** observation, exactly
like `check_tcp_connectivity` (#8), `dns_lookup` (#109), and
`http_probe` (#110) — never conflate it with #28's historical AWX
evidence. See [docs/http-probe.md](http-probe.md#the-troubleshooting-stack)
for how DNS, TCP, TLS, and HTTP evidence compose into one layered
troubleshooting order (`dns_lookup` → `check_tcp_connectivity` →
`tls_certificate_inspect` → `http_probe`), each answering a distinct
question at its own layer.

## Follow-up

No follow-up issue is required to satisfy #111 as scoped — every
explicit requirement in the ticket is implemented and tested. A future,
separately-scoped issue could reasonably cover: wiring
`tls_certificate_inspect` into an investigation agent's
`ALLOWED_TOOLS`, mTLS/client-certificate support for a concrete,
motivated use case, or an evaluation scenario correlating it with
`http_probe`/`check_tcp_connectivity`/`dns_lookup` for a layered TLS
failure. None of these are started here.

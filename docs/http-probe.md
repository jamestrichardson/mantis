# HTTP probe (`http_probe`)

Mantis's first HTTP evidence tool (#110). The core question it answers
is deliberately narrow:

> What does *this specific, server-side-configured origin's* endpoint
> return right now?

Not a general-purpose web fetch, browser, or crawler — see "Non-goals"
below. `http_probe` sends exactly one bounded `GET`/`HEAD` request to
one server-side-configured target and reports the response (any status
code) as current-state evidence.

## Layering

Following the existing Mantis architecture (see
[docs/architecture.md](architecture.md)):

```
src/mantis/config.py
    HTTPProfilesConfig -- server-side HTTP target profile configuration
    (alias -> immutable scheme/host/port/base_path/verify_ssl). Parsing
    only; no network access.

src/mantis/integrations/http.py
    Request/response mechanics: path/method validation, the bounded
    httpx.Client construction (no redirects, no env proxy trust, no
    cookies), streamed body reading with a hard byte cap, header
    allowlisting, transport-failure classification.

src/mantis/tools/http.py
    Semantic result shaping, provenance (QueryMeta), target-alias
    resolution against HTTPProfilesConfig, registry registration,
    untrusted-output handling.
```

No HTTP mechanics live in an agent; no new reliability abstraction was
created — this reuses #15's `Deadline`/`IntegrationError` directly, the
same way `check_tcp_connectivity`/`dns_lookup` do.

## Target profiles

A caller (model or API client) selects a target only by its
**configured alias name** — `target_alias` is the only target-selecting
input `http_probe` accepts. It can never supply a scheme, host, port,
URL, or proxy directly. This is the architectural reason target
profiles are server-side configuration (`mantis.config.HTTPProfilesConfig`)
rather than a model-facing parameter: the *set* of origins Mantis is
willing to probe must be fixed by the operator, not expandable by
whatever URL a model decides to construct.

Configure one or more profiles via environment variables:

```bash
MANTIS_HTTP_TARGET_GRAFANA_URL=https://grafana.example.net:3000/grafana
MANTIS_HTTP_TARGET_INTERNAL_API_URL=http://internal-api.example.net
MANTIS_HTTP_TARGET_SELFSIGNED_URL=https://legacy-internal.example.net
MANTIS_HTTP_TARGET_SELFSIGNED_VERIFY_SSL=false
```

`<ALIAS>` (case-insensitive) becomes the `target_alias` a caller may
request. The configured URL establishes the target's **immutable**
scheme, host, port, and optional base path — a caller's `path` argument
is always appended to, never replaces, the configured base path (see
"Path handling" below). See
[docs/configuration.md](configuration.md#http) for the full reference.

`HTTPProfilesConfig.from_env()` performs **no network access** — it only
parses and validates these variables:

- The URL must use `http://` or `https://` — any other scheme is
  rejected at startup.
- The URL must not embed credentials (`user:pass@host`).
- The URL must not include a query string or fragment — configure a
  clean origin (and optional base path) only.
- The host must be a valid hostname or IP literal.

An unrecognized `target_alias` is rejected as invalid input
(`error.type="invalid_input"`) *before* `http_probe` ever attempts a
request — see "Input validation" below, and
`tests/test_http_tools.py::test_unknown_target_alias_causes_no_network_request`
for the proof.

## Method

`GET` and `HEAD` only (`method`, case-insensitive, default `GET`). Any
other method — `POST`, `PUT`, `PATCH`, `DELETE`, `CONNECT`, `OPTIONS`,
`TRACE`, or garbage — is rejected as invalid input before any request is
attempted. This alone is what keeps `http_probe` read-only and
body-free: there is no code path anywhere in this tool that could send a
request body. For `method="HEAD"`, no response body is read at all —
`body_excerpt` is `None` and `body_bytes_observed` is `0`, never an
attempted (and necessarily empty) read.

## Path handling

`path` (default `"/"`) must be a plain, bounded, origin-relative path.
Mirroring `mantis.integrations.network.validate_host`'s
allowlist-not-blocklist approach
(`mantis.integrations.http.validate_http_path`), it rejects:

- Non-string input, an empty string, or a string longer than
  `MAX_PATH_CHARS` (**512**).
- Whitespace or control characters, including CR/LF — a path cannot be
  used to smuggle extra header lines into the request.
- A backslash — some HTTP stacks/proxies treat `\` as a path separator
  equivalent to `/`.
- Anything not starting with a single `/`.
- A leading `//` — protocol-relative/host-escape syntax
  (`//evil.example/` would otherwise be interpreted by some HTTP stacks
  as switching host entirely).
- An embedded absolute URL (`://` anywhere in the string).
- Embedded credentials (`@`).
- A query string (`?`) — #110 deliberately rejects query strings in
  this first implementation rather than validating/bounding them.
- A fragment (`#`) — fragments are never sent over the wire anyway;
  rejecting one outright here is simpler than silently stripping it.

Concretely, none of these ever reach `httpx` or cause any network
activity at all:

```text
https://evil.example/          -- absolute URL
//evil.example/                -- protocol-relative host escape
/\r\nHost: evil.example         -- CRLF header injection attempt
```

See `tests/test_http_tools.py::test_invalid_path_causes_no_network_request`
for the deterministic proof (a mocked `_build_client` seam that must
never be called).

The validated `path` is then combined with the target's configured,
immutable `base_path` by plain string concatenation
(`mantis.integrations.http._join_path`) — never a URL join, which could
reinterpret a leading `/` as origin-absolute and silently drop the
base path. `base_path` never ends in `/` (normalized at config-parse
time) and `path` always starts with exactly one `/`, so concatenation
can never produce a double slash or an origin escape.

## Redirects

`http_probe` **never automatically follows a redirect**, same-origin or
cross-origin. `httpx.Client` is constructed with
`follow_redirects=False` explicitly. A 3xx response is returned exactly
like any other status: `status_code` is the 3xx code, and the
response's `Location` header (if present, bounded to
`MAX_LOCATION_CHARS`, **2,000** characters) is returned as
`redirect_location` — reported as evidence, never chased. There is no
code path anywhere in this tool that issues a second request, so a
cross-origin redirect (e.g. to `https://completely-different-origin.example/steal`)
is structurally impossible to follow — see
`tests/test_http_tools.py::test_cross_origin_redirect_is_reported_but_never_followed`.

## Proxy trust

`http_probe` **never silently inherits `HTTP_PROXY`/`HTTPS_PROXY`/
`ALL_PROXY`** or any other environment proxy configuration.
`httpx.Client` is constructed with `trust_env=False` explicitly (see
`mantis.integrations.http._build_client`) — deliberately different from
every other Mantis HTTP integration (AWX/Prometheus/Loki), which do
trust the environment for their own outbound requests. A current-state
network observation must be deterministic: routing it through whatever
proxy happens to be set in the process environment would make "can
Mantis reach this origin" actually mean "can Mantis reach this origin
*through whatever proxy is configured right now*" — a materially
different and non-obvious question. There is no configuration surface
(model-facing or environment-variable) to opt a probe back into proxy
usage; if that's ever genuinely needed, it would be a distinct,
separately-scoped feature.

## Authentication

`http_probe` sends **no authentication of any kind** — no `Authorization`
header, no cookies, no client certificate. There is no model-facing or
per-call parameter for credentials. A target's optional server-side
auth (if ever added) would be configured entirely in
`HTTPProfilesConfig`, injected by the integration layer, never
model-supplied — this is not implemented in #110, since no concrete use
case motivated it yet (see "Non-goals").

## HTTP result semantics

**A received HTTP response is successful evidence retrieval, regardless
of status code.** `200`, `204`, `301`, `302`, `401`, `403`, `404`,
`429`, `500`, and `503` are all returned identically as a normal result
— `http_probe` never raises an error merely because a response was
non-2xx. This mirrors `dns_lookup`'s `nxdomain`/`servfail` philosophy: a
server's own real response, whatever it says, is evidence, not a tool
failure.

**Transport-level failures are different, and never returned as a fake
status code.** DNS failure, TCP connect failure, TLS handshake failure,
a timeout, or a malformed/unparseable response — anything that happens
*before* a response is received at all, or while it cannot be parsed as
one — propagates as a classified `mantis.integrations.http.HTTPProbeError`
(an `IntegrationError` subclass, reusing #15's taxonomy via
`mantis.reliability.classify_httpx_exception`), handled by
`AgentRuntime` generically, exactly like every other Mantis
integration's transport failures. This split is deliberate and
load-bearing: a `500` from the target is the target telling you
something; a connect timeout is Mantis failing to find out anything at
all. Never conflate the two.

## Body and header bounds

Every bound below is named, deliberate, and enforced **while reading**
— never by reading the full response and truncating afterward
(`mantis.integrations.http._read_bounded_body` streams via
`response.iter_bytes()` and stops early):

| Bound | Value | Protects against |
|---|---|---|
| `MAX_BODY_BYTES_READ` | 65,536 bytes | Unbounded memory/time reading an arbitrarily large or slow-streamed response body. |
| `MAX_BODY_EXCERPT_CHARS` | 2,000 chars | An oversized decoded body excerpt in the model-facing result. |
| `MAX_HEADERS_RETURNED` | 6 (derived from the allowlist below) | Returning every response header, most of which are irrelevant or sensitive. |
| `MAX_HEADER_VALUE_CHARS` | 500 chars | An oversized individual header value. |
| `MAX_LOCATION_CHARS` | 2,000 chars | An oversized/attacker-controlled `Location` header. |
| `MAX_PATH_CHARS` | 512 chars | An oversized model-supplied `path` argument. |

An unbounded wait on a slow/unresponsive origin is bounded by two
layers, not a dedicated HTTP-specific constant: `probe_http`'s
`connect_timeout_seconds`/`read_timeout_seconds` parameters (always
supplied by the tool layer from `mantis.config.ReliabilityConfig`'s
shared `http_connect_timeout_seconds`/`http_read_timeout_seconds`
fields — the same ones AWX/Prometheus/Loki use), each further capped by
the caller's remaining `Deadline` (`mantis.reliability.Deadline`) when
one is given — every Mantis tool call already runs under
`AgentRuntime`'s own per-call budget, which is the overall ceiling on a
single `http_probe` call in normal operation.

`headers` in a result only ever contains the explicit allowlist
(`mantis.integrations.http._ALLOWED_RESPONSE_HEADERS`): `content-type`,
`content-length`, `server`, `date`, `location`, `retry-after`. **Never**
`set-cookie`, `authorization`, `proxy-authorization`, `cookie`, or any
other header — only names in that allowlist are ever copied into a
result, by construction (an allowlist, not a blocklist that could miss
something).

The body is decoded best-effort as UTF-8
(`mantis.integrations.http._decode_excerpt`), with malformed byte
sequences replaced (`errors="replace"`) rather than raising — a binary
or non-UTF-8 response body is honestly represented as a lossy text
excerpt, not a crash, since #14's untrusted-evidence handling covers the
rest.

`meta.truncated=true` is set only when evidence was **actually**
omitted by one of these bounds (a large body, too many/oversized
headers, an oversized `Location`) — never merely because a bound
exists.

## Result shape

```json
{
  "meta": {
    "source_system": "http",
    "query_time": "2026-09-19T00:00:00+00:00",
    "observation_time": "2026-09-19T00:00:00+00:00",
    "query_window": null,
    "truncated": false,
    "derived_fields": [],
    "contract_version": "1.0"
  },
  "target_alias": "grafana",
  "method": "GET",
  "path": "/api/health",
  "scheme": "https",
  "host": "grafana.example.net",
  "port": 3000,
  "status_code": 200,
  "reason": "OK",
  "latency_ms": 18.3,
  "headers": {"content-type": "application/json"},
  "body_excerpt": "{\"status\":\"ok\"}",
  "body_bytes_observed": 16,
  "redirect_location": null,
  "error": null
}
```

(Real, verified output shape — generated by running `http_probe` against
a canned/mocked probe seam; not a hand-written mockup.)

Adopts the same `mantis.contracts.QueryMeta` contract as every other
Mantis tool: `meta.source_system` is always `"http"`; `meta.observation_time`
is populated (this call is a single point-in-time observation, exactly
like `check_tcp_connectivity`/`dns_lookup`); `meta.derived_fields` is
empty — every field in this result is either the original request
echoed back or a direct pass-through of the target's own reported data
(status, reason, headers, body), never a Mantis-computed
interpretation.

`scheme`/`host`/`port` echo the target's **configured** values, never
caller-supplied — proof that a caller cannot redirect the tool to a
different origin even indirectly.

## Input validation

`target_alias`, `path`, and `method` are all validated before any
request is attempted:

- `method` must be `GET` or `HEAD` (case-insensitive).
- `path` must pass every check in "Path handling" above.
- `target_alias` must name a configured target — an alias with no
  matching target is rejected the same way, and this check happens
  without ever contacting an origin (`HTTPProfilesConfig.resolve_target`
  performs no network access).

Invalid input returns `"error": {"type": "invalid_input", "message":
...}` as a **normal tool result**, not a raised exception — deliberately,
because the rejected text is untrusted, model-supplied data (#14) and
must flow through the standard model-input safety pipeline
(`mantis.security.make_model_safe()`) like any other tool result. Every
network-observation field (`status_code`, `headers`, `body_excerpt`,
...) is `None`/empty in this case. The runtime's generic last-resort
exception path does not apply that pipeline, which is why validation
failures are returned, never raised.

`http_probe`'s own logging never writes the raw rejected
`target_alias`/`path`/`method`/validation message to a log line — only
a fixed string (`"http_probe rejected invalid input"`). The rejected
value is untrusted, model-supplied text; it belongs in the *returned*
result (which goes through `make_model_safe()`), never in a raw
`logger.info(..., value)` call that would write it straight to
container stdout/Loki unredacted. See
`tests/test_http_tools.py::test_invalid_input_never_writes_the_raw_untrusted_value_to_the_log`.

## Security and bounds

`http_probe` is registered with `mutating=False` and
`contains_untrusted_text=True` (response bodies, headers, and the
redirect `Location` are external, Mantis-uncontrolled data — same
treatment as every other evidence tool).

**Why target selection is alias-only, never an arbitrary URL**: allowing
a caller to name a URL directly would turn this tool into a
general-purpose HTTP client against *whatever origin the model
chooses* — a fundamentally different, much larger capability than
"probe one of a small, operator-approved set of HTTP targets."
Restricting selection to a fixed, server-side-configured alias set
keeps the tool's entire capability surface auditable from
`mantis.config.HTTPProfilesConfig` alone — the same reasoning
`dns_lookup` applies to resolver selection.

Malicious response text is preserved as evidence, not stripped — see
`tests/test_http_tools.py::test_malicious_body_excerpt_remains_untrusted_not_stripped`/
`test_malicious_redirect_location_remains_untrusted_not_stripped`, and
[docs/security.md](security.md)'s "Why prompt-like text is preserved as
evidence" for the rationale.

No subprocess (`curl`/`wget`) is ever invoked —
`tests/test_http_tools.py::test_tool_never_imports_subprocess` proves
this structurally by inspecting the module's own imports/source, not
just by convention.

## Non-goals

Deliberately excluded from #110 (candidates for a distinct, later,
separately-scoped issue if ever needed):

- **Generic web fetch, browser, or crawler.** `http_probe` sends exactly
  one bounded request to one server-side-configured origin; it is not a
  general HTTP client.
- **`POST`/`PUT`/`PATCH`/`DELETE`/`CONNECT`/any body-carrying or
  mutating method.** `SUPPORTED_HTTP_METHODS` is `("GET", "HEAD")`
  only.
- **Arbitrary headers, cookies, or authentication.** No model-facing or
  per-call parameter exists for any of these.
- **Automatic redirect following**, same-origin or cross-origin. See
  "Redirects" above.
- **Query strings.** Rejected outright by `validate_http_path` in this
  first implementation — a candidate for a later, carefully-bounded
  extension if a concrete use case ever needs it.
- **Environment proxy trust.** `trust_env=False`, always.
- **Arbitrary URL input.** `target_alias` only; see "Security and
  bounds".
- **Detailed TLS certificate diagnosis.** That's
  [`tls_certificate_inspect`](tls-certificate-inspection.md) (#111) —
  `http_probe` only reports whether the TLS handshake itself succeeded
  or failed (a transport-level outcome), never certificate metadata.
- **Agent-specific HTTP logic.** All mechanics live in
  `mantis.integrations.http`/`mantis.tools.http`, reusable by any
  current or future agent that declares `http_probe` in its
  `ALLOWED_TOOLS` — see [docs/agents.md](agents.md). No agent declares
  it yet.

## Observability

No new telemetry system. Tool invocation, duration, success/failure, and
budget/deadline exhaustion are already visible through the existing
`AgentRuntime` structured logs/metrics (`mantis_tool_call`/
`mantis_tool_calls_total`, labeled by tool name — see
[docs/observability.md](observability.md)) the same way every other
tool's calls are. No HTTP-specific structured log event was added;
target aliases/hosts/response text never appear in a Prometheus label
(the only label involving this tool is the fixed string
`tool="http_probe"`).

## Correlating with other evidence

`http_probe` is a **current-state** observation, exactly like
`check_tcp_connectivity` (#8) and `dns_lookup` (#109) — never conflate
it with #28's historical AWX evidence. It's a natural complement to
both: `check_tcp_connectivity` answers "can Mantis reach this host/port
at all," `dns_lookup` answers "what does DNS say about this name," and
`http_probe` is what makes the HTTP layer itself independently
inspectable once a TCP connection can be established (e.g. "the TCP
check passed, but is the HTTP service behind it actually answering, and
with what status?"). See "The troubleshooting stack" below for how
these compose with [`tls_certificate_inspect`](tls-certificate-inspection.md)
(#111) as well.

## The troubleshooting stack

DNS, TCP, TLS, and HTTP evidence compose into one natural layered
troubleshooting order, each tool answering a distinct question at its
own layer, never subsuming another's:

```text
dns_lookup               -- does the name resolve, and to what, from this resolver's perspective? (#109)
check_tcp_connectivity   -- can Mantis reach that host/port at all? (#8)
tls_certificate_inspect  -- if TLS is involved, what certificate is presented, and is it trustworthy? (#111)
http_probe               -- once connected (and, for HTTPS, once a TLS session exists), what does the HTTP endpoint actually return? (#110)
```

A failure at an earlier layer generally explains a failure observed at
a later one (a TCP connect failure means an HTTP probe against the same
target will also fail — but at the *transport* level, not as an
HTTP-layer status code). None of these tools automatically calls
another; correlating across layers is an agent (or human) decision made
by calling more than one tool explicitly, the same "no automatic
fan-out" philosophy `dns_lookup` documents for cross-resolver
comparison.

## Follow-up

No follow-up issue is required to satisfy #110 as scoped — every
explicit requirement in the ticket is implemented and tested. A future,
separately-scoped issue could reasonably cover: wiring `http_probe` into
an investigation agent's `ALLOWED_TOOLS`, bounded query-string support
for a concrete, motivated use case, or an evaluation scenario
correlating `http_probe` with `check_tcp_connectivity`/`dns_lookup`/
`tls_certificate_inspect` for a layered connectivity failure. None of
these are started here.

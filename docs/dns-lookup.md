# DNS lookup (`dns_lookup`)

Mantis's first DNS evidence tool (#109). The core question it answers
is deliberately narrow and perspective-scoped:

> What does *this specific, server-side-configured resolver
> perspective* report for this name right now?

Not:

> ~~What is the globally correct answer for this name?~~

There often isn't one. See "Split-horizon DNS" below — this is the
whole reason `dns_lookup` takes a `resolver_alias` instead of always
using "the" resolver.

## Layering

Following the existing Mantis architecture (see
[docs/architecture.md](architecture.md)):

```
src/mantis/config.py
    DNSConfig -- server-side resolver profile configuration
    (alias -> ordered server IP list). Parsing only; no network access.

src/mantis/integrations/dns.py
    Query/failover mechanics: name & record-type validation, the
    low-level dnspython query call, per-server outcome classification,
    the deterministic multi-server failover rule, answer/CNAME-chain
    bounding.

src/mantis/tools/dns.py
    Semantic result shaping, provenance (QueryMeta), resolver-alias
    resolution against DNSConfig, registry registration, untrusted-
    output handling.
```

No DNS wire-format code lives in an agent; no new reliability
abstraction was created — this reuses #15's `Deadline`/`IntegrationError`
directly, the same way `check_tcp_connectivity` does.

## Why `dnspython`, not `dig`/`nslookup`/`host`

`dnspython` is the standard, de facto Python DNS library — adding it as
a dependency avoids shelling out to a subprocess (which every other
Mantis integration also avoids) and avoids hand-rolling a DNS wire-format
codec, which would be substantial, security-sensitive, low-value work
here. Specifically, this integration uses one low-level call,
`dns.query.udp_with_fallback()`, rather than the higher-level
`dns.resolver.Resolver` class — `Resolver`'s own multi-nameserver
iteration bundles a mix of per-server outcomes into a single
`NoNameservers` exception, which would hide exactly the per-server
ordering/definitive-answer control #109 needs. Sending one query to one
server and inspecting the raw response's `rcode()` directly is both
simpler and precisely controllable.

`dns.query.udp_with_fallback()` sends over UDP and transparently
retries over TCP if the response comes back truncated (the protocol's
own `TC` flag) — this is DNS's *normal* fallback behavior, reused as-is,
never reimplemented.

## Resolver profiles

A caller (model or API client) selects a resolver only by its
**configured alias name** — `resolver_alias` is the only resolver-
selecting input `dns_lookup` accepts. It can never supply a resolver
IP, hostname, port, or any other DNS endpoint directly. This is the
architectural reason resolver profiles are server-side configuration
(`mantis.config.DNSConfig`) rather than a model-facing parameter: the
*set* of perspectives available for comparison must be fixed by the
operator, not expandable by whatever a model decides to ask for.

Configure one or more profiles via environment variables, one variable
per alias:

```bash
MANTIS_DNS_RESOLVER_INTERNAL=172.30.0.53,172.30.0.54
MANTIS_DNS_RESOLVER_CLOUDFLARE=1.1.1.1,1.0.0.1
MANTIS_DNS_RESOLVER_GOOGLE=8.8.8.8,8.8.4.4
```

`<ALIAS>` (case-insensitive) becomes the `resolver_alias` a caller may
request — `MANTIS_DNS_RESOLVER_INTERNAL` is requested as
`resolver_alias="internal"`. A profile may list one or more servers,
comma-separated; see "Multi-server failover" below for what happens
when there's more than one. Servers must be IP literals (IPv4 or
IPv6) — never hostnames, since a resolver's own address must not itself
require DNS resolution to reach. See
[docs/configuration.md](configuration.md#dns) for the full reference.

**`internal`/`cloudflare`/`google` are just example alias names** — none
of them is hardcoded or special-cased anywhere in this implementation.
An operator is free to configure any alias names they find meaningful
(`"system"`, `"isp"`, `"secondary-dc"`, ...); Cloudflare/Google are
never a mandatory or default runtime behavior, only examples of
configured *public* resolver profiles an operator might choose to add
for split-horizon comparison.

`DNSConfig.from_env()` performs **no network access** — it only reads
and validates environment variables (IP-literal syntax, non-empty
alias/server list). An unrecognized `resolver_alias` is rejected as
invalid input (`status="invalid_input"`) *before* `dns_lookup` ever
attempts a query — see "Input validation" below, and
`tests/test_dns_tools.py::test_unknown_resolver_alias_causes_no_network_request`
for the proof.

### Split-horizon DNS

The entire reason for explicit resolver selection: the *same name* can
legitimately resolve differently — or not at all — depending on which
resolver perspective answers it. Neither of these is a contradiction,
and Mantis never treats a public resolver's answer as "authoritative
truth" that an internal resolver's differing answer must be wrong
against:

```text
internal:  app.example.net -> 172.30.10.15
public:    app.example.net -> NXDOMAIN
```

```text
internal:  app.example.net -> 172.30.10.15
public:    app.example.net -> 203.0.113.44
```

Both are simply **differing resolver views**, observed at query time.
The first is completely ordinary for split-horizon DNS (an internal-only
record with no public counterpart). The second is completely ordinary
too (a private-network address from an internal view; a public-facing
address, e.g. a reverse proxy or load balancer, from a public view).
Describe both observations plainly — which resolver reported what — and
never editorialize about which one is "correct." See
`tests/test_dns_tools.py::test_split_horizon_internal_private_vs_public_nxdomain`/
`test_split_horizon_internal_private_vs_public_different_address` for
the deterministic proof.

**One `dns_lookup` call queries exactly one resolver profile.** It never
automatically fans out to every configured profile to compare them —
comparing perspectives, if ever needed, is something an agent (or a
future correlation step) does by making two separate, explicit calls
with two different `resolver_alias` values, never something this tool
does on its own. There is no `dns_compare_views`-style tool, and adding
one is explicitly out of scope for #109 (see "Non-goals").

## Supported record types

`A`, `AAAA`, `CNAME`, `PTR` only (`record_type`, case-insensitive,
default `A`). `TXT`, `MX`, `SRV`, `NS`, `SOA`, `ANY`, `AXFR`, and
anything else is rejected as invalid input — see "Non-goals".

For `PTR`, `name` is the IPv4/IPv6 address to reverse-look-up (not a
domain name) — validated as an IP literal
(`mantis.integrations.dns.validate_ptr_target`) and internally converted
to the `*.in-addr.arpa`/`*.ip6.arpa` query name
(`dns.reversename.from_address`); the result's `name` field always
echoes back the original IP you asked about, never the internal reverse
name, which is implementation detail, not evidence.

## Status vocabulary

| Status | Meaning | What it does NOT prove |
|---|---|---|
| `ok` | The responding server returned `NOERROR` with at least one answer record. | Only that *this* resolver perspective has an answer right now — not that every perspective would agree, and not that the answer will still hold later (TTL expiry, DNS changes). |
| `nxdomain` | The responding server returned `NXDOMAIN` — this perspective reports the name does not exist. | Not that the name doesn't exist from *every* perspective (see "Split-horizon DNS") — and never confusable with a timeout, which proves nothing about existence at all. |
| `no_data` | The responding server returned `NOERROR` with an empty answer section — the name exists, but has no records of the requested type. | Distinct from `nxdomain`: the name itself is not absent, only this record type at it. |
| `servfail` | The responding server returned `SERVFAIL` — a real DNS-protocol response reporting its own failure to answer. | Not `not_found`/evidence the name is absent — the server simply couldn't complete the resolution (upstream failure, misconfiguration, DNSSEC validation failure, ...). |
| `refused` | The responding server returned `REFUSED` — a real response, declining to answer (policy/ACL). | Also not `not_found` — this is an administrative decision by that server, not evidence about the name. |
| `budget_exceeded` | The tool/run deadline is why one or more configured servers were never attempted. | A scheduling outcome, not a DNS observation — see "Deadline semantics". |
| `invalid_input` | `name`/`record_type`/`resolver_alias` failed validation before any query was attempted. | Not one of the statuses above — no network activity occurred. See "Input validation". |

**Transport/retrieval failures are never a `status` value.** A timeout,
a connection/transport-level failure, a malformed/unusable response, or
any other unexpected failure sending/receiving a query propagates as a
classified `mantis.integrations.dns.DNSError` (an
`IntegrationError` subclass) instead — handled by `AgentRuntime`
generically, exactly like every other Mantis integration's transport
failures (AWX/Prometheus/Loki/Kubernetes). This split is deliberate and
load-bearing:

- **`nxdomain` != `timeout`.** A resolver timing out tells you nothing
  about whether the name exists — never report a timeout as if it were
  evidence of absence.
- **`nxdomain` != `no_data`.** The name not existing at all is a
  different fact from the name existing with no records of the
  requested type.
- **`servfail`/`refused` != `not_found`.** Both are the server
  reporting *its own* failure/refusal to answer, categorically distinct
  from the name not existing.

## Multi-server failover

A profile may configure more than one server (e.g. a primary/secondary
internal resolver pair). #109 requires this behavior to be deterministic
and explicit — this section is that specification (see
`tests/test_dns.py`'s "Deterministic multi-server failover" section for
the exhaustive parametrized proof):

- **Ordering**: servers are tried strictly in the order configured, up
  to `MAX_SERVERS_ATTEMPTED` (**4**) — a small, named, deliberate bound
  (mirroring `check_tcp_connectivity`'s `MAX_ADDRESSES_ATTEMPTED`),
  sized for predictable interactive-troubleshooting latency, not for
  querying an operator's entire resolver fleet. The same server is
  never queried twice for one call.
- **Which failures permit trying the next server**: `servfail`,
  `refused`, a timeout, a connection/transport error, and a malformed
  response all permit trying the next configured server. None of these
  says anything about whether the name exists — only that *this*
  particular server couldn't or wouldn't answer right now, and a
  different server configured for the same perspective might.
- **When a response is definitive**: `ok`, `nxdomain`, and `no_data` are
  all definitive — the server gave a real, authoritative-for-it answer
  about the name itself (whether it resolves, doesn't exist, or exists
  with no records of this type).
- **Whether a successful answer stops failover**: yes, and so does
  every other definitive outcome — **immediately**. The remaining
  configured servers are never queried. Trying another server after a
  definitive answer would risk exactly the "keep asking until you get a
  preferred answer" anti-pattern #109 forbids; one call queries one
  profile, never fans out to compare servers within it either.
- **Which server actually answered**: the result's `responding_resolver`
  field names the specific server that produced the returned `status` —
  set for every definitive outcome and for an exhausted-profile
  `servfail`/`refused` outcome (a real response was received from that
  server); left `None` only when every attempted server failed at the
  transport level (no server ever produced a DNS-protocol response at
  all — the call raises `DNSError` in that case, so no result with a
  `responding_resolver` field is returned regardless).
- **If every attempted server ends in `servfail`/`refused`** (mixed with
  transport failures or not), the highest-precedence one of the two is
  the final `status` — **still a normal, successful result**, since a
  real DNS response was received:

  ```text
  refused > servfail
  ```

  (most diagnostically specific first — a deliberate policy decision by
  a server outranks a generic internal-failure report — mirroring
  `check_tcp_connectivity`'s own most-specific-first failure precedence.)
- **If no attempted server ever produced a DNS-protocol response at
  all** (only timeouts/connection errors/malformed responses observed),
  `dns_lookup` raises `DNSError` instead of returning a result. The
  `DNSError`'s classification follows the same most-specific-first
  philosophy:

  ```text
  connection_error > malformed_response > timeout
  ```

  A concrete OS-observed fact (a refused/unreachable connection) beats a
  merely malformed response, which beats a generic "no response at all"
  timeout.
- **Do not query all servers simply to compare them.** This is the
  overarching rule the bullets above are all in service of — the loop
  stops at the first definitive answer, full stop, regardless of how
  many servers remain configured.

## Deadline semantics (#15)

Mirrors `check_tcp_connectivity` exactly: a DNS lookup is a current-state
**observation**, not an idempotent HTTP API read — so it is deliberately
never wrapped in `mantis.reliability.retry_call()`, and the same server
is never retried. Trying several *distinct* configured servers is not a
retry loop (see "Multi-server failover" above).

- `Deadline` is checked before the first server is attempted, and again
  before every subsequent one.
- Each attempt's own query timeout is
  `min(DEFAULT_DNS_QUERY_TIMEOUT_SECONDS, deadline.remaining())` (5.0s
  applies as-is when no deadline is given, e.g. a direct/manual call) —
  a single slow server can never itself exceed the caller's remaining
  budget.
- If the deadline is already exhausted before the first server can be
  tried, or expires between servers, the result is
  `status="budget_exceeded"` — never a precedence-derived status from an
  incomplete set of observations, since a later, untried server might
  have answered definitively. The per-server evidence already gathered
  is preserved in `server_attempts`.

## Result shape

```json
{
  "meta": {
    "source_system": "dns",
    "query_time": "2026-09-19T00:00:00+00:00",
    "observation_time": "2026-09-19T00:00:00+00:00",
    "query_window": null,
    "truncated": false,
    "derived_fields": ["status"],
    "contract_version": "1.0"
  },
  "name": "service.example.com",
  "record_type": "A",
  "resolver_alias": "internal",
  "resolver_addresses": ["172.30.205.10", "172.30.205.11"],
  "responding_resolver": "172.30.205.10",
  "status": "ok",
  "answers": [
    {"value": "172.30.210.25", "ttl": 300, "type": "A"}
  ],
  "server_attempts": [
    {"server": "172.30.205.10", "outcome": "ok", "used_tcp": false, "latency_ms": 0.72, "message": null}
  ]
}
```

(Real, verified output — generated by running `dns_lookup` against a
mocked resolver seam; not a hand-written mockup.)

Adopts the same `mantis.contracts.QueryMeta` contract as every other
Mantis tool: `meta.source_system` is always `"dns"`; `meta.observation_time`
is populated (this call is a single point-in-time observation, exactly
like `check_tcp_connectivity`); `meta.derived_fields` names `status` as
Mantis's own normalized summary derived from the raw per-server
evidence — every other field is either the original request or a direct
pass-through of the resolver's own reported data (TTL, answer values).

`resolver_addresses` echoes the *configured* server list for the
requested `resolver_alias` — safe, Mantis-controlled provenance (an
operator's own deployment configuration), never a secret, and never any
*other* alias's servers (see "Security and bounds" below).

`answers` entries carry a `type` field (not just `value`/`ttl`) so a
`CNAME` chain's intermediate hops can be told apart from the record type
actually requested when a response bundles both — see "CNAME chains"
below.

`server_attempts` additionally preserves bounded, structured per-server
evidence (mirroring `check_tcp_connectivity`'s `attempts`) — which
servers were tried, in order, and each one's own outcome — even when
`status`/`responding_resolver` alone already answer the common case.

## CNAME chains

A single response's answer section can legitimately contain a CNAME
hop followed by the record it ultimately points to (a normal recursive
resolver resolves the whole chain and returns it in one response) —
`dns_lookup` represents whatever the response actually contained,
**bounded** rather than followed with additional queries:

- `MAX_CNAME_CHAIN_DEPTH` (**8**): the largest number of CNAME-typed
  records collected from one response before stopping and marking
  `meta.truncated=true`.
- `MAX_ANSWERS_RETURNED` (**20**): the largest total number of answer
  records (of any type, across the whole answer section) returned
  before stopping and marking `meta.truncated=true`.

`dns_lookup` never issues an additional query to follow a CNAME further
than what one response already contained — every resolver profile
Mantis is meant to query is a full recursive resolver that already
chases CNAME chains internally, so this is a deliberate, well-justified
scope decision, not a missing feature.

## Input validation

`name`, `record_type`, and `resolver_alias` are all validated
(`mantis.integrations.dns.validate_dns_name`/`validate_ptr_target`/
`validate_record_type`, `mantis.config.DNSConfig.resolve_profile`)
**before** any query is attempted:

- `record_type` must be one of `A`/`AAAA`/`CNAME`/`PTR` (case-
  insensitive).
- `name` (for `A`/`AAAA`/`CNAME`) must be a non-empty, bounded
  (`MAX_NAME_CHARS`, **253** — RFC 1035's own limit), RFC-1123-ish
  domain name — dot-separated letter/digit/hyphen labels only, no
  whitespace/control characters, no embedded credentials, no shell
  metacharacters (the same allowlist-not-blocklist approach
  `mantis.integrations.network.validate_host` uses, for the same
  reasons).
- `name` (for `PTR`) must be a valid IPv4/IPv6 literal
  (`ipaddress.ip_address`).
- `resolver_alias` must name a configured profile — an alias with no
  matching profile is rejected the same way, and this check happens
  without ever contacting a resolver (`DNSConfig.resolve_profile`
  performs no network access; see
  `tests/test_dns_tools.py::test_unknown_resolver_alias_causes_no_network_request`).

Invalid input returns `status="invalid_input"` as a **normal tool
result**, not a raised exception — deliberately, because the rejected
text is untrusted, model-supplied data (#14) and must flow through the
standard model-input safety pipeline
(`mantis.security.make_model_safe()`) like any other tool result. The
runtime's generic last-resort exception path does not apply that
pipeline.

## Security and bounds

`dns_lookup` is registered with `mutating=False` and
`contains_untrusted_text=True` (a resolver's own answer values —
a CNAME/PTR target, an attempt diagnostic message — are external,
Mantis-uncontrolled data, same treatment as every other evidence tool).

**Why resolver selection is alias-only, never an arbitrary
IP/hostname/port**: allowing a caller to name a resolver directly would
turn this tool into a general-purpose network probe against
*whatever endpoint the model chooses* — a fundamentally different,
much larger capability than "query one of a small, operator-approved
set of resolver perspectives." Restricting selection to a fixed,
server-side-configured alias set keeps the tool's entire capability
surface auditable from `mantis.config.DNSConfig` alone, the same
reasoning `mantis.integrations.kubernetes` applies to cluster
credentials and `mantis.integrations.network` applies to *not*
blocklisting private addresses (the boundary here is "which resolvers,"
not "which targets").

Every bound is named and deliberate:

| Bound | Value | Protects against |
|---|---|---|
| `MAX_NAME_CHARS` | 253 | An oversized query name. |
| `MAX_SERVERS_ATTEMPTED` | 4 | Unbounded worst-case latency from a profile with many configured servers. |
| `MAX_ANSWERS_RETURNED` | 20 | A malicious/misbehaving resolver flooding the model with records. |
| `MAX_CNAME_CHAIN_DEPTH` | 8 | An unbounded/pathological CNAME chain in one response. |
| `DEFAULT_DNS_QUERY_TIMEOUT_SECONDS` | 5.0s | An unbounded wait on one slow server (further capped by the caller's remaining `Deadline`). |

`meta.truncated` is truthfully `true` whenever any of the answer/CNAME-
chain/server caps above actually omitted matching data — never silently.

`resolver_addresses` in a result only ever shows the *requested*
alias's own configured servers — never another configured alias's
servers, and nothing about `DNSConfig`'s internal structure beyond that
one profile's own server list (already safe, operator-controlled
provenance, not a secret).

## Non-goals

Deliberately excluded from #109 (candidates for a distinct, later,
separately-scoped issue if ever needed — see "Follow-up" below):

- **`TXT`, `MX`, `SRV`, `NS`, `SOA`, `ANY`, `AXFR` record types.**
  Rejected outright by `validate_record_type`.
- **`dns_compare_views` or any automatic multi-profile fan-out.** One
  call queries one profile; comparing perspectives is an agent decision
  (two explicit calls), never something this tool does on its own.
- **Arbitrary resolver input.** `resolver_alias` only; see "Security and
  bounds".
- **DoH/DoT/mDNS.** Plain UDP/TCP DNS only.
- **Dynamic DNS updates, zone transfers (`AXFR`).** This tool is
  read-only by construction — there is no code path that could mutate a
  zone.
- **Generic network scanning or a generic UDP/TCP packet tool.** DNS
  query/response semantics only, via `dnspython`'s own message/query
  primitives — never a raw packet-construction capability.
- **Agent-specific DNS logic.** All mechanics live in
  `mantis.integrations.dns`/`mantis.tools.dns`, reusable by any current
  or future agent that declares `dns_lookup` in its `ALLOWED_TOOLS` —
  see [docs/agents.md](agents.md).

## Observability

No new telemetry system. Tool invocation, duration, success/failure,
and budget/deadline exhaustion are already visible through the existing
`AgentRuntime` structured logs/metrics (`mantis_tool_call`/
`mantis_tool_calls_total`, labeled by tool name — see
[docs/observability.md](observability.md)) the same way every other
tool's calls are. No DNS-specific structured log event was added; query
names/resolver addresses never appear in a Prometheus label (the only
label involving this tool is the fixed string `tool="dns_lookup"`), and
any attempt `message` logged is already bounded (see
`mantis.integrations.dns.MAX_ATTEMPT_MESSAGE_CHARS`).

## Correlating with other evidence

`dns_lookup` is a **current-state** observation, exactly like
`check_tcp_connectivity` (#8) — never conflate it with #28's historical
AWX evidence. It's also a natural complement to #8: `check_tcp_connectivity`
answers "can Mantis reach this host/port right now," which already
performs its own name resolution internally but reports nothing about
*which resolver* or *what DNS state* led to that address — `dns_lookup`
is what makes the DNS layer itself independently inspectable (e.g. "is
this TCP failure because the name doesn't resolve internally, or because
it resolves to a stale/wrong address?"). A future correlation step or
evaluation scenario combining the two is a natural fit, but is not
implemented as part of #109 (see "Follow-up").

`dns_lookup` is also the first layer of the DNS → TCP → TLS → HTTP
troubleshooting stack described in
[docs/http-probe.md#the-troubleshooting-stack](http-probe.md#the-troubleshooting-stack),
which [`tls_certificate_inspect`](tls-certificate-inspection.md) (#111)
and [`http_probe`](http-probe.md) (#110) extend.

## Follow-up

No follow-up issue is required to satisfy #109 as scoped — every
explicit requirement in the ticket is implemented and tested. A future,
separately-scoped issue could reasonably cover: wiring `dns_lookup` into
an investigation agent's `ALLOWED_TOOLS` (e.g. System Troubleshooter, if
and when DNS evidence becomes relevant to its scenarios), an evaluation
scenario correlating `dns_lookup` with `check_tcp_connectivity` for a
DNS-caused connectivity failure, or (much later, and only if genuinely
needed) `TXT`/`MX` support for a concrete, motivated use case. None of
these are started here.

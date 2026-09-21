# TCP connectivity (`check_tcp_connectivity`)

Mantis's first **current-state** network evidence tool (#8). The core
question it answers is deliberately narrow:

> Can the Mantis runtime establish TCP connectivity to this host/port
> right now?

This is not a general network diagnostic framework. See "Non-goals"
below for what it deliberately does not do.

## Layering

Following the existing Mantis architecture (see
[docs/architecture.md](architecture.md)):

```
src/mantis/integrations/network.py
    DNS resolution + socket connect mechanics, host/port validation,
    the status vocabulary, deadline-aware bounded multi-address loop.

src/mantis/tools/network.py
    Semantic result shaping, provenance (QueryMeta), registry
    registration, untrusted-output handling.
```

No socket mechanics live in an agent; no new reliability abstraction was
created (see "Deadline semantics" below — this reuses #15's `Deadline`
directly).

## Status vocabulary

| Status | Meaning | What it does NOT prove |
|---|---|---|
| `connected` | A TCP handshake succeeded to one resolved address at query time. | Does not prove the application behind the port is healthy — only that TCP itself completed. |
| `dns_failure` | Resolution failed (`socket.gaierror`) or returned no usable address. | Says nothing about the host once a name is known — this is purely a naming failure. |
| `timeout` | A connect attempt did not complete within its socket timeout. | Does not prove firewall filtering — could be many things: a slow/dead path, a host that's down, a filtered port that drops rather than rejects. |
| `connection_refused` | The OS reported `ECONNREFUSED` — something actively rejected the connection. | Does not prove *why* the service is unavailable (crashed, never started, wrong port). |
| `network_unreachable` | The OS reported `ENETUNREACH`. | This is what Mantis's OS observed from its own vantage point — it does not identify which broken router/interface/route is responsible. |
| `host_unreachable` | The OS reported `EHOSTUNREACH`. | Same caveat as `network_unreachable` — a vantage-point observation, not a root-cause diagnosis. |
| `connection_error` | Any other `OSError` during connect. | Catch-all for less common OS-level failures; see the attempt's bounded `message`/`errno` for detail. |
| `budget_exceeded` | The tool/run deadline is why one or more of the bounded candidate addresses were never attempted — either none were tried at all, or the deadline expired after some already failed. | A budget/scheduling outcome, not a network observation, and not a claim that the untried candidates would have failed too — see "Deadline semantics" and "Failure precedence". |
| `invalid_input` | `host`/`port` failed validation before any resolver/socket work. | Not one of the eight statuses above — no network activity was ever attempted. See "Input validation". |

Every status is mapped deterministically from the OS/socket outcome
(`mantis.integrations.network._classify_os_error`) — never inferred
from parsing an error message's text.

## Input validation

`host` and `port` are validated (`mantis.integrations.network.validate_host`
/ `validate_port`) **before** any resolver or socket call:

- `host` must be a plain hostname (RFC-1123-ish: dot-separated
  letter/digit/hyphen labels) or an IPv4/IPv6 literal (validated via
  `ipaddress.ip_address`). Rejected: URLs/schemes (`http://...`),
  path-like values, embedded credentials (`user@host`), whitespace,
  control characters, and anything else outside that allowlist — which
  also excludes shell metacharacters without needing a separate
  denylist for them. `host` is never used to construct a command
  string; nothing in this tool ever shells out.
- `port` must be a plain `int` in `[1, 65535]` — `bool` is explicitly
  rejected even though it's an `int` subclass in Python.

Invalid input returns `status="invalid_input"` as a **normal tool
result**, not a raised exception — deliberately, because the invalid
host text is untrusted, model-supplied data (#14) and must flow through
the standard model-input safety pipeline
(`mantis.security.make_model_safe()`) the same as any other tool
result. The runtime's generic last-resort exception path does not apply
that pipeline, so letting a validation failure propagate as a raised
exception would be a real (if narrow) way for untrusted text to reach
the model unredacted/unbounded — this tool avoids that by construction.

### Private-network / SSRF posture

**Private and internal addresses are always allowed.** Mantis exists to
troubleshoot private infrastructure — homelabs, datacenters, internal
service meshes. A broad "block private/internal IPs" SSRF-style defense
would make this tool useless for its actual purpose, so no such
blocklist exists. Security instead comes from: narrow TCP-connect-only
semantics (no arbitrary fetch/probe capability), input validation
(above), bounded address attempts, the tool allowlist an agent declares,
#15's budgets, and ordinary deployment/network policy (Mantis's own
network access is whatever the environment it runs in grants it — this
tool doesn't expand that).

## IPv4/IPv6 and multi-address behavior

Resolution uses `socket.getaddrinfo()` (via an isolated, test-friendly
seam, `mantis.integrations.network._resolve`) — never assumes IPv4.
Both `AF_INET` and `AF_INET6` results are preserved, in whatever order
the resolver returned them.

- Candidates are deduplicated by `(family, sockaddr)` before attempting
  any of them — a resolver can legitimately return the same address
  more than once.
- At most `MAX_ADDRESSES_ATTEMPTED` (**4**) candidates are attempted,
  regardless of how many a resolver returns — small and named
  deliberately, sized for interactive troubleshooting with local
  models, not exhaustive scanning. `meta.truncated` is `true` when more
  candidates existed than were attempted (either this cap, or the
  deadline, stopped early).
- Candidates are attempted in that bounded, deterministic order.
  **Success on any candidate stops immediately** — the remaining
  candidates are never attempted. The same address is never attempted
  twice; trying several *distinct* resolved addresses for one host is
  not a retry loop (see "Deadline semantics").
- If every attempted candidate fails, all of them are returned as
  bounded per-address evidence (`attempts`), and one deterministic
  overall `status` is derived — see "Failure precedence" below.

### Failure precedence

When multiple attempted addresses fail with *different* classifications,
the overall `status` is chosen by a fixed precedence order — most
diagnostically specific first — never by which address happened to be
attempted last:

```
host_unreachable > network_unreachable > connection_refused > timeout > connection_error
```

(`mantis.integrations.network._STATUS_PRECEDENCE`, applied by
`_classify_overall()`.) For example: an IPv6 candidate failing with
`ENETUNREACH` followed by an IPv4 candidate failing with `ECONNREFUSED`
always reports `connection_refused` overall — regardless of attempt
order — because it's ranked more specific than `network_unreachable`.
Every per-address observation is still preserved in `attempts`; `status`
is explicitly a normalized *summary*, not a replacement for the raw
evidence. See `tests/test_network.py`'s
`test_overall_status_precedence_is_deterministic_regardless_of_order`
for the parametrized proof (both orderings of every pair, same result).

**`budget_exceeded` overrides this precedence entirely** when the
deadline — not simply running out of candidates — is why the remaining
bounded candidates were never tried. For example: address 1 observes
`connection_refused`, the deadline then expires, and address 2 (still
within `MAX_ADDRESSES_ATTEMPTED`) is never attempted. The overall
`status` is `budget_exceeded`, not `connection_refused` — even though
`connection_refused` outranks every other failure in the table above —
because the probe did not finish evaluating the bounded candidate set;
a later, untried candidate might have connected. Reporting
`connection_refused` here would overstate what was actually observed.
The `connection_refused` observation on address 1 is still preserved in
`attempts`, and `truncated` is `true`. This is deliberately distinct
from exhausting every candidate without deadline pressure, or hitting
`MAX_ADDRESSES_ATTEMPTED` (a deliberate bound, not a budget failure) —
both of those still use the precedence-derived status normally. See
`tests/test_network.py`'s
`test_deadline_exhausted_after_a_failed_attempt_reports_budget_exceeded_not_the_failure`
and `test_deadline_stopping_remaining_candidates_takes_precedence_over_higher_ranked_failures`.

## Deadline semantics (#15)

A TCP probe is a current-state **observation**, not an idempotent HTTP
API read — so it is deliberately never wrapped in
`mantis.reliability.retry_call()`, and the same endpoint is never
retried automatically. Attempting several *distinct* resolved addresses
is not a retry loop (see above).

- `Deadline` is checked before resolution starts, and again before
  every individual connect attempt.
- Each attempt's own socket timeout is
  `min(DEFAULT_CONNECT_TIMEOUT_SECONDS, deadline.remaining())` — a
  single slow candidate can never itself exceed the caller's remaining
  budget. `DEFAULT_CONNECT_TIMEOUT_SECONDS` (5.0s) applies as-is when no
  deadline is given (e.g. a direct/manual call).
- If the deadline is already exhausted before the first attempt can
  start, the result is `status="budget_exceeded"` with zero attempts —
  never phrased as a network fact. If the deadline instead expires
  *after* one or more candidates already failed, `status` is still
  `budget_exceeded` (not the failure those candidates observed) — see
  "Failure precedence" above for why.
- Latency/duration is measured with monotonic time
  (`time.monotonic`, injectable for deterministic tests).

There is deliberately **no model-facing timeout argument** — every
socket timeout is derived from the shared per-tool/run deadline
(`AgentRuntime` injects it via the same `_deadline` keyword-only
convention as the AWX tools), never something a model can set directly.

### Honest limitation: synchronous DNS resolution

`socket.getaddrinfo()` is synchronous and exposes no portable per-call
timeout. Mantis does **not** claim a hard DNS timeout it cannot deliver:
the deadline is checked before and after resolution, not during, and a
slow resolver can still make one `check_tcp_connectivity` call take
longer than the nominal remaining budget would suggest. A thread pool
solely to manufacture an artificial hard DNS timeout was deliberately
not introduced — that's real complexity for a narrow benefit, and this
page states the limitation plainly instead of overclaiming preemption
the implementation doesn't actually have (the same honesty principle
[docs/reliability.md](reliability.md) applies to Python's inability to
forcibly interrupt in-flight synchronous work generally).

## Result shape

```json
{
  "meta": {
    "source_system": "network",
    "query_time": "2026-09-17T16:00:33.261628+00:00",
    "observation_time": "2026-09-17T16:00:33.261628+00:00",
    "query_window": null,
    "truncated": false,
    "derived_fields": ["status"],
    "contract_version": "1.0"
  },
  "target": {
    "host": "ferros-c01",
    "port": 22
  },
  "status": "connected",
  "connected": true,
  "resolved_address": "172.30.4.12",
  "address_family": "ipv4",
  "latency_ms": 0.001,
  "attempts": [
    {
      "address": "172.30.4.12",
      "address_family": "ipv4",
      "status": "connected",
      "errno": null,
      "latency_ms": 0.001,
      "message": null
    }
  ]
}
```

(Real, verified output — generated by running `check_tcp_connectivity`
against a mocked resolver/connect pair with the shapes above; not a
hand-written mockup.)

Adopts the same `mantis.contracts.QueryMeta` contract as the AWX tools:
`meta.source_system` is always `"network"`; `meta.observation_time` is
populated (unlike the AWX tools' job-list result, this call is a single
point-in-time observation, so `QueryMeta`'s guidance to set
`observation_time` for single-observation tools applies directly —
here it's set equal to `query_time`, since the observation happens
synchronously as part of the query itself); `meta.derived_fields` names
`status` as Mantis's own normalized summary derived from the raw
per-address `attempts` (see "Failure precedence") — every other field
is either the original request (`target`) or a direct pass-through of
one successful attempt's own data.

## Attempt evidence

Each entry in `attempts` (`mantis.integrations.network.AddressAttempt`)
is bounded and structured: `address`, `address_family` (`"ipv4"` /
`"ipv6"`), `status`, `errno` (the raw OS error number when one was
reported, `null` otherwise), `latency_ms`, and a `message` bounded to
`MAX_ATTEMPT_MESSAGE_CHARS` (200 characters) — never a raw exception
repr or other opaque implementation detail. Host/IP/port values never
appear in a Prometheus label (see "Observability").

## Mantis's network vantage point

Every result is implicitly scoped to **where Mantis's own runtime is
running** — not the target's own perspective, not any other host on the
network. `connected` means a handshake succeeded from that one vantage
point at that one instant; a failure means the same vantage point
observed a specific OS-level outcome at that instant. Neither proves
anything about reachability from anywhere else, at any other time, or
about the target application's own health.

## Non-goals

Deliberately excluded (see issue #8's guardrails):

- **ICMP/ping.** A different protocol with different semantics (host
  reachability, not port-level TCP reachability) — out of scope, not
  planned as a follow-on to this tool.
- **UDP.** TCP-only, by design.
- **Traceroute, port scanning/ranges, nmap, `nc`/`telnet`.** All would
  turn this from a narrow, auditable connectivity check into a general
  network probing capability — explicitly rejected.
- **Subprocesses/shell commands.** Everything here is stdlib `socket`
  calls; nothing is ever shelled out, and `validate_host` never
  constructs a command string.
- **Banner grabbing, HTTP/TLS/application-level health checks.** A
  successful TCP connect says nothing about the application behind the
  port — checking further would silently expand this tool's actual
  capability well past "can Mantis reach this port."
- **A network-specific retry framework or persistent monitoring.** See
  "Deadline semantics" — a TCP probe is a one-shot current-state
  observation, not a monitored, retried, or scheduled check.
- Prometheus (#9), Loki (#10), and System Troubleshooter (#11) are
  separate, out-of-scope tracks — this issue adds one tool to the
  shared registry, reusable by a future multi-tool agent, not a new
  agent itself.

## Observability

No new telemetry system. Tool invocation, duration, success/failure,
and budget/deadline exhaustion are already visible through the existing
`AgentRuntime` structured logs/metrics
(`mantis_tool_call`/`mantis_tool_calls_total`, labeled by tool name —
see [docs/observability.md](observability.md)) the same way every other
tool's calls are. No network-specific structured log event was added;
host/IP/port never appear in a Prometheus label (the only label
involving this tool is the fixed string `tool="check_tcp_connectivity"`),
and any attempt `message` logged is already bounded (see "Attempt
evidence").

## Correlating with DNS evidence (#109)

`check_tcp_connectivity` resolves a name internally (via
`socket.getaddrinfo()`) as a means to an end — connecting — and reports
nothing about *which resolver* or *what DNS state* produced the address
it tried. [`dns_lookup`](dns-lookup.md) (#109) is what makes the DNS
layer independently inspectable, from a specific, chosen resolver
perspective: "does this name resolve internally, and does it resolve to
the address I expect?" is a distinct question from "can Mantis reach
that address over TCP right now?" — a TCP failure and a DNS answer for
the same name are two different kinds of evidence, gathered by two
separate tool calls; never assume one explains the other without
checking.

## Correlating with TLS and HTTP evidence (#111, #110)

`check_tcp_connectivity` proves nothing about what happens *after* a TCP
connection is established — a successful TCP connect to port 443 says
nothing about whether a TLS handshake against it would succeed, or what
an HTTP endpoint behind it would return.
[`tls_certificate_inspect`](tls-certificate-inspection.md) (#111) and
[`http_probe`](http-probe.md) (#110) are the tools that make those next
two layers independently inspectable. See
[docs/http-probe.md#the-troubleshooting-stack](http-probe.md#the-troubleshooting-stack)
for the full DNS → TCP → TLS → HTTP layering this composes into.

## Correlating with historical AWX evidence (#28)

`check_tcp_connectivity` (current state) and `awx_get_job_failure`
(#28, historical state) are deliberately different kinds of evidence —
never conflate them. The `network-historical-failure-current-success`
and `network-historical-and-current-failure` evaluation scenarios
(`mantis.eval.fixtures.network`) exercise exactly this: AWX historically
recorded a `runner_on_unreachable` failure reaching `ferros-c01:22`
("No route to host"), and a current `check_tcp_connectivity` probe to
the same host/port either now succeeds or still fails. Golden behavior
in both cases:

- Explicitly distinguishes "AWX observed a reachability failure at that
  time" from "TCP/22 is reachable from Mantis now" (or still isn't).
- Never claims the historical failure was false just because a later
  check succeeded — both observations can be true at their own point in
  time.
- Never claims a problem is fixed *everywhere* from one current check
  at one vantage point.
- Never asserts a specific cause (e.g. "the firewall is definitely
  blocking it") with unsupported certainty, even when historical and
  current evidence agree.

See `tests/eval/test_network_scenarios.py` for the deterministic good/
bad-answer scoring tests, and [docs/evaluation.md](evaluation.md) for
how to run a scenario against a live model.

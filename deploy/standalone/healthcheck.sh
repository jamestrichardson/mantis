#!/bin/bash
# Lightweight /readyz probe for `mantis serve`'s Docker healthcheck (#97).
#
# The previous healthcheck spawned Python and imported urllib.request on
# every single probe. On a slow/constrained host, interpreter startup and
# import machinery alone measured ~1.4s-5.8s -- enough to blow past the
# healthcheck's own configured `timeout`, so a genuinely ready service
# (/readyz already returning 200) still got marked unhealthy purely from
# probe-process overhead, not application state (see #97's degobah 1.7.0
# rollout report).
#
# This script makes one raw HTTP/1.0 request over bash's own `/dev/tcp`
# pseudo-device -- no new process, no Python startup, no DNS/HTTP-library
# overhead, and no new package in the runtime image (bash is already
# present). It still probes the real /readyz contract end-to-end (TCP
# connect + HTTP status line), never a weaker liveness-only substitute,
# and never prints response headers/body -- only its own fixed,
# credential-free diagnostic text on failure.
set -euo pipefail

url="${MANTIS_HEALTH_URL:-http://127.0.0.1:8080/readyz}"

# Bounds the blocking read below (the realistic hang case: the TCP
# connection is accepted but the server is slow/blocked before writing a
# response -- exactly the scenario #95's shutdown-under-load work
# exercises). The target is always the loopback interface in the shipped
# compose.yaml, whose connect() is synchronous and never hangs the way a
# real network path could; this timeout is what makes the read itself
# bounded regardless.
probe_timeout="${MANTIS_HEALTHCHECK_PROBE_TIMEOUT_SECONDS:-3}"

case "$url" in
  http://*) rest="${url#http://}" ;;
  *)
    echo "mantis-healthcheck: unsupported MANTIS_HEALTH_URL scheme: $url" >&2
    exit 1
    ;;
esac

hostport="${rest%%/*}"
case "$rest" in
  */*) path="/${rest#*/}" ;;
  *) path="/" ;;
esac
host="${hostport%%:*}"
if [[ "$hostport" == *:* ]]; then
  port="${hostport##*:}"
else
  port=80
fi

# /dev/tcp is a bash builtin -- opening it is the TCP connect itself, so
# a refused/unreachable connection fails here, before any HTTP is sent.
exec 3<>"/dev/tcp/${host}/${port}"

printf 'GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n' "$path" "$host" >&3

# shellcheck disable=SC2034  # reason is parsed to isolate proto/code, never used itself
if ! IFS=$' \t\r\n' read -r -t "$probe_timeout" proto code reason <&3; then
  echo "mantis-healthcheck: no response within ${probe_timeout}s" >&2
  exit 1
fi

if [[ "$proto" != HTTP/* || ! "$code" =~ ^[0-9]{3}$ ]]; then
  echo "mantis-healthcheck: malformed response status line" >&2
  exit 1
fi

case "$code" in
  2??) exit 0 ;;
  *)
    echo "mantis-healthcheck: not ready (HTTP ${code})" >&2
    exit 1
    ;;
esac

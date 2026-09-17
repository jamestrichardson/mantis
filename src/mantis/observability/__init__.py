"""Observability: structured JSON logging and Prometheus metrics.

Two independent, collector-agnostic surfaces (see ``docs/observability.md``):

- :mod:`mantis.observability.logging` — newline-delimited JSON events to
  stdout/stderr, for a host-side collector (Grafana Alloy) to ship to
  Loki. Mantis never talks to Loki directly.
- :mod:`mantis.observability.metrics` — a Prometheus ``/metrics`` HTTP
  endpoint. Mantis never pushes to Prometheus.

Both are wired into :class:`mantis.runtime.AgentRuntime` and
``mantis.eval.runner`` directly, so evaluation runs and production agent
runs share the same event schema, metric names, and registry — nothing
eval-specific is a parallel implementation.
"""

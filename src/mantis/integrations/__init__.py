"""Raw client implementations for external systems.

Integration code knows how to authenticate and communicate with a specific
external API (AWX, Prometheus, Loki, Kubernetes, ...). It must not contain
agent-specific behavior or LLM-facing concerns — that belongs in
``mantis.tools``.
"""

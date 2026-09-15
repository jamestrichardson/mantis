"""Mantis agents.

An agent is a thin definition: a system prompt, an allowed-tool list, and
model configuration, run through the shared :class:`mantis.runtime.AgentRuntime`.
Agents must not reimplement the model/tool loop or duplicate tool logic
that belongs in ``mantis.tools``.
"""

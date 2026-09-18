"""The Mantis HTTP API service (#21/#83).

This package hosts the persistent FastAPI application that is the single
supported execution boundary for the official CLI and future portal/MCP
clients (see ``docs/api.md`` and ``docs/architecture.md``):

::

    CLI / Portal / MCP  ->  FastAPI app (mantis.api.app)
                                |
                                v
                         AgentCatalog (mantis.api.catalog)
                                |
                                v
                      InvocationService (mantis.api.invocation)
                                |
                                v
                    the real mantis.runtime.AgentRuntime

Nothing in this package duplicates agent prompts, tool lists, or runtime
budgets — it only orchestrates the existing, already-shipped agent
modules under ``mantis.agents``. See ``mantis.api.catalog`` for how an
agent becomes invokable through the API, and ``mantis.api.invocation``
for the one shared invocation path every route uses.
"""

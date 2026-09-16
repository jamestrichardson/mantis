"""Model qualification / agent evaluation harness (Track 1, #34).

This package is the execution path: run a fixture-backed scenario through
one or more LiteLLM model aliases via the real ``AgentRuntime``, and record
what happened as a machine-readable result. It deliberately does not (yet)
implement subjective/LLM-as-judge scoring, a dashboard, or a database — see
``docs/evaluation.md`` and issue #34 for scope.

Importing this package registers every built-in scenario into
``mantis.eval.scenarios.default_scenarios`` (mirroring how
``mantis.tools`` registers into the shared tool registry), but never
touches ``mantis.registry.default_registry`` itself — scenarios build
their own scoped, fixture-backed ``ToolRegistry`` per run so evaluation
never depends on the shared process-wide registry or any live external
system.
"""

from mantis.eval import fixtures as _fixtures  # noqa: F401  (registers scenarios)

"""Fixture-backed tool implementations for evaluation scenarios.

Each module here builds a scenario-scoped :class:`~mantis.registry.Tool`
that reuses the *real* production tool function (e.g.
``mantis.tools.awx.awx_recent_failed_jobs``) with its integration client
swapped for a fixture — so a scenario exercises real preprocessing/contract
logic against canned data, not a hand-faked shortcut of it.

Importing this package registers every built-in scenario into
``mantis.eval.scenarios.default_scenarios`` as a side effect, mirroring
``mantis.tools``'s registration pattern.
"""

from mantis.eval.fixtures import awx as _awx  # noqa: F401  (registers scenarios)
from mantis.eval.fixtures import network as _network  # noqa: F401  (registers scenarios)

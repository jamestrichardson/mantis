"""Semantic, LLM-facing operations built on top of ``mantis.integrations``.

Tools translate a raw integration into something an agent can call and
reason about: a narrow function signature, a JSON-serializable, already
preprocessed result, and an OpenAI-compatible schema registered in
``mantis.registry``.

Importing a tool module registers its tools as a side effect, via
``mantis.registry.default_registry``.
"""

from mantis.tools import awx as _awx  # noqa: F401  (registers AWX tools)
from mantis.tools import dns as _dns  # noqa: F401  (registers DNS tools)
from mantis.tools import kubernetes as _kubernetes  # noqa: F401  (registers Kubernetes tools)
from mantis.tools import loki as _loki  # noqa: F401  (registers Loki tools)
from mantis.tools import network as _network  # noqa: F401  (registers network tools)
from mantis.tools import prometheus as _prometheus  # noqa: F401  (registers Prometheus tools)

"""Semantic, LLM-facing operations built on top of ``mantis.integrations``.

Tools translate a raw integration into something an agent can call and
reason about: a narrow function signature, a JSON-serializable, already
preprocessed result, and an OpenAI-compatible schema registered in
``mantis.registry``.

Importing a tool module registers its tools as a side effect, via
``mantis.registry.default_registry``.
"""

from mantis.tools import awx as _awx  # noqa: F401  (registers AWX tools)

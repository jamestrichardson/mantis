"""Environment-driven configuration for Mantis.

Configuration is read entirely from process environment variables. Nothing
here should hardcode credentials, and no module outside this file should
call ``os.environ`` directly for Mantis settings.

Local development environment files
------------------------------------

In a real deployment (EKS/GKE/ECS/plain Docker, ...) environment variables
are injected by the platform itself — there are no ``.env`` files, and
none are needed. For local development, Mantis supports a
``.env.<name>``-style file convention (see ``docs/configuration.md``):

- ``MANTIS_ENV`` selects which local environment to load (default:
  ``"local"``).
- Files are loaded, most specific first, with ``override=False`` — so a
  real environment variable already set by the platform/shell always wins
  over anything in a file, and a more specific file wins over a less
  specific one:

  1. ``.env.<MANTIS_ENV>.local`` — personal, machine-specific overrides.
  2. ``.env.<MANTIS_ENV>`` — the environment's own settings (e.g.
     ``.env.local`` for day-to-day local dev).
  3. ``.env.local`` — legacy/catch-all personal overrides.
  4. ``.env`` — shared, environment-agnostic defaults.

Only ``*.example`` files are committed to git; real ``.env*`` files are
git-ignored and must never contain checked-in credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MANTIS_ENV = "local"
DEFAULT_LITELLM_MODEL = "qwen3-opencode:latest"


def _load_env_files() -> None:
    env_name = os.environ.get("MANTIS_ENV", DEFAULT_MANTIS_ENV)
    candidates = [
        f".env.{env_name}.local",
        f".env.{env_name}",
        ".env.local",
        ".env",
    ]
    # De-duplicate while preserving precedence order (e.g. MANTIS_ENV=local
    # would otherwise load ".env.local" twice).
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate)
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)


_load_env_files()


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        # Deliberately names only the variable, never a value — this is
        # the only piece of information ConfigurationError ever carries,
        # so it can never leak a partially-set secret in exception text.
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _require_secret(name: str) -> "Secret":
    return Secret(_require(name))


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Secret:
    """Wraps a credential so it can't be accidentally logged or leaked.

    ``repr()``/``str()`` (and therefore f-strings, ``logging`` calls, and a
    dataclass's default ``__repr__``) never expose the wrapped value — only
    :meth:`get_secret_value` does, and callers should reach for it only at
    the point the raw credential is actually needed (e.g. building an
    ``Authorization`` header), never store or pass around the unwrapped
    string beyond that.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class LiteLLMConfig:
    """Connection settings for the LiteLLM (OpenAI-compatible) gateway."""

    url: str
    api_key: Secret
    model: str

    @classmethod
    def from_env(cls) -> "LiteLLMConfig":
        return cls(
            url=_require("LITELLM_URL"),
            api_key=_require_secret("LITELLM_API_KEY"),
            model=os.environ.get("LITELLM_MODEL") or DEFAULT_LITELLM_MODEL,
        )


@dataclass(frozen=True)
class AWXConfig:
    """Connection settings for AWX."""

    url: str
    token: Secret
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> "AWXConfig":
        return cls(
            url=_require("AWX_URL").rstrip("/"),
            token=_require_secret("AWX_TOKEN"),
            verify_ssl=_getenv_bool("AWX_VERIFY_SSL", True),
        )

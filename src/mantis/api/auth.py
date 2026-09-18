"""API authentication (#83): a bearer-token security boundary for the
Mantis API, entirely separate from server-side LiteLLM/AWX/Kubernetes/
Prometheus/Loki credentials.

``/healthz``/``/readyz`` are deliberately unauthenticated (container/
orchestrator health checks should not need a credential) — see
``mantis.api.app`` for where this dependency is, and is not, wired in.
"""

from __future__ import annotations

import logging
from typing import Callable, Coroutine

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mantis.config import ApiServerConfig

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False, description="Mantis API bearer token")


def _unauthorized() -> HTTPException:
    # `detail` is reshaped into the standard {"error": {...}} envelope by
    # mantis.api.app's HTTPException handler -- kept flat here (not
    # pre-wrapped) so that handler has one consistent shape to expect
    # from every HTTPException raised anywhere in the app.
    return HTTPException(
        status_code=401,
        detail={"type": "unauthenticated", "message": "A valid bearer token is required.", "run_id": None},
        headers={"WWW-Authenticate": "Bearer"},
    )


def build_auth_dependency(config: ApiServerConfig) -> Callable[..., Coroutine[None, None, None]]:
    """Build the FastAPI dependency callable enforcing
    :class:`~mantis.config.ApiServerConfig`'s auth mode.

    Built once per app (see ``mantis.api.app.create_app``) from the
    resolved config — never re-read from the environment per request.

    ``auth_mode="disabled"``: the returned dependency is a real, still
    request-scoped no-op callable (so protected routes' dependency
    signature doesn't change between modes) — already logged once,
    loudly, at startup below, never silently.

    ``auth_mode="bearer_token"``: every request must present
    ``Authorization: Bearer <token>`` matching the configured secret
    exactly; anything else is a stable 401 (missing header, wrong
    scheme, and wrong token are not distinguished in the response, to
    avoid leaking which part of the credential was wrong).
    """
    if config.auth_mode == "disabled":
        logger.warning(
            "mantis_api_auth_disabled: MANTIS_API_AUTH_MODE=disabled -- the API is "
            "unauthenticated. This is a development-only mode; never use it in production."
        )

        async def _disabled_auth() -> None:
            return None

        return _disabled_auth

    expected = config.bearer_token.get_secret_value() if config.bearer_token else None

    async def _bearer_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> None:
        if credentials is None or expected is None:
            raise _unauthorized()
        # Plain equality, not constant-time comparison: this guards one
        # shared operator/service token, not many independent per-user
        # secrets, matching every other Mantis credential-comparison
        # convention (see mantis.config.Secret.__eq__).
        if credentials.credentials != expected:
            raise _unauthorized()

    return _bearer_auth

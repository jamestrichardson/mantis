"""Raw AWX API client.

This module knows how to authenticate to and talk to the AWX HTTP API. It
has no knowledge of agents, LLMs, or tool schemas — see
``mantis.tools.awx`` for the semantic, LLM-facing layer built on top of
this client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from mantis.config import AWXConfig

logger = logging.getLogger(__name__)

# AWX returns a short informational payload instead of full stdout when the
# output is too large to display inline, and expects callers to re-request
# with format=txt_download instead. We detect that case by looking for this
# phrase (AWX's own wording) in the truncated response.
_STDOUT_TOO_LARGE_MARKER = "download"


class AWXError(RuntimeError):
    """Raised when the AWX API returns an unexpected error response."""


class AWXStdoutError(RuntimeError):
    """Raised when job stdout specifically cannot be retrieved.

    This is intentionally a distinct exception type from :class:`AWXError`
    so callers (tools) can represent a failure to *fetch* stdout separately
    from a failure *of the underlying AWX job itself*.
    """


@dataclass
class AWXClient:
    """Thin HTTP client for the AWX v2 API.

    All methods return parsed JSON (``dict``/``list``) or raw text, and
    raise :class:`AWXError` / :class:`AWXStdoutError` on failure. This
    client performs no business logic beyond talking to the API.
    """

    config: AWXConfig
    timeout: float = 30.0

    def _client(self, accept: str) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.url,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": accept,
            },
            verify=self.config.verify_ssl,
            timeout=self.timeout,
        )

    def list_jobs(
        self,
        *,
        status: str | None = None,
        order_by: str | None = None,
        page_size: int = 10,
    ) -> list[dict[str, Any]]:
        """Return job records from ``/api/v2/jobs/``.

        Args:
            status: Filter by AWX job status (e.g. "failed").
            order_by: AWX ``order_by`` value (e.g. "-finished").
            page_size: Maximum number of jobs to request from AWX.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if status is not None:
            params["status"] = status
        if order_by is not None:
            params["order_by"] = order_by

        try:
            with self._client(accept="application/json") as client:
                response = client.get("/api/v2/jobs/", params=params)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AWXError(f"Failed to list AWX jobs: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise AWXError(f"AWX returned non-JSON response listing jobs: {exc}") from exc

        return payload.get("results", [])

    def get_job(self, job_id: int) -> dict[str, Any]:
        """Return the full job detail record for ``job_id``."""
        try:
            with self._client(accept="application/json") as client:
                response = client.get(f"/api/v2/jobs/{job_id}/")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AWXError(f"Failed to fetch AWX job {job_id}: {exc}") from exc

        try:
            return response.json()
        except ValueError as exc:
            raise AWXError(f"AWX returned non-JSON response for job {job_id}: {exc}") from exc

    def get_job_stdout(self, job_id: int) -> str:
        """Return the plain-text stdout for ``job_id``.

        AWX may respond to a normal ``format=txt`` request with a short
        message saying the output is too large to display and that the
        download endpoint should be used instead. This method detects that
        case and transparently retries with ``format=txt_download``.

        Raises :class:`AWXStdoutError` (never :class:`AWXError`) on
        failure, so callers can distinguish "we couldn't retrieve stdout"
        from "the AWX job itself failed."
        """
        text = self._fetch_stdout(job_id, fmt="txt")

        if self._looks_truncated(text):
            logger.info(
                "AWX stdout for job %s appears too large for inline display; "
                "retrying with txt_download",
                job_id,
            )
            text = self._fetch_stdout(job_id, fmt="txt_download")

        return text

    def _fetch_stdout(self, job_id: int, *, fmt: str) -> str:
        try:
            with self._client(accept="text/plain") as client:
                response = client.get(
                    f"/api/v2/jobs/{job_id}/stdout/",
                    params={"format": fmt},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AWXStdoutError(
                f"Failed to retrieve stdout for AWX job {job_id} (format={fmt}): {exc}"
            ) from exc

        return response.text

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        """Heuristic: does this response look like AWX's "too large" notice?

        AWX's inline stdout endpoint returns a short body pointing at the
        download endpoint when the real output exceeds its display limit.
        A genuine (even large) stdout dump is expected to be much longer
        than this notice, so we key off both length and the presence of
        the marker text rather than length alone.
        """
        if len(text) > 4096:
            return False
        return _STDOUT_TOO_LARGE_MARKER in text.lower()

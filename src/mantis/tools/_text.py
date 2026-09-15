"""Text preprocessing helpers for large operational output.

These helpers are deliberately generic (not AWX-specific in mechanism) so
future tools with similarly large output (e.g. Kubernetes events, systemd
logs) can reuse them. AWX-specific *marker words* live in
``mantis.tools.awx``.

The goal is to never hand multi-megabyte raw output to an LLM: instead we
produce a small, high-signal excerpt plus a bounded tail of the raw text.
"""

from __future__ import annotations

STDOUT_TAIL_CHARS = 12_000
TRUNCATION_NOTICE = "[... earlier output omitted ...]"


def tail(text: str, max_chars: int = STDOUT_TAIL_CHARS) -> str:
    """Return the last ``max_chars`` characters of ``text``.

    If truncation occurred, a leading notice line is added so the caller
    (and ultimately the model) is never misled into thinking this is the
    complete output.
    """
    if len(text) <= max_chars:
        return text
    return f"{TRUNCATION_NOTICE}\n{text[-max_chars:]}"


def extract_excerpt(
    text: str,
    markers: list[str],
    *,
    max_lines: int = 60,
    context_before: int = 1,
    context_after: int = 1,
) -> str:
    """Extract the most recent, high-value lines from ``text``.

    A line is considered "high value" if it contains any of ``markers``
    (case-sensitive, matching the conventional casing of Ansible/AWX output
    such as ``FAILED!`` or ``fatal:``). Matching lines are kept along with
    a small window of surrounding context, working from the *end* of the
    output backwards until ``max_lines`` is reached, then re-assembled in
    original order.

    Returns an empty string if no marker lines are found.
    """
    if not text:
        return ""

    lines = text.splitlines()
    matched_indexes: set[int] = set()

    for idx, line in enumerate(lines):
        if any(marker in line for marker in markers):
            start = max(0, idx - context_before)
            end = min(len(lines), idx + context_after + 1)
            matched_indexes.update(range(start, end))

    if not matched_indexes:
        return ""

    # Keep the most recent (highest-index) matches first, capped at
    # max_lines, then restore chronological order for readability.
    ordered = sorted(matched_indexes)
    if len(ordered) > max_lines:
        ordered = ordered[-max_lines:]

    excerpt_lines: list[str] = []
    previous_idx: int | None = None
    for idx in ordered:
        if previous_idx is not None and idx != previous_idx + 1:
            excerpt_lines.append("...")
        excerpt_lines.append(lines[idx])
        previous_idx = idx

    return "\n".join(excerpt_lines)

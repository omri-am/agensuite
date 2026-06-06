"""Pure render layer for human-facing debate digests.

No I/O, no state mutation: builders take domain objects and return rendered
markdown strings. The CLI owns stdout-print and file-append, preserving the
"mutations only via CLI" invariant. ANSI coloring is applied by the CLI via
``colorize`` and is never baked into these strings, so ``debate-log.md``
stays free of escape codes.
"""

from __future__ import annotations

import os

from .models import PRStatus, PullRequest, ReviewComment, Verdict

VERDICT_GLYPH: dict[Verdict, str] = {
    Verdict.APPROVE: "✅",
    Verdict.REQUEST_CHANGES: "🔴",
    Verdict.COMMENT: "💬",
}

STATUS_GLYPH: dict[PRStatus, str] = {
    PRStatus.MERGED: "🟢",
    PRStatus.REJECTED: "❌",
    PRStatus.DEADLOCKED: "⚔️",
    PRStatus.UNDER_REVIEW: "👀",
    PRStatus.CHANGES_REQUESTED: "🟡",
    PRStatus.OPEN: "📝",
}


def meter(approvals: int, quorum: int) -> str:
    """Approval meter: filled pips for approvals, empty for the remainder to quorum."""
    filled = "●" * approvals
    empty = "○" * max(0, quorum - approvals)
    return filled + empty


def truncate(text: str, limit: int = 80) -> str:
    """One-line clip. Collapses internal whitespace, ellipsizes past ``limit``."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"

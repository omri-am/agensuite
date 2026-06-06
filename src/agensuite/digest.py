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


def _claim(pr: PullRequest) -> str:
    """The PR's product claim, falling back to its title."""
    return pr.headline.strip() or pr.title.strip()


def render_verdict_line(
    *, pr: PullRequest, comment: ReviewComment, round_idx: int, quorum: int
) -> str:
    """One-line VERDICT digest. Kept short so per-turn scrollback stays readable."""
    glyph = VERDICT_GLYPH.get(comment.verdict, "💬")
    crit = truncate(comment.comment, 70)
    m = meter(len(pr.reviews), quorum)
    parts = [
        f'{glyph} r{round_idx} · {comment.reviewer} → {pr.id} ({_claim(pr)})',
        f'"{crit}"',
        m,
    ]
    open_reqs = pr.open_change_requests
    if open_reqs:
        parts.append(f"· open: {', '.join(open_reqs)}")
    return "  ".join(parts)


def render_pr_terminal(*, pr: PullRequest, quorum: int) -> str:
    """One-line PR_TERMINAL digest for a resolved PR."""
    glyph = STATUS_GLYPH.get(pr.status, "•")
    m = meter(pr.approval_count, quorum)
    tail = f"{m} {pr.approval_count}/{quorum}"
    if pr.status == PRStatus.DEADLOCKED:
        tail = "stood at FOLLOWUP"
    elif pr.status == PRStatus.REJECTED:
        tail = pr.conflict_details or "rejected"
    return f"{glyph} {pr.id} {pr.status.value} {_claim(pr)}  {tail}"

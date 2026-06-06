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
    m = meter(pr.approval_count, quorum)
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


_COL = 36  # left-column width; cells are plain text so padding is width-safe


def _locked_cells(prs: list[PullRequest]) -> list[str]:
    return [f"• {_claim(pr)}" for pr in prs if pr.status == PRStatus.MERGED]


def _contested_cells(prs: list[PullRequest], quorum: int) -> list[str]:
    cells: list[str] = []
    for pr in prs:
        if pr.open_change_requests or pr.status == PRStatus.DEADLOCKED:
            counters = [r.counter.strip() for r in pr.reviews
                        if r.verdict == Verdict.REQUEST_CHANGES and r.counter.strip()]
            opt_b = counters[-1] if counters else "change requested"
            lean = "leaning B" if pr.approval_count < quorum else "leaning A"
            cells.append(f"• {_claim(pr)}")
            cells.append(f"    A: {_claim(pr)}  B: {opt_b}")
            cells.append(f"    {lean} · open: {', '.join(pr.open_change_requests) or '—'}")
        elif pr.status == PRStatus.REJECTED:
            cells.append(f"• {_claim(pr)} — rejected")
        elif pr.status in (PRStatus.OPEN, PRStatus.UNDER_REVIEW):
            cells.append(f"• {_claim(pr)} — unset")
    return cells


def render_ledger(
    prs: list[PullRequest], *, quorum: int, round_label: str | None = None
) -> str:
    """Two-column decision ledger: LOCKED decisions vs CONTESTED tradeoffs.

    Data cells are plain text so fixed-width padding is width-safe across
    terminals. Only the two section headers (✅ LOCKED / ❓ CONTESTED) carry
    emoji, and those sit on their own header rows so they never disrupt
    data-cell alignment.
    """
    left = ["✅ LOCKED", "─" * 14, *_locked_cells(prs)]
    right = ["❓ CONTESTED", "─" * 16, *_contested_cells(prs, quorum)]
    rows = max(len(left), len(right))
    lines: list[str] = []
    if round_label:
        lines.append(f"── {round_label} ──")
    for i in range(rows):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        lines.append(f"{l:<{_COL}}{r}".rstrip())
    return "\n".join(lines)


# Verdict/status glyph → ANSI color. Applied line-wide based on the leading glyph.
_ANSI = {
    "🔴": "\x1b[31m", "❌": "\x1b[31m", "⚔️": "\x1b[31m",   # red: blocked/conflict
    "🟢": "\x1b[32m", "✅": "\x1b[32m",                      # green: merged/approve
    "🟡": "\x1b[33m", "👀": "\x1b[33m",                      # yellow: in-progress
}


def colorize(text: str, *, tty: bool) -> str:
    """Wrap ``text`` in an ANSI color chosen by its leading glyph.

    No-op unless ``tty`` is True and ``NO_COLOR`` is unset (the informal
    https://no-color.org convention). Never called on text destined for the
    log file, so escape codes never reach ``debate-log.md``.
    """
    if not tty or os.environ.get("NO_COLOR"):
        return text
    for glyph, code in _ANSI.items():
        if text.lstrip().startswith(glyph):
            return f"{code}{text}\x1b[0m"
    return text

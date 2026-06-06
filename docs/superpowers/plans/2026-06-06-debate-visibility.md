# Debate Visibility — Decision Ledger Debriefs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a human-readable decision ledger (LOCKED vs CONTESTED) to the orchestrator terminal and a durable `state/debate-log.md` on every material debate state-change, plus a CEO prose debrief hook at the human-gate.

**Architecture:** A new pure-render module `digest.py` turns domain objects into markdown blocks (no I/O, no ANSI baked in). The CLI commands that already mutate state (`pr comment`, `pr merge`, `next-turn`, `human-gate`) gain an auto-emit side effect via one `_emit_digest` helper that prints to stdout (ANSI only on a TTY) and appends the plain form to `state/debate-log.md`. Two new optional schema fields (`PullRequest.headline`, `ReviewComment.counter`) plus a render-bookkeeping field (`DebateState.last_emitted_round`) let the ledger render crisp deterministically; this is a `schema_version` bump.

**Tech Stack:** Python 3, Pydantic v2, Typer CLI, pytest (subprocess-driven `cli` fixture + in-process unit tests). No new dependencies.

---

## File Structure

- **Modify** `src/agensuite/models.py` — add `PullRequest.headline`, `ReviewComment.counter`, `DebateState.last_emitted_round`.
- **Modify** `src/agensuite/state.py` — bump `STATE_SCHEMA_VERSION` 2 → 3.
- **Create** `src/agensuite/digest.py` — pure render layer (glyphs, meters, verdict line, PR-terminal line, ledger, `colorize`).
- **Modify** `src/agensuite/cli.py` — `_debate_log_path` + `_emit_digest` helpers; wire `pr_open` (`--headline`), `pr_comment` (`--counter` + emit), `pr_merge` (emit), `debate_next_turn` (round emit), `human_gate` (emit); add `debate digest` command.
- **Modify** `AGENTS.md` and `src/agensuite/templates/AGENTS.md` — sprint-loop snippet, CLI surface, new invariant.
- **Modify** `src/agensuite/templates/.claude/agents/ceo.md` — CEO debrief instruction.
- **Create** `tests/test_digest.py` — pure-render golden tests.
- **Modify** `tests/test_cli.py` — end-to-end emit + new-flag tests.
- **Modify** `tests/test_models.py`, `tests/test_state.py` — field defaults + schema-version bump.

A note on the ledger: cells are **plain text** (no emoji inside columns — emoji only in section headers) so fixed-width column alignment is safe across terminals.

---

## Task 1: Schema fields + version bump

**Files:**
- Modify: `src/agensuite/models.py` (`ReviewComment`, `PullRequest`, `DebateState`)
- Modify: `src/agensuite/state.py:42` (`STATE_SCHEMA_VERSION`)
- Test: `tests/test_models.py`, `tests/test_state.py`

- [ ] **Step 1: Write failing tests for the new fields**

Add to `tests/test_models.py`:

```python
def test_pullrequest_headline_defaults_empty():
    from agensuite.models import PullRequest
    pr = PullRequest(id="pr-1", title="t", branch="b", author="a", sprint_id="s")
    assert pr.headline == ""
    pr2 = PullRequest(
        id="pr-2", title="t", branch="b", author="a", sprint_id="s",
        headline="Postgres over Dynamo for sub-50ms reads",
    )
    assert pr2.headline == "Postgres over Dynamo for sub-50ms reads"


def test_reviewcomment_counter_defaults_empty():
    from agensuite.models import ReviewComment
    rc = ReviewComment(reviewer="a", comment="c")
    assert rc.counter == ""
    rc2 = ReviewComment(reviewer="a", comment="c", counter="per-entity TTL policy")
    assert rc2.counter == "per-entity TTL policy"


def test_debatestate_last_emitted_round_defaults_minus_one():
    from agensuite.models import DebateState
    ds = DebateState(sprint_id="s")
    assert ds.last_emitted_round == -1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py -k "headline or counter or last_emitted_round" -v`
Expected: FAIL — `headline`/`counter`/`last_emitted_round` rejected by `extra="forbid"`.

- [ ] **Step 3: Add the fields**

In `src/agensuite/models.py`, `ReviewComment` (after the `phase` field, before `timestamp`):

```python
    counter: str = ""
```

In `PullRequest` (after `description: str = ""`):

```python
    headline: str = ""
```

In `DebateState` (after `cursor: int = 0`):

```python
    last_emitted_round: int = -1
```

- [ ] **Step 4: Bump the schema version**

In `src/agensuite/state.py`, change:

```python
STATE_SCHEMA_VERSION = 2
```
to
```python
STATE_SCHEMA_VERSION = 3
```

- [ ] **Step 5: Update the schema-version assertion test**

In `tests/test_state.py`, find the test asserting the current version (search for `schema_version` / `== 2`) and update the expected value to `3`. If a test writes a fixture with `"schema_version": 2` to assert `StateSchemaMismatch`, leave it — it now correctly represents an *old* version and should still raise.

- [ ] **Step 6: Run the full model + state suites**

Run: `pytest tests/test_models.py tests/test_state.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agensuite/models.py src/agensuite/state.py tests/test_models.py tests/test_state.py
git commit -m "feat: add headline/counter/last_emitted_round fields, bump schema_version to 3"
```

---

## Task 2: `digest.py` — meters, glyphs, truncate

**Files:**
- Create: `src/agensuite/digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_digest.py`:

```python
from agensuite import digest


def test_meter_filled_and_empty():
    assert digest.meter(1, 2) == "●○"
    assert digest.meter(2, 2) == "●●"
    assert digest.meter(0, 2) == "○○"


def test_meter_never_negative_when_over_quorum():
    # more approvals than quorum: no empty pips, no crash
    assert digest.meter(3, 2) == "●●●"


def test_truncate_short_passthrough():
    assert digest.truncate("hello", 80) == "hello"


def test_truncate_long_adds_ellipsis():
    out = digest.truncate("x" * 100, 10)
    assert out == "x" * 9 + "…"
    assert len(out) == 10
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_digest.py -v`
Expected: FAIL — `No module named 'agensuite.digest'`.

- [ ] **Step 3: Create `digest.py` with the helpers**

```python
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
    """One-line clip. Collapses internal newlines, ellipsizes past ``limit``."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_digest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/digest.py tests/test_digest.py
git commit -m "feat: add digest render primitives (meter, truncate, glyph maps)"
```

---

## Task 3: `digest.py` — verdict line + PR-terminal line

**Files:**
- Modify: `src/agensuite/digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_digest.py`:

```python
from agensuite.models import PRStatus, PullRequest, ReviewComment, Verdict, TurnPhase


def _pr(**kw):
    base = dict(id="pr-2", title="data model", branch="b", author="cdo", sprint_id="s")
    base.update(kw)
    return PullRequest(**base)


def test_render_verdict_line_request_changes():
    pr = _pr(headline="90-day flat TTL")
    pr.reviews.append(ReviewComment(
        reviewer="cto", comment="90-day TTL breaks GDPR minimization",
        verdict=Verdict.REQUEST_CHANGES, counter="per-entity TTL policy",
    ))
    line = digest.render_verdict_line(pr=pr, comment=pr.reviews[-1], round_idx=1, quorum=2)
    assert line.startswith("🔴 r1 · cto → pr-2")
    assert "90-day flat TTL" in line          # headline shown
    assert "GDPR minimization" in line         # critique quoted
    assert "●○" in line                        # 0 approvals of quorum 2
    assert "open: cto" in line


def test_render_verdict_line_falls_back_to_title_without_headline():
    pr = _pr(headline="")
    pr.reviews.append(ReviewComment(reviewer="cto", comment="ok", verdict=Verdict.APPROVE))
    line = digest.render_verdict_line(pr=pr, comment=pr.reviews[-1], round_idx=0, quorum=1)
    assert "data model" in line                # title used when headline empty
    assert line.startswith("✅ r0 · cto → pr-2")


def test_render_pr_terminal_merged():
    pr = _pr(headline="Postgres over Dynamo", status=PRStatus.MERGED)
    pr.reviews.append(ReviewComment(reviewer="cto", comment="lgtm", verdict=Verdict.APPROVE))
    pr.reviews.append(ReviewComment(reviewer="cco", comment="lgtm", verdict=Verdict.APPROVE))
    line = digest.render_pr_terminal(pr=pr, quorum=2)
    assert line.startswith("🟢 pr-2 MERGED")
    assert "Postgres over Dynamo" in line
    assert "●● 2/2" in line


def test_render_pr_terminal_deadlocked():
    pr = _pr(headline="strict compliance posture", status=PRStatus.DEADLOCKED)
    line = digest.render_pr_terminal(pr=pr, quorum=2)
    assert line.startswith("⚔️ pr-2 DEADLOCKED")
    assert "strict compliance posture" in line
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_digest.py -k "verdict_line or pr_terminal" -v`
Expected: FAIL — functions undefined.

- [ ] **Step 3: Implement the two renderers**

Append to `src/agensuite/digest.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_digest.py -k "verdict_line or pr_terminal" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/digest.py tests/test_digest.py
git commit -m "feat: render verdict and PR-terminal digest lines"
```

---

## Task 4: `digest.py` — decision ledger

**Files:**
- Modify: `src/agensuite/digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_digest.py`:

```python
def test_render_ledger_groups_locked_and_contested():
    merged = _pr(id="pr-1", headline="Postgres over Dynamo", status=PRStatus.MERGED)
    contested = _pr(id="pr-2", headline="90-day flat TTL", status=PRStatus.CHANGES_REQUESTED)
    contested.reviews.append(ReviewComment(
        reviewer="cto", comment="breaks GDPR", verdict=Verdict.REQUEST_CHANGES,
        counter="per-entity TTL policy",
    ))
    pending = _pr(id="pr-3", headline="three pricing tiers", status=PRStatus.OPEN)

    out = digest.render_ledger([merged, contested, pending], quorum=2, round_label="ROUND 1")
    assert "ROUND 1" in out
    assert "LOCKED" in out and "CONTESTED" in out
    assert "Postgres over Dynamo" in out        # locked column
    assert "90-day flat TTL" in out             # contested: option A (headline)
    assert "per-entity TTL policy" in out       # contested: option B (counter)
    assert "three pricing tiers" in out         # pending shown
    # plain text only — no ANSI escapes in the rendered ledger
    assert "\x1b[" not in out


def test_render_ledger_empty_is_total():
    out = digest.render_ledger([], quorum=1, round_label=None)
    assert "LOCKED" in out  # renders headers even with nothing to show
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_digest.py -k ledger -v`
Expected: FAIL — `render_ledger` undefined.

- [ ] **Step 3: Implement the ledger**

Append to `src/agensuite/digest.py`:

```python
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
        elif pr.status in (PRStatus.OPEN, PRStatus.UNDER_REVIEW):
            cells.append(f"• {_claim(pr)} — unset")
    return cells


def render_ledger(
    prs: list[PullRequest], *, quorum: int, round_label: str | None = None
) -> str:
    """Two-column decision ledger: LOCKED decisions vs CONTESTED tradeoffs.

    Cells are plain text (no emoji inside columns) so fixed-width alignment is
    stable across terminals; emoji live only in the section headers.
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_digest.py -k ledger -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/digest.py tests/test_digest.py
git commit -m "feat: render two-column decision ledger"
```

---

## Task 5: `digest.py` — `colorize` (ANSI, TTY + NO_COLOR aware)

**Files:**
- Modify: `src/agensuite/digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_digest.py`:

```python
import os as _os


def test_colorize_noop_when_not_tty():
    assert digest.colorize("🔴 hi", tty=False) == "🔴 hi"


def test_colorize_wraps_when_tty_and_no_NO_COLOR(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = digest.colorize("🔴 hi", tty=True)
    assert out.startswith("\x1b[")     # ANSI prefix present
    assert out.endswith("\x1b[0m")     # reset suffix
    assert "🔴 hi" in out


def test_colorize_respects_NO_COLOR(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert digest.colorize("🔴 hi", tty=True) == "🔴 hi"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_digest.py -k colorize -v`
Expected: FAIL — `colorize` undefined.

- [ ] **Step 3: Implement `colorize`**

Append to `src/agensuite/digest.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_digest.py -k colorize -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/digest.py tests/test_digest.py
git commit -m "feat: add TTY/NO_COLOR-aware colorize for digest stdout"
```

---

## Task 6: CLI sink — `_debate_log_path` + `_emit_digest`

**Files:**
- Modify: `src/agensuite/cli.py` (add helpers near the other `_` helpers, e.g. after `_short_id` ~line 271)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write a failing end-to-end test**

Add to `tests/test_cli.py` (new class):

```python
class TestDigestSink:
    def test_emit_writes_stdout_and_log_without_ansi(self, cli, project_root):
        cli("bootstrap")
        # drive a minimal PR + one verdict so a digest emits
        cli("branch", "create", "feat/a/x")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "draft", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s",
                 "--headline", "Postgres over Dynamo", "--files", "doc.md").stdout.strip()
        cli("debate", "next-turn", "--sprint", "s")
        p = cli("pr", "comment", "--id", pr, "--reviewer", "b",
                "--comment", "looks risky", "--verdict", "REQUEST_CHANGES",
                "--counter", "per-entity policy")
        # stdout carries the digest (machine JSON is still on its own line)
        assert "🔴" in p.stdout
        assert "Postgres over Dynamo" in p.stdout
        # log file exists, contains the line, and has NO ansi escapes
        log = (project_root / "state" / "debate-log.md").read_text(encoding="utf-8")
        assert "Postgres over Dynamo" in log
        assert "\x1b[" not in log
```

(This test also exercises Tasks 7–8; it will fully pass once those land. Run the isolated helper unit in Step 2 first.)

- [ ] **Step 2: Add the helpers**

In `src/agensuite/cli.py`, add near the other underscore helpers (after `_short_id`):

```python
import sys  # ensure imported at top of file if not already

from . import digest as _digest


def _debate_log_path(root: Path) -> Path:
    return root / "state" / "debate-log.md"


def _emit_digest(root: Path, text: str) -> None:
    """Print ``text`` to stdout (ANSI on a TTY) and append the plain form to
    ``state/debate-log.md``. Append failure warns to stderr but never aborts
    the caller — the state write is the source of truth.
    """
    tty = sys.stdout.isatty()
    for line in text.splitlines() or [text]:
        typer.echo(_digest.colorize(line, tty=tty))
    path = _debate_log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip("\n") + "\n")
    except OSError as e:
        typer.echo(f"warning: could not append to {path}: {e}", err=True)
```

Verify `import sys` exists at the top of `cli.py`; add it if missing. Confirm `from pathlib import Path` is already imported (it is — used by `_root`).

- [ ] **Step 3: Run the model/digest suites to confirm no import breakage**

Run: `pytest tests/test_digest.py -q && python -c "import agensuite.cli"`
Expected: PASS / no import error. (The Step-1 end-to-end test stays red until Task 7.)

- [ ] **Step 4: Commit**

```bash
git add src/agensuite/cli.py tests/test_cli.py
git commit -m "feat: add _emit_digest sink (stdout + state/debate-log.md, ansi-on-tty)"
```

---

## Task 7: Wire `pr open --headline` and `pr comment --counter` + verdict emit

**Files:**
- Modify: `src/agensuite/cli.py` (`pr_open` ~648, `pr_comment` ~757)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_cli.py`:

```python
class TestHeadlineCounter:
    def test_pr_open_stores_headline(self, cli, project_root):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "d", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s",
                 "--headline", "claim X", "--files", "doc.md").stdout.strip()
        prs = json.loads((project_root / "state" / "prs.json").read_text())
        assert prs["prs"][pr]["headline"] == "claim X"

    def test_pr_comment_stores_counter_and_emits_verdict(self, cli, project_root):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "d", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s", "--headline", "claim X",
                 "--files", "doc.md").stdout.strip()
        cli("debate", "next-turn", "--sprint", "s")
        p = cli("pr", "comment", "--id", pr, "--reviewer", "b",
                "--comment", "no", "--verdict", "REQUEST_CHANGES",
                "--counter", "alt Y")
        prs = json.loads((project_root / "state" / "prs.json").read_text())
        assert prs["prs"][pr]["reviews"][-1]["counter"] == "alt Y"
        assert "🔴" in p.stdout            # verdict digest emitted
        # machine-readable JSON line still present for the orchestrator
        assert '"verdict": "REQUEST_CHANGES"' in p.stdout
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_cli.py -k "stores_headline or stores_counter" -v`
Expected: FAIL — unknown option `--headline` / `--counter`.

- [ ] **Step 3: Add `--headline` to `pr_open`**

In `pr_open`'s signature (after `description` option, line ~656):

```python
    headline: str = typer.Option("", "--headline",
        help="One-line product claim this PR makes (feeds the decision ledger)."),
```

In the `PullRequest(...)` construction (line ~689), add:

```python
                description=description,
                headline=headline,
```

- [ ] **Step 4: Add `--counter` to `pr_comment` and emit the verdict digest**

In `pr_comment`'s signature (after `parent_turn_idx`, line ~783):

```python
    counter: str = typer.Option("", "--counter",
        help="One-line alternative you push (use with --verdict REQUEST_CHANGES); "
             "feeds the contested column of the ledger."),
```

In the `ReviewComment(...)` construction (line ~840), add `counter=counter`:

```python
            review = ReviewComment(
                reviewer=reviewer,
                file=file,
                comment=comment,
                verdict=verdict,
                phase=phase,
                counter=counter,
            )
```

Then, immediately **before** the final `typer.echo(json.dumps({...}))` (line ~889) — outside the `with state_lock` block so the digest reflects committed state — add:

```python
    _emit_digest(
        root,
        _digest.render_verdict_line(
            pr=pr, comment=review, round_idx=round_idx, quorum=cfg.approval_quorum
        ),
    )
```

Note: `pr`, `review`, `round_idx`, and `cfg` are all in scope from the `with` block (Python has no block scope). The machine-readable JSON echo stays last so orchestrator parsing is unchanged.

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_cli.py -k "stores_headline or stores_counter or DigestSink" -v`
Expected: PASS (the Task-6 `TestDigestSink` test now passes too).

- [ ] **Step 6: Commit**

```bash
git add src/agensuite/cli.py tests/test_cli.py
git commit -m "feat: capture headline/counter and emit verdict digest on pr comment"
```

---

## Task 8: Emit on merge, round boundary, and human-gate

**Files:**
- Modify: `src/agensuite/cli.py` (`pr_merge` ~1111, `debate_next_turn` ~1211, `human_gate` ~1383)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_cli.py`:

```python
class TestTerminalAndGateEmit:
    def _open_and_merge(self, cli):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "d", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s", "--headline", "Postgres over Dynamo",
                 "--files", "doc.md").stdout.strip()
        cli("debate", "next-turn", "--sprint", "s")
        cli("pr", "comment", "--id", pr, "--reviewer", "b",
            "--comment", "lgtm", "--verdict", "APPROVE")
        return pr

    def test_merge_emits_terminal_and_ledger(self, cli, project_root):
        pr = self._open_and_merge(cli)
        p = cli("pr", "merge", "--id", pr)
        assert "🟢" in p.stdout
        assert "LOCKED" in p.stdout            # ledger snapshot follows
        log = (project_root / "state" / "debate-log.md").read_text(encoding="utf-8")
        assert "Postgres over Dynamo" in log

    def test_human_gate_emits_ledger(self, cli, project_root):
        self._open_and_merge(cli)
        # non-interactive: EOF auto-continues; --sprint drives the ledger
        p = cli("human-gate", "--message", "inspect", "--sprint", "s")
        assert "LOCKED" in p.stdout
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_cli.py -k "merge_emits or human_gate_emits" -v`
Expected: FAIL — no ledger in output.

- [ ] **Step 3: Emit on `pr_merge`**

In `pr_merge`, after the `with state_lock` block and before `typer.echo(sha)` (line ~1133):

```python
    # Re-load committed state for the digest (terminal line + full ledger).
    cfg = _load_sprint_or_die(root, prs[id].sprint_id)
    sprint_prs = sorted(
        [p for p in prs.values() if p.sprint_id == prs[id].sprint_id],
        key=lambda p: p.created_at,
    )
    _emit_digest(root, _digest.render_pr_terminal(pr=prs[id], quorum=cfg.approval_quorum))
    _emit_digest(root, _digest.render_ledger(sprint_prs, quorum=cfg.approval_quorum))
```

(`prs` is still in scope from the `with` block.)

- [ ] **Step 4: Emit a ledger snapshot on round boundary in `debate_next_turn`**

In `debate_next_turn`, inside the `with state_lock` block, **after** `turn` is selected and before `DebateStore.save(root, debate)` (line ~1263), add round-crossing detection:

```python
            emit_ledger_for_round: Optional[str] = None
            if turn is not None and turn.round_idx > debate.last_emitted_round:
                debate.last_emitted_round = turn.round_idx
                emit_ledger_for_round = f"ROUND {turn.round_idx}"
```

Then, after the `with` block closes but before building `result` (line ~1306), add:

```python
    if emit_ledger_for_round is not None:
        _emit_digest(
            root,
            _digest.render_ledger(
                sprint_prs, quorum=cfg.approval_quorum, round_label=emit_ledger_for_round
            ),
        )
```

`sprint_prs` and `cfg` are in scope from the `with` block. Guard: `emit_ledger_for_round` must be initialised to `None` before the `with` block if any early `return` path could skip it — declare `emit_ledger_for_round = None` just after `root = _root(ctx)` (line ~1229) to be safe, and remove the inner re-declaration's type annotation (keep the assignment).

- [ ] **Step 5: Emit a ledger on `human_gate`**

In `human_gate`, in the default (non-`resolve_deadlocks`) branch, after the banner is printed and before/after the `input(...)` block (line ~1414), add — only when `--sprint` is supplied:

```python
    if sprint:
        try:
            with state_lock(root):
                cfg = _load_sprint_or_die(root, sprint)
                prs = PRRegistry.load(root)
                sprint_prs = sorted(
                    [p for p in prs.values() if p.sprint_id == sprint],
                    key=lambda p: p.created_at,
                )
            _emit_digest(root, _digest.render_ledger(sprint_prs, quorum=cfg.approval_quorum,
                                                     round_label="HUMAN GATE"))
        except (StateLockTimeout, StateSchemaMismatch):
            pass
```

`root` is not yet defined in `human_gate` — add `root = _root(ctx)` at the top of the function (line ~1402, before the `resolve_deadlocks` check) and confirm `_resolve_deadlocks_loop` does its own `_root` (it takes `ctx`, so leave it).

- [ ] **Step 6: Run to verify pass**

Run: `pytest tests/test_cli.py -k "merge_emits or human_gate_emits" -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS (watch for any pre-existing test that asserted exact stdout of `pr merge` / `human-gate` / `next-turn` — if one breaks because the digest now appends lines, update that assertion to check for the machine-readable token specifically, not full-string equality).

- [ ] **Step 8: Commit**

```bash
git add src/agensuite/cli.py tests/test_cli.py
git commit -m "feat: emit PR-terminal + ledger on merge, round-boundary, and human-gate"
```

---

## Task 9: `debate digest` command (on-demand ledger + CEO narrative note)

**Files:**
- Modify: `src/agensuite/cli.py` (add under the `debate_app` commands, near `debate_tail` ~1353)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_cli.py`:

```python
class TestDebateDigestCommand:
    def test_digest_renders_ledger_on_demand(self, cli, project_root):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "d", "--files", "doc.md")
        cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
            "--title", "T", "--sprint", "s", "--headline", "claim X", "--files", "doc.md")
        p = cli("debate", "digest", "--sprint", "s")
        assert "LOCKED" in p.stdout

    def test_digest_note_appends_narrative(self, cli, project_root):
        cli("bootstrap")
        cli("debate", "digest", "--sprint", "s", "--note",
            "This sprint locked the data layer on Postgres.")
        log = (project_root / "state" / "debate-log.md").read_text(encoding="utf-8")
        assert "locked the data layer on Postgres" in log
        assert "CEO DEBRIEF" in log
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_cli.py -k "DebateDigestCommand" -v`
Expected: FAIL — no such command `digest`.

- [ ] **Step 3: Add the command**

After `debate_tail` (line ~1383, before `@app.command("human-gate")`):

```python
@debate_app.command("digest")
def debate_digest(
    ctx: typer.Context,
    sprint: str = typer.Option(..., "--sprint"),
    full: bool = typer.Option(False, "--full",
        help="Also append the ledger to the log (default: stdout + log)."),
    note: Optional[str] = typer.Option(None, "--note",
        help="Append a CEO prose debrief paragraph to stdout + the log."),
) -> None:
    """Render the decision ledger on demand, and/or append a CEO narrative note.

    The orchestrator calls this at a human-gate after spawning the CEO
    subagent: it pipes the CEO's prose product debrief in via ``--note`` so it
    lands in ``state/debate-log.md`` alongside the deterministic ledger.
    """
    root = _root(ctx)
    try:
        with state_lock(root):
            cfg = _load_sprint_or_die(root, sprint)
            prs = PRRegistry.load(root)
            sprint_prs = sorted(
                [p for p in prs.values() if p.sprint_id == sprint],
                key=lambda p: p.created_at,
            )
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    if note is not None:
        _emit_digest(root, f"📣 CEO DEBRIEF — {sprint}\n{note.strip()}")
    else:
        _emit_digest(root, _digest.render_ledger(sprint_prs, quorum=cfg.approval_quorum,
                                                 round_label="DIGEST"))
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_cli.py -k "DebateDigestCommand" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/cli.py tests/test_cli.py
git commit -m "feat: add 'debate digest' command for on-demand ledger + CEO note"
```

---

## Task 10: Docs — AGENTS.md, template AGENTS.md, CEO persona

**Files:**
- Modify: `AGENTS.md`
- Modify: `src/agensuite/templates/AGENTS.md`
- Modify: `src/agensuite/templates/.claude/agents/ceo.md`

No tests (docs only); verify by re-reading.

- [ ] **Step 1: Update the sprint-loop snippet in both AGENTS.md files**

In the `--- spoke drafting ---` block, change the `pr open` line to pass a headline:

```
    pr_id = agensuite pr open --branch {branch} --author {role} \
        --title "{ROLE}: {cfg.title}" --headline "<one-line product claim>" \
        --files <paths> --sprint {sprint_id}
```

In the REVIEW prompt template, instruct the reviewer to pass `--counter` on REQUEST_CHANGES:

```
    if turn.phase == "REVIEW":
        prompt = (f"Review PR {turn.pr_id}. Recent debate: {tail}. "
                  f"Verdict: APPROVE | REQUEST_CHANGES | COMMENT. If REQUEST_CHANGES, "
                  f"state the one-line alternative you propose (passed as --counter).")
```

And the `pr comment` call:

```
    agensuite pr comment --id {turn.pr_id} --reviewer {turn.speaker} \
        --verdict <APPROVE|REQUEST_CHANGES|COMMENT> --phase {turn.phase} \
        --comment "<critique>" [--counter "<one-line alternative>"] [{extra}]
```

- [ ] **Step 2: Add the human-gate CEO debrief step**

In the `--- human gate ---` block of both AGENTS.md files, replace the single `human-gate` line with:

```
# --- human gate + product debrief ---
agensuite human-gate --message "Inspect debate for {sprint_id}" --sprint {sprint_id}
# Spawn the CEO for a prose product debrief, then persist it to the log:
spawn_subagent(subagent_type="ceo",
               prompt="Read the decision ledger + debate tail. Write a short "
                      "PRODUCT debrief: what locked, what was conceded and why, "
                      "what stayed unresolved and where it's carried.")
agensuite debate digest --sprint {sprint_id} --note "<ceo prose>"
```

- [ ] **Step 3: Add a new invariant**

In the `## 6. Invariants` section of both AGENTS.md files, add:

```
- **Visibility is CLI-emitted, not LLM-narrated.** The decision ledger and
  verdict/terminal lines are printed by the CLI as a side effect of state
  mutations (`pr comment`, `pr merge`, `next-turn`, `human-gate`) and appended
  to `state/debate-log.md`. The orchestrator does not hand-roll status
  summaries. Spokes SHOULD pass `--headline` (on `pr open`) and `--counter`
  (on a REQUEST_CHANGES `pr comment`) so the ledger renders the product claim
  and its alternative; omitting them degrades the cell, never breaks it.
```

- [ ] **Step 4: Update the CLI Reference block**

In the `## Reference: CLI Surface` of both AGENTS.md files, update/add:

```
agensuite pr open --branch <b> --author <a> --title <t> --sprint <s> \
    [--headline <one-line product claim>] [--files ...] [--description ...]
agensuite pr comment --id <pr> --reviewer <r> --comment <c> \
    [--verdict APPROVE|REQUEST_CHANGES|COMMENT] [--phase REVIEW|REBUTTAL|FOLLOWUP] \
    [--counter <one-line alternative>] [--parent-turn-idx <n>] [--file <f>]
agensuite human-gate --message <msg> [--sprint <s>]   # --sprint prints the ledger
agensuite debate digest --sprint <s> [--full] [--note <ceo prose>]
```

- [ ] **Step 5: Update the CEO persona**

In `src/agensuite/templates/.claude/agents/ceo.md`, add a short responsibility under the CEO's duties (find the section listing what the CEO produces — ADR, next sprint — and add):

```markdown
- **Product debrief at the human-gate.** When asked, read the decision ledger
  and debate tail and write a short prose debrief framed on the *product*:
  what decisions locked, what was conceded and why, and what stayed unresolved
  (and which future sprint carries it). Keep it to a few sentences — it is
  piped to `agensuite debate digest --note` and lands in the human's log.
```

- [ ] **Step 6: Verify docs read correctly**

Run: `grep -n "headline\|--counter\|debate digest\|CLI-emitted" AGENTS.md src/agensuite/templates/AGENTS.md`
Expected: each term present in both files.

- [ ] **Step 7: Commit**

```bash
git add AGENTS.md src/agensuite/templates/AGENTS.md src/agensuite/templates/.claude/agents/ceo.md
git commit -m "docs: document headline/counter, ledger emit, and CEO debrief in the contract"
```

---

## Task 11: ADR for the schema/contract change + full-suite green

**Files:**
- Modify: `src/agensuite/__init__.py` (only if it carries a version string to bump — check first)
- Test: whole suite

- [ ] **Step 1: Record the contract change**

The bounded-rebuttal contract note in AGENTS.md says a `schema_version` bump and protocol changes are contract changes. This repo tracks its *own* design history under `docs/`. Add a short ADR-style note at `docs/superpowers/specs/2026-06-06-debate-visibility-design.md` is already the spec; append a one-paragraph "Implemented" stamp to the spec's Status line:

Change the spec header `- **Status:** Approved (pending implementation)` to:

```markdown
- **Status:** Implemented 2026-06-06 — schema_version 2 → 3 (added PR.headline,
  ReviewComment.counter, DebateState.last_emitted_round). Pre-3 on-disk state is
  rejected by state.py; regenerate via `agensuite bootstrap --reset`.
```

- [ ] **Step 2: Run the entire test suite**

Run: `pytest -q`
Expected: PASS, no failures.

- [ ] **Step 3: Run a manual smoke of the human-visible output**

Run:
```bash
cd /tmp && rm -rf vis-smoke && mkdir vis-smoke && cd vis-smoke
AGENSUITE_ROOT=$PWD python -m agensuite.cli bootstrap >/dev/null 2>&1 || true
```
(If `bootstrap` needs a sprint, copy `tests`' minimal sprint or run from a scaffolded project.) Then drive `pr open --headline … → next-turn → pr comment --verdict REQUEST_CHANGES --counter … → pr merge` and confirm the ledger + colored lines print and `state/debate-log.md` accumulates plain-text entries.
Expected: ledger visible in terminal; `debate-log.md` has no `\x1b[` sequences.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-06-debate-visibility-design.md
git commit -m "docs: stamp visibility spec as implemented (schema_version 3)"
```

---

## Self-Review Notes (author checklist — already applied)

- **Spec coverage:** hybrid generator (Tasks 2–5 deterministic render; Task 9 `--note` carries CEO prose) ✓; on-state-change cadence (Task 7 verdict, Task 8 merge/round/gate) ✓; dual sink (Task 6) ✓; auto-emit-on-mutation wiring (Tasks 7–8) ✓; decision-ledger shape (Task 4) ✓; Path X enriched schema (Task 1) ✓; visual glyph/meter format + ANSI-on-TTY (Tasks 2–5) ✓; emit-discipline table (verdict=one-liner, terminal/round/gate=full ledger) ✓; error handling (log-append warns-not-aborts, render total over empty state) ✓; tests (golden + e2e + NO_COLOR matrix + schema rejection) ✓; AGENTS.md + CEO persona (Task 10) ✓.
- **Type consistency:** `render_verdict_line`/`render_pr_terminal` use keyword-only args matching the call sites in Tasks 7–8; `render_ledger(prs, *, quorum, round_label)` signature matches all four call sites; `meter(approvals, quorum)`, `truncate(text, limit)`, `colorize(text, *, tty)` consistent across module and tests; `_emit_digest(root, text)` signature matches every call.
- **Open risk flagged in Task 8 Step 7:** pre-existing tests that assert exact full-string stdout of `pr merge` / `human-gate` / `next-turn` may need to switch to substring/JSON-token assertions now that digests append lines. Handle inline if they fail.
```

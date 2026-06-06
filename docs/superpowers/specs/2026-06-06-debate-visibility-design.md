# Debate Visibility — Decision Ledger Debriefs

- **Date:** 2026-06-06
- **Status:** Approved (pending implementation)
- **Scope:** Visibility only. The worker/hire tier (letting chiefs summon
  implementation agents that write real product code) is a deliberately
  deferred follow-up spec, not in scope here.

## Problem

The C-suite debate loop runs many turns across many rounds, but a human
watching the orchestrator has no readable view of how the *product plan*
is evolving. All debate substance lives in `state/*.json`
(`DebateState.transcript`, `PullRequest.reviews`); the only surfacing path,
`agensuite debate tail`, exists to feed orchestrator prompts, not humans.
`human-gate` prints a bare banner. Net effect: the simulation can deliberate
for hours and the human cannot answer "what have we decided, and what is
still contested?"

## Goal

Surface, on every material state change, a **decision ledger** — a
two-column board of LOCKED decisions vs CONTESTED tradeoffs — to both the
orchestrator's terminal and a durable log, so a human can understand
*product decisions* at a glance. Augment it with a single prose debrief from
the CEO at each human-gate.

Non-goals: changing the debate protocol (verdict + phase + append-only
schedule + verdict-based termination is untouched); producing product code;
any per-turn LLM call.

## Design Decisions (resolved during brainstorming)

1. **Generator = hybrid.** A deterministic CLI render handles the ledger
   every state change (zero token cost, always in sync). One CEO subagent
   narrative fires only at human-gate / sprint end.
2. **Cadence = on state change**, not per-turn and not round-only. Quiet on
   no-op turns; emits when a verdict lands, a PR goes terminal, or a round
   boundary crosses.
3. **Sink = both.** CLI prints to stdout (live in the orchestrator terminal)
   *and* appends to `state/debate-log.md` (durable scrollback).
4. **Wiring = auto-emit on mutation.** The commands that already change state
   emit as a side effect — visibility cannot be forgotten by a lazy
   orchestrator. No dependence on AGENTS.md discipline for the emit itself.
5. **Debrief shape = decision ledger.** Two columns: LOCKED (decisions that
   merged / were recorded) and CONTESTED (open tradeoffs with A/B framing and
   a lean). Updates each round.
6. **Cell source = enriched schema (Path X).** Add author-time structured
   fields so the ledger renders crisp deterministically, rather than scraping
   PR titles / comment text.

## Architecture

```
state mutation (pr comment / pr merge / resolve-deadlocks / round cross)
        │
        ▼
  digest.py  ──build entry from DebateState + event──►  render (plain, UTF-8)
        │                                                 │
        ├──► stdout  (ANSI-wrapped when isatty)  ─────────┤
        └──► append state/debate-log.md  (plain, no ANSI) ┘

human-gate  ──spawn CEO subagent──►  prose product debrief  ──►  log + stdout
```

### New module: `src/agensuite/digest.py`

Pure render layer. **No I/O, no state mutation inside the builders** — they
take a `DebateState` (+ the list of `PullRequest`s and the triggering event)
and return a rendered string (one markdown block). The calling CLI command
owns the stdout-print and the file-append. This preserves the existing
"mutations only via CLI" invariant and keeps `digest.py` trivially testable
with golden strings.

Public surface (illustrative):

```python
def render_verdict_line(state, prs, pr_id, comment) -> str: ...
def render_pr_terminal(state, prs, pr_id) -> str: ...
def render_ledger(prs, *, round_label: str | None = None) -> str: ...
def colorize(text: str, *, tty: bool) -> str: ...   # ANSI; respects NO_COLOR
```

### Schema changes (`models.py`)

Two optional fields, both default `""` so render degrades gracefully when a
spoke omits one:

- `PullRequest.headline: str = ""` — one-line product claim the PR makes
  (e.g. `"Postgres over Dynamo for sub-50ms reads"`). Captured at `pr open`.
- `ReviewComment.counter: str = ""` — when a reviewer posts
  `REQUEST_CHANGES`, the one-line alternative they push (e.g.
  `"per-entity TTL policy"`). Captured at `pr comment`.

Bump `schema_version`. Per AGENTS.md this is a contract change requiring an
ADR; that ADR is part of this work. `state.py` rejects pre-bump on-disk
state up front — acceptable because the tool scaffolds fresh projects
(`bootstrap` starts clean); no in-flight migration path is owed.

### Ledger assembly (deterministic)

From the current `PullRequest` list:

- **LOCKED** = PRs with status `MERGED` → their `headline`. (Once an ADR is
  recorded, its decision lines may also feed this column.)
- **CONTESTED** = PRs with non-empty `open_change_requests` → `headline` as
  Option A, each change-requester's `counter` as Option B.
- **lean** = derived from the existing `approval_count` vs
  `open_change_requests` tally — no new semantics.
- **unset** = PRs still `OPEN` / `UNDER_REVIEW` with no resolution → shown as
  pending so the human sees what hasn't been addressed.

`approval_count` and `open_change_requests` already exist on `PullRequest`;
the ledger reads them, never recomputes.

## Visual format

Fixed glyph vocabulary (renders identically in terminal and markdown;
chosen width-safe to avoid box misalignment):

```
verdict   ✅ APPROVE   🔴 REQUEST_CHANGES   💬 COMMENT
status    🟢 MERGED   ❌ REJECTED   ⚔️  DEADLOCKED   👀 UNDER_REVIEW   🟡 CHANGES_REQ
approvals ●●○  (filled = approvals counted, empty = remaining to quorum)
```

**VERDICT** (one-liner — keeps scrollback short):

```
🔴 turn 7 · r1 · CTO → PR-2 (data-retention model)  "90-day TTL breaks GDPR minimization"  ●○ · 1 open: CTO
```

**PR_TERMINAL** (one-liner):

```
🟢 PR-1 MERGED  core PRD  ●● 2/2 · clean
⚔️  PR-3 DEADLOCKED  compliance posture  CTO stood @ FOLLOWUP
```

**Decision ledger** (full snapshot — on PR_TERMINAL, round boundary, gate):

```
✅ LOCKED                      ❓ CONTESTED
──────────────                 ────────────────
• Postgres over Dynamo         • PII retention
  (latency)                      A: 90-day TTL  B: per-entity policy
• Email-first onboarding         leaning B · CDO owes rebuttal
• Activation = 1st-import-24h  • Pricing tier count — unset
```

### Rendering contract

`digest.py` emits plain text (glyphs + light rules, UTF-8). The CLI wraps
with ANSI **only** when `sys.stdout.isatty()` and `NO_COLOR` is unset —
verdict color (red/green/yellow), dimmed provenance. The file-append always
receives the plain form so no escape codes pollute `debate-log.md`. One
`colorize(text, tty=...)` chokepoint.

## Emit discipline (cadence detail)

| Event source                         | Emits                                  |
|--------------------------------------|----------------------------------------|
| `pr comment` (verdict lands)         | VERDICT one-liner + that PR's contested row |
| `pr merge` / conflict / deadlock     | PR_TERMINAL one-liner + full ledger    |
| `debate next-turn` crosses round     | full ledger snapshot (round label)     |
| `human-gate`                         | full ledger + CEO prose narrative      |
| `human-gate --resolve-deadlocks`     | PR_TERMINAL line per resolved PR + ledger |

Verdict turns stay one-liners; full ledger only on terminal/round/gate. A
30-turn debate yields readable scrollback, not 30 board dumps.

## CEO narrative at human-gate

`human-gate` (the existing pause point) additionally spawns the CEO subagent
with the current ledger + `debate tail` as input. The subagent returns a
short prose debrief framed on **product**, not process — what locked, what
was conceded and why, what stayed unresolved and where it's carried. Appended
to `debate-log.md` and printed. This is the *only* LLM call the visibility
layer makes, keeping cost near-zero.

## CLI surface changes

```
agensuite pr open    ... [--headline <one-line product claim>]
agensuite pr comment ... [--counter <one-line alternative>]   # use with --verdict REQUEST_CHANGES
agensuite debate digest --sprint <s> [--full]                 # on-demand re-render (optional, powers log re-read)
```

`human-gate`, `pr comment`, `pr merge`, `next-turn`, `resolve-deadlocks`
gain the auto-emit side effect. AGENTS.md sprint-loop snippet and the
Invariants section are updated so spokes populate `--headline` / `--counter`,
and a new invariant documents that the digest is emitted by the CLI, not the
LLM.

## Error handling

- Missing `headline`/`counter` → cell renders a neutral placeholder
  (`— claim not stated —`), never crashes. Render is total over any valid
  `DebateState`.
- `debate-log.md` append failure (e.g. read-only FS) → warn to stderr, do not
  abort the mutation; the state write already succeeded and is the source of
  truth.
- Non-UTF-8 / non-TTY stdout → plain form, no ANSI; glyphs still emit (UTF-8
  assumed for the log file regardless).

## Testing

- `digest.py` is pure → golden-string unit tests per entry kind (VERDICT,
  PR_TERMINAL, ledger).
- Ledger assembly from a fixture `DebateState` covering the locked /
  contested / unset mix, plus the empty-state and all-merged edges.
- `NO_COLOR` + `isatty()` matrix: ANSI present only for TTY-without-NO_COLOR;
  stripped otherwise.
- File-append never contains escape codes (assert on written bytes).
- Width-safe glyph set asserted (no double-width char inside a box border).
- `pr open --headline` / `pr comment --counter` round-trip through state load
  + save.
- Schema-version rejection: pre-bump on-disk state is refused by `state.py`.

## Out of scope (explicit)

- Worker / hire tier and any real product-code generation — separate spec.
- Changes to the debate protocol, schedule, or termination rules.
- Per-turn LLM summarization.
```

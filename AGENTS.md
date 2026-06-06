# AGENTS.md — Coding-Agent Contract (Hybrid Edition)

> This file is read by any coding agent (Claude Code, Codex, Cursor, …)
> when it opens this repository. It defines how that agent acts as the
> C-suite orchestrator for a generic software-startup simulation.
>
> **Hybrid execution model**:
> 1. **Domain intelligence** lives in `.claude/agents/{role}.md` — native
>    project-level subagent playbooks (YAML frontmatter + persona body).
>    The orchestrator delegates work to these via its **native subagent
>    primitive** (Claude Code's `Agent` tool with `subagent_type: <role>`,
>    Codex's `agents.spawn(...)`, Cursor's background composers, etc.).
> 2. **Repository & state mutations** flow exclusively through the
>    `agensuite` Python CLI under `src/agensuite/`. The CLI is the
>    deterministic rails: atomic JSON state files, synchronous git
>    worktrees, debate scheduling, reviewer-author guards.
>
> Personas provide **what to think**. The CLI enforces **what is
> allowed to happen**. Neither half is optional.

---

## 1. You Are the Orchestrator

You play the **CEO**. You will:

- **Delegate** each spoke role (CPO, CTO, CDO, CCO) to its native
  subagent playbook in `.claude/agents/{role}.md`. Use your platform's
  native subagent primitive — do **not** inline-prompt the persona text
  yourself; the description fields in those files are written so your
  router will auto-pick the right role.
- **Drive a turn-based debate** between spokes via PR comments produced
  through the CLI.
- Never let spokes talk to each other outside the PR + transcript channels.
- Treat every state mutation as something only the **`agensuite` CLI** is
  allowed to perform. Reading files (with your normal Read/Bash tools) is
  fine; writing repo state must flow through the CLI so this template
  works identically across coding-agent platforms.
- **Extend the project across sprints.** After each sprint's ADR is
  recorded, the CEO subagent authors the next sprint blueprint
  (`sprints/sprint-(N+1).md`) by synthesising the open questions and
  gaps left in the debate transcript. The repository ships only with
  `sprints/sprint-1.md`; everything beyond it is generated at runtime.

### Where Intelligence Lives

| Concern                                  | Lives in                          | Touched by  |
|------------------------------------------|-----------------------------------|-------------|
| Persona, biases, deliverable schema      | `.claude/agents/{role}.md`        | LLM only    |
| Branch / worktree / commit / PR / merge  | `agensuite` CLI                    | Shell only  |
| Debate turn schedule (append-only)       | `DebateState.schedule` in CLI     | Shell only  |
| Turn phase (REVIEW / REBUTTAL / FOLLOWUP)| `DebateTurn.phase` in CLI         | Shell only  |
| Verdict-based termination                | `agensuite debate next-turn`       | Shell only  |
| ADR scaffolding                          | `agensuite adr record`             | Shell only  |
| Sprint config (YAML frontmatter + body)  | `sprints/sprint-{n}.md`           | Both        |
| Next-sprint authoring                    | CEO subagent post-ADR             | LLM only    |
| Deadlock resolution                      | `agensuite human-gate --resolve-deadlocks` | Both |
| Chat notifications (outbound)            | `agensuite notify` / `adr record` / `human-gate --async` | Both |
| Chat-driven deadlock resolution          | `agensuite bot` + `human-gate --drain`    | Both |

If you find yourself paraphrasing persona text, stop — spawn the
subagent instead. If you find yourself touching `state/*.json` or
running raw `git` commands inside `workspace/`, stop — use the CLI.

## 2. One-Shot Setup

```
pip install -e .
agensuite bootstrap
```

`bootstrap` is idempotent. It creates `workspace/main/` (a real git
repository for the simulated product) and `state/` (JSON registries).
Re-running it without `--reset` is a no-op.

## 3. Subagent Playbooks

Native subagent files (project-level):

- `.claude/agents/ceo.md`
- `.claude/agents/cpo.md`
- `.claude/agents/cto.md`
- `.claude/agents/cdo.md`
- `.claude/agents/cco.md`

Each file has YAML frontmatter — `id`, `name`, `description`,
`systemPrompt` — followed by the persona body. **The body IS the
system prompt that gets injected** when the orchestrator spawns the
subagent. The sprint loop **pins `subagent_type` explicitly** by role,
so dispatch is deterministic; the `description` field is for the
platform's router to use on *ad-hoc* delegations outside the sprint
loop (e.g. mid-conversation "ask the CCO about a risk class") and for
human readability.

When you spawn a subagent, do **not** paraphrase or summarize the
persona — your platform will load the file verbatim. Trust the
playbook; fidelity matters because reviewer behavior depends on the
exact deliverable schema declared in each file.

## 4. Sprint Loop

The sprint loop is the operational heart of this repo. Iterate over the
sprint files **as they appear on disk**, in id order. Out of the box
only `sprint-1.md` exists; the CEO subagent emits `sprint-2.md` (and so
on) after each preceding sprint's ADR is recorded, so the loop keeps
finding new work without a pre-baked roadmap.

```
cfg = parse `sprints/{sprint_id}.md`            # YAML frontmatter
prereqs = { p.path: agensuite read --branch p.branch --path p.path
            for p in cfg.prerequisite_files }

# --- spoke drafting (parallel) ---
for role in cfg.participants:
    branch = f"feat/{role}/{slug_of(cfg.title)}"
    agensuite branch create {branch}

    # Native subagent spawn — DO NOT inline the persona text.
    # The playbook at .claude/agents/{role}.md is loaded by the platform.
    spawn_subagent(
        subagent_type = role,                         # cpo|cto|cdo|cco (CEO never drafts; only composes ADR + next sprint)
        prompt        = cfg.body
                      + prereqs
                      + f"Write to <output_dir>/<slug>.md inside the worktree.",
    )
    <subagent edits workspace/wt/feat__{role}__{slug}/...>

    agensuite commit --branch {branch} --author {role} \
        --message "{role} draft for {sprint_id}" --files <paths>
    pr_id = agensuite pr open --branch {branch} --author {role} \
        --title "{ROLE}: {cfg.title}" --files <paths> --sprint {sprint_id}

# --- debate (verdict-terminated; bounded threaded rebuttals) ---
# Termination is verdict-based: the schedule is an UPPER BOUND. The debate
# exits the moment every PR is either terminal (MERGED / REJECTED /
# DEADLOCKED) or meets quorum with no open change requests. Each turn
# carries a `phase` (REVIEW / REBUTTAL / FOLLOWUP) — pick the prompt
# template accordingly.
last_done = None
while True:
    turn = json.loads(agensuite debate next-turn --sprint {sprint_id})
    if turn.get("done"):
        last_done = turn
        break
    tail = agensuite debate tail --sprint {sprint_id} --window 6

    if turn.phase == "REVIEW":
        prompt = (f"Review PR {turn.pr_id}. Recent debate: {tail}. "
                  f"Verdict: APPROVE | REQUEST_CHANGES | COMMENT.")
    elif turn.phase == "REBUTTAL":
        prompt = (f"You are the author of PR {turn.pr_id}. Address every "
                  f"change request collectively: {turn.open_change_requests}. "
                  f"Recent debate: {tail}.")
    elif turn.phase == "FOLLOWUP":
        prompt = (f"You requested changes on PR {turn.pr_id}. The author's "
                  f"rebuttal (msg id={turn.rebuttal_msg_id}) is in {tail}. "
                  f"Stand (REQUEST_CHANGES) or withdraw (APPROVE / COMMENT).")

    spawn_subagent(subagent_type=turn.speaker, prompt=prompt)
    <subagent returns a short critique + verdict>

    extra = []
    if turn.phase == "FOLLOWUP":
        extra = ["--parent-turn-idx", str(turn.parent_turn_idx)]
    agensuite pr comment --id {turn.pr_id} --reviewer {turn.speaker} \
        --verdict <APPROVE|REQUEST_CHANGES|COMMENT> --phase {turn.phase} \
        --comment "<critique>" [{extra}]

# --- deadlock resolution (only if any reviewer stood at FOLLOWUP) ---
if last_done.get("reason") == "deadlocked":
    agensuite human-gate --sprint {sprint_id} --resolve-deadlocks

# --- human gate (STOP — this is a real pause, not a banner) ---
# With no tty (you run this through a subprocess) the command fails closed:
# it exits non-zero (code 2) instead of auto-continuing. That non-zero exit
# is the gate, not an error. When you see it: STOP your turn. Do NOT proceed
# to `pr merge`. Surface the gate summary (sprint, the PRs about to merge,
# key resolutions) to the human in the conversation and WAIT for their reply.
# The human's in-chat go-ahead IS the clearance — do not re-run the command.
exit_code = agensuite human-gate --message "Inspect debate for {sprint_id}"
if exit_code != 0:
    # halt here; ask the human in the conversation; only continue below
    # after they explicitly approve the merge in-chat
    STOP_AND_ASK_HUMAN

# --- merge + ADR (only after the human approved at the gate) ---
for pr in agensuite pr list --sprint {sprint_id}:
    if pr meets quorum and has no open change requests:
        agensuite pr merge --id {pr.id}     # marks REJECTED on conflict

# ADR composition is delegated to the CEO subagent for narrative,
# but persisted via the CLI:
spawn_subagent(subagent_type = "ceo",
               prompt = "Compose ADR for {sprint_id} from merged PRs and debate tail.")
agensuite adr record --sprint {sprint_id}

# --- dynamic next-sprint authoring ---
# The CEO subagent inspects the debate transcript for unresolved
# questions and gaps, then writes the NEXT sprint blueprint to
# sprints/sprint-(N+1).md using the same YAML frontmatter standard.
spawn_subagent(subagent_type = "ceo",
               prompt = "Author sprints/sprint-(N+1).md from the open "
                        "questions and gaps in {sprint_id}'s debate.")
```

All CLI commands return non-zero on error so you can detect failures from
the shell exit code. `agensuite debate next-turn` is the single source of
truth for **the next required turn, derived from current PR state** —
never derive turn order from the LLM side. The `DebateState.schedule` is
seeded at sprint kickoff and extended in place (append-only) as
reviewers post `REQUEST_CHANGES` (which appends one REBUTTAL slot) or
authors post a REBUTTAL (which appends one FOLLOWUP slot per open
change-requester).

### Chat integration (opt-in, Telegram-first)

By default every `human-gate` call blocks on stdin and every `adr record`
call is silent. An optional Telegram channel can replace the blocking prompt
with async button taps and add outbound event notices. Nothing in this section
is required — if unconfigured, the original terminal stdin behavior is
completely unchanged.

**Enabling.** Create `state/notify.json` (the `state/` directory is
gitignored; never commit this file) and export `AGENSUITE_TELEGRAM_TOKEN`
in the environment where the agent runs. The token must live only in the
environment variable — never in a file:

```json
{
  "channel": "telegram",
  "chat_id": "<your-chat-id>",
  "events": ["gate", "decision", "sprint-start"]
}
```

If `state/notify.json` is absent, malformed, or `AGENSUITE_TELEGRAM_TOKEN`
is unset, a `NullNotifier` is used and every send is a no-op. Call sites
never branch on an "enabled" flag — the notifier seam handles degradation
transparently.

**Outbound events** (fired only when configured and the event name appears in
`events`):

- `agensuite notify sprint-start --sprint <s>` — call at the top of the loop
  (just before spoke drafting) to announce the new sprint.
- `agensuite adr record --sprint <s>` — after persisting the ADR also sends
  the decision summary (event `"decision"`).
- `agensuite human-gate --sprint <s> --resolve-deadlocks --async` — sends one
  inline-keyboard message per deadlocked PR with Merge / Reject / ADR-options
  / Skip buttons, then returns `{"status":"awaiting_human","pending":[...]}`.
  This call does **not** block on stdin.

**Inbound flow (chat-driven deadlock resolution).** Use this sequence when
chat is configured and you want human decisions to arrive from the chat rather
than the terminal:

```
# 1. Start the relay sidecar once; keep it running throughout the sprint.
agensuite bot          # long-polls Telegram, appends button taps to
                       # state/gate_inbox.json — never touches the PR registry.

# 2. Raise the async gate (after the debate loop exits with reason="deadlocked").
agensuite human-gate --sprint <s> --resolve-deadlocks --async
#    → sends buttons to chat; returns immediately.

# 3. Human taps buttons in Telegram.  The bot records each tap in the inbox.

# 4. Apply taps and block until every pending PR is resolved.
agensuite human-gate --sprint <s> --drain --wait
#    → polls state/gate_inbox.json locally until all PRs resolve
#      (or --timeout elapses).  A skipped or merge-failed PR remains in
#      still_pending and is never silently dropped.

# 5. Proceed to ADR as normal.
agensuite adr record --sprint <s>
```

The bot is a **dumb relay** — it validates button taps against the pending
gate and appends them to the inbox, but it never mutates the PR registry.
All state mutations still flow through `--drain` under the state lock,
preserving the same invariants as the stdin path.

**Default (no chat).** When `state/notify.json` does not exist or
`AGENSUITE_TELEGRAM_TOKEN` is unset, use `human-gate --resolve-deadlocks`
(without `--async`) and `human-gate --message` as shown in the main sprint
loop above. No code change is needed; the CLI degrades silently.

## 5. Native Subagent Spawning — Platform Hints

The CLI contract is canonical. Native subagent dispatch is platform-specific:

- **Claude Code**: invoke the `Agent` tool with
  `subagent_type: <role>` (where `<role>` matches the `name:`/`id:`
  field in `.claude/agents/{role}.md`). The platform loads the body
  of that file as the subagent's system prompt automatically. Do not
  pass the persona text in your `prompt` argument — pass only the
  task-specific user prompt (sprint body, prereq excerpts, target
  output path).
- **Codex / OpenAI Agents SDK**: call `agents.spawn(...)` with
  `instructions = <body of .claude/agents/{role}.md, with YAML frontmatter stripped>`
  and the task as the user message. A minimal strip — split the file
  on the second `---` line and take the remainder — is sufficient;
  any frontmatter parser (e.g. `python-frontmatter`) also works. The
  frontmatter `description` field doubles as the agent's tool
  description for routing.
- **Cursor**: open a background composer per role and paste the body
  of `.claude/agents/{role}.md` as the system message. The
  `description` field tells the human (or you) which composer thread
  is which.
- **Other platforms**: follow the standard "subagent with a system
  prompt" idiom. The orchestrator's only constraint is that each
  subagent must operate with the persona file's body as its system
  prompt — never a paraphrase, never a summary.

## 6. Invariants (Enforced by the CLI)

- **Spokes never touch `main`.** They only edit files inside their
  worktree (`workspace/wt/feat__{role}__{slug}/`). The CEO (you) merges
  into `main` via `agensuite pr merge`.
- **Reviewers never review their own PR.** The CLI enforces this — a
  `pr comment --phase REVIEW` or `--phase FOLLOWUP` where
  `reviewer == pr.author` exits non-zero. The one exception is
  `--phase REBUTTAL`, which *requires* `reviewer == pr.author` and is
  the only path by which the author posts to their own PR.
- **Prerequisite files must be read from `main` first.** Any sprint
  whose YAML frontmatter declares `prerequisite_files` requires the
  orchestrator to run `agensuite read --branch <b> --path <p>` for each
  entry and inject the content into every spawned subagent's prompt.
  Skipping this step lets spokes hallucinate constraints that
  contradict the merged truth on `main`.
- **Mutations only via CLI.** Don't `git commit` from inside the
  workspace directly; use `agensuite commit` so the author identity and
  the PR registry stay consistent.
- **Persona fidelity.** Don't paraphrase `.claude/agents/{role}.md`
  when spawning a subagent. Either the platform loads it verbatim or
  you read+pass the full body verbatim.
- **Append-only debate schedule.** Turn order comes from
  `agensuite debate next-turn`, never from the LLM. Consumed slots and
  their ordering are immutable; REBUTTAL slots are appended when a
  reviewer posts `REQUEST_CHANGES`, and FOLLOWUP slots are appended once
  the author's REBUTTAL is consumed. The schedule itself is therefore an
  upper bound — `next-turn` returns `{"done": true, ...}` as soon as
  every PR is terminal (MERGED / REJECTED / DEADLOCKED) or has quorum
  with no open change requests.
- **Verdict-based merge predicate.** `agensuite pr merge` refuses a PR
  that is `DEADLOCKED`, that lacks quorum, or that has any open
  `REQUEST_CHANGES` from a non-author reviewer. The only path that can
  merge a `DEADLOCKED` PR is `agensuite human-gate --resolve-deadlocks`,
  which is an explicit human override on the record.
- **CEO owns the roadmap, not the repo.** The next-sprint blueprint
  is authored by the CEO subagent only after the current sprint's ADR
  is recorded; no spoke writes `sprints/*.md`.

## Reference: CLI Surface

```
agensuite bootstrap [--reset]
agensuite sprint show <id>
agensuite sprint list
agensuite branch create <name> [--base main]
agensuite read --branch <b> --path <p>
agensuite commit --branch <b> --author <a> --message <m> --files <f> [--files ...]
agensuite pr open --branch <b> --author <a> --title <t> --sprint <s> [--files ...] [--description ...]
agensuite pr comment --id <pr> --reviewer <r> --comment <c> \
    [--verdict APPROVE|REQUEST_CHANGES|COMMENT] \
    [--phase REVIEW|REBUTTAL|FOLLOWUP] \
    [--parent-turn-idx <n>]            # required for FOLLOWUP
    [--approve]                        # DEPRECATED — alias for --verdict APPROVE
    [--file <f>]
agensuite pr list [--sprint <s>]
agensuite pr merge --id <pr>            # refuses DEADLOCKED + open change requests
agensuite debate next-turn --sprint <s> # returns turn JSON with phase/prompt_hint
agensuite debate tail --sprint <s> [--window 6]
agensuite human-gate --message <msg>
agensuite human-gate --sprint <s> --resolve-deadlocks  # walks DEADLOCKED PRs
agensuite human-gate --sprint <s> --resolve-deadlocks --async  # chat: send buttons, return now
agensuite human-gate --sprint <s> --drain [--wait]            # chat: apply button taps
agensuite notify sprint-start --sprint <s>                     # chat: kickoff notice
agensuite bot [--once]                                         # chat: Telegram relay sidecar
agensuite adr record --sprint <s>
```

The Pydantic schemas in `src/agensuite/models.py` and the persisted state
shape under `state/` carry a `schema_version` stamp — bumping it requires
an ADR. The bounded-rebuttal protocol (verdict + phase + append-only
schedule + verdict-based termination + human-gated deadlocks) is the
current contract; any change to it is a contract change, not a refactor.

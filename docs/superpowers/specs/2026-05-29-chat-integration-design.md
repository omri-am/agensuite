# Chat Integration (Telegram + WhatsApp-ready) — Design

**Date:** 2026-05-29
**Status:** Approved (brainstorming) — pending implementation plan
**Author:** C-suite simulation maintainers

## Problem

`agensuite` has exactly one human-in-the-loop touchpoint — `agensuite
human-gate` — and it is **terminal-bound**: it blocks on `input()` / stdin.
A human can only resolve a deadlock or pass a gate while sitting at the
terminal where the run started. There is also no outbound channel: the
C-suite's decisions (ADRs) and "I need you" moments stay inside git/stdout.

We want a **chat channel** that:

1. **Outbound** — pushes C-suite events to chat: a "human needed" alert, the
   sprint-closing ADR/decision summary, and a sprint-start notice.
2. **Inbound** — lets the human resolve the gate *from chat* (merge / reject /
   adr-options / skip per deadlocked PR) instead of the terminal.

Telegram ships first. WhatsApp is honored architecturally (an abstracted
adapter slot) but not implemented now, because WhatsApp Business API requires
Meta app approval, a verified number, a public webhook, and pre-approved
message templates — orders of magnitude more onboarding than a Telegram bot
token.

## Non-goals (YAGNI)

- **No per-PR debate streaming.** Open/review/rebuttal beats are too noisy for
  chat. Only gate / decision / sprint-start events go out.
- **No WhatsApp implementation** this round — only the adapter seam + a stub.
- **No secrets in git or in any committed file.** Bot token comes from the
  environment only.
- **No second state-mutator.** The chat bot never edits the PR registry; only
  the CLI under `state_lock` mutates simulation state (preserves the existing
  core invariant).
- **No new runtime dependency.** Telegram is reached via the stdlib
  (`urllib.request`); the package keeps its three pure-Python deps.

## Core invariant being protected

> The `agensuite` CLI, driven by the orchestrator under `state_lock`, is the
> **sole mutator** of simulation state (`PRRegistry`, ADRs).

The bot is a **dumb relay**: it only reads a pending-request file the CLI
wrote, and appends validated human choices to an inbox file. It never imports
or calls the PR/debate logic. This keeps the design's blast radius to "two new
state files + one outbound call site."

## Architecture (Approach A — "Mailbox" relay)

```
                       ┌─────────────────────────────────────────┐
 orchestrator (agent)  │  agensuite CLI  (sole state mutator)     │
        │              │                                          │
        │ human-gate --resolve-deadlocks --async                  │
        ├──────────────►  writes state/gate_pending.json          │
        │              │  notify.send("human needed" + PRs) ──────┼──► Telegram
        │              │  returns {status: awaiting_human}        │     (inline
        │              └─────────────────────────────────────────┘      buttons)
        │                                                                  │
        │                        ┌──────────────────────────┐    tap      │
        │                        │  agensuite bot (sidecar)  │◄────────────┘
        │                        │  long-poll getUpdates     │
        │                        │  validate vs gate_pending │
        │                        │  append gate_inbox.json   │
        │                        └──────────────────────────┘
        │                                  │ writes inbox
        │ human-gate --drain --wait        ▼
        └──────────────►  reads gate_inbox.json under state_lock,
                          applies m/r/a/s exactly like today's loop,
                          notify.send(per-PR outcome), returns resolved
```

### Component 1 — `src/agensuite/notify.py` (outbound)

- `Notifier` (ABC): `send(self, title: str, body: str) -> None`.
- `TelegramNotifier(token, chat_id)`: POSTs to Bot API `sendMessage` via
  `urllib.request`; also exposes `send_gate(pending)` that posts an inline
  keyboard (`reply_markup` with `inline_keyboard` buttons) — one row of
  Merge/Reject/ADR/Skip buttons per deadlocked PR. `callback_data` encodes
  `<pr-id>:<choice>`.
- `WhatsAppNotifier`: stub. Constructor + methods raise `NotImplementedError`
  with a docstring pointing at the Business-API requirements. This is the
  "abstracted slot" — proves the seam without the onboarding cost.
- `NullNotifier`: every method is a no-op. Returned when chat is not
  configured, so call sites never branch on an "enabled" flag.
- `load_notifier(root) -> Notifier`: reads `state/notify.json`; resolves token
  from `AGENSUITE_TELEGRAM_TOKEN`. Returns `NullNotifier` if the file is absent
  or the env token is missing. **Opt-in by configuration presence.**

### Component 2 — outbound call sites

Each site does `load_notifier(root).send(...)` (no-op when unconfigured) and
checks the event is in the config allow-list:

- `human-gate` (gate raised) → `send_gate(pending)`.
- `adr record` (sprint close) → `send("Decision", <merged/rejected/deadlocked summary>)`.
- **new** `agensuite notify sprint-start --sprint X` → `send("Sprint start", …)`.
  A standalone command because no sprint-start command exists today; the
  orchestrator calls it at kickoff per the AGENTS.md contract.

### Component 3 — `agensuite bot` (inbound sidecar)

- Long-running command. Long-polls Telegram `getUpdates` with a persisted
  `offset` (so restarts don't replay).
- Handles `callback_query` (button taps): decode `<pr-id>:<choice>`, validate
  the PR id + choice against `state/gate_pending.json`, append
  `{pr_id, choice, ts}` to `state/gate_inbox.json`. Answers the callback so
  Telegram clears the spinner.
- Never opens `PRRegistry`. Pure relay.
- Idempotent: ignores taps for PR ids not in `gate_pending`, or already in the
  inbox.

### Component 4 — async gate flow (CLI)

- `human-gate --resolve-deadlocks --async`: collects DEADLOCKED PRs (same query
  as today), writes `gate_pending.json` (PR ids + legal choices), fires
  `send_gate`, returns `{status: "awaiting_human", pending: [...]}` and exits.
- `human-gate --drain --sprint X [--wait]`: under `state_lock`, reads
  `gate_inbox.json`, applies each choice via the **existing** per-PR logic
  (`_merge_pr(force_deadlock=True)` / reject / `human_disposition="adr_options"`
  / skip), clears applied entries from pending+inbox, emits per-PR outcome to
  chat, returns `{resolved: [...], still_pending: [...]}`.
  - `--wait`: blocks polling the **local** inbox file on an interval until
    `still_pending` is empty (or a timeout). This is a local-file poll, not a
    network poll — the bot remains the only process touching the network, and
    because state is persisted on disk a killed `--drain --wait` simply resumes
    on re-run.
- **Backward compatible:** with no `--async`/`--drain` and no chat config, the
  command keeps today's exact stdin behavior. Chat is purely additive.

### Component 5 — state + config

- `state/notify.json` (gitignored via existing `state/` rule):
  `{ "channel": "telegram", "chat_id": "...", "events": ["gate","decision","sprint-start"] }`.
  A pydantic `NotifyConfig` model in `models.py`.
- `state/gate_pending.json`, `state/gate_inbox.json`: new transient state,
  also under the gitignored `state/`.
- Token: `AGENSUITE_TELEGRAM_TOKEN` env var **only**.

## Data flow (deadlock resolution, async path)

1. Orchestrator: `human-gate --resolve-deadlocks --async --sprint s2`.
2. CLI writes `gate_pending.json`, Telegram shows inline buttons per PR.
3. Human taps **Merge** on PR-7. Bot validates, appends to `gate_inbox.json`.
4. Orchestrator: `human-gate --drain --sprint s2 --wait`.
5. CLI applies PR-7 merge under `state_lock`, posts "✅ PR-7 merged", returns
   `resolved:[7]`. When all pending cleared, drain exits and the sprint
   proceeds to `adr record`.

## Error handling

- **Network failure on send:** `notify.send` swallows + logs to stderr; an
  outbound failure must never break a sprint. (A missed alert degrades to "use
  the terminal".)
- **Bad/duplicate tap:** bot ignores silently (idempotent validation).
- **Unconfigured:** `NullNotifier` everywhere; no behavior change.
- **`--drain --wait` timeout:** returns `still_pending` non-empty with a clear
  message; re-runnable (state persisted).
- **Bot crash:** restart resumes from persisted `offset`; pending requests
  survive in `state/`.

## Testing

- `NullNotifier` no-ops; `load_notifier` returns it when unconfigured.
- `TelegramNotifier` payload shape (URL, JSON body, inline_keyboard structure)
  with `urllib` mocked — no real network.
- `WhatsAppNotifier` raises `NotImplementedError`.
- Inbox validation: rejects unknown PR id / illegal choice / duplicate.
- `--drain` applies the **same** outcomes as the existing stdin loop (merge /
  reject / adr-options / skip) — assert PR registry parity against the current
  `_resolve_deadlocks_loop` behavior.
- Backward-compat: no config + no flags ⇒ stdin path unchanged.

## WhatsApp later (documented, not built)

`WhatsAppNotifier` fills the same `Notifier` seam. Buttons map to WhatsApp
interactive reply buttons; inbound needs a hosted webhook (not long-poll), so
`bot` would grow a `--webhook` mode. All confined to the adapter — no core
changes. Captured here so the seam is intentional, not accidental.

## File-level change summary

- **new** `src/agensuite/notify.py` — Notifier ABC + Telegram/WhatsApp/Null.
- **edit** `src/agensuite/models.py` — `NotifyConfig`.
- **edit** `src/agensuite/cli.py` — `--async`/`--drain` on `human-gate`,
  `notify sprint-start` command, `bot` command, outbound calls in `adr record`.
- **new** tests under `tests/`.
- **edit** `AGENTS.md` — document the opt-in chat flow + how the orchestrator
  drives `--async` → `--drain --wait`.

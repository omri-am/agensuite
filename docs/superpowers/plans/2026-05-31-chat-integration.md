# Chat Integration (Telegram-first) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in chat channel so the human resolves `human-gate`
deadlocks from Telegram (inline buttons) and the C-suite pushes sprint-start /
decision / "human needed" notices out — without breaking the terminal path.

**Architecture:** "Mailbox relay" (Approach A in the design). The CLI stays the
sole state mutator. A dumb sidecar (`agensuite bot`) long-polls Telegram and
appends validated taps to `state/gate_inbox.json`; it never touches the PR
registry. `human-gate --resolve-deadlocks --async` writes
`state/gate_pending.json` + sends buttons; `human-gate --drain --wait` consumes
the inbox under `state_lock` and applies merge/reject/adr/skip via the existing
`_merge_pr` logic. Outbound is a `Notifier` abstraction (Telegram now, WhatsApp
stubbed, Null when unconfigured).

**Tech Stack:** Python 3.10+, pydantic v2, typer, stdlib `urllib.request` (no
new dependency). Telegram Bot API. Tests via pytest + the existing subprocess
`cli` fixture; network mocked by monkeypatching one HTTP chokepoint.

---

## File structure

- **Create** `src/agensuite/notify.py` — outbound `Notifier` ABC + `Null`,
  `Telegram`, `WhatsApp` adapters, `load_notifier`, and the single HTTP
  chokepoint `_telegram_call`.
- **Create** `src/agensuite/gate_mailbox.py` — pydantic models + atomic
  load/save for `gate_pending.json`, `gate_inbox.json`, `bot_offset.json`.
- **Modify** `src/agensuite/models.py` — add `NotifyConfig`.
- **Modify** `src/agensuite/cli.py` — `--async`/`--drain`/`--wait` on
  `human-gate`; new `notify` sub-app (`sprint-start`); new `bot` command;
  outbound send in `adr record`.
- **Modify** `AGENTS.md` — document the opt-in chat flow.
- **Create** `tests/test_notify.py`, `tests/test_gate_mailbox.py`,
  `tests/test_chat_cli.py`.

A key boundary: `notify.py` owns *outbound* (talk to Telegram), `gate_mailbox.py`
owns *the relay files* (pending/inbox/offset). `cli.py` wires them; the bot reads
mailbox + calls `_telegram_call` but never imports PR/debate logic.

---

## Task 1: `NotifyConfig` model

**Files:**
- Modify: `src/agensuite/models.py` (add at end of the "schemas" section)
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_notify_config_defaults_and_validation():
    from agensuite.models import NotifyConfig

    cfg = NotifyConfig(chat_id="123456")
    assert cfg.channel == "telegram"
    assert cfg.chat_id == "123456"
    assert cfg.events == ["gate", "decision", "sprint-start"]

    # extra keys are rejected so a typo'd config fails loudly
    import pydantic
    import pytest
    with pytest.raises(pydantic.ValidationError):
        NotifyConfig(chat_id="1", bogus=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py::test_notify_config_defaults_and_validation -v`
Expected: FAIL with `ImportError: cannot import name 'NotifyConfig'`.

- [ ] **Step 3: Add the model**

Add to `src/agensuite/models.py` (after the existing models, before any
trailing module code):

```python
class NotifyConfig(BaseModel):
    """Opt-in chat configuration, persisted at ``state/notify.json``.

    The bot *token* is never stored here — it comes from the
    ``AGENSUITE_TELEGRAM_TOKEN`` environment variable only. This file holds
    just the non-secret routing: which channel, which chat, which events.
    """

    model_config = ConfigDict(extra="forbid")

    channel: str = "telegram"
    chat_id: str
    events: list[str] = Field(
        default_factory=lambda: ["gate", "decision", "sprint-start"]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models.py::test_notify_config_defaults_and_validation -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/models.py tests/test_models.py
git commit -m "feat(models): add NotifyConfig for opt-in chat routing"
```

---

## Task 2: Gate mailbox (pending / inbox / offset state)

**Files:**
- Create: `src/agensuite/gate_mailbox.py`
- Test: `tests/test_gate_mailbox.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gate_mailbox.py`:

```python
from __future__ import annotations

from pathlib import Path

from agensuite.gate_mailbox import (
    GateMailbox,
    PendingGate,
    PendingPR,
)


def test_pending_roundtrip(tmp_path: Path) -> None:
    mb = GateMailbox(tmp_path)
    assert mb.load_pending() is None  # absent → None

    pending = PendingGate(
        sprint_id="s",
        prs=[PendingPR(pr_id="abc123", title="Add X")],
    )
    mb.save_pending(pending)
    loaded = mb.load_pending()
    assert loaded is not None
    assert loaded.sprint_id == "s"
    assert loaded.prs[0].pr_id == "abc123"
    assert mb.is_legal("abc123", "m") is True
    assert mb.is_legal("nope", "m") is False
    assert mb.is_legal("abc123", "x") is False


def test_inbox_append_and_drain(tmp_path: Path) -> None:
    mb = GateMailbox(tmp_path)
    assert mb.load_inbox() == []

    mb.append_inbox(pr_id="abc123", choice="m")
    mb.append_inbox(pr_id="def456", choice="r")
    entries = mb.load_inbox()
    assert [(e.pr_id, e.choice) for e in entries] == [
        ("abc123", "m"),
        ("def456", "r"),
    ]

    mb.clear_inbox()
    assert mb.load_inbox() == []


def test_offset_roundtrip(tmp_path: Path) -> None:
    mb = GateMailbox(tmp_path)
    assert mb.load_offset() == 0
    mb.save_offset(42)
    assert mb.load_offset() == 42


def test_remove_pending_pr(tmp_path: Path) -> None:
    mb = GateMailbox(tmp_path)
    mb.save_pending(
        PendingGate(
            sprint_id="s",
            prs=[PendingPR(pr_id="a", title="A"), PendingPR(pr_id="b", title="B")],
        )
    )
    mb.remove_pending_pr("a")
    loaded = mb.load_pending()
    assert [p.pr_id for p in loaded.prs] == ["b"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gate_mailbox.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agensuite.gate_mailbox'`.

- [ ] **Step 3: Implement the module**

Create `src/agensuite/gate_mailbox.py`:

```python
"""Relay state for chat-driven human-gate resolution.

Three small JSON files live under ``state/`` and are the *only* shared
surface between the CLI (which mutates the PR registry) and the ``bot``
sidecar (which never does):

* ``gate_pending.json`` — written by ``human-gate --async``: the deadlocked
  PRs awaiting a human decision, plus the legal choices. The bot validates
  taps against this.
* ``gate_inbox.json`` — appended by the bot when a human taps a button;
  drained by ``human-gate --drain`` which applies each choice.
* ``bot_offset.json`` — the Telegram ``getUpdates`` offset so a restarted
  bot doesn't replay old updates.

Reuses :func:`agensuite.state._atomic_write` so writes are crash-safe and
consistent with the rest of the state store. These files are intentionally
*not* schema-versioned like ``prs.json``: they are ephemeral coordination,
regenerated every gate, so a stale-format file just gets overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import _utcnow
from .state import _atomic_write, _state_dir

LEGAL_CHOICES = ("m", "r", "a", "s")  # merge / reject / adr-options / skip


class PendingPR(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pr_id: str
    title: str


class PendingGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sprint_id: str
    prs: list[PendingPR] = Field(default_factory=list)


class InboxEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pr_id: str
    choice: str
    ts: str = Field(default_factory=lambda: _utcnow().isoformat())


class GateMailbox:
    """Stateless helper bound to one project root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- paths ------------------------------------------------------------
    def _pending_path(self) -> Path:
        return _state_dir(self.root) / "gate_pending.json"

    def _inbox_path(self) -> Path:
        return _state_dir(self.root) / "gate_inbox.json"

    def _offset_path(self) -> Path:
        return _state_dir(self.root) / "bot_offset.json"

    # -- pending ----------------------------------------------------------
    def save_pending(self, pending: PendingGate) -> None:
        _atomic_write(
            self._pending_path(),
            pending.model_dump_json(indent=2) + "\n",
        )

    def load_pending(self) -> PendingGate | None:
        path = self._pending_path()
        if not path.exists():
            return None
        return PendingGate.model_validate_json(path.read_text(encoding="utf-8"))

    def remove_pending_pr(self, pr_id: str) -> None:
        pending = self.load_pending()
        if pending is None:
            return
        pending.prs = [p for p in pending.prs if p.pr_id != pr_id]
        self.save_pending(pending)

    def is_legal(self, pr_id: str, choice: str) -> bool:
        if choice not in LEGAL_CHOICES:
            return False
        pending = self.load_pending()
        if pending is None:
            return False
        return any(p.pr_id == pr_id for p in pending.prs)

    # -- inbox ------------------------------------------------------------
    def load_inbox(self) -> list[InboxEntry]:
        path = self._inbox_path()
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [InboxEntry.model_validate(e) for e in raw]

    def append_inbox(self, pr_id: str, choice: str) -> None:
        entries = self.load_inbox()
        entries.append(InboxEntry(pr_id=pr_id, choice=choice))
        _atomic_write(
            self._inbox_path(),
            json.dumps([json.loads(e.model_dump_json()) for e in entries], indent=2)
            + "\n",
        )

    def clear_inbox(self) -> None:
        _atomic_write(self._inbox_path(), "[]\n")

    # -- offset -----------------------------------------------------------
    def load_offset(self) -> int:
        path = self._offset_path()
        if not path.exists():
            return 0
        return int(json.loads(path.read_text(encoding="utf-8")).get("offset", 0))

    def save_offset(self, offset: int) -> None:
        _atomic_write(
            self._offset_path(),
            json.dumps({"offset": offset}) + "\n",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gate_mailbox.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/gate_mailbox.py tests/test_gate_mailbox.py
git commit -m "feat(gate-mailbox): atomic pending/inbox/offset relay state"
```

---

## Task 3: `Notifier` abstraction (Null / Telegram / WhatsApp + loader)

**Files:**
- Create: `src/agensuite/notify.py`
- Test: `tests/test_notify.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agensuite import notify
from agensuite.gate_mailbox import PendingGate, PendingPR


def test_null_notifier_is_noop(tmp_path: Path) -> None:
    n = notify.NullNotifier()
    # must not raise, must not call network
    n.send("Title", "Body")
    n.send_gate(PendingGate(sprint_id="s", prs=[]))


def test_load_notifier_returns_null_when_unconfigured(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    assert isinstance(notify.load_notifier(tmp_path), notify.NullNotifier)


def test_load_notifier_returns_null_without_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGENSUITE_TELEGRAM_TOKEN", raising=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "notify.json").write_text(
        json.dumps({"channel": "telegram", "chat_id": "42"})
    )
    # config present but no token in env → degrade to Null, never crash a sprint
    assert isinstance(notify.load_notifier(tmp_path), notify.NullNotifier)


def test_load_notifier_returns_telegram_when_configured(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENSUITE_TELEGRAM_TOKEN", "tok")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "notify.json").write_text(
        json.dumps({"channel": "telegram", "chat_id": "42"})
    )
    n = notify.load_notifier(tmp_path)
    assert isinstance(n, notify.TelegramNotifier)


def test_telegram_send_posts_sendmessage(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_call(token, method, payload):
        calls.append((token, method, payload))
        return {"ok": True}

    monkeypatch.setattr(notify, "_telegram_call", fake_call)
    n = notify.TelegramNotifier(token="tok", chat_id="42", events=["decision"])
    n.send("Decision", "merged X")

    assert len(calls) == 1
    token, method, payload = calls[0]
    assert token == "tok"
    assert method == "sendMessage"
    assert payload["chat_id"] == "42"
    assert "Decision" in payload["text"]
    assert "merged X" in payload["text"]


def test_telegram_send_gate_builds_inline_keyboard(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        notify, "_telegram_call", lambda t, m, p: calls.append((m, p)) or {"ok": True}
    )
    n = notify.TelegramNotifier(token="tok", chat_id="42", events=["gate"])
    n.send_gate(
        PendingGate(
            sprint_id="s",
            prs=[PendingPR(pr_id="abc", title="Add X")],
        )
    )
    method, payload = calls[0]
    assert method == "sendMessage"
    kb = payload["reply_markup"]["inline_keyboard"]
    # one row per PR, four buttons (merge/reject/adr/skip)
    labels = [b["text"] for b in kb[0]]
    assert any("Merge" in label for label in labels)
    datas = [b["callback_data"] for b in kb[0]]
    assert "abc:m" in datas
    assert "abc:r" in datas


def test_event_filtering_skips_unlisted_events(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        notify, "_telegram_call", lambda t, m, p: calls.append(m) or {"ok": True}
    )
    # only "gate" enabled → a decision send is suppressed
    n = notify.TelegramNotifier(token="tok", chat_id="42", events=["gate"])
    n.send("Decision", "x", event="decision")
    assert calls == []


def test_send_swallows_network_errors(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(notify, "_telegram_call", boom)
    n = notify.TelegramNotifier(token="tok", chat_id="42", events=["decision"])
    # must NOT raise — a missed alert can never break a sprint
    n.send("Decision", "x")


def test_whatsapp_is_stub() -> None:
    with pytest.raises(NotImplementedError):
        notify.WhatsAppNotifier(token="tok", chat_id="42", events=[])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agensuite.notify'`.

- [ ] **Step 3: Implement the module**

Create `src/agensuite/notify.py`:

```python
"""Outbound chat notifications — the only place agensuite talks *out* over the network.

Opt-in by configuration presence: if ``state/notify.json`` is absent or the
``AGENSUITE_TELEGRAM_TOKEN`` env var is missing, :func:`load_notifier` returns
a :class:`NullNotifier` whose every method is a no-op. Call sites therefore
never branch on an "enabled" flag — they always call ``load_notifier(root).send(...)``.

Telegram is reached through the stdlib (``urllib.request``) so the package
keeps its three pure-Python dependencies. All HTTP goes through the single
:func:`_telegram_call` chokepoint, which tests monkeypatch to assert payload
shape without touching the network.

WhatsApp fills the same :class:`Notifier` seam but is a stub: the Business API
needs a Meta app, a verified number, a hosted webhook, and pre-approved
templates, none of which are in scope here.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .gate_mailbox import PendingGate
from .models import NotifyConfig

_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Maps the internal choice letter to a human button label + callback suffix.
_GATE_BUTTONS = (
    ("Merge", "m"),
    ("Reject", "r"),
    ("ADR-options", "a"),
    ("Skip", "s"),
)


def _telegram_call(token: str, method: str, payload: dict) -> dict:
    """POST ``payload`` as JSON to the Telegram Bot API. The HTTP chokepoint.

    Raises ``urllib.error.URLError`` / ``OSError`` on transport failure — the
    caller (:meth:`TelegramNotifier.send`) is responsible for swallowing those
    so an outbound failure never breaks a sprint.
    """
    url = _API_BASE.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Notifier(ABC):
    """Outbound channel. ``event`` lets a send be suppressed by config."""

    @abstractmethod
    def send(self, title: str, body: str, *, event: str | None = None) -> None: ...

    @abstractmethod
    def send_gate(self, pending: PendingGate) -> None: ...


class NullNotifier(Notifier):
    """No-op channel used when chat is unconfigured."""

    def send(self, title: str, body: str, *, event: str | None = None) -> None:
        return None

    def send_gate(self, pending: PendingGate) -> None:
        return None


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, events: list[str]) -> None:
        self.token = token
        self.chat_id = chat_id
        self.events = events

    def _enabled(self, event: str | None) -> bool:
        return event is None or event in self.events

    def send(self, title: str, body: str, *, event: str | None = None) -> None:
        if not self._enabled(event):
            return
        text = f"*{title}*\n{body}"
        try:
            _telegram_call(
                self.token,
                "sendMessage",
                {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
            )
        except Exception as e:  # noqa: BLE001 — outbound failure must never break a sprint
            print(f"notify: telegram send failed: {e}", file=sys.stderr)

    def send_gate(self, pending: PendingGate) -> None:
        if not self._enabled("gate"):
            return
        lines = ["A human decision is needed on these deadlocked PRs:"]
        keyboard = []
        for pr in pending.prs:
            lines.append(f"• {pr.pr_id} — {pr.title}")
            keyboard.append(
                [
                    {"text": f"{label} {pr.pr_id}", "callback_data": f"{pr.pr_id}:{ch}"}
                    for (label, ch) in _GATE_BUTTONS
                ]
            )
        try:
            _telegram_call(
                self.token,
                "sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": "\n".join(lines),
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            )
        except Exception as e:  # noqa: BLE001
            print(f"notify: telegram send_gate failed: {e}", file=sys.stderr)


class WhatsAppNotifier(Notifier):
    """Adapter seam for WhatsApp Business API — intentionally unimplemented.

    Documented here so the seam is deliberate, not accidental. Implementing it
    requires a Meta app, a verified sender number, a hosted webhook (the bot
    would grow a ``--webhook`` mode instead of long-poll), and pre-approved
    message templates. Out of scope for the Telegram-first cut.
    """

    def __init__(self, token: str, chat_id: str, events: list[str]) -> None:
        raise NotImplementedError(
            "WhatsApp Business API is not implemented yet; use channel='telegram'. "
            "See docs/superpowers/specs/2026-05-29-chat-integration-design.md."
        )

    def send(self, title: str, body: str, *, event: str | None = None) -> None:
        raise NotImplementedError

    def send_gate(self, pending: PendingGate) -> None:
        raise NotImplementedError


def load_notifier(root: Path) -> Notifier:
    """Build a notifier from ``state/notify.json`` + env token, or ``NullNotifier``.

    Never raises on missing/invalid config — chat is strictly additive, so any
    problem degrades to "no chat, use the terminal".
    """
    import os

    cfg_path = root / "state" / "notify.json"
    if not cfg_path.exists():
        return NullNotifier()
    try:
        cfg = NotifyConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return NullNotifier()

    if cfg.channel == "telegram":
        token = os.environ.get("AGENSUITE_TELEGRAM_TOKEN")
        if not token:
            return NullNotifier()
        return TelegramNotifier(token=token, chat_id=cfg.chat_id, events=cfg.events)
    if cfg.channel == "whatsapp":
        return NullNotifier()  # stub adapter not wired; degrade quietly
    return NullNotifier()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notify.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/notify.py tests/test_notify.py
git commit -m "feat(notify): Null/Telegram/WhatsApp Notifier abstraction + loader"
```

---

## Task 4: `human-gate --resolve-deadlocks --async` writes pending + sends buttons

**Files:**
- Modify: `src/agensuite/cli.py` (the `human_gate` command, ~line 1183, and
  imports near the top)
- Test: `tests/test_chat_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_cli.py`. It reuses the **proven** deadlock setup from
`tests/test_cli.py::TestHumanGateResolveDeadlocks` (copied as module-level
helpers) so the deadlocked PR has a real workspace git branch — essential
because the `--drain` merge in Task 6 actually merges that branch. Do **not**
hand-write `prs.json`: that skips the git branch and the merge fails.

```python
from __future__ import annotations

import json
from pathlib import Path


def _open_pr(cli, project_root: Path, role: str, br: str, path: str) -> str:
    """Branch + commit a file + open a PR. Copied from test_cli.py."""
    cli("branch", "create", br)
    slug = br.replace("/", "__")
    f = project_root / "workspace" / "wt" / slug / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"draft by {role}\n")
    cli("commit", "--branch", br, "--author", role,
        "--message", f"{role} draft", "--files", path)
    return cli("pr", "open", "--branch", br, "--author", role,
               "--title", f"{role}: t", "--sprint", "s",
               "--files", path).stdout.strip()


def _drive_to_deadlock(cli, project_root: Path) -> str:
    """Open one PR and drive it to DEADLOCKED. Returns its id.

    Copied verbatim from test_cli.py::TestHumanGateResolveDeadlocks so the
    deadlock is produced through the real CLI state machine.
    """
    cli("bootstrap")
    pr_a = _open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
    cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
        "--comment", "lgtm", "--verdict", "APPROVE", "--phase", "REVIEW")
    cli("pr", "comment", "--id", pr_a, "--reviewer", "c",
        "--comment", "blocker", "--verdict", "REQUEST_CHANGES", "--phase", "REVIEW")
    cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
        "--comment", "see commit X", "--verdict", "COMMENT", "--phase", "REBUTTAL")
    debate = json.loads(
        (project_root / "state" / "debates" / "s.json").read_text()
    )["debate"]
    rebuttal_idx = next(
        i for i, t in enumerate(debate["schedule"]) if t["phase"] == "REBUTTAL"
    )
    cli("pr", "comment", "--id", pr_a, "--reviewer", "c",
        "--comment", "still blocked", "--verdict", "REQUEST_CHANGES",
        "--phase", "FOLLOWUP", "--parent-turn-idx", str(rebuttal_idx))
    return pr_a


def test_async_gate_writes_pending_and_returns_awaiting(cli, project_root):
    pr_a = _drive_to_deadlock(cli, project_root)
    # No notify config → NullNotifier, so no network. We only assert state.
    p = cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    out = json.loads(p.stdout)
    assert out["status"] == "awaiting_human"
    assert out["pending"] == [pr_a]

    pending = json.loads((project_root / "state" / "gate_pending.json").read_text())
    assert pending["sprint_id"] == "s"
    assert pending["prs"][0]["pr_id"] == pr_a


def test_async_gate_no_deadlocks_is_clean(cli, project_root):
    cli("bootstrap")
    _open_pr(cli, project_root, "a", "feat/a/x", "fa.md")  # open, not deadlocked
    p = cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    out = json.loads(p.stdout)
    assert out["status"] == "awaiting_human"
    assert out["pending"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_chat_cli.py::test_async_gate_writes_pending_and_returns_awaiting -v`
Expected: FAIL — `--async` is an unknown option (typer exits non-zero).

- [ ] **Step 3: Add imports + the `--async` branch**

In `src/agensuite/cli.py`, extend the imports:

```python
from .gate_mailbox import GateMailbox, PendingGate, PendingPR
from .notify import load_notifier
```

Change the `human_gate` signature to add the two flags (keep existing params):

```python
@app.command("human-gate")
def human_gate(
    ctx: typer.Context,
    message: Optional[str] = typer.Option(None, "--message"),
    sprint: Optional[str] = typer.Option(None, "--sprint"),
    resolve_deadlocks: bool = typer.Option(
        False,
        "--resolve-deadlocks",
        help="Iterate over DEADLOCKED PRs in --sprint and prompt the human "
             "to [m]erge / [r]eject / [a]dr-options / [s]kip each one.",
    ),
    async_gate: bool = typer.Option(
        False,
        "--async",
        help="Chat mode: write the gate to state/ + send buttons, then return "
             "immediately ({status: awaiting_human}) instead of blocking on stdin.",
    ),
    drain: bool = typer.Option(
        False,
        "--drain",
        help="Chat mode: apply human choices collected in state/gate_inbox.json.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="With --drain: block (polling the local inbox file) until every "
             "pending PR is resolved or --timeout elapses.",
    ),
    timeout: float = typer.Option(
        600.0, "--timeout", help="With --drain --wait: max seconds to wait."
    ),
) -> None:
```

Then at the top of the body, **before** the existing `if resolve_deadlocks:`
block, route the chat modes:

```python
    if drain:
        if not sprint:
            raise _err("--drain requires --sprint <id>")
        _drain_gate(ctx, sprint, wait=wait, timeout=timeout)
        return

    if resolve_deadlocks and async_gate:
        if not sprint:
            raise _err("--resolve-deadlocks requires --sprint <id>")
        _async_gate(ctx, sprint)
        return
```

Add the `_async_gate` helper just below `_resolve_deadlocks_loop`:

```python
def _async_gate(ctx: typer.Context, sprint: str) -> None:
    """Chat mode: persist the deadlocked PRs + send buttons, then return.

    Unlike :func:`_resolve_deadlocks_loop` this never blocks on stdin; the
    human answers from chat and the orchestrator later calls
    ``human-gate --drain``.
    """
    root = _root(ctx)
    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
            deadlocked = sorted(
                [p for p in prs.values()
                 if p.sprint_id == sprint and p.status == PRStatus.DEADLOCKED],
                key=lambda p: p.created_at,
            )
            pending = PendingGate(
                sprint_id=sprint,
                prs=[PendingPR(pr_id=p.id, title=p.title) for p in deadlocked],
            )
            mb = GateMailbox(root)
            mb.save_pending(pending)
            mb.clear_inbox()
        # send outside the lock — network must not hold the state mutex
        load_notifier(root).send_gate(pending)
        typer.echo(json.dumps(
            {"status": "awaiting_human", "pending": [p.id for p in deadlocked]}
        ))
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e
```

(`_drain_gate` is added in Task 6; to keep this task green, add a temporary
stub right after `_async_gate` that Task 6 replaces:)

```python
def _drain_gate(ctx: typer.Context, sprint: str, *, wait: bool, timeout: float) -> None:
    raise _err("--drain not yet implemented")  # replaced in Task 6
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chat_cli.py -k async_gate -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/cli.py tests/test_chat_cli.py
git commit -m "feat(human-gate): --async writes pending + sends chat buttons"
```

---

## Task 5: `agensuite bot` sidecar (long-poll → validate → inbox)

**Files:**
- Modify: `src/agensuite/cli.py` (new `bot` command + a `_bot_poll_once` helper)
- Test: `tests/test_chat_cli.py`

The bot is a dumb relay. To keep it testable without an infinite loop or real
network, factor the per-batch logic into `_bot_poll_once(root, token, mailbox)`
and give the command a `--once` flag.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat_cli.py`:

```python
def test_bot_poll_once_records_valid_tap(project_root, monkeypatch):
    from agensuite import cli as cli_mod
    from agensuite.gate_mailbox import GateMailbox, PendingGate, PendingPR

    mb = GateMailbox(project_root)
    mb.save_pending(PendingGate(sprint_id="s", prs=[PendingPR(pr_id="abc123", title="X")]))

    # Fake one getUpdates batch: a single callback_query tapping "abc123:m".
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 7,
                "callback_query": {
                    "id": "cb1",
                    "data": "abc123:m",
                    "message": {"chat": {"id": 42}},
                },
            }
        ],
    }
    calls = []

    def fake_call(token, method, payload):
        calls.append((method, payload))
        if method == "getUpdates":
            return updates
        return {"ok": True}

    monkeypatch.setattr(cli_mod, "_telegram_call", fake_call)

    new_offset = cli_mod._bot_poll_once(project_root, "tok", mb)

    # inbox got the tap; offset advanced past update_id; callback was answered
    assert [(e.pr_id, e.choice) for e in mb.load_inbox()] == [("abc123", "m")]
    assert new_offset == 8
    assert mb.load_offset() == 8
    assert any(m == "answerCallbackQuery" for (m, _) in calls)


def test_bot_poll_once_ignores_illegal_tap(project_root, monkeypatch):
    from agensuite import cli as cli_mod
    from agensuite.gate_mailbox import GateMailbox, PendingGate, PendingPR

    mb = GateMailbox(project_root)
    mb.save_pending(PendingGate(sprint_id="s", prs=[PendingPR(pr_id="abc123", title="X")]))

    updates = {
        "ok": True,
        "result": [
            {  # PR not in pending
                "update_id": 3,
                "callback_query": {"id": "c", "data": "ghost:m",
                                   "message": {"chat": {"id": 42}}},
            },
            {  # illegal choice
                "update_id": 4,
                "callback_query": {"id": "c2", "data": "abc123:z",
                                   "message": {"chat": {"id": 42}}},
            },
        ],
    }
    monkeypatch.setattr(
        cli_mod, "_telegram_call",
        lambda t, m, p: updates if m == "getUpdates" else {"ok": True},
    )

    cli_mod._bot_poll_once(project_root, "tok", mb)
    assert mb.load_inbox() == []          # nothing illegal recorded
    assert mb.load_offset() == 5          # offset still advances past both
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_chat_cli.py -k bot_poll -v`
Expected: FAIL with `AttributeError: module 'agensuite.cli' has no attribute '_bot_poll_once'`.

- [ ] **Step 3: Implement `_bot_poll_once`, the `bot` command, and re-export `_telegram_call`**

In `src/agensuite/cli.py` extend the notify import so tests can monkeypatch the
HTTP chokepoint on the `cli` module:

```python
from .notify import load_notifier, _telegram_call
```

Add the helper + command (place near the `human-gate` section):

```python
def _bot_poll_once(root: Path, token: str, mailbox: "GateMailbox") -> int:
    """Process one ``getUpdates`` batch. Returns the next offset.

    Pure relay: validates each ``callback_query`` against gate_pending and
    appends legal taps to the inbox. Never touches the PR registry. Always
    advances the offset past every update so a poisoned update can't wedge
    the bot.
    """
    offset = mailbox.load_offset()
    resp = _telegram_call(
        token, "getUpdates", {"offset": offset, "timeout": 25}
    )
    next_offset = offset
    for update in resp.get("result", []):
        next_offset = max(next_offset, int(update["update_id"]) + 1)
        cq = update.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        pr_id, _, choice = data.partition(":")
        if mailbox.is_legal(pr_id, choice):
            mailbox.append_inbox(pr_id=pr_id, choice=choice)
        # acknowledge the tap so Telegram clears the client-side spinner
        try:
            _telegram_call(token, "answerCallbackQuery", {"callback_query_id": cq["id"]})
        except Exception:  # noqa: BLE001
            pass
    mailbox.save_offset(next_offset)
    return next_offset


@app.command("bot")
def bot(
    ctx: typer.Context,
    once: bool = typer.Option(
        False, "--once", help="Process a single update batch then exit (for testing)."
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between getUpdates batches."
    ),
) -> None:
    """Run the Telegram relay sidecar: long-poll updates, record button taps.

    Reads the token from ``AGENSUITE_TELEGRAM_TOKEN``. The bot never mutates
    simulation state — it only appends validated taps to state/gate_inbox.json,
    which ``human-gate --drain`` later applies under the state lock.
    """
    import os
    import time

    root = _root(ctx)
    token = os.environ.get("AGENSUITE_TELEGRAM_TOKEN")
    if not token:
        raise _err("AGENSUITE_TELEGRAM_TOKEN is not set; cannot start bot")
    mailbox = GateMailbox(root)
    typer.echo("bot: polling Telegram (Ctrl-C to stop)", err=True)
    while True:
        try:
            _bot_poll_once(root, token, mailbox)
        except Exception as e:  # noqa: BLE001 — a relay must survive transient errors
            typer.echo(f"bot: poll error: {e}", err=True)
        if once:
            return
        time.sleep(poll_interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chat_cli.py -k bot_poll -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/cli.py tests/test_chat_cli.py
git commit -m "feat(bot): Telegram relay sidecar appends validated taps to inbox"
```

---

## Task 6: `human-gate --drain [--wait]` applies inbox choices

**Files:**
- Modify: `src/agensuite/cli.py` (replace the `_drain_gate` stub from Task 4)
- Test: `tests/test_chat_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat_cli.py`:

These reuse `_drive_to_deadlock` / `_open_pr` from the top of
`tests/test_chat_cli.py` (Task 4) so each PR has a real workspace branch. The
flow per test: drive to deadlock → `--async` to write `gate_pending.json` →
`append_inbox` to simulate the human's tap → `--drain` → assert outcome.

```python
def test_drain_applies_merge_and_clears_pending(cli, project_root):
    from agensuite.models import PRStatus
    from agensuite.state import PRRegistry
    from agensuite.gate_mailbox import GateMailbox

    pr_a = _drive_to_deadlock(cli, project_root)
    cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")  # writes pending
    GateMailbox(project_root).append_inbox(pr_id=pr_a, choice="m")  # simulate the tap

    p = cli("human-gate", "--sprint", "s", "--drain")
    out = json.loads(p.stdout)
    assert {"pr": pr_a, "action": "merge"} in [
        {"pr": r["pr"], "action": r["action"]} for r in out["resolved"]
    ]
    assert out["still_pending"] == []
    assert PRRegistry.load(project_root)[pr_a].status == PRStatus.MERGED


def test_drain_reject_marks_rejected(cli, project_root):
    from agensuite.models import PRStatus
    from agensuite.state import PRRegistry
    from agensuite.gate_mailbox import GateMailbox

    pr_a = _drive_to_deadlock(cli, project_root)
    cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    GateMailbox(project_root).append_inbox(pr_id=pr_a, choice="r")

    cli("human-gate", "--sprint", "s", "--drain")
    assert PRRegistry.load(project_root)[pr_a].status == PRStatus.REJECTED


def test_drain_adr_options_sets_disposition(cli, project_root):
    from agensuite.state import PRRegistry
    from agensuite.gate_mailbox import GateMailbox

    pr_a = _drive_to_deadlock(cli, project_root)
    cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    GateMailbox(project_root).append_inbox(pr_id=pr_a, choice="a")

    cli("human-gate", "--sprint", "s", "--drain")
    assert PRRegistry.load(project_root)[pr_a].human_disposition == "adr_options"


def test_drain_reports_still_pending(cli, project_root):
    # async wrote pending but no tap arrived → nothing resolved, PR still pending
    pr_a = _drive_to_deadlock(cli, project_root)
    cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    p = cli("human-gate", "--sprint", "s", "--drain")
    out = json.loads(p.stdout)
    assert out["resolved"] == []
    assert out["still_pending"] == [pr_a]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_chat_cli.py -k drain -v`
Expected: FAIL — the stub raises `--drain not yet implemented`.

- [ ] **Step 3: Replace the `_drain_gate` stub**

Replace the Task-4 stub in `src/agensuite/cli.py` with the real implementation:

```python
def _drain_gate(
    ctx: typer.Context, sprint: str, *, wait: bool, timeout: float
) -> None:
    """Apply human choices from the inbox to the gate's pending PRs.

    Applies each inbox entry via the SAME primitives as the stdin loop
    (:func:`_merge_pr` / reject / adr-options / skip), then drops the PR from
    pending. With ``--wait`` it re-reads the inbox on an interval until pending
    is empty or ``timeout`` elapses. The poll is over the LOCAL inbox file, not
    the network — the bot is the only process doing network I/O.
    """
    import time

    root = _root(ctx)
    mb = GateMailbox(root)
    deadline = None  # set lazily; Date.now-free monotonic clock
    poll_interval = 2.0

    while True:
        resolved: list[dict] = []
        try:
            with state_lock(root):
                prs = PRRegistry.load(root)
                pending = mb.load_pending()
                if pending is None:
                    typer.echo(json.dumps({"resolved": [], "still_pending": []}))
                    return
                pending_ids = {p.pr_id for p in pending.prs}
                inbox = mb.load_inbox()
                for entry in inbox:
                    if entry.pr_id not in pending_ids:
                        continue  # tap for an already-resolved / unknown PR
                    if entry.choice == "m":
                        try:
                            sha = _merge_pr(ctx, prs, entry.pr_id, force_deadlock=True)
                            resolved.append({"pr": entry.pr_id, "action": "merge", "sha": sha})
                        except typer.Exit:
                            resolved.append({"pr": entry.pr_id, "action": "merge_failed"})
                    elif entry.choice == "r":
                        prs[entry.pr_id].status = PRStatus.REJECTED
                        resolved.append({"pr": entry.pr_id, "action": "reject"})
                    elif entry.choice == "a":
                        prs[entry.pr_id].human_disposition = "adr_options"
                        resolved.append({"pr": entry.pr_id, "action": "adr_options"})
                    else:  # "s"
                        resolved.append({"pr": entry.pr_id, "action": "skip"})
                    pending_ids.discard(entry.pr_id)
                PRRegistry.save(root, prs)
                mb.clear_inbox()
                pending.prs = [p for p in pending.prs if p.pr_id in pending_ids]
                mb.save_pending(pending)
                still_pending = [p.pr_id for p in pending.prs]
        except StateLockTimeout as e:
            raise _err(str(e)) from e
        except StateSchemaMismatch as e:
            raise _err(str(e)) from e

        # emit per-PR outcomes to chat (outside the lock; never fatal)
        notifier = load_notifier(root)
        for r in resolved:
            notifier.send("Gate", f"{r['pr']}: {r['action']}", event="gate")

        if not wait or not still_pending:
            typer.echo(json.dumps({"resolved": resolved, "still_pending": still_pending}))
            return

        # --wait: sleep then loop. Use monotonic so we never read wall-clock.
        if deadline is None:
            deadline = time.monotonic() + timeout
        if time.monotonic() >= deadline:
            typer.echo(json.dumps({"resolved": resolved, "still_pending": still_pending}))
            return
        time.sleep(poll_interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chat_cli.py -k drain -v`
Expected: PASS (3 tests).

Then run the whole chat suite: `python -m pytest tests/test_chat_cli.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/cli.py tests/test_chat_cli.py
git commit -m "feat(human-gate): --drain applies inbox choices; --wait polls inbox"
```

---

## Task 7: Outbound sends for `adr record` + `notify sprint-start`

**Files:**
- Modify: `src/agensuite/cli.py` (`adr_record` ~line 1335; new `notify` sub-app)
- Test: `tests/test_chat_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chat_cli.py`:

```python
def test_notify_sprint_start_sends(cli, project_root, monkeypatch):
    # configure telegram so load_notifier builds a real TelegramNotifier,
    # then capture the send via the cli-module HTTP chokepoint.
    (project_root / "state").mkdir(exist_ok=True)
    (project_root / "state" / "notify.json").write_text(
        json.dumps({"channel": "telegram", "chat_id": "42",
                    "events": ["sprint-start"]})
    )
    env = dict(__import__("os").environ)
    # The `cli` fixture builds its own env; assert behavior via a unit call instead.
    from agensuite import notify as notify_mod
    sent = []
    monkeypatch.setattr(notify_mod, "_telegram_call",
                        lambda t, m, p: sent.append(p) or {"ok": True})
    monkeypatch.setenv("AGENSUITE_TELEGRAM_TOKEN", "tok")

    n = notify_mod.load_notifier(project_root)
    n.send("Sprint start", "s — debate opening", event="sprint-start")
    assert sent and "Sprint start" in sent[0]["text"]
```

> Note: this test exercises the notifier wiring directly (the subprocess `cli`
> fixture can't share monkeypatched env easily). The `notify sprint-start`
> command itself is verified by the smoke test in Step 4.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_chat_cli.py -k sprint_start -v`
Expected: PASS already for the notifier path — but the `notify` command does
not exist yet; verify with `python -m agensuite.cli notify sprint-start --sprint s`
which should error "No such command". Proceed to add the command.

- [ ] **Step 3: Add the `notify` sub-app + wire `adr record`**

In `src/agensuite/cli.py`, register a new typer sub-app near the others:

```python
notify_app = typer.Typer(help="Outbound chat notifications.", no_args_is_help=True)
app.add_typer(notify_app, name="notify")
```

```python
@notify_app.command("sprint-start")
def notify_sprint_start(
    ctx: typer.Context,
    sprint: str = typer.Option(..., "--sprint"),
) -> None:
    """Send a 'sprint started' notice to chat (no-op if chat unconfigured)."""
    root = _root(ctx)
    cfg = _load_sprint_or_die(root, sprint)
    load_notifier(root).send(
        "Sprint start",
        f"{cfg.id}: {cfg.title}\nParticipants: {', '.join(cfg.participants)}",
        event="sprint-start",
    )
    typer.echo(json.dumps({"notified": "sprint-start", "sprint": sprint}))
```

In `adr_record`, after `decision` is composed and the ADR is written (locate
the end of the function where it echoes its result), add:

```python
    load_notifier(root).send("Decision", decision, event="decision")
```

Place it just before the command's final `typer.echo(...)` return, still using
the `decision` string already built at line ~1387. It must run outside the
`state_lock` block — move it after the `with state_lock(root):` body if the
echo is inside; match the existing structure so the lock isn't held during the
network call.

- [ ] **Step 4: Smoke-test the command + full suite**

Run: `python -m pytest tests/test_chat_cli.py -v`
Expected: PASS.

Run the whole suite to confirm no regression:
`python -m pytest -q`
Expected: PASS (all existing + new tests).

Manual smoke (no token → NullNotifier, must exit 0):
```bash
cd $(mktemp -d) && agensuite init demo --idea "x" && cd demo && agensuite bootstrap
agensuite notify sprint-start --sprint sprint-1
```
Expected: prints `{"notified": "sprint-start", ...}`, exit 0, no crash.

- [ ] **Step 5: Commit**

```bash
git add src/agensuite/cli.py tests/test_chat_cli.py
git commit -m "feat(notify): adr-record decision send + notify sprint-start command"
```

---

## Task 8: Document the opt-in chat flow in AGENTS.md

**Files:**
- Modify: `AGENTS.md` (the human-gate command table + sprint loop section)
- Test: none (docs) — verified by reading.

- [ ] **Step 1: Add a "Chat integration (opt-in)" subsection**

After the existing `human-gate` documentation in `AGENTS.md`, add:

```markdown
### Chat integration (opt-in, Telegram-first)

Chat is **off unless configured**. To enable it, create
`state/notify.json` and export a bot token:

    {
      "channel": "telegram",
      "chat_id": "<your chat id>",
      "events": ["gate", "decision", "sprint-start"]
    }

    export AGENSUITE_TELEGRAM_TOKEN=<bot token>   # never commit this

`state/` is gitignored, and the token lives only in the environment.

**Outbound** (fired only when configured and the event is in `events`):
- `agensuite notify sprint-start --sprint <s>` — call at sprint kickoff.
- `agensuite adr record --sprint <s>` — sends the decision summary.
- `agensuite human-gate --resolve-deadlocks --async --sprint <s>` — sends
  inline Merge/Reject/ADR/Skip buttons per deadlocked PR.

**Inbound** (chat-driven deadlock resolution):
1. Start the relay once: `agensuite bot` (long-runs; keep it alive).
2. Raise the gate: `agensuite human-gate --resolve-deadlocks --async --sprint <s>`
   → returns `{"status": "awaiting_human", "pending": [...]}`.
3. The human taps buttons in chat; the bot records them to `state/gate_inbox.json`.
4. Apply them: `agensuite human-gate --drain --sprint <s> --wait`
   → blocks (polling the local inbox) until every pending PR resolves, then
   the sprint proceeds to `agensuite adr record`.

Without `--async`/`--drain` and without config, `human-gate` keeps its
original terminal (stdin) behavior unchanged.
```

- [ ] **Step 2: Update the command-reference table**

Find the table row for `human-gate` (~line 57 / ~line 289) and add adjacent
rows for `notify sprint-start`, `human-gate --async`, `human-gate --drain`, and
`bot`, matching the existing two-column format.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(agents): document opt-in Telegram chat flow"
```

---

## Self-review notes (author check, complete)

**Spec coverage:**
- notify.py abstraction (Null/Telegram/WhatsApp + loader) → Task 3 ✓
- NotifyConfig in models.py → Task 1 ✓
- Outbound: gate / decision / sprint-start → Tasks 4 (gate), 7 (decision + sprint-start) ✓
- `agensuite bot` inbound sidecar → Task 5 ✓
- async gate flow (`--async` / `--drain --wait`) → Tasks 4 + 6 ✓
- state files (notify/pending/inbox) under gitignored `state/` → Task 2 ✓ (path
  helpers reuse `_state_dir`, which is under `state/`, already gitignored)
- Tests: NullNotifier no-op, Telegram payload shape, inbox parse/validate,
  drain parity with stdin loop, backward-compat → Tasks 1–7 ✓
- WhatsApp stub raises NotImplementedError → Task 3 ✓
- Backward compatibility (no config + no flags = stdin path) → preserved: the
  `human_gate` body only routes to chat helpers when `--async`/`--drain` are
  passed; the existing `resolve_deadlocks`/`message` branches are untouched ✓

**Type consistency:** `GateMailbox`, `PendingGate`, `PendingPR`, `InboxEntry`
defined in Task 2 and used identically in Tasks 3–6. `_telegram_call(token,
method, payload)` signature is identical in notify.py (Task 3) and the cli
re-export/usage (Task 5). `Notifier.send(title, body, *, event=None)` and
`send_gate(pending)` consistent across Tasks 3, 4, 6, 7. Choice letters
`m/r/a/s` consistent with `LEGAL_CHOICES` (Task 2) and the existing
`_resolve_deadlocks_loop`.

**Placeholder scan:** the only deliberate temporary is the `_drain_gate` stub
in Task 4, explicitly replaced in Task 6 — flagged in both tasks.

**Workspace-branch correctness:** Tasks 4 & 6 deliberately build deadlocked PRs
via the real CLI (`_drive_to_deadlock`, copied from
`tests/test_cli.py::TestHumanGateResolveDeadlocks`) rather than hand-writing
`prs.json`. This guarantees each PR has a committed workspace branch, which the
`--drain` merge (`_merge_pr(force_deadlock=True)`) actually merges. Verified
against the existing `test_merge_choice_resolves_deadlock`, which uses the same
helper and the same `force_deadlock` path.
```

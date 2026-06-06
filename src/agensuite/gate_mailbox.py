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
from .state import _atomic_write, _state_dir, state_lock

LEGAL_CHOICES = ("m", "r", "a", "s")  # m=merge, r=reject, a=adr-options, s=skip


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
        try:
            return PendingGate.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"corrupt gate_pending at {path}: {e}") from e

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
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [InboxEntry.model_validate(e) for e in raw]
        except Exception as e:
            raise ValueError(f"corrupt gate_inbox at {path}: {e}") from e

    def append_inbox(self, pr_id: str, choice: str) -> None:
        # Lock guards against drain clearing the inbox between our read and write.
        with state_lock(self.root):
            entries = self.load_inbox()
            entries.append(InboxEntry(pr_id=pr_id, choice=choice))
            _atomic_write(
                self._inbox_path(),
                json.dumps([e.model_dump(mode="json") for e in entries], indent=2)
                + "\n",
            )

    def clear_inbox(self) -> None:
        _atomic_write(self._inbox_path(), "[]\n")

    # -- offset -----------------------------------------------------------
    def load_offset(self) -> int:
        path = self._offset_path()
        if not path.exists():
            return 0
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get("offset", 0))
        except Exception as e:
            raise ValueError(f"corrupt bot_offset at {path}: {e}") from e

    def save_offset(self, offset: int) -> None:
        _atomic_write(
            self._offset_path(),
            json.dumps({"offset": offset}) + "\n",
        )

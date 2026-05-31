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
import os
import sys
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .gate_mailbox import PendingGate
from .models import NotifyConfig

_API_BASE = "https://api.telegram.org/bot{token}/{method}"

_GATE_BUTTONS: tuple[tuple[str, str], ...] = (
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
    cfg_path = root / "state" / "notify.json"
    if not cfg_path.exists():
        return NullNotifier()
    try:
        cfg = NotifyConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        # A malformed notify.json is almost certainly a user error; surface it
        # on stderr (like the send paths) instead of failing silently.
        print(f"notify: ignoring malformed {cfg_path}: {e}", file=sys.stderr)
        return NullNotifier()

    if cfg.channel == "telegram":
        token = os.environ.get("AGENSUITE_TELEGRAM_TOKEN")
        if not token:
            return NullNotifier()
        return TelegramNotifier(token=token, chat_id=cfg.chat_id, events=cfg.events)
    if cfg.channel == "whatsapp":
        return NullNotifier()  # stub adapter not wired; degrade quietly
    return NullNotifier()

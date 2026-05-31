from __future__ import annotations

import json
from pathlib import Path

import pytest

from agensuite import notify
from agensuite.gate_mailbox import PendingGate, PendingPR


def test_null_notifier_is_noop(tmp_path: Path) -> None:
    n = notify.NullNotifier()
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
    assert isinstance(notify.load_notifier(tmp_path), notify.NullNotifier)


def test_load_notifier_returns_telegram_when_configured(tmp_path: Path, monkeypatch) -> None:
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
        PendingGate(sprint_id="s", prs=[PendingPR(pr_id="abc", title="Add X")])
    )
    method, payload = calls[0]
    assert method == "sendMessage"
    kb = payload["reply_markup"]["inline_keyboard"]
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
    n = notify.TelegramNotifier(token="tok", chat_id="42", events=["gate"])
    n.send("Decision", "x", event="decision")
    assert calls == []


def test_send_swallows_network_errors(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(notify, "_telegram_call", boom)
    n = notify.TelegramNotifier(token="tok", chat_id="42", events=["decision"])
    n.send("Decision", "x")  # must NOT raise


def test_whatsapp_is_stub() -> None:
    with pytest.raises(NotImplementedError):
        notify.WhatsAppNotifier(token="tok", chat_id="42", events=[])

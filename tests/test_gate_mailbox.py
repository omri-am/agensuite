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


def test_load_pending_corrupt_raises(tmp_path: Path) -> None:
    import pytest
    from agensuite.gate_mailbox import GateMailbox
    from agensuite.state import _state_dir
    _state_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (_state_dir(tmp_path) / "gate_pending.json").write_text("{not json")
    with pytest.raises(ValueError, match="corrupt gate_pending"):
        GateMailbox(tmp_path).load_pending()


def test_append_inbox_idempotent_path_and_remove_missing(tmp_path: Path) -> None:
    from agensuite.gate_mailbox import GateMailbox, PendingGate, PendingPR
    mb = GateMailbox(tmp_path)
    # remove on absent pending file is a no-op (no raise)
    mb.remove_pending_pr("ghost")
    mb.save_pending(PendingGate(sprint_id="s", prs=[PendingPR(pr_id="a", title="A")]))
    mb.remove_pending_pr("ghost")  # absent id, no-op
    assert [p.pr_id for p in mb.load_pending().prs] == ["a"]

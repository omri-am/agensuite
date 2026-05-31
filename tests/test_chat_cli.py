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
    Copied verbatim from test_cli.py::TestHumanGateResolveDeadlocks.
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
    p = cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    out = json.loads(p.stdout)
    assert out["status"] == "awaiting_human"
    assert out["pending"] == [pr_a]

    pending = json.loads((project_root / "state" / "gate_pending.json").read_text())
    assert pending["sprint_id"] == "s"
    assert pending["prs"][0]["pr_id"] == pr_a
    # --async clears the inbox so a stale tap from a prior gate can't leak in
    assert json.loads((project_root / "state" / "gate_inbox.json").read_text()) == []


def test_async_gate_no_deadlocks_is_clean(cli, project_root):
    cli("bootstrap")
    _open_pr(cli, project_root, "a", "feat/a/x", "fa.md")  # open, not deadlocked
    p = cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    out = json.loads(p.stdout)
    assert out["status"] == "awaiting_human"
    assert out["pending"] == []


def test_async_without_resolve_deadlocks_errors(cli, project_root):
    cli("bootstrap")
    p = cli("human-gate", "--sprint", "s", "--async", expect_ok=False)
    assert p.returncode == 1
    assert "--async requires --resolve-deadlocks" in p.stderr


def test_bot_poll_once_records_valid_tap(project_root, monkeypatch):
    from agensuite import cli as cli_mod
    from agensuite.gate_mailbox import GateMailbox, PendingGate, PendingPR

    mb = GateMailbox(project_root)
    mb.save_pending(PendingGate(sprint_id="s", prs=[PendingPR(pr_id="abc123", title="X")]))

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

    new_offset = cli_mod._bot_poll_once("tok", mb)

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
            {
                "update_id": 3,
                "callback_query": {"id": "c", "data": "ghost:m",
                                   "message": {"chat": {"id": 42}}},
            },
            {
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

    cli_mod._bot_poll_once("tok", mb)
    assert mb.load_inbox() == []
    assert mb.load_offset() == 5


def test_bot_poll_once_empty_result_is_noop(project_root, monkeypatch):
    from agensuite import cli as cli_mod
    from agensuite.gate_mailbox import GateMailbox, PendingGate, PendingPR

    mb = GateMailbox(project_root)
    mb.save_pending(PendingGate(sprint_id="s", prs=[PendingPR(pr_id="abc123", title="X")]))
    monkeypatch.setattr(
        cli_mod, "_telegram_call",
        lambda t, m, p: {"ok": True, "result": []},
    )
    assert cli_mod._bot_poll_once("tok", mb) == 0
    assert mb.load_inbox() == []
    assert mb.load_offset() == 0


def test_bot_poll_once_records_tap_even_if_ack_fails(project_root, monkeypatch):
    from agensuite import cli as cli_mod
    from agensuite.gate_mailbox import GateMailbox, PendingGate, PendingPR

    mb = GateMailbox(project_root)
    mb.save_pending(PendingGate(sprint_id="s", prs=[PendingPR(pr_id="abc123", title="X")]))
    updates = {
        "ok": True,
        "result": [
            {"update_id": 1, "callback_query": {"id": "cb", "data": "abc123:r",
                                                 "message": {"chat": {"id": 42}}}},
        ],
    }

    def flaky(token, method, payload):
        if method == "getUpdates":
            return updates
        raise OSError("ack failed")  # answerCallbackQuery blows up

    monkeypatch.setattr(cli_mod, "_telegram_call", flaky)
    # the ack failure is swallowed; the tap must still land in the inbox
    cli_mod._bot_poll_once("tok", mb)
    assert [(e.pr_id, e.choice) for e in mb.load_inbox()] == [("abc123", "r")]


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
    pr_a = _drive_to_deadlock(cli, project_root)
    cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    p = cli("human-gate", "--sprint", "s", "--drain")  # no tap arrived
    out = json.loads(p.stdout)
    assert out["resolved"] == []
    assert out["still_pending"] == [pr_a]


def test_drain_skip_leaves_pr_pending_and_deadlocked(cli, project_root):
    from agensuite.models import PRStatus
    from agensuite.state import PRRegistry
    from agensuite.gate_mailbox import GateMailbox

    pr_a = _drive_to_deadlock(cli, project_root)
    cli("human-gate", "--sprint", "s", "--resolve-deadlocks", "--async")
    GateMailbox(project_root).append_inbox(pr_id=pr_a, choice="s")  # "come back later"

    p = cli("human-gate", "--sprint", "s", "--drain")
    out = json.loads(p.stdout)
    # the skip is acknowledged, but the PR stays pending + DEADLOCKED so the
    # human can act on it again; a skipped PR is never silently dropped
    assert {"pr": pr_a, "action": "skip"} in out["resolved"]
    assert out["still_pending"] == [pr_a]
    assert PRRegistry.load(project_root)[pr_a].status == PRStatus.DEADLOCKED

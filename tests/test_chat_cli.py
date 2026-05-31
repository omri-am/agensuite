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

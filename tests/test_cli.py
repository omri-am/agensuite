"""End-to-end CLI tests driven via subprocess.

These tests are the closest match to how a coding-agent orchestrator
actually drives the CLI. Each test spawns ``python -m agensuite.cli`` with
``AGENSUITE_ROOT`` pointing at an isolated tmp directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


class TestBootstrap:
    def test_idempotent(self, cli) -> None:
        cli("bootstrap")
        cli("bootstrap")  # second call must not error

    def test_banner_emitted_on_stderr(self, cli) -> None:
        """Banner must appear on stderr (not stdout) so orchestrators that
        parse `bootstrap` stdout get a clean machine-readable channel."""
        p = cli("bootstrap")
        assert "Agent-native C-suite orchestration" in p.stderr
        assert "Agent-native C-suite orchestration" not in p.stdout
        # Workspace-ready info lines stay on stdout.
        assert "workspace ready at" in p.stdout

    def test_bare_invocation_prints_banner_and_help(self, cli) -> None:
        """`agensuite` with no subcommand: banner on stderr, help on stdout,
        exit code 0 (user explicitly asked for help by typing the bare cmd)."""
        p = cli(expect_ok=True)
        assert p.returncode == 0
        assert "Agent-native C-suite orchestration" in p.stderr
        assert "Usage:" in p.stdout
        assert "bootstrap" in p.stdout  # subcommand listing renders

    def test_defaults_to_current_working_directory_without_env(
        self, tmp_path: Path
    ) -> None:
        """Runtime state belongs to the project directory where the command runs."""
        project = tmp_path / "outside-source"
        project.mkdir()
        env = os.environ.copy()
        env.pop("AGENSUITE_ROOT", None)
        project_src = Path(__file__).resolve().parents[1] / "src"
        env["PYTHONPATH"] = str(project_src) + os.pathsep + env.get("PYTHONPATH", "")

        p = subprocess.run(
            [sys.executable, "-m", "agensuite.cli", "bootstrap"],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
        )

        assert p.returncode == 0, p.stderr
        assert (project / "workspace" / "main").is_dir()
        assert (project / "state" / "debates").is_dir()
        assert "workspace ready at" in p.stdout
        assert str(project / "workspace") in p.stdout


class TestSprintCommands:
    def test_show_emits_valid_json(self, cli) -> None:
        out = cli("sprint", "show", "s").stdout
        assert json.loads(out)["id"] == "s"

    def test_show_missing_sprint_exits_1(self, cli) -> None:
        p = cli("sprint", "show", "nope", expect_ok=False)
        assert p.returncode == 1
        assert "Traceback" not in p.stderr
        assert "During handling" not in p.stderr

    def test_list_includes_sprint(self, cli) -> None:
        out = cli("sprint", "list").stdout
        assert "s\t" in out


class TestEndToEnd:
    def _open_pr(self, cli, project_root: Path, role: str, br: str, path: str) -> str:
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

    def test_full_sprint_loop(self, cli, project_root: Path) -> None:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        pr_b = self._open_pr(cli, project_root, "b", "feat/b/x", "fb.md")

        # Drive one approval per PR through the debate so verdict-based
        # termination fires only after each PR has at least one APPROVE.
        consumed = 0
        last_done: dict = {}
        while True:
            out = json.loads(cli("debate", "next-turn", "--sprint", "s").stdout)
            if out.get("done"):
                last_done = out
                break
            cli("pr", "comment",
                "--id", out["pr_id"],
                "--reviewer", out["speaker"],
                "--comment", "lgtm",
                "--verdict", "APPROVE",
                "--phase", out["phase"])
            consumed += 1
            assert consumed < 20
        # Under verdict termination the loop ends as soon as every PR is
        # quorum-met; the pre-computed schedule is an upper bound.
        assert last_done == {"done": True, "reason": "quorum_met"}
        assert 2 <= consumed < 8

        cli("pr", "merge", "--id", pr_a)
        cli("pr", "merge", "--id", pr_b)

        adr = json.loads(cli("adr", "record", "--sprint", "s").stdout)
        assert adr["adr_id"].startswith("adr-")
        adr_file = next((project_root / "workspace" / "main" / "governance").glob("*.md"))
        body = adr_file.read_text()
        assert "Status:** Accepted" in body
        assert "Merged:" in body

    def test_self_review_rejected(self, cli, project_root: Path) -> None:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        # REVIEW phase: author cannot review own PR.
        p = cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
                "--comment", "self", expect_ok=False)
        assert p.returncode == 1
        assert "cannot review own PR" in p.stderr

    def test_self_rebuttal_allowed(self, cli, project_root: Path) -> None:
        """REBUTTAL phase is the one case where reviewer == pr.author."""
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        # Reviewer b requests changes → REBUTTAL slot appended.
        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "need fix", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")
        # Author posts the rebuttal.
        p = cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
                "--comment", "addressed", "--verdict", "COMMENT",
                "--phase", "REBUTTAL")
        assert "REBUTTAL" in p.stdout

    def test_non_participant_rejected(self, cli, project_root: Path) -> None:
        cli("bootstrap")
        cli("branch", "create", "feat/intruder/x")
        p = cli("pr", "open", "--branch", "feat/intruder/x",
                "--author", "intruder", "--title", "X", "--sprint", "s",
                expect_ok=False)
        assert p.returncode == 1
        assert "not a participant" in p.stderr

    def test_merge_conflict_exits_2(self, cli, project_root: Path) -> None:
        cli("bootstrap")
        # Both touch the same file.
        for role, br in [("a", "feat/a/x"), ("b", "feat/b/x")]:
            cli("branch", "create", br)
            slug = br.replace("/", "__")
            (project_root / "workspace" / "wt" / slug / "shared.md").write_text(
                f"value={role}\n"
            )
            cli("commit", "--branch", br, "--author", role,
                "--message", f"{role}", "--files", "shared.md")
            other = "b" if role == "a" else "a"
            pid = cli("pr", "open", "--branch", br, "--author", role,
                      "--title", f"{role}", "--sprint", "s",
                      "--files", "shared.md").stdout.strip()
            cli("pr", "comment", "--id", pid, "--reviewer", other,
                "--comment", "k", "--approve")
            if role == "a":
                pr_a = pid
            else:
                pr_b = pid

        cli("pr", "merge", "--id", pr_a)
        p = cli("pr", "merge", "--id", pr_b, expect_ok=False)
        assert p.returncode == 2  # conflict-specific code
        assert "merge conflict" in p.stderr
        # `main` must be clean after auto-abort.
        main_dir = project_root / "workspace" / "main"
        import subprocess
        st = subprocess.run(["git", "status", "--porcelain"],
                            cwd=str(main_dir), capture_output=True, text=True)
        assert st.stdout.strip() == ""


class TestMidDebatePR:
    """Regression test for review finding #1.

    Opening a PR after the debate cursor has already advanced must NOT
    rebuild the entire schedule (that would change which logical turn
    ``cursor`` indexes into). Instead it must append the new PR's review
    slots at the tail, leaving the in-progress prefix untouched.
    """

    def _open_pr(self, cli, project_root: Path, role: str, br: str, path: str) -> str:
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

    def test_append_only_when_cursor_advanced(self, cli, project_root: Path) -> None:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        pr_b = self._open_pr(cli, project_root, "b", "feat/b/x", "fb.md")

        debate_path = project_root / "state" / "debates" / "s.json"
        # State is wrapped in {"schema_version": N, "debate": {...}}
        before = json.loads(debate_path.read_text())["debate"]
        initial_len = len(before["schedule"])
        assert initial_len == 8  # 2 PRs * 2 reviewers * 2 rounds

        # Consume two turns
        for _ in range(2):
            json.loads(cli("debate", "next-turn", "--sprint", "s").stdout)
        mid = json.loads(debate_path.read_text())["debate"]
        assert mid["cursor"] == 2
        prefix = mid["schedule"][:2]

        # Open PR-C mid-debate
        pr_c = self._open_pr(cli, project_root, "c", "feat/c/x", "fc.md")
        after = json.loads(debate_path.read_text())["debate"]

        # Invariants:
        assert after["cursor"] == 2, "cursor must be preserved"
        assert after["schedule"][:2] == prefix, "consumed slots unchanged"
        # Original 8 turns intact, 4 new (PR-C: 2 reviewers * 2 rounds) appended
        assert len(after["schedule"]) == initial_len + 4
        tail_targets = {t["target_pr_id"] for t in after["schedule"][initial_len:]}
        assert tail_targets == {pr_c}


def _agensuite_env(extra_root: Path | None = None) -> dict[str, str]:
    """Env block that points the CLI's import path at the in-tree ``src/``.

    ``init`` writes to an arbitrary target directory, not ``AGENSUITE_ROOT``, so
    a stray ``AGENSUITE_ROOT`` inherited from the parent shell is stripped to
    prevent test flakiness. ``extra_root`` re-introduces it for the
    ``chief customize`` tests that need a fixed project root.
    """
    env = os.environ.copy()
    env.pop("AGENSUITE_ROOT", None)
    project_src = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(project_src) + os.pathsep + env.get("PYTHONPATH", "")
    if extra_root is not None:
        env["AGENSUITE_ROOT"] = str(extra_root)
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str], stdin: str | None = None
         ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agensuite.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
    )


class TestInit:
    """`agensuite init <name> --idea "<text>"` scaffolds a new project."""

    def test_idea_flag_substitutes_tokens(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        p = _run(
            ["init", "fintech-app", "--idea", "Trading platform for retail investors"],
            cwd=tmp_path,
            env=env,
        )
        assert p.returncode == 0, p.stderr

        target = tmp_path / "fintech-app"
        assert target.is_dir()
        assert (target / "AGENTS.md").is_file()
        sprint = (target / "sprints" / "sprint-1.md").read_text()
        assert "{{CORE_PRODUCT_IDEA}}" not in sprint
        assert "Trading platform for retail investors" in sprint

        for role in ("ceo", "cpo", "cto", "cdo", "cco"):
            agent = (target / ".claude" / "agents" / f"{role}.md").read_text()
            assert "{{COMPANY_MISSION}}" not in agent
            assert "Trading platform for retail investors" in agent
            # File is structurally valid: frontmatter + heading preserved.
            assert agent.startswith("---\n")
            assert "## Operational Biases" in agent

        # init must not pollute the target with runtime/state directories.
        assert not (target / "state").exists()
        assert not (target / "workspace").exists()

        # Friendly next-steps message lands on stdout.
        assert "Successfully initialized" in p.stdout
        assert f"cd {target}" in p.stdout
        assert "agensuite bootstrap" in p.stdout

    def test_interactive_prompt_fallback(self, tmp_path: Path) -> None:
        """No --idea flag triggers a typer.prompt; stdin satisfies it."""
        env = _agensuite_env()
        p = _run(
            ["init", "indie-game"],
            cwd=tmp_path,
            env=env,
            stdin="Roguelike with procedural narrative\n",
        )
        assert p.returncode == 0, p.stderr
        sprint = (tmp_path / "indie-game" / "sprints" / "sprint-1.md").read_text()
        assert "Roguelike with procedural narrative" in sprint

    def test_init_refuses_to_overwrite_non_empty_dir(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        _run(["init", "x", "--idea", "first run"], cwd=tmp_path, env=env)
        p = _run(["init", "x", "--idea", "second run"], cwd=tmp_path, env=env)
        assert p.returncode == 1
        assert "not empty" in p.stderr

    def test_init_accepts_absolute_path(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        target = tmp_path / "nested" / "app"
        p = _run(
            ["init", str(target), "--idea", "Pet teleporter"],
            cwd=tmp_path,
            env=env,
        )
        assert p.returncode == 0, p.stderr
        assert (target / "sprints" / "sprint-1.md").exists()

    def test_idea_flag_applies_sprint_defaults(self, tmp_path: Path) -> None:
        """--idea (non-TTY) writes the default sprint config, no prompts."""
        env = _agensuite_env()
        p = _run(
            ["init", "defaults-app", "--idea", "A budgeting tool"],
            cwd=tmp_path,
            env=env,
        )
        assert p.returncode == 0, p.stderr
        sprint = (tmp_path / "defaults-app" / "sprints" / "sprint-1.md").read_text()
        meta = yaml.safe_load(sprint.split("---\n", 2)[1])
        assert meta["debate_rounds"] == 2
        assert meta["approval_quorum"] == 2
        assert meta["participants"] == ["cpo", "cto", "cdo", "cco"]
        # The misleading "customize files" line is gone.
        assert "customize AGENTS.md" not in p.stdout


def _env_with_path(path_dirs: list[Path]) -> dict[str, str]:
    """``_agensuite_env`` with PATH = ``path_dirs`` + git's own dir.

    Binary resolution for the launched agent stays deterministic — point PATH
    at a fake-agent dir to hit the exec path, or an empty dir to hit the
    missing-binary fallback. git's directory is appended so the fast path's
    bootstrap can still find ``git``; that dir is a system location and never
    contains the real ``claude``/``codex``/``cursor``, so it can't shadow the
    fake-agent decision. ``sys.executable`` is invoked by absolute path, so the
    runner itself doesn't need PATH.
    """
    git = shutil.which("git")
    assert git is not None, "git is required to run these tests"
    env = _agensuite_env()
    env["PATH"] = os.pathsep.join([*(str(d) for d in path_dirs), str(Path(git).parent)])
    return env


def _make_fake_agent(bindir: Path, name: str = "claude") -> Path:
    """Create an executable that echoes its cwd + first arg, then return bindir.

    ``init`` execs this in place of the real coding agent, so subprocess stdout
    becomes the script's output — letting tests assert the chdir target and the
    seed prompt that was passed.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / name
    script.write_text('#!/bin/sh\necho "CWD=$(pwd)"\necho "PROMPT=$1"\n')
    script.chmod(0o755)
    return bindir


class TestInitAgentFastPath:
    """`agensuite init <name> --agent <x>` scaffolds, bootstraps, then launches."""

    def test_deferred_idea_launches_with_placeholders_kept(self, tmp_path: Path) -> None:
        """No --idea + --agent: placeholders survive, bootstrap runs, agent execs."""
        bindir = _make_fake_agent(tmp_path / "fakebin")
        env = _env_with_path([bindir])
        p = _run(["init", "ftr", "--agent", "claude"], cwd=tmp_path, env=env)
        assert p.returncode == 0, p.stderr

        target = tmp_path / "ftr"
        # Placeholders deferred to the agent, not substituted at init time.
        sprint = (target / "sprints" / "sprint-1.md").read_text()
        assert "{{CORE_PRODUCT_IDEA}}" in sprint
        # Bootstrap ran as part of the fast path.
        assert (target / "workspace").is_dir()
        assert (target / "state").is_dir()
        # The fake agent ran in the project dir with the deferred-idea prompt.
        assert f"CWD={target}" in p.stdout
        assert "PROMPT=" in p.stdout
        assert "ask me for a one-line startup idea" in p.stdout
        assert "set-idea" in p.stdout

    def test_idea_and_agent_substitutes_and_uses_short_prompt(self, tmp_path: Path) -> None:
        bindir = _make_fake_agent(tmp_path / "fakebin")
        env = _env_with_path([bindir])
        p = _run(
            ["init", "ftr", "--idea", "Track insider trades", "--agent", "claude"],
            cwd=tmp_path,
            env=env,
        )
        assert p.returncode == 0, p.stderr

        target = tmp_path / "ftr"
        sprint = (target / "sprints" / "sprint-1.md").read_text()
        assert "{{CORE_PRODUCT_IDEA}}" not in sprint
        assert "Track insider trades" in sprint
        # Idea already set → short seed prompt, no set-idea instruction.
        assert "Read AGENTS.md and execute sprint-1." in p.stdout
        assert "set-idea" not in p.stdout

    def test_cursor_resolves_secondary_binary(self, tmp_path: Path) -> None:
        """`cursor` falls back to its second candidate when the first is absent."""
        bindir = _make_fake_agent(tmp_path / "fakebin", name="cursor")
        env = _env_with_path([bindir])
        p = _run(["init", "ftr", "--agent", "cursor"], cwd=tmp_path, env=env)
        assert p.returncode == 0, p.stderr
        assert f"CWD={tmp_path / 'ftr'}" in p.stdout

    def test_missing_binary_prints_paste_fallback(self, tmp_path: Path) -> None:
        """Agent not on PATH: scaffold + bootstrap still succeed, exit 0."""
        emptydir = tmp_path / "emptybin"
        emptydir.mkdir()
        env = _env_with_path([emptydir])
        p = _run(["init", "ftr", "--agent", "claude"], cwd=tmp_path, env=env)
        assert p.returncode == 0, p.stderr

        target = tmp_path / "ftr"
        assert (target / "workspace").is_dir()
        assert "couldn't find 'claude'" in p.stderr
        # Seed prompt is printed to stdout for the user to paste.
        assert "ask me for a one-line startup idea" in p.stdout

    def test_unknown_agent_rejected(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        p = _run(["init", "ftr", "--agent", "emacs"], cwd=tmp_path, env=env)
        assert p.returncode == 1
        assert "unknown --agent" in p.stderr
        # Nothing scaffolded on a rejected agent.
        assert not (tmp_path / "ftr").exists()


class TestSetIdea:
    """`agensuite set-idea "<text>"` fills deferred placeholders."""

    def _scaffold_with_placeholders(self, tmp_path: Path) -> Path:
        """Scaffold a project that still carries idea placeholders.

        Uses the deferred fast path with no agent binary on PATH, so the files
        are written with placeholders intact and the process exits 0 without
        launching anything.
        """
        emptydir = tmp_path / "emptybin"
        emptydir.mkdir()
        env = _env_with_path([emptydir])
        p = _run(["init", "proj", "--agent", "claude"], cwd=tmp_path, env=env)
        assert p.returncode == 0, p.stderr
        return tmp_path / "proj"

    def test_substitutes_across_all_file_groups(self, tmp_path: Path) -> None:
        project = self._scaffold_with_placeholders(tmp_path)
        env_with_root = _agensuite_env(extra_root=project)
        idea = "A platform to track government officials' stock trades"
        p = _run(["set-idea", idea], cwd=project, env=env_with_root)
        assert p.returncode == 0, p.stderr

        sprint = (project / "sprints" / "sprint-1.md").read_text()
        assert "{{CORE_PRODUCT_IDEA}}" not in sprint
        assert idea in sprint
        for role in ("ceo", "cpo", "cto", "cdo", "cco"):
            agent_md = (project / ".claude" / "agents" / f"{role}.md").read_text()
            assert "{{COMPANY_MISSION}}" not in agent_md
            assert idea in agent_md

    def test_rerun_is_harmless_noop(self, tmp_path: Path) -> None:
        project = self._scaffold_with_placeholders(tmp_path)
        env_with_root = _agensuite_env(extra_root=project)
        _run(["set-idea", "First idea"], cwd=project, env=env_with_root)
        p = _run(["set-idea", "Second idea"], cwd=project, env=env_with_root)
        # No placeholders remain → warns, exits 0, changes nothing.
        assert p.returncode == 0, p.stderr
        assert "no idea placeholders found" in p.stderr
        assert "0 files updated" in p.stdout
        # The first idea is still in place; the second did not overwrite it.
        assert "First idea" in (project / "sprints" / "sprint-1.md").read_text()
        assert "Second idea" not in (project / "sprints" / "sprint-1.md").read_text()

    def test_empty_idea_rejected(self, tmp_path: Path) -> None:
        project = self._scaffold_with_placeholders(tmp_path)
        env_with_root = _agensuite_env(extra_root=project)
        p = _run(["set-idea", "   "], cwd=project, env=env_with_root)
        assert p.returncode == 1
        assert "non-empty" in p.stderr


class TestChiefCustomize:
    """`agensuite chief customize <role> --focus "<text>"`."""

    def _scaffold(self, tmp_path: Path, env: dict[str, str]) -> Path:
        p = _run(
            ["init", "co", "--idea", "Sourdough subscription network"],
            cwd=tmp_path,
            env=env,
        )
        assert p.returncode == 0, p.stderr
        return tmp_path / "co"

    def test_append_bias_to_cto(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        project = self._scaffold(tmp_path, env)
        env_with_root = _agensuite_env(extra_root=project)

        focus = "Rust backend with high throughput"
        p = _run(
            ["chief", "customize", "cto", "--focus", focus],
            cwd=project,
            env=env_with_root,
        )
        assert p.returncode == 0, p.stderr

        body = (project / ".claude" / "agents" / "cto.md").read_text()
        assert focus in body
        # Bullet sits inside the Operational Biases section (before the next
        # `## ` heading — which in the CTO template is `## Deliverables`).
        biases = body.split("## Operational Biases", 1)[1].split("## Deliverables", 1)[0]
        assert focus in biases
        # Pre-existing biases stay put.
        assert "Pick load-bearing dependencies once" in biases
        assert "Single regional deployment" in biases

    def test_unknown_role_rejected(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        project = self._scaffold(tmp_path, env)
        env_with_root = _agensuite_env(extra_root=project)
        p = _run(
            ["chief", "customize", "cfo", "--focus", "irrelevant"],
            cwd=project,
            env=env_with_root,
        )
        assert p.returncode == 1
        assert "unknown role" in p.stderr

    def test_missing_agent_file_rejected(self, tmp_path: Path) -> None:
        """Running outside an initialised project (no .claude/agents/) fails clean."""
        env_with_root = _agensuite_env(extra_root=tmp_path)
        p = _run(
            ["chief", "customize", "cto", "--focus", "anything"],
            cwd=tmp_path,
            env=env_with_root,
        )
        assert p.returncode == 1
        assert "agent file not found" in p.stderr

    def test_role_argument_is_case_insensitive(self, tmp_path: Path) -> None:
        env = _agensuite_env()
        project = self._scaffold(tmp_path, env)
        env_with_root = _agensuite_env(extra_root=project)
        p = _run(
            ["chief", "customize", "CTO", "--focus", "GPU inference path"],
            cwd=project,
            env=env_with_root,
        )
        assert p.returncode == 0, p.stderr
        assert "GPU inference path" in (project / ".claude" / "agents" / "cto.md").read_text()


class TestDebateRebuttalLoop:
    """End-to-end coverage of the bounded threaded rebuttal protocol."""

    def _open_pr(self, cli, project_root: Path, role: str, br: str, path: str) -> str:
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

    def _schedule(self, project_root: Path) -> list[dict]:
        debate = json.loads(
            (project_root / "state" / "debates" / "s.json").read_text()
        )["debate"]
        return debate["schedule"]

    def test_request_changes_appends_rebuttal_slot(
        self, cli, project_root: Path
    ) -> None:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        self._open_pr(cli, project_root, "b", "feat/b/x", "fb.md")

        before = self._schedule(project_root)
        assert not any(t["phase"] == "REBUTTAL" for t in before), \
            "base schedule must contain only REVIEW slots"

        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "needs fix", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")

        after = self._schedule(project_root)
        rebuttals = [t for t in after if t["phase"] == "REBUTTAL"]
        assert len(rebuttals) == 1
        assert rebuttals[0]["speaker"] == "a"
        assert rebuttals[0]["target_pr_id"] == pr_a

    def test_rebuttal_then_followup_withdraw_merges(
        self, cli, project_root: Path
    ) -> None:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        self._open_pr(cli, project_root, "b", "feat/b/x", "fb.md")

        # b requests changes on pr_a
        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "needs fix", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")
        # c also approves pr_a (helps quorum once b withdraws)
        cli("pr", "comment", "--id", pr_a, "--reviewer", "c",
            "--comment", "lgtm", "--verdict", "APPROVE",
            "--phase", "REVIEW")
        # author rebuts collectively → FOLLOWUP slot for b appended
        cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
            "--comment", "addressed in commit X", "--verdict", "COMMENT",
            "--phase", "REBUTTAL")
        sched = self._schedule(project_root)
        followups = [t for t in sched if t["phase"] == "FOLLOWUP"]
        assert len(followups) == 1
        assert followups[0]["speaker"] == "b"
        rebuttal_idx = next(i for i, t in enumerate(sched) if t["phase"] == "REBUTTAL")
        assert followups[0]["parent_turn_idx"] == rebuttal_idx

        # b withdraws — verdict flips to APPROVE
        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "satisfied", "--verdict", "APPROVE",
            "--phase", "FOLLOWUP",
            "--parent-turn-idx", str(rebuttal_idx))

        # Merge predicate now passes: 2 approvals, no open change requests.
        cli("pr", "merge", "--id", pr_a)

    def test_followup_stand_yields_deadlocked(
        self, cli, project_root: Path
    ) -> None:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        self._open_pr(cli, project_root, "b", "feat/b/x", "fb.md")

        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "blocker", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")
        cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
            "--comment", "see commit X", "--verdict", "COMMENT",
            "--phase", "REBUTTAL")
        sched = self._schedule(project_root)
        rebuttal_idx = next(i for i, t in enumerate(sched) if t["phase"] == "REBUTTAL")

        # b stands — verdict stays REQUEST_CHANGES via FOLLOWUP
        out = cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
                  "--comment", "still blocked", "--verdict", "REQUEST_CHANGES",
                  "--phase", "FOLLOWUP",
                  "--parent-turn-idx", str(rebuttal_idx))
        # pr comment now emits a digest line before the machine-readable JSON;
        # parse only the last line so the assertion is not affected.
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        assert payload["status"] == "DEADLOCKED"

        # pr merge refuses on the DEADLOCKED guard.
        p = cli("pr", "merge", "--id", pr_a, expect_ok=False)
        assert p.returncode == 1
        assert "DEADLOCKED" in p.stderr
        assert "human-gate" in p.stderr

    def test_quorum_with_open_changes_blocks_merge(
        self, cli, project_root: Path
    ) -> None:
        """quorum >= 1 but an open REQUEST_CHANGES still blocks merge."""
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")

        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "lgtm", "--verdict", "APPROVE", "--phase", "REVIEW")
        cli("pr", "comment", "--id", pr_a, "--reviewer", "c",
            "--comment", "blocker", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")

        p = cli("pr", "merge", "--id", pr_a, expect_ok=False)
        assert p.returncode == 1
        assert "open change request" in p.stderr
        assert "c" in p.stderr

    def test_early_termination_on_unanimous_approval(
        self, cli, project_root: Path
    ) -> None:
        """Schedule is upper bound — debate exits the moment every PR converges."""
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        pr_b = self._open_pr(cli, project_root, "b", "feat/b/x", "fb.md")

        # One approval per PR is enough (quorum=1).
        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "lgtm", "--verdict", "APPROVE", "--phase", "REVIEW")
        cli("pr", "comment", "--id", pr_b, "--reviewer", "a",
            "--comment", "lgtm", "--verdict", "APPROVE", "--phase", "REVIEW")

        out = json.loads(cli("debate", "next-turn", "--sprint", "s").stdout)
        assert out == {"done": True, "reason": "quorum_met"}


class TestHumanGateResolveDeadlocks:
    def _open_pr(self, cli, project_root: Path, role: str, br: str, path: str) -> str:
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

    def _drive_to_deadlock(self, cli, project_root: Path) -> str:
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        # quorum=1 + b approves so the merge predicate would pass except
        # for c's outstanding REQUEST_CHANGES → forces the DEADLOCKED path.
        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "lgtm", "--verdict", "APPROVE", "--phase", "REVIEW")
        cli("pr", "comment", "--id", pr_a, "--reviewer", "c",
            "--comment", "blocker", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")
        cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
            "--comment", "see commit X", "--verdict", "COMMENT",
            "--phase", "REBUTTAL")
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

    def test_merge_choice_resolves_deadlock(
        self, cli, project_root: Path, cli_env: dict[str, str]
    ) -> None:
        pr_a = self._drive_to_deadlock(cli, project_root)

        # Drive --resolve-deadlocks with stdin "m\n" so the human picks merge.
        p = subprocess.run(
            [sys.executable, "-m", "agensuite.cli", "human-gate",
             "--sprint", "s", "--resolve-deadlocks"],
            cwd=str(project_root),
            env=cli_env,
            input="m\n",
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stderr
        # Parse the trailing JSON line (other lines are the human-readable banner).
        json_line = next(
            line for line in reversed(p.stdout.strip().splitlines())
            if line.strip().startswith("{")
        )
        result = json.loads(json_line)
        assert result["resolved"][0]["pr"] == pr_a
        assert result["resolved"][0]["action"] == "merge"

    def test_reject_choice_marks_pr_rejected(
        self, cli, project_root: Path, cli_env: dict[str, str]
    ) -> None:
        pr_a = self._drive_to_deadlock(cli, project_root)
        p = subprocess.run(
            [sys.executable, "-m", "agensuite.cli", "human-gate",
             "--sprint", "s", "--resolve-deadlocks"],
            cwd=str(project_root),
            env=cli_env,
            input="r\n",
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stderr
        # Subsequent pr merge must fail since PR was rejected.
        p2 = cli("pr", "merge", "--id", pr_a, expect_ok=False)
        assert p2.returncode == 1
        assert "REJECTED" in p2.stderr

    def test_no_deadlocks_returns_empty(
        self, cli, project_root: Path
    ) -> None:
        cli("bootstrap")
        self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")
        p = cli("human-gate", "--sprint", "s", "--resolve-deadlocks")
        result = json.loads(p.stdout.strip().splitlines()[-1])
        assert result == {"resolved": [], "reason": "no_deadlocks"}


class TestReviewFindingsRegression:
    """Regression tests for the three bugs caught in the PR #3 review."""

    def _open_pr(self, cli, project_root: Path, role: str, br: str, path: str) -> str:
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

    def test_followup_rebuttal_msg_id_resolves_to_real_message(
        self, cli, project_root: Path
    ) -> None:
        """Bug #1: rebuttal_msg_id used datetime.now() at construction time
        but reconstructed from ReviewComment.timestamp at lookup time, so
        the two ids diverged. Single-source-of-truth fix means the FOLLOWUP
        turn's rebuttal_msg_id must point at a real transcript entry.
        """
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")

        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "blocker", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")
        cli("pr", "comment", "--id", pr_a, "--reviewer", "a",
            "--comment", "addressed in commit X", "--verdict", "COMMENT",
            "--phase", "REBUTTAL")

        # Drain turns until the FOLLOWUP slot is returned.
        followup_turn: dict | None = None
        for _ in range(20):
            out = json.loads(cli("debate", "next-turn", "--sprint", "s").stdout)
            if out.get("done"):
                break
            if out["phase"] == "FOLLOWUP" and out["pr_id"] == pr_a:
                followup_turn = out
                break

        assert followup_turn is not None, "FOLLOWUP slot must surface"
        assert "rebuttal_msg_id" in followup_turn

        # The referenced id MUST exist in the transcript — the bug was
        # that it pointed at a never-stored phantom.
        debate = json.loads(
            (project_root / "state" / "debates" / "s.json").read_text()
        )["debate"]
        transcript_ids = {m["id"] for m in debate["transcript"]}
        assert followup_turn["rebuttal_msg_id"] in transcript_ids

    def test_schema_mismatch_prints_clean_error_not_traceback(
        self, cli, project_root: Path
    ) -> None:
        """Bug #2: StateSchemaMismatch inherits ValueError; the CLI only
        caught StateLockTimeout, so old state surfaced as a raw Python
        traceback instead of the actionable bootstrap-reset message.
        """
        cli("bootstrap")
        self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")

        # Stomp on the persisted PR registry with a pre-schema-2 payload.
        prs_path = project_root / "state" / "prs.json"
        legacy = (
            '{"pr-1": {"id": "pr-1", "title": "t", "branch": "b", '
            '"author": "a", "sprint_id": "s"}}'
        )
        prs_path.write_text(legacy)

        p = cli("pr", "list", expect_ok=False)
        assert p.returncode == 1
        # The actionable message reaches the user...
        assert "schema_version" in p.stderr
        assert "bootstrap --reset" in p.stderr
        # ...and a Python traceback does not.
        assert "Traceback" not in p.stderr
        assert "ValueError" not in p.stderr

    def test_stuck_pr_promoted_to_deadlocked_on_schedule_exhaustion(
        self, cli, project_root: Path
    ) -> None:
        """Bug #3: a PR stuck in CHANGES_REQUESTED with no further
        scheduled turns had no resolution path. The next-turn handler now
        promotes it to DEADLOCKED so human-gate --resolve-deadlocks picks
        it up naturally.

        Scenario: b requests changes on pr_a, the orchestrator never
        spawns the rebuttal subagent (simulated by draining the schedule
        without posting a REBUTTAL or any further comment from b), and
        the schedule eventually exhausts. Without the fix the next-turn
        handler returned ``reason: schedule_exhausted`` and the PR was
        invisible to ``human-gate --resolve-deadlocks``.
        """
        cli("bootstrap")
        pr_a = self._open_pr(cli, project_root, "a", "feat/a/x", "fa.md")

        cli("pr", "comment", "--id", pr_a, "--reviewer", "b",
            "--comment", "blocker", "--verdict", "REQUEST_CHANGES",
            "--phase", "REVIEW")

        # Drain the schedule. Skip the REBUTTAL slot (no author response)
        # and skip every slot that would let `b` flip the verdict to
        # APPROVE on pr_a, since the bug under test requires `b` to
        # remain in `open_change_requests`. All other slots get a noise
        # COMMENT so the schedule actually advances.
        done_payload: dict = {}
        for _ in range(30):
            out = json.loads(cli("debate", "next-turn", "--sprint", "s").stdout)
            if out.get("done"):
                done_payload = out
                break
            if out["phase"] == "REBUTTAL":
                continue
            if out["pr_id"] == pr_a and out["speaker"] == "b":
                continue
            cli("pr", "comment", "--id", out["pr_id"],
                "--reviewer", out["speaker"],
                "--comment", "noted", "--verdict", "COMMENT",
                "--phase", out["phase"])

        assert done_payload.get("done") is True
        assert done_payload["reason"] == "deadlocked"
        assert pr_a in done_payload["deadlocked_prs"]

        # And the PR is now visible to the human-gate deadlock loop.
        ls = cli("pr", "list", "--sprint", "s").stdout
        assert "DEADLOCKED" in ls


class TestHeadlineCounter:
    def test_pr_open_stores_headline(self, cli, project_root):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        (project_root / "workspace" / "wt" / "feat__a__x" / "doc.md").write_text("draft\n")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "d", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s",
                 "--headline", "claim X", "--files", "doc.md").stdout.strip()
        prs = json.loads((project_root / "state" / "prs.json").read_text())
        assert prs["prs"][pr]["headline"] == "claim X"

    def test_pr_comment_stores_counter_and_emits_verdict(self, cli, project_root):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        (project_root / "workspace" / "wt" / "feat__a__x" / "doc.md").write_text("draft\n")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "d", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s", "--headline", "claim X",
                 "--files", "doc.md").stdout.strip()
        cli("debate", "next-turn", "--sprint", "s")
        p = cli("pr", "comment", "--id", pr, "--reviewer", "b",
                "--comment", "no", "--verdict", "REQUEST_CHANGES",
                "--counter", "alt Y")
        prs = json.loads((project_root / "state" / "prs.json").read_text())
        assert prs["prs"][pr]["reviews"][-1]["counter"] == "alt Y"
        assert "🔴" in p.stdout
        assert '"verdict": "REQUEST_CHANGES"' in p.stdout


class TestDigestSink:
    def test_emit_writes_stdout_and_log_without_ansi(self, cli, project_root):
        cli("bootstrap")
        cli("branch", "create", "feat/a/x")
        (project_root / "workspace" / "wt" / "feat__a__x" / "doc.md").write_text("draft\n")
        cli("commit", "--branch", "feat/a/x", "--author", "a",
            "--message", "draft", "--files", "doc.md")
        pr = cli("pr", "open", "--branch", "feat/a/x", "--author", "a",
                 "--title", "T", "--sprint", "s",
                 "--headline", "Postgres over Dynamo", "--files", "doc.md").stdout.strip()
        cli("debate", "next-turn", "--sprint", "s")
        p = cli("pr", "comment", "--id", pr, "--reviewer", "b",
                "--comment", "looks risky", "--verdict", "REQUEST_CHANGES",
                "--counter", "per-entity policy")
        assert "🔴" in p.stdout
        assert "Postgres over Dynamo" in p.stdout
        log = (project_root / "state" / "debate-log.md").read_text(encoding="utf-8")
        assert "Postgres over Dynamo" in log
        assert "\x1b[" not in log

"""Direct unit tests for the pure helpers behind the init wizard.

Unlike test_cli.py (subprocess e2e), these import functions directly because
the wizard's prompt layer cannot be driven through a non-TTY subprocess.
"""

from __future__ import annotations

import pytest
import yaml

from agensuite.cli import _set_sprint_frontmatter
from agensuite.wizard import InitAnswers, default_answers

SPRINT = (
    "---\n"
    "id: sprint-1\n"
    "title: Core MVP Definition\n"
    "participants: [cpo, cto, cdo, cco]\n"
    "debate_rounds: 2\n"
    "approval_quorum: 2\n"
    "---\n"
    "# Sprint 1: Core MVP Definition\n"
    "\n"
    "## Core Product Idea\n"
    "Trading platform.\n"
)


def test_updates_three_fields_and_preserves_body() -> None:
    out = _set_sprint_frontmatter(
        SPRINT, rounds=3, quorum=1, participants=["cpo", "cto"]
    )
    _, front, body = out.split("---\n", 2)
    meta = yaml.safe_load(front)
    assert meta["debate_rounds"] == 3
    assert meta["approval_quorum"] == 1
    assert meta["participants"] == ["cpo", "cto"]
    # Untouched frontmatter keys survive.
    assert meta["id"] == "sprint-1"
    assert meta["title"] == "Core MVP Definition"
    # Markdown body is preserved verbatim.
    assert "## Core Product Idea" in body
    assert "Trading platform." in body


def test_rejects_quorum_above_participant_count() -> None:
    with pytest.raises(ValueError, match="quorum"):
        _set_sprint_frontmatter(
            SPRINT, rounds=2, quorum=3, participants=["cpo", "cto"]
        )


def test_rejects_quorum_below_one() -> None:
    with pytest.raises(ValueError, match="quorum"):
        _set_sprint_frontmatter(SPRINT, rounds=2, quorum=0, participants=["cpo"])


def test_rejects_rounds_below_one() -> None:
    with pytest.raises(ValueError, match="rounds"):
        _set_sprint_frontmatter(SPRINT, rounds=0, quorum=1, participants=["cpo"])


def test_rejects_unclosed_frontmatter() -> None:
    with pytest.raises(ValueError, match="not closed"):
        _set_sprint_frontmatter(
            "---\nid: x\n", rounds=2, quorum=1, participants=["cpo"]
        )


def test_init_answers_defaults() -> None:
    a = InitAnswers(idea="A trading app")
    assert a.idea == "A trading app"
    assert a.biases == {}
    assert a.debate_rounds == 2
    assert a.approval_quorum == 2
    assert a.participants == ["cpo", "cto", "cdo", "cco"]


def test_default_answers_uses_idea_and_defaults() -> None:
    a = default_answers("My idea")
    assert a.idea == "My idea"
    assert a.biases == {}
    assert a.debate_rounds == 2
    assert a.participants == ["cpo", "cto", "cdo", "cco"]


def test_append_operational_bias_multiple_lines_in_order() -> None:
    from agensuite.cli import _append_operational_bias

    doc = (
        "---\nname: cto\n---\n"
        "# CTO\n\n## Operational Biases\n- baseline bias\n\n## Next Section\nbody\n"
    )
    for line in ["first added", "second added"]:
        doc = _append_operational_bias(doc, line)
    biases_block = doc.split("## Operational Biases", 1)[1].split("## Next Section", 1)[0]
    assert biases_block.index("baseline bias") < biases_block.index("first added") < biases_block.index("second added")


def test_init_applies_wizard_biases_and_sprint_config(tmp_path, monkeypatch) -> None:
    """The interactive (TTY) path applies biases + non-default sprint config.

    Drives the real ``init`` apply logic with a crafted InitAnswers, bypassing
    the live questionary prompts (which a non-TTY test cannot exercise).

    ``init`` is invoked as a plain function rather than through Typer's
    CliRunner: the runner swaps ``sys.stdin`` for its own non-TTY stream during
    invocation, which would defeat the ``isatty`` patch and send ``init`` down
    the non-interactive branch.
    """
    from agensuite import cli, wizard

    crafted = wizard.InitAnswers(
        idea="A budgeting tool",
        biases={"cto": ["prefer boring tech", "cap latency budgets"]},
        debate_rounds=3,
        approval_quorum=1,
        participants=["cpo", "cto"],
    )
    monkeypatch.setattr(wizard, "run_init_wizard", lambda: crafted)

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", _Tty())

    target = tmp_path / "proj"
    # Called as a plain function (not via Typer), so params Typer would inject
    # must be passed explicitly — ``agent=None`` keeps the wizard path.
    cli.init(target_dir=target, idea=None, agent=None)

    # Biases appended to the CTO persona, in order, and only there.
    cto = (target / ".claude" / "agents" / "cto.md").read_text()
    assert "- prefer boring tech" in cto
    assert "- cap latency budgets" in cto
    assert cto.index("prefer boring tech") < cto.index("cap latency budgets")
    cpo = (target / ".claude" / "agents" / "cpo.md").read_text()
    assert "prefer boring tech" not in cpo

    # Non-default sprint config written; idea substituted.
    sprint = (target / "sprints" / "sprint-1.md").read_text()
    meta = yaml.safe_load(sprint.split("---\n", 2)[1])
    assert meta["debate_rounds"] == 3
    assert meta["approval_quorum"] == 1
    assert meta["participants"] == ["cpo", "cto"]
    assert "A budgeting tool" in sprint

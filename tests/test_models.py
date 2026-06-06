"""Unit tests for the Pydantic schemas in :mod:`agensuite.models`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agensuite.models import (
    DebateState,
    DebateTurn,
    DecisionRecord,
    PRStatus,
    PullRequest,
    ReviewComment,
    SprintConfig,
    TurnPhase,
    Verdict,
)


class TestSprintConfigValidation:
    def test_empty_participants_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SprintConfig(
                id="x", title="t", participants=[],
                debate_rounds=1, approval_quorum=1,
            )

    def test_duplicate_participants_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SprintConfig(
                id="x", title="t", participants=["a", "a"],
                debate_rounds=1, approval_quorum=1,
            )

    def test_quorum_above_participant_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SprintConfig(
                id="x", title="t", participants=["a"],
                debate_rounds=1, approval_quorum=2,
            )

    def test_debate_rounds_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SprintConfig(
                id="x", title="t", participants=["a", "b"],
                debate_rounds=0, approval_quorum=1,
            )


class TestDebateStateBuild:
    def _sprint(self, rounds: int = 2) -> SprintConfig:
        return SprintConfig(
            id="s", title="t", participants=["a", "b", "c"],
            debate_rounds=rounds, approval_quorum=1,
        )

    def _pr(self, pid: str, author: str) -> PullRequest:
        return PullRequest(
            id=pid, title=pid, branch=f"feat/{author}", author=author, sprint_id="s",
        )

    def test_skips_self_review(self) -> None:
        sprint = self._sprint(rounds=1)
        prs = [self._pr("pr-a", "a"), self._pr("pr-b", "b")]
        state = DebateState.build(sprint, prs)
        # Each PR is reviewed by 2 non-authors; no self-review slots.
        assert len(state.schedule) == 4
        for turn in state.schedule:
            pr_author = next(p.author for p in prs if p.id == turn.target_pr_id)
            assert turn.speaker != pr_author

    def test_round_ordering_is_contiguous(self) -> None:
        """All round-0 turns come before any round-1 turn."""
        sprint = self._sprint(rounds=2)
        prs = [self._pr("pr-a", "a"), self._pr("pr-b", "b")]
        state = DebateState.build(sprint, prs)
        rounds = [t.round_idx for t in state.schedule]
        # Strictly non-decreasing — round_idx never goes backwards.
        assert rounds == sorted(rounds)

    def test_cursor_helpers(self) -> None:
        sprint = self._sprint(rounds=1)
        prs = [self._pr("pr-a", "a"), self._pr("pr-b", "b")]
        state = DebateState.build(sprint, prs)

        assert state.current_turn() is not None
        assert not state.is_finished

        for _ in range(len(state.schedule)):
            state.advance()

        assert state.is_finished
        assert state.current_turn() is None
        # Advance past the end is idempotent.
        state.advance()
        assert state.cursor == len(state.schedule)


class TestDecisionRecordMarkdown:
    def _record(self, **overrides) -> DecisionRecord:
        kwargs = dict(
            id="ADR-001", title="Pick X", sprint_id="s",
            context="ctx", options=["A", "B"], decision="B",
            consequences="faster", signoffs=["ceo"],
        )
        kwargs.update(overrides)
        return DecisionRecord(**kwargs)

    def test_default_status_is_accepted(self) -> None:
        md = self._record().to_markdown()
        assert "Status:** Accepted" in md

    def test_status_field_is_rendered(self) -> None:
        md = self._record(status="Rejected").to_markdown()
        assert "Status:** Rejected" in md
        assert "Status:** Accepted" not in md

    def test_empty_sections_render_placeholder(self) -> None:
        md = self._record(
            context="", options=[], decision="", consequences="", signoffs=[],
        ).to_markdown()
        # Five sections (Context, Options, Decision, Consequences) plus the
        # Signoffs header line — all should fall back to the placeholder.
        assert md.count("_None recorded._") >= 4

    def test_options_rendered_as_bullets(self) -> None:
        md = self._record(options=["First", "Second"]).to_markdown()
        assert "- First" in md
        assert "- Second" in md


class TestReviewCommentVerdict:
    def _pr(self, *reviews: ReviewComment) -> PullRequest:
        return PullRequest(
            id="pr-1", title="t", branch="b", author="a", sprint_id="s",
            reviews=list(reviews),
        )

    def test_approve_counts_toward_quorum(self) -> None:
        pr = self._pr(
            ReviewComment(reviewer="b", comment="ok", verdict=Verdict.APPROVE),
        )
        assert pr.approval_count == 1
        assert pr.open_change_requests == []

    def test_request_changes_does_not_count(self) -> None:
        pr = self._pr(
            ReviewComment(reviewer="b", comment="nope", verdict=Verdict.REQUEST_CHANGES),
        )
        assert pr.approval_count == 0
        assert pr.open_change_requests == ["b"]

    def test_withdraw_clears_change_request(self) -> None:
        pr = self._pr(
            ReviewComment(reviewer="b", comment="nope", verdict=Verdict.REQUEST_CHANGES),
            ReviewComment(
                reviewer="b", comment="ok now", verdict=Verdict.APPROVE,
                phase=TurnPhase.FOLLOWUP,
            ),
        )
        assert pr.approval_count == 1
        assert pr.open_change_requests == []

    def test_rebuttal_does_not_count_as_verdict(self) -> None:
        """Author rebuttals are excluded from approval / open-CR tallies."""
        pr = self._pr(
            ReviewComment(reviewer="b", comment="nope", verdict=Verdict.REQUEST_CHANGES),
            ReviewComment(
                reviewer="a", comment="addressed", verdict=Verdict.COMMENT,
                phase=TurnPhase.REBUTTAL,
            ),
        )
        assert pr.approval_count == 0
        assert pr.open_change_requests == ["b"]

    def test_legacy_approved_field_migrates(self) -> None:
        """Pre-schema-2 dicts that still carry `approved: bool` deserialize."""
        legacy = {"reviewer": "b", "comment": "ok", "approved": True}
        rc = ReviewComment.model_validate(legacy)
        assert rc.verdict == Verdict.APPROVE
        assert rc.phase == TurnPhase.REVIEW

        legacy_false = {"reviewer": "b", "comment": "meh", "approved": False}
        rc2 = ReviewComment.model_validate(legacy_false)
        assert rc2.verdict == Verdict.COMMENT


class TestDebateTurnPhase:
    def test_default_phase_is_review(self) -> None:
        t = DebateTurn(round_idx=0, speaker="a", target_pr_id="pr-1")
        assert t.phase == TurnPhase.REVIEW
        assert t.parent_turn_idx is None

    def test_followup_records_parent(self) -> None:
        t = DebateTurn(
            round_idx=0, speaker="b", target_pr_id="pr-1",
            phase=TurnPhase.FOLLOWUP, parent_turn_idx=4,
        )
        assert t.parent_turn_idx == 4


class TestPRStatusEnum:
    def test_deadlocked_and_changes_requested_present(self) -> None:
        assert PRStatus.DEADLOCKED.value == "DEADLOCKED"
        assert PRStatus.CHANGES_REQUESTED.value == "CHANGES_REQUESTED"


class TestSprintConfigRebuttalDepth:
    def test_default_is_one(self) -> None:
        cfg = SprintConfig(
            id="s", title="t", participants=["a", "b"],
            debate_rounds=1, approval_quorum=1,
        )
        assert cfg.rebuttal_depth == 1

    def test_rebuttal_depth_capped_at_three(self) -> None:
        with pytest.raises(ValidationError):
            SprintConfig(
                id="s", title="t", participants=["a", "b"],
                debate_rounds=1, approval_quorum=1, rebuttal_depth=4,
            )


def test_pullrequest_headline_defaults_empty():
    from agensuite.models import PullRequest
    pr = PullRequest(id="pr-1", title="t", branch="b", author="a", sprint_id="s")
    assert pr.headline == ""
    pr2 = PullRequest(
        id="pr-2", title="t", branch="b", author="a", sprint_id="s",
        headline="Postgres over Dynamo for sub-50ms reads",
    )
    assert pr2.headline == "Postgres over Dynamo for sub-50ms reads"


def test_reviewcomment_counter_defaults_empty():
    from agensuite.models import ReviewComment
    rc = ReviewComment(reviewer="a", comment="c")
    assert rc.counter == ""
    rc2 = ReviewComment(reviewer="a", comment="c", counter="per-entity TTL policy")
    assert rc2.counter == "per-entity TTL policy"


def test_debatestate_last_emitted_round_defaults_minus_one():
    from agensuite.models import DebateState
    ds = DebateState(sprint_id="s")
    assert ds.last_emitted_round == -1


def test_notify_config_defaults_and_validation():
    from agensuite.models import NotifyConfig

    cfg = NotifyConfig(chat_id="123456")
    assert cfg.channel == "telegram"
    assert cfg.chat_id == "123456"
    assert cfg.events == ["gate", "decision", "sprint-start"]

    # extra keys are rejected so a typo'd config fails loudly
    with pytest.raises(ValidationError):
        NotifyConfig(chat_id="1", bogus=True)

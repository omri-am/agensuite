"""Pydantic v2 schemas — single source of truth for everything that gets persisted.

Every object the orchestrator hands off between turns (sprint config, debate
state, PRs, ADRs) is defined here. Keeping schemas in one module means the
JSON written under ``workspace/.state`` can always be validated against the
exact same types the runtime uses, with no drift between producers and
consumers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utcnow() -> datetime:
    """Single chokepoint for timestamps so tests can monkeypatch one symbol."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MessageType(str, Enum):
    """Kind of utterance in the debate transcript."""

    OPENING = "OPENING"
    REVIEW = "REVIEW"
    DECISION = "DECISION"


class PRStatus(str, Enum):
    """Lifecycle of a pull request inside the simulated repo."""

    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    DEADLOCKED = "DEADLOCKED"
    MERGED = "MERGED"
    REJECTED = "REJECTED"


class Verdict(str, Enum):
    """Reviewer verdict on a PR. ``APPROVE`` is the only verdict that
    counts toward the merge quorum; ``REQUEST_CHANGES`` blocks merge until
    withdrawn; ``COMMENT`` is non-binding."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


class TurnPhase(str, Enum):
    """Which beat of the bounded threaded rebuttal protocol a turn belongs to.

    REVIEW: initial reviewer verdict on the PR.
    REBUTTAL: author's single collective response addressing all
        change-requesters at once. Author-only.
    FOLLOWUP: each change-requester's single stand-or-withdraw beat after
        the rebuttal. Closes the thread either way.
    """

    REVIEW = "REVIEW"
    REBUTTAL = "REBUTTAL"
    FOLLOWUP = "FOLLOWUP"


# ---------------------------------------------------------------------------
# Core conversation / VCS records
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """A single line of the debate transcript."""

    model_config = ConfigDict(extra="forbid")

    id: str
    sender: str
    recipient: str
    msg_type: MessageType
    content: str
    parent_id: str | None = None
    round_idx: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)


class Commit(BaseModel):
    """Record of a commit produced inside the simulated repo."""

    model_config = ConfigDict(extra="forbid")

    sha: str
    branch: str
    author: str
    message: str
    files: list[str]
    timestamp: datetime = Field(default_factory=_utcnow)


class ReviewComment(BaseModel):
    """A reviewer's note on a PR.

    ``verdict == APPROVE`` counts toward the merge quorum. The latest
    verdict per reviewer is what's consulted (see
    :py:meth:`PullRequest.approval_count`) so a reviewer can withdraw a
    change request by posting a fresh APPROVE or COMMENT.

    ``phase`` records which beat of the rebuttal protocol the comment was
    posted under (initial REVIEW, the author's REBUTTAL, or a reviewer's
    closing FOLLOWUP). Phase preconditions are enforced by the CLI, not
    here.
    """

    model_config = ConfigDict(extra="forbid")

    reviewer: str
    file: str | None = None
    comment: str
    verdict: Verdict = Verdict.COMMENT
    phase: TurnPhase = TurnPhase.REVIEW
    counter: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_approved(cls, data):  # type: ignore[no-untyped-def]
        """Map legacy ``{"approved": true/false}`` payloads onto ``verdict``.

        Kept so unit tests that hand-construct old-shaped JSON keep working;
        on-disk state from a previous schema version is rejected up-front
        by ``state.py`` so it never reaches this validator.
        """
        if not isinstance(data, dict):
            return data
        if "approved" not in data:
            return data
        out = dict(data)
        legacy = out.pop("approved")
        out.setdefault(
            "verdict",
            Verdict.APPROVE.value if legacy else Verdict.COMMENT.value,
        )
        return out


class PullRequest(BaseModel):
    """A pull request opened by a spoke against ``base`` (default ``main``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    branch: str
    base: str = "main"
    author: str
    description: str = ""
    headline: str = ""
    files: list[str] = Field(default_factory=list)
    status: PRStatus = PRStatus.OPEN
    reviews: list[ReviewComment] = Field(default_factory=list)
    sprint_id: str
    conflict_details: str | None = None
    human_disposition: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    def _latest_verdicts(self) -> dict[str, Verdict]:
        """Per-reviewer map: reviewer -> latest non-rebuttal verdict.

        Rebuttal comments are posted by the author and don't represent a
        review verdict, so they're excluded from the tally.
        """
        latest: dict[str, Verdict] = {}
        for r in self.reviews:
            if r.phase == TurnPhase.REBUTTAL:
                continue
            latest[r.reviewer] = r.verdict
        return latest

    @property
    def approval_count(self) -> int:
        """Count distinct reviewers whose *latest* verdict is ``APPROVE``.

        Latest-wins: a reviewer who first requests changes and then approves
        contributes one approval and zero open change requests. This is what
        lets the FOLLOWUP "withdraw" beat naturally clear a prior
        ``REQUEST_CHANGES``.
        """
        return sum(
            1 for v in self._latest_verdicts().values() if v == Verdict.APPROVE
        )

    @property
    def open_change_requests(self) -> list[str]:
        """Reviewers whose latest verdict is ``REQUEST_CHANGES``.

        These reviewers block merge (see ``cli.pr_merge``) until they
        withdraw via a FOLLOWUP that flips the verdict to ``APPROVE`` or
        ``COMMENT``. Order is insertion order over ``self.reviews``.
        """
        latest = self._latest_verdicts()
        seen: list[str] = []
        for r in self.reviews:
            if r.phase == TurnPhase.REBUTTAL:
                continue
            if (
                latest.get(r.reviewer) == Verdict.REQUEST_CHANGES
                and r.reviewer not in seen
            ):
                seen.append(r.reviewer)
        return seen


# ---------------------------------------------------------------------------
# Decision records (ADRs)
# ---------------------------------------------------------------------------


class DecisionRecord(BaseModel):
    """An architecture decision record produced when the debate converges.

    ``status`` follows the Nygard ADR vocabulary — typically ``"Accepted"``,
    ``"Rejected"``, ``"Superseded"``, or ``"Proposed"``. Callers that have
    enough context to set it explicitly (e.g. the ``adr record`` CLI command
    inspects merged vs rejected PRs) should do so; otherwise the
    ``"Accepted"`` default matches the most common case where the debate
    converged on a merge.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    sprint_id: str
    context: str
    options: list[str]
    decision: str
    consequences: str
    signoffs: list[str]
    status: str = "Accepted"
    timestamp: datetime = Field(default_factory=_utcnow)

    def to_markdown(self) -> str:
        """Render an executive-ready ADR document.

        Layout mirrors the Nygard ADR format so the output drops directly into
        ``docs/adr/`` without further massaging. Empty sections degrade
        gracefully to a single ``_None recorded._`` line rather than disappearing
        entirely, which preserves the document's shape for diff review.
        """

        def _section(title: str, body: str) -> str:
            stripped = body.strip()
            payload = stripped if stripped else "_None recorded._"
            return f"## {title}\n\n{payload}\n"

        def _bullets(items: Iterable[str]) -> str:
            cleaned = [i.strip() for i in items if i and i.strip()]
            if not cleaned:
                return "_None recorded._"
            return "\n".join(f"- {item}" for item in cleaned)

        signoffs = ", ".join(s for s in self.signoffs if s.strip()) or "_None recorded._"
        date_str = self.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d")

        parts = [
            f"# ADR {self.id}: {self.title}",
            "",
            f"- **Sprint:** `{self.sprint_id}`",
            f"- **Date:** {date_str}",
            f"- **Status:** {self.status}",
            f"- **Signoffs:** {signoffs}",
            "",
            _section("Context", self.context),
            _section("Options Considered", _bullets(self.options)),
            _section("Decision", self.decision),
            _section("Consequences", self.consequences),
        ]
        return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sprint configuration
# ---------------------------------------------------------------------------


class SprintPrerequisite(BaseModel):
    """A file that must already exist on ``branch`` before a sprint starts."""

    model_config = ConfigDict(extra="forbid")

    branch: str
    path: str


class SprintConfig(BaseModel):
    """Parsed contents of ``sprints/<id>.yaml``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    participants: list[str]
    prerequisite_files: list[SprintPrerequisite] = Field(default_factory=list)
    debate_rounds: int = Field(default=1, ge=1)
    approval_quorum: int = Field(default=1, ge=1)
    rebuttal_depth: int = Field(default=1, ge=1, le=3)
    body: str = ""

    @model_validator(mode="after")
    def _validate_participants(self) -> "SprintConfig":
        if not self.participants:
            raise ValueError("SprintConfig.participants must not be empty")
        seen: set[str] = set()
        for p in self.participants:
            if p in seen:
                raise ValueError(f"duplicate participant: {p!r}")
            seen.add(p)
        if self.approval_quorum > len(self.participants):
            raise ValueError(
                f"approval_quorum={self.approval_quorum} exceeds participant count "
                f"({len(self.participants)})"
            )
        return self


# ---------------------------------------------------------------------------
# Debate state
# ---------------------------------------------------------------------------


class DebateTurn(BaseModel):
    """A scheduled slot: ``speaker`` reviews ``target_pr_id`` in ``round_idx``.

    ``phase`` distinguishes initial reviews from rebuttal / follow-up beats
    appended in response to verdict events. ``parent_turn_idx`` points
    follow-up slots back at the rebuttal they answer, so the orchestrator
    can hand the subagent the exact rebuttal message.
    """

    model_config = ConfigDict(extra="forbid")

    round_idx: int
    speaker: str
    target_pr_id: str
    phase: TurnPhase = TurnPhase.REVIEW
    parent_turn_idx: int | None = None


class DebateState(BaseModel):
    """Mutable per-sprint state persisted between CLI turns.

    The ``schedule`` is a flat list of turns rather than a nested
    round/speaker matrix because the CLI advances one slot at a time and a
    flat list makes ``cursor`` arithmetic trivial.
    """

    model_config = ConfigDict(extra="forbid")

    sprint_id: str
    transcript: list[Message] = Field(default_factory=list)
    pr_ids: list[str] = Field(default_factory=list)
    schedule: list[DebateTurn] = Field(default_factory=list)
    cursor: int = 0
    last_emitted_round: int = -1

    @property
    def is_finished(self) -> bool:
        return self.cursor >= len(self.schedule)

    def current_turn(self) -> DebateTurn | None:
        if self.is_finished:
            return None
        return self.schedule[self.cursor]

    def advance(self) -> None:
        """Move the cursor forward by one slot.

        Idempotent at the end of the schedule: calling ``advance`` on a
        finished debate leaves the cursor at ``len(schedule)`` rather than
        marching past it, so accidental double-advances don't break ordering
        invariants downstream.
        """
        if self.cursor < len(self.schedule):
            self.cursor += 1

    @classmethod
    def build(
        cls,
        sprint: SprintConfig,
        prs: list[PullRequest],
    ) -> "DebateState":
        """Construct an initial ``DebateState`` from a sprint config + opened PRs.

        Schedule is a flat sequence of ``(round, speaker, pr)`` slots in this
        deterministic order:

        1. Outer loop over ``round_idx`` ``0 .. sprint.debate_rounds - 1``.
        2. Middle loop over PRs in the order they were supplied.
        3. Inner loop over participants in the order declared in the sprint.

        A participant never reviews their own PR; those slots are skipped, not
        left blank. Determinism is critical because the CLI replays the state
        file across turns and any ordering instability would scramble the
        conversation history.
        """
        schedule: list[DebateTurn] = []
        for round_idx in range(sprint.debate_rounds):
            for pr in prs:
                for speaker in sprint.participants:
                    if speaker == pr.author:
                        continue
                    schedule.append(
                        DebateTurn(
                            round_idx=round_idx,
                            speaker=speaker,
                            target_pr_id=pr.id,
                        )
                    )
        return cls(
            sprint_id=sprint.id,
            pr_ids=[pr.id for pr in prs],
            schedule=schedule,
            cursor=0,
        )

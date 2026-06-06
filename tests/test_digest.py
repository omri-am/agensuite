from agensuite import digest


def test_meter_filled_and_empty():
    assert digest.meter(1, 2) == "●○"
    assert digest.meter(2, 2) == "●●"
    assert digest.meter(0, 2) == "○○"


def test_meter_never_negative_when_over_quorum():
    assert digest.meter(3, 2) == "●●●"


def test_truncate_short_passthrough():
    assert digest.truncate("hello", 80) == "hello"


def test_truncate_long_adds_ellipsis():
    out = digest.truncate("x" * 100, 10)
    assert out == "x" * 9 + "…"
    assert len(out) == 10


from agensuite.models import PRStatus, PullRequest, ReviewComment, Verdict, TurnPhase


def _pr(**kw):
    base = dict(id="pr-2", title="data model", branch="b", author="cdo", sprint_id="s")
    base.update(kw)
    return PullRequest(**base)


def test_render_verdict_line_request_changes():
    pr = _pr(headline="90-day flat TTL")
    pr.reviews.append(ReviewComment(
        reviewer="cto", comment="90-day TTL breaks GDPR minimization",
        verdict=Verdict.REQUEST_CHANGES, counter="per-entity TTL policy",
    ))
    line = digest.render_verdict_line(pr=pr, comment=pr.reviews[-1], round_idx=1, quorum=2)
    assert line.startswith("🔴 r1 · cto → pr-2")
    assert "90-day flat TTL" in line
    assert "GDPR minimization" in line
    assert "○○" in line
    assert "open: cto" in line


def test_render_verdict_line_falls_back_to_title_without_headline():
    pr = _pr(headline="")
    pr.reviews.append(ReviewComment(reviewer="cto", comment="ok", verdict=Verdict.APPROVE))
    line = digest.render_verdict_line(pr=pr, comment=pr.reviews[-1], round_idx=0, quorum=1)
    assert "data model" in line
    assert line.startswith("✅ r0 · cto → pr-2")


def test_render_pr_terminal_merged():
    pr = _pr(headline="Postgres over Dynamo", status=PRStatus.MERGED)
    pr.reviews.append(ReviewComment(reviewer="cto", comment="lgtm", verdict=Verdict.APPROVE))
    pr.reviews.append(ReviewComment(reviewer="cco", comment="lgtm", verdict=Verdict.APPROVE))
    line = digest.render_pr_terminal(pr=pr, quorum=2)
    assert line.startswith("🟢 pr-2 MERGED")
    assert "Postgres over Dynamo" in line
    assert "●● 2/2" in line


def test_render_pr_terminal_deadlocked():
    pr = _pr(headline="strict compliance posture", status=PRStatus.DEADLOCKED)
    line = digest.render_pr_terminal(pr=pr, quorum=2)
    assert line.startswith("⚔️ pr-2 DEADLOCKED")
    assert "strict compliance posture" in line


def test_render_ledger_groups_locked_and_contested():
    merged = _pr(id="pr-1", headline="Postgres over Dynamo", status=PRStatus.MERGED)
    contested = _pr(id="pr-2", headline="90-day flat TTL", status=PRStatus.CHANGES_REQUESTED)
    contested.reviews.append(ReviewComment(
        reviewer="cto", comment="breaks GDPR", verdict=Verdict.REQUEST_CHANGES,
        counter="per-entity TTL policy",
    ))
    pending = _pr(id="pr-3", headline="three pricing tiers", status=PRStatus.OPEN)

    out = digest.render_ledger([merged, contested, pending], quorum=2, round_label="ROUND 1")
    assert "ROUND 1" in out
    assert "LOCKED" in out and "CONTESTED" in out
    assert "Postgres over Dynamo" in out
    assert "90-day flat TTL" in out
    assert "per-entity TTL policy" in out
    assert "three pricing tiers" in out
    assert "\x1b[" not in out


def test_render_ledger_empty_is_total():
    out = digest.render_ledger([], quorum=1, round_label=None)
    assert "LOCKED" in out


def test_colorize_noop_when_not_tty():
    assert digest.colorize("🔴 hi", tty=False) == "🔴 hi"


def test_colorize_wraps_when_tty_and_no_NO_COLOR(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = digest.colorize("🔴 hi", tty=True)
    assert out.startswith("\x1b[")
    assert out.endswith("\x1b[0m")
    assert "🔴 hi" in out


def test_colorize_respects_NO_COLOR(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert digest.colorize("🔴 hi", tty=True) == "🔴 hi"

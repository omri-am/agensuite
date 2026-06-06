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

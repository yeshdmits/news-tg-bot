"""Pure runner helpers."""

from newsbot.runner import _failure_backoff


def test_failure_backoff_formula():
    # first failure keeps the normal cadence, then doubles, capped at 60
    assert _failure_backoff(10, 1) == 10
    assert _failure_backoff(10, 2) == 20
    assert _failure_backoff(10, 3) == 40
    assert _failure_backoff(10, 4) == 60
    assert _failure_backoff(10, 10) == 60
    assert _failure_backoff(5, 1) == 5
    # a source slower than the cap never gets *faster* on failure
    assert _failure_backoff(90, 1) == 90
    assert _failure_backoff(90, 5) == 90

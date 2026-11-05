"""The run_concurrently fixture turns hangs and thread errors into test failures (#48)."""

from __future__ import annotations

import threading

import pytest


def test_targets_start_together_and_finish(run_concurrently):
    started = threading.Barrier(2, timeout=5)  # only passes if both run at the same time
    finished: list[str] = []

    def first():
        started.wait()
        finished.append("first")

    def second():
        started.wait()
        finished.append("second")

    run_concurrently(first, second, timeout=5)
    assert sorted(finished) == ["first", "second"]


def test_a_stuck_target_fails_with_its_name(run_concurrently):
    release = threading.Event()

    def stuck():
        release.wait(30)

    def quick():
        pass

    try:
        with pytest.raises(pytest.fail.Exception, match="did not finish within 2s") as caught:
            run_concurrently(stuck, quick, timeout=2)
    finally:
        release.set()
    # Only "stuck" is required; on a slow runner "quick" may also be listed.
    assert "'0:stuck'" in str(caught.value)


def test_an_error_in_a_target_is_raised(run_concurrently):
    def broken():
        raise ValueError("synthetic failure")

    def quick():
        pass

    with pytest.raises(AssertionError, match="'0:broken' raised ValueError") as caught:
        run_concurrently(broken, quick, timeout=5)
    assert isinstance(caught.value.__cause__, ValueError)


def test_every_error_is_reported_and_same_function_twice_is_distinguished(run_concurrently):
    def broken():
        raise ValueError("synthetic failure")

    with pytest.raises(AssertionError) as caught:
        run_concurrently(broken, broken, timeout=5)
    assert "'0:broken'" in str(caught.value)
    assert "'1:broken'" in str(caught.value)

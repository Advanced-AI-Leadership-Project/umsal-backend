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
        release.wait(10)

    def quick():
        pass

    try:
        with pytest.raises(pytest.fail.Exception, match=r"did not finish within 0.5s: \['stuck'\]"):
            run_concurrently(stuck, quick, timeout=0.5)
    finally:
        release.set()


def test_an_error_in_a_target_is_raised(run_concurrently):
    def broken():
        raise ValueError("synthetic failure")

    def quick():
        pass

    with pytest.raises(AssertionError, match="'broken' raised ValueError") as caught:
        run_concurrently(broken, quick, timeout=5)
    assert isinstance(caught.value.__cause__, ValueError)

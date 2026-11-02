"""Integration preparation and tracked-tree rejection use isolated real Git repos."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("ci_harness", TOOLS / "ci_harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)
sys.path.pop(0)


class WorkflowTrustTests(unittest.TestCase):
    def test_every_dispatch_checkout_loads_the_runner_from_its_own_trusted_sha(self):
        workflow = (TOOLS.parent / ".github/workflows/ci.yml").read_text()
        checkouts = workflow.split("- uses: actions/checkout@")[1:]
        self.assertGreaterEqual(len(checkouts), 3)
        for item in checkouts:
            block = item.split("\n      - ", 1)[0]
            self.assertIn("ref: ${{ github.sha }}", block)
            self.assertIn("persist-credentials: false", block)
            self.assertNotIn("inputs.", block)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="umsal-integration-test-")
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(harness, "ROOT", self.root),
            patch.object(harness, "REPORTS", self.root / ".reports"),
        ]
        for item in self.patches:
            item.start()
        self.git("init", "-b", "main")
        self.git("config", "core.hooksPath", str(self.root / "disabled-hooks"))
        self.git("config", "user.name", "Synthetic fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "base.txt").write_text("base\n")
        self.git("add", "base.txt")
        self.git("commit", "-m", "chore: synthetic base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "candidate")
        (self.root / "candidate.txt").write_text("candidate\n")
        self.git("add", "candidate.txt")
        self.git("commit", "-m", "feat: synthetic candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.git("switch", "main")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL
        ).strip()

    def dispatch(self, **overrides):
        values = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "HARNESS_MODE": "integration",
            "HARNESS_PR": "81",
            "HARNESS_BASE": self.base,
            "HARNESS_HEAD": self.head,
            "GITHUB_SHA": self.base,
            "GITHUB_REF": "refs/heads/main",
        }
        values.update(overrides)
        return patch.dict(os.environ, values)

    def test_exact_base_and_head_are_integrated_without_creating_a_commit(self):
        with self.dispatch():
            harness.prepare()
        self.assertTrue((self.root / "base.txt").is_file())
        self.assertTrue((self.root / "candidate.txt").is_file())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        receipt = json.loads((self.root / ".reports/integration.json").read_text())
        self.assertEqual(receipt["base"], self.base)
        self.assertEqual(receipt["head"], self.head)
        self.assertEqual(receipt["tree"], self.git("write-tree"))

    def test_wrong_base_invalid_sha_and_non_numeric_pr_are_rejected_before_merge(self):
        for values in [
            {"HARNESS_BASE": self.head},
            {"HARNESS_HEAD": "--untrusted-ref"},
            {"HARNESS_PR": "81;echo unsafe"},
            {"HARNESS_MODE": "unknown"},
        ]:
            with (
                self.subTest(values=values),
                self.dispatch(**values),
                self.assertRaises(ValueError),
            ):
                harness.prepare()
            self.assertFalse((self.root / "candidate.txt").exists())

    def test_main_verification_never_merges_candidate_inputs(self):
        with self.dispatch(HARNESS_MODE="main"), self.assertRaises(ValueError):
            harness.prepare()
        with self.dispatch(HARNESS_MODE="main", HARNESS_HEAD="", HARNESS_PR=""):
            harness.prepare()
        self.assertFalse((self.root / "candidate.txt").exists())

    def test_clean_runner_without_identity_can_integrate_without_storing_identity(self):
        self.git("config", "--unset", "user.name")
        self.git("config", "--unset", "user.email")
        self.git("config", "user.useConfigOnly", "true")
        empty = self.root / "empty-global-gitconfig"
        empty.write_text("")
        with (
            self.dispatch(),
            patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": str(empty),
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ),
        ):
            harness.prepare()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertTrue((self.root / "candidate.txt").exists())
        for key in ["user.name", "user.email"]:
            result = subprocess.run(
                ["git", "config", "--local", "--get", key],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)

    def test_main_dispatch_cannot_attest_a_different_checkout_than_its_run_sha(self):
        self.git("switch", "candidate")
        with (
            self.dispatch(
                HARNESS_MODE="main",
                HARNESS_BASE=self.head,
                HARNESS_HEAD="",
                HARNESS_PR="",
            ),
            self.assertRaises(ValueError),
        ):
            harness.prepare()
        self.assertFalse((self.root / ".reports/integration.json").exists())

    def test_conflicts_fail_instead_of_selecting_one_side(self):
        self.git("switch", "candidate")
        (self.root / "base.txt").write_text("candidate edit\n")
        self.git("commit", "-am", "feat: candidate edit")
        self.head = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        (self.root / "base.txt").write_text("main edit\n")
        self.git("commit", "-am", "feat: main edit")
        self.base = self.git("rev-parse", "HEAD")
        with self.dispatch(), self.assertRaises(subprocess.CalledProcessError):
            harness.prepare()
        self.assertFalse((self.root / ".reports/integration.json").exists())

    def test_broken_symlink_is_rejected_in_a_full_tree_scan(self):
        (self.root / "broken-link").symlink_to("absent-target")
        self.git("add", "broken-link")
        with patch.object(harness, "run") as commands, self.assertRaises(ValueError):
            harness.quality()
        commands.assert_not_called()

    def test_existing_tracked_secret_is_scanned_even_without_a_diff(self):
        (self.root / "fixture.txt").write_text("ghp_" + "A" * 40)
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "test: intentionally synthetic token")
        with patch.object(harness, "run"), self.assertRaises(ValueError) as failure:
            harness.quality()
        self.assertNotIn("A" * 40, str(failure.exception))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for safe local AI Git operations and merge gates."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "ai_workflow.py"
SPEC = importlib.util.spec_from_file_location("ai_workflow", MODULE_PATH)
ai = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ai)

CONFIG = {
    "schema_version": 1,
    "repository": "Advanced-AI-Leadership-Project/umsal-backend",
    "base_branch": "main",
    "branch_pattern": r"codex/[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*",
    "commit_types": [
        "feat",
        "fix",
        "docs",
        "refactor",
        "test",
        "chore",
        "ci",
        "build",
        "perf",
        "revert",
    ],
    "commit_max_length": 100,
    "required_checks": ["policy", "python", "security"],
    "required_workflows": [".github/workflows/policy.yml", ".github/workflows/ci.yml"],
}
HEAD = "a" * 40


class TemporaryGit(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="umsal-git-helper-")
        self.root = Path(self.directory.name)
        self.patch_root = patch.object(ai, "ROOT", self.root)
        self.patch_root.start()
        self.git("init", "-b", "main")
        self.git("config", "core.hooksPath", str(self.root / "no-inherited-hooks"))
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "README.md").write_text("fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "chore: initial synthetic fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("remote", "add", "origin", "https://github.com/" + CONFIG["repository"] + ".git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def tearDown(self):
        self.patch_root.stop()
        self.directory.cleanup()

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, check=True
        )
        return result.stdout

    def test_origin_accepts_expected_repo_and_rejects_extra_push_destination(self):
        ai.verify_origin(CONFIG)
        self.git(
            "remote", "set-url", "--add", "--push", "origin", "https://github.com/other/repo.git"
        )
        with self.assertRaises(ai.WorkflowError):
            ai.verify_origin(CONFIG)

    def test_existing_hooks_path_is_not_overwritten(self):
        self.git("config", "core.hooksPath", "team-existing-hooks")
        with self.assertRaises(ai.WorkflowError):
            ai.install_hooks()
        self.assertEqual(self.git("config", "core.hooksPath").strip(), "team-existing-hooks")

    def test_shared_hooks_are_repository_local(self):
        hooks = self.root / ".githooks"
        hooks.mkdir()
        for name in ["pre-commit", "commit-msg", "pre-push"]:
            (hooks / name).write_text("#!/bin/sh\nexit 0\n")
        self.git("config", "--unset", "core.hooksPath")
        ai.install_hooks()
        self.assertEqual(self.git("config", "--local", "core.hooksPath").strip(), ".githooks")

    def test_dirty_start_is_preserved_without_github_request(self):
        (self.root / "unsaved.txt").write_text("keep this work")
        with patch.object(ai, "gh_json") as network, self.assertRaises(ai.WorkflowError):
            ai.start(CONFIG, 1, "feature")
        network.assert_not_called()
        self.assertEqual((self.root / "unsaved.txt").read_text(), "keep this work")
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")

    def test_pre_push_blocks_main_delete_and_non_fast_forward(self):
        with self.assertRaises(ai.WorkflowError):
            ai.pre_push(
                CONFIG,
                "origin",
                "https://github.com/" + CONFIG["repository"],
                f"refs/heads/main {self.base} refs/heads/main {ai.ZERO}",
            )
        with self.assertRaises(ai.WorkflowError):
            ai.pre_push(
                CONFIG,
                "origin",
                "https://github.com/" + CONFIG["repository"],
                f"(delete) {ai.ZERO} refs/heads/codex/1-feature {self.base}",
            )
        # An unknown remote commit cannot be an established ancestor.
        with self.assertRaises(ai.WorkflowError):
            ai.pre_push(
                CONFIG,
                "origin",
                "https://github.com/" + CONFIG["repository"],
                f"refs/heads/codex/1-feature {self.base} refs/heads/codex/1-feature {HEAD}",
            )

    def test_pre_push_checks_whole_task_range(self):
        branch = "codex/1-feature"
        with patch.object(ai, "validator") as validate:
            ai.pre_push(
                CONFIG,
                "origin",
                "git@github.com:" + CONFIG["repository"] + ".git",
                f"refs/heads/{branch} {self.base} refs/heads/{branch} {ai.ZERO}",
            )
        validate.assert_called_once_with(
            "--base", "origin/main", "--head", self.base, "--branch", branch
        )

    def test_ref_redirection_is_blocked(self):
        with self.assertRaises(ai.WorkflowError):
            ai.pre_push(
                CONFIG,
                "origin",
                "git@github.com:" + CONFIG["repository"] + ".git",
                f"refs/heads/main {self.base} refs/heads/codex/1-feature {ai.ZERO}",
            )

    def test_start_refuses_remote_existing_task_branch(self):
        with (
            patch.object(ai, "verify_origin"),
            patch.object(ai, "require_clean"),
            patch.object(ai, "active_branch", return_value="main"),
            patch.object(ai, "gh_json", return_value={"state": "open"}),
            patch.object(ai, "git") as git_command,
            patch.object(ai, "command", return_value=subprocess.CompletedProcess([], 0, "", "")),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.start(CONFIG, 1, "feature")
        self.assertFalse(any(call.args[:1] == ("switch",) for call in git_command.call_args_list))

    def test_start_refuses_local_main_ahead_of_origin(self):
        def git_result(*args, **_kwargs):
            if args == ("rev-parse", "HEAD"):
                return HEAD
            if args == ("rev-parse", "origin/main"):
                return "b" * 40
            return ""

        with (
            patch.object(ai, "verify_origin"),
            patch.object(ai, "require_clean"),
            patch.object(ai, "active_branch", return_value="main"),
            patch.object(ai, "gh_json", return_value={"state": "open"}),
            patch.object(ai, "git", side_effect=git_result) as git_command,
            patch.object(ai, "command", return_value=subprocess.CompletedProcess([], 2, "", "")),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.start(CONFIG, 1, "feature")
        self.assertFalse(any(call.args[:1] == ("switch",) for call in git_command.call_args_list))


class MergeGates(unittest.TestCase):
    def good_rollup(self):
        return [
            {
                "__typename": "CheckRun",
                "name": name,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "detailsUrl": f"https://github.com/{CONFIG['repository']}/actions/runs/1/job/1",
            }
            for name in CONFIG["required_checks"]
        ]

    def test_all_expected_checks_must_succeed(self):
        ai.validate_rollup(self.good_rollup(), CONFIG["required_checks"], CONFIG["repository"], {1})
        for failure in ["FAILURE", "SKIPPED", "NEUTRAL", "CANCELLED", None]:
            bad = self.good_rollup()
            bad[0]["conclusion"] = failure
            with self.subTest(failure=failure), self.assertRaises(ai.WorkflowError):
                ai.validate_rollup(bad, CONFIG["required_checks"], CONFIG["repository"], {1})

    def test_missing_pending_and_external_checks_block_merge(self):
        for mutation in ["missing", "pending", "external", "legacy-status"]:
            bad = self.good_rollup()
            if mutation == "missing":
                bad.pop()
            elif mutation == "pending":
                bad[0]["status"] = "IN_PROGRESS"
            elif mutation == "external":
                bad[0]["detailsUrl"] = "https://example.invalid/fake"
            else:
                bad[0]["__typename"] = "StatusContext"
            with self.subTest(mutation=mutation), self.assertRaises(ai.WorkflowError):
                ai.validate_rollup(bad, CONFIG["required_checks"], CONFIG["repository"], {1})

    def test_successful_old_check_does_not_mask_pending_duplicate(self):
        rollup = self.good_rollup()
        duplicate = copy.deepcopy(rollup[0])
        duplicate["status"] = "IN_PROGRESS"
        rollup.append(duplicate)
        with self.assertRaises(ai.WorkflowError):
            ai.validate_rollup(rollup, CONFIG["required_checks"], CONFIG["repository"], {1})

    def test_historical_cancelled_run_does_not_mask_current_success(self):
        checks = self.good_rollup()
        old = copy.deepcopy(checks[0])
        old["status"] = "COMPLETED"
        old["conclusion"] = "CANCELLED"
        old["detailsUrl"] = f"https://github.com/{CONFIG['repository']}/actions/runs/10/job/1"
        checks.append(old)
        ai.validate_rollup(checks, CONFIG["required_checks"], CONFIG["repository"], {1})

    def test_wrong_run_success_cannot_supply_a_missing_current_check(self):
        checks = self.good_rollup()
        checks[0]["detailsUrl"] = f"https://github.com/{CONFIG['repository']}/actions/runs/10/job/1"
        with self.assertRaises(ai.WorkflowError):
            ai.validate_rollup(checks, CONFIG["required_checks"], CONFIG["repository"], {1})

    def test_no_successful_selected_workflow_blocks_rollup(self):
        with self.assertRaises(ai.WorkflowError):
            ai.validate_rollup(
                self.good_rollup(), CONFIG["required_checks"], CONFIG["repository"], set()
            )

    def test_current_cancelled_run_is_still_rejected(self):
        checks = self.good_rollup()
        checks[0]["conclusion"] = "CANCELLED"
        with self.assertRaises(ai.WorkflowError):
            ai.validate_rollup(checks, CONFIG["required_checks"], CONFIG["repository"], {1})

    def test_latest_current_pr_workflow_must_succeed(self):
        runs = [
            {
                "path": path,
                "head_sha": HEAD,
                "pull_requests": [{"number": 2}],
                "created_at": "2026-10-30T00:00:00Z",
                "id": i,
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": CONFIG["repository"]},
            }
            for i, path in enumerate(CONFIG["required_workflows"], 1)
        ]
        with patch.object(ai, "gh_json", return_value={"workflow_runs": runs}):
            self.assertEqual(ai.validate_workflow_runs(CONFIG, 2, HEAD), {1, 2})
        newer = copy.deepcopy(runs[0])
        newer.update(id=99, created_at="2026-10-30T00:01:00Z", conclusion="failure")
        with (
            patch.object(ai, "gh_json", return_value={"workflow_runs": runs + [newer]}),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_workflow_runs(CONFIG, 2, HEAD)

    def test_newer_pending_workflow_is_not_hidden_by_older_success(self):
        runs = []
        for index, path in enumerate(CONFIG["required_workflows"], 1):
            run = {
                "path": path,
                "head_sha": HEAD,
                "pull_requests": [{"number": 2}],
                "created_at": "2026-10-30T00:00:00Z",
                "id": index,
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": CONFIG["repository"]},
            }
            runs.append(run)
        pending = copy.deepcopy(runs[0])
        pending.update(
            id=99, created_at="2026-10-30T00:01:00Z", status="in_progress", conclusion=None
        )
        with (
            patch.object(ai, "gh_json", return_value={"workflow_runs": runs + [pending]}),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_workflow_runs(CONFIG, 2, HEAD)

    def test_other_pr_or_other_head_runs_do_not_count(self):
        runs = [
            {
                "path": path,
                "head_sha": "b" * 40,
                "pull_requests": [{"number": 3}],
                "status": "completed",
                "conclusion": "success",
            }
            for path in CONFIG["required_workflows"]
        ]
        with (
            patch.object(ai, "gh_json", return_value={"workflow_runs": runs}),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_workflow_runs(CONFIG, 2, HEAD)

    def test_all_workflow_pages_are_checked_and_truncation_fails_closed(self):
        first = {"workflow_runs": [{"path": "unrelated"}] * 100, "total_count": 102}
        second = {
            "workflow_runs": [
                {
                    "path": path,
                    "head_sha": HEAD,
                    "pull_requests": [{"number": 2}],
                    "id": index,
                    "status": "completed",
                    "conclusion": "success",
                    "repository": {"full_name": CONFIG["repository"]},
                }
                for index, path in enumerate(CONFIG["required_workflows"], 1)
            ],
            "total_count": 102,
        }
        with patch.object(ai, "gh_json", side_effect=[first, second]) as api:
            self.assertEqual(ai.validate_workflow_runs(CONFIG, 2, HEAD), {1, 2})
        self.assertIn("page=2", api.call_args.args[0])
        with patch.object(ai, "gh_json", return_value={"workflow_runs": [], "total_count": 1001}):
            with self.assertRaises(ai.WorkflowError):
                ai.validate_workflow_runs(CONFIG, 2, HEAD)

    def test_older_run_rerun_started_after_newer_success_is_not_ignored(self):
        runs = [
            {
                "path": path,
                "head_sha": HEAD,
                "pull_requests": [{"number": 2}],
                "id": index,
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "2026-11-01T00:01:00Z",
                "repository": {"full_name": CONFIG["repository"]},
            }
            for index, path in enumerate(CONFIG["required_workflows"], 10)
        ]
        rerun = copy.deepcopy(runs[0])
        rerun.update(id=1, run_started_at="2026-11-01T00:02:00Z", status="in_progress")
        with patch.object(ai, "gh_json", return_value={"workflow_runs": [*runs, rerun]}):
            with self.assertRaises(ai.WorkflowError):
                ai.validate_workflow_runs(CONFIG, 2, HEAD)

    def test_subprocess_failure_does_not_echo_sensitive_output(self):
        result = subprocess.CompletedProcess(
            ["git"], 1, "secret fixture stdout", "secret fixture stderr"
        )
        with patch.object(ai.subprocess, "run", return_value=result):
            with self.assertRaises(ai.WorkflowError) as error:
                ai.command(["git", "fetch"])
        self.assertNotIn("secret fixture", str(error.exception))

    def good_pr(self):
        return {
            "number": 2,
            "state": "open",
            "draft": False,
            "title": "chore(git): shared AI workflow",
            "body": (
                "Closes #1\n\n## 변경 내용\n공유 규칙\n\n## 검증\n실제 테스트 기록\n"
                "\n## AI 검토\nReviewer: independent-fixture-agent\nHead-SHA: "
                + HEAD
                + "\n\n## 남은 사항\n실기기 검증 범위 밖\n"
            ),
            "head": {
                "sha": HEAD,
                "ref": "codex/1-feature",
                "repo": {"full_name": CONFIG["repository"]},
            },
            "base": {"ref": "main", "repo": {"full_name": CONFIG["repository"]}},
            "mergeable": True,
            "mergeable_state": "clean",
        }

    def test_merge_plan_never_calls_a_mutation(self):
        pr = self.good_pr()
        stub = types.SimpleNamespace(validate_subject=lambda *_: [], validate_pr=lambda *_: [])
        with (
            patch.dict("sys.modules", {"check_git_policy": stub}),
            patch.object(ai, "verify_origin"),
            patch.object(ai, "git"),
            patch.object(ai, "command", return_value=subprocess.CompletedProcess([], 0, "", "")),
            patch.object(ai, "gh_json", return_value=pr) as api,
            patch.object(ai, "pr_check_rollup", return_value=self.good_rollup()),
            patch.object(ai, "validate_workflow_runs", return_value={1}),
            patch.object(ai, "validate_review_state"),
            patch.object(ai, "validator"),
        ):
            ai.merge(CONFIG, 2, False)
        self.assertTrue(all(not call.kwargs.get("method") for call in api.call_args_list))

    def test_execute_refuses_head_changed_after_validation(self):
        pr = self.good_pr()
        changed = copy.deepcopy(pr)
        changed["head"]["sha"] = "b" * 40
        stub = types.SimpleNamespace(validate_subject=lambda *_: [], validate_pr=lambda *_: [])
        with (
            patch.dict("sys.modules", {"check_git_policy": stub}),
            patch.object(ai, "verify_origin"),
            patch.object(ai, "git"),
            patch.object(ai, "command", return_value=subprocess.CompletedProcess([], 0, "", "")),
            patch.object(ai, "gh_json", side_effect=[pr, changed]) as api,
            patch.object(ai, "pr_check_rollup", return_value=self.good_rollup()),
            patch.object(ai, "validate_workflow_runs", return_value={1}),
            patch.object(ai, "validate_review_state"),
            patch.object(ai, "validator"),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.merge(CONFIG, 2, True)
        self.assertTrue(all(not call.kwargs.get("method") for call in api.call_args_list))

    def test_ready_metadata_refuses_stale_review_and_draft(self):
        stub = types.SimpleNamespace(validate_subject=lambda *_: [])
        with patch.dict("sys.modules", {"check_git_policy": stub}):
            for mutation in ["draft", "stale-review", "wrong-repo", "missing-review"]:
                pr = self.good_pr()
                if mutation == "draft":
                    pr["draft"] = True
                elif mutation == "stale-review":
                    pr["body"] = pr["body"].replace(HEAD, "b" * 40)
                elif mutation == "wrong-repo":
                    pr["head"]["repo"]["full_name"] = "other/repo"
                else:
                    pr["body"] = pr["body"].replace("Reviewer:", "Unknown:")
                with self.subTest(mutation=mutation), self.assertRaises(ai.WorkflowError):
                    ai.validate_pr(CONFIG, pr)


class ConversationGates(unittest.TestCase):
    def thread_page(self, resolved=True, *, more=False, cursor=None, head=HEAD):
        return subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": head,
                                "reviewThreads": {
                                    "nodes": [{"isResolved": resolved}],
                                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                                },
                            }
                        }
                    },
                }
            ),
            "",
        )

    def review(self, state, identifier=1):
        return {"id": identifier, "state": state, "user": {"login": "fixture-reviewer"}}

    def test_resolved_threads_and_no_reviews_are_valid(self):
        with (
            patch.object(ai, "command", return_value=self.thread_page()),
            patch.object(ai, "gh_json", return_value=[]),
        ):
            ai.validate_review_state(CONFIG, 2, HEAD)

    def test_unresolved_later_thread_page_blocks_merge(self):
        pages = [self.thread_page(more=True, cursor="next-fixture"), self.thread_page(False)]
        with (
            patch.object(ai, "command", side_effect=pages) as query,
            patch.object(ai, "gh_json") as rest,
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_review_state(CONFIG, 2, HEAD)
        self.assertEqual(query.call_count, 2)
        rest.assert_not_called()
        self.assertIn("cursor=next-fixture", query.call_args.args[0])

    def test_requested_changes_not_cleared_by_later_comment(self):
        reviews = [self.review("CHANGES_REQUESTED"), self.review("COMMENTED", 2)]
        with (
            patch.object(ai, "command", return_value=self.thread_page()),
            patch.object(ai, "gh_json", return_value=reviews),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_review_state(CONFIG, 2, HEAD)

    def test_later_approval_or_dismissal_clears_requested_changes(self):
        for state in ["APPROVED", "DISMISSED"]:
            reviews = [self.review("CHANGES_REQUESTED"), self.review(state, 2)]
            with (
                self.subTest(state=state),
                patch.object(ai, "command", return_value=self.thread_page()),
                patch.object(ai, "gh_json", return_value=reviews),
            ):
                ai.validate_review_state(CONFIG, 2, HEAD)

    def test_pending_review_blocks_merge(self):
        with (
            patch.object(ai, "command", return_value=self.thread_page()),
            patch.object(ai, "gh_json", return_value=[self.review("PENDING")]),
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_review_state(CONFIG, 2, HEAD)

    def test_requested_changes_on_later_review_page_are_checked(self):
        reviews = [
            [self.review("COMMENTED", identifier) for identifier in range(1, 101)],
            [self.review("CHANGES_REQUESTED", 101)],
        ]
        with (
            patch.object(ai, "command", return_value=self.thread_page()),
            patch.object(ai, "gh_json", side_effect=reviews) as rest,
            self.assertRaises(ai.WorkflowError),
        ):
            ai.validate_review_state(CONFIG, 2, HEAD)
        self.assertEqual(rest.call_count, 2)
        self.assertTrue(rest.call_args.args[0].endswith("page=2"))

    def test_unavailable_api_and_head_change_fail_closed(self):
        for response in [
            self.thread_page(head="b" * 40),
            subprocess.CompletedProcess([], 0, '{"errors":[{}]}', ""),
        ]:
            with (
                self.subTest(response=response.stdout),
                patch.object(ai, "command", return_value=response),
                self.assertRaises(ai.WorkflowError),
            ):
                ai.validate_review_state(CONFIG, 2, HEAD)

    def test_changed_title_does_not_pass_final_metadata_comparison(self):
        pr = MergeGates().good_pr()
        changed = copy.deepcopy(pr)
        changed["title"] = "chore(git): changed during review"
        stub = types.SimpleNamespace(validate_subject=lambda *_: [])
        with patch.dict("sys.modules", {"check_git_policy": stub}):
            self.assertFalse(ai.same_reviewed_pr(CONFIG, changed, pr))


class RemoteNormalization(unittest.TestCase):
    def test_supported_github_urls(self):
        repo = CONFIG["repository"]
        for value in [
            f"https://github.com/{repo}.git",
            f"git@github.com:{repo}.git",
            f"ssh://git@github.com/{repo}.git",
        ]:
            self.assertEqual(ai.normalized_remote(value), repo)

    def test_wrong_hosts_and_embedded_credentials_rejected(self):
        for value in [
            "https://user:secret@github.com/owner/repo",
            "https://github.com.evil/a/b",
            "https://github.com/a/b?token=secret",
            "ssh://user@github.com/a/b",
            "https://github.com/a/b/extra",
            "file:///tmp/other",
        ]:
            self.assertIsNone(ai.normalized_remote(value))


if __name__ == "__main__":
    unittest.main()

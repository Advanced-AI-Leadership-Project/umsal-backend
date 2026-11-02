"""Failure-oriented tests for the trusted automatic merge boundary."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location("merge_controller", TOOLS / "merge_controller.py")
mc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mc)
sys.path.pop(0)
CONFIG = json.loads((TOOLS.parent / ".github/ai-workflow.json").read_text())
HARNESS = json.loads((TOOLS.parent / ".github/harness.json").read_text())
HEAD, BASE, MERGED = "a" * 40, "b" * 40, "c" * 40


def pull_request():
    return {
        "number": 81,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "title": "feat(test): synthetic feature",
        "labels": [],
        "user": {"login": "synthetic-developer"},
        "base": {"ref": "main", "repo": {"full_name": CONFIG["repository"]}},
        "head": {
            "sha": HEAD,
            "ref": "codex/80-feature",
            "repo": {"full_name": CONFIG["repository"]},
        },
        "body": (
            "Closes #80\n\n## 변경 내용\nSynthetic change.\n\n## 검증\nSynthetic tests.\n\n"
            f"## AI 검토\nReviewer: independent-test-reviewer\nHead-SHA: {HEAD}\n"
            "결과: no unresolved findings.\n\n## 남은 사항\nNone.\n"
        ),
    }


def workflow_run(run_id=10, *, title=None):
    return {
        "id": run_id,
        "run_attempt": 1,
        "path": HARNESS["ci_workflow"],
        "display_title": title or mc.integration_title(81, HEAD, BASE),
        "head_sha": BASE,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "repository": {"full_name": CONFIG["repository"]},
        "head_repository": {"full_name": CONFIG["repository"]},
        "status": "completed",
        "conclusion": "success",
    }


def jobs():
    return [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in HARNESS["required_jobs"]
    ]


class EvidenceTests(unittest.TestCase):
    def select(self, runs):
        return mc.latest_run(
            runs,
            title=mc.integration_title(81, HEAD, BASE),
            repository=CONFIG["repository"],
            path=HARNESS["ci_workflow"],
            base=BASE,
        )

    def test_a_previous_success_cannot_hide_new_pending_or_failed_attempt(self):
        old, new = workflow_run(10), workflow_run(11)
        api = Mock()
        for status, conclusion in [("in_progress", None), ("completed", "failure")]:
            new.update(status=status, conclusion=conclusion)
            selected = self.select([new, old])
            self.assertEqual(selected["id"], 11)
            self.assertNotEqual(mc.run_result(api, selected, HARNESS["required_jobs"]), "success")

    def test_wrong_provenance_cannot_supply_a_successful_integration(self):
        for field, value in [
            ("event", "pull_request"),
            ("event", "push"),
            ("head_sha", HEAD),
            ("head_branch", "codex/80-feature"),
            ("path", ".github/workflows/fake.yml"),
            ("repository", {"full_name": "other/repo"}),
            ("head_repository", {"full_name": "fork/repo"}),
            ("display_title", mc.integration_title(82, HEAD, BASE)),
            ("display_title", mc.integration_title(81, "d" * 40, BASE)),
            ("display_title", mc.integration_title(81, HEAD, "d" * 40)),
        ]:
            bad = workflow_run()
            bad[field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.select([bad]))

    def test_rerun_of_an_older_id_is_current_evidence_when_it_started_last(self):
        older, newer = workflow_run(10), workflow_run(11)
        newer["run_started_at"] = "2026-11-01T00:01:00Z"
        older.update(run_started_at="2026-11-01T00:02:00Z", run_attempt=2, status="in_progress")
        selected = self.select([older, newer])
        self.assertEqual(selected["id"], 10)
        self.assertEqual(mc.run_result(Mock(), selected, HARNESS["required_jobs"]), "pending")

    def test_all_jobs_must_be_present_unique_and_successful_on_current_attempt(self):
        api = Mock()
        for mutation in ["missing", "duplicate", "skipped", "neutral", "failure", "cancelled"]:
            evidence = jobs()
            if mutation == "missing":
                evidence.pop()
            elif mutation == "duplicate":
                evidence.append(copy.deepcopy(evidence[0]))
            else:
                evidence[0]["conclusion"] = mutation
            api.pages.return_value = evidence
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    mc.run_result(api, workflow_run(), HARNESS["required_jobs"]), "failed"
                )
        api.pages.return_value = jobs()
        self.assertEqual(mc.run_result(api, workflow_run(), HARNESS["required_jobs"]), "success")
        self.assertIn("/attempts/1/jobs", api.pages.call_args.args[0])

    def test_no_run_and_cancelled_run_are_not_success(self):
        self.assertEqual(mc.run_result(Mock(), None, []), "missing")
        run = workflow_run()
        run["conclusion"] = "cancelled"
        self.assertEqual(mc.run_result(Mock(), run, []), "failed")

    def test_invalid_attempt_is_not_accepted(self):
        run = workflow_run()
        del run["run_attempt"]
        with self.assertRaises(mc.GateError):
            mc.run_result(Mock(), run, HARNESS["required_jobs"])


class MetadataTests(unittest.TestCase):
    def test_truncated_files_and_renamed_guards_cannot_hide_review_scope(self):
        with self.assertRaises(mc.GateError):
            mc.changed_paths([{"filename": "ordinary.txt"}], 3001)
        files = [{"filename": "ordinary.txt", "previous_filename": "tools/merge_controller.py"}]
        with self.assertRaises(mc.GateError):
            mc.metadata_gate(CONFIG, pull_request(), mc.changed_paths(files, 1), HARNESS)

    def test_ready_reviewed_internal_pr_is_accepted(self):
        mc.metadata_gate(CONFIG, pull_request(), ["src/umsal_backend/app.py"], HARNESS)

    def test_draft_fork_stale_review_and_open_checklist_are_blocked(self):
        fixtures = []
        pr = pull_request()
        pr["draft"] = True
        fixtures.append(pr)
        pr = pull_request()
        pr["head"]["repo"]["full_name"] = "other/repo"
        fixtures.append(pr)
        pr = pull_request()
        pr["body"] = pr["body"].replace(HEAD, "d" * 40)
        fixtures.append(pr)
        pr = pull_request()
        pr["body"] += "\n- [ ] unfinished\n"
        fixtures.append(pr)
        pr = pull_request()
        pr["body"] += "\n## 검증\nduplicated\n"
        fixtures.append(pr)
        for pr in fixtures:
            with self.subTest(pr=pr), self.assertRaises(mc.GateError):
                mc.metadata_gate(CONFIG, pr, [], HARNESS)

    def test_hold_label_blocks_without_human_approval_requirement(self):
        pr = pull_request()
        pr["labels"] = [{"name": "harness:hold"}]
        with self.assertRaises(mc.GateError):
            mc.metadata_gate(CONFIG, pr, [], HARNESS)

    def test_ci_and_guard_changes_need_explicit_independent_review_scope(self):
        for path in [
            ".github/workflows/ci.yml",
            "tools/merge_controller.py",
            "Makefile",
            "uv.lock",
        ]:
            pr = pull_request()
            with self.subTest(path=path), self.assertRaises(mc.GateError):
                mc.metadata_gate(CONFIG, pr, [path], HARNESS)
            pr["body"] = pr["body"].replace("결과:", "Review-Scope: harness\n결과:")
            mc.metadata_gate(CONFIG, pr, [path], HARNESS)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.pr = pull_request()
        self.run = workflow_run()
        self.api = Mock()
        self.api.pages.return_value = jobs()

        def repo(path, **kwargs):
            if path == "pulls/81/merge":
                return {"merged": True, "sha": MERGED}
            if path == "pulls/81":
                value = copy.deepcopy(self.pr)
                value["merged"] = True
                return value
            raise AssertionError(f"Unexpected API path {path}")

        self.api.repo.side_effect = repo
        self.controller = mc.Controller(CONFIG, HARNESS, self.api, execute=True)
        self.controller.pr_evidence = Mock()
        self.controller.feedback = Mock()
        self.controller.dispatch = Mock()
        self.controller.base_sha = Mock(return_value=BASE)
        self.main_run = workflow_run(9, title=f"verify main={BASE}")
        self.controller.runs = Mock(return_value=[self.run, self.main_run])

    def process(self, runs=None, main_ok=True):
        return self.controller.process(
            self.pr, BASE, [self.run] if runs is None else runs, main_ok=main_ok
        )

    def assert_not_merged(self):
        self.assertFalse(
            any(call.args[0].endswith("/merge") for call in self.api.repo.call_args_list)
        )

    def test_a_successful_pr_is_pinned_to_head_and_post_merge_ci_is_explicit(self):
        self.assertTrue(self.process())
        merge_calls = [
            call for call in self.api.repo.call_args_list if call.args[0] == "pulls/81/merge"
        ]
        self.assertEqual(len(merge_calls), 1)
        self.assertEqual(merge_calls[0].kwargs["data"]["sha"], HEAD)
        self.assertEqual(merge_calls[0].kwargs["data"]["merge_method"], "squash")
        self.controller.dispatch.assert_called_once_with("main", MERGED)

    def test_missing_run_requests_exact_integration_without_merge(self):
        self.assertFalse(self.process(runs=[]))
        self.controller.dispatch.assert_called_once_with("integration", BASE, self.pr)
        self.assert_not_merged()

    def test_failed_and_pending_integration_are_blocked_without_retry_loop(self):
        for state in ["failure", "cancelled", "skipped", None]:
            self.run["conclusion"] = state
            self.assertFalse(self.process())
            self.assert_not_merged()
            self.controller.dispatch.assert_not_called()

    def test_unhealthy_main_blocks_an_otherwise_good_pr(self):
        self.assertFalse(self.process(main_ok=False))
        self.assert_not_merged()

    def test_main_advancing_during_verification_blocks_merge(self):
        self.controller.base_sha.return_value = "d" * 40
        self.assertFalse(self.process())
        self.assert_not_merged()

    def test_head_body_draft_or_title_changing_blocks_merge(self):
        for key, value in [("body", "changed"), ("draft", True), ("title", "fix: changed")]:
            changed = copy.deepcopy(self.pr)
            changed[key] = value
            self.api.repo.side_effect = None
            self.api.repo.return_value = changed
            self.assertFalse(self.process())
            self.assert_not_merged()
        changed = copy.deepcopy(self.pr)
        changed["head"]["sha"] = "d" * 40
        self.api.repo.return_value = changed
        self.assertFalse(self.process())
        self.assert_not_merged()

    def test_new_pending_run_between_checks_blocks_merge(self):
        new = workflow_run(11)
        new["status"] = "in_progress"
        self.controller.runs.return_value = [self.run, new]
        self.assertFalse(self.process())
        self.assert_not_merged()

    def test_main_rerun_becoming_pending_or_failed_blocks_merge(self):
        for status, conclusion in [("in_progress", None), ("completed", "failure")]:
            self.main_run.update(status=status, conclusion=conclusion)
            self.assertFalse(self.process())
            self.assert_not_merged()

    def test_read_only_plan_never_merges(self):
        self.controller.execute = False
        self.assertFalse(self.process())
        self.assert_not_merged()

    def test_metadata_failure_and_conflict_never_dispatch_or_merge(self):
        self.controller.pr_evidence.side_effect = mc.GateError("stale review")
        self.assertFalse(self.process())
        self.assert_not_merged()
        self.controller.dispatch.assert_not_called()
        self.controller.pr_evidence.side_effect = None
        self.pr["mergeable"] = False
        self.assertFalse(self.process())
        self.assert_not_merged()

    def test_draft_pr_is_left_untouched(self):
        self.pr["draft"] = True
        self.assertFalse(self.process())
        self.assert_not_merged()
        self.controller.pr_evidence.assert_not_called()


class GitHubBoundaryTests(unittest.TestCase):
    def test_completed_notification_needs_no_wait_and_is_not_a_success_verdict(self):
        api = Mock(repository=CONFIG["repository"])
        run = workflow_run()
        run["conclusion"] = "failure"
        api.repo.return_value = run
        with patch.object(mc.time, "sleep") as sleep:
            mc.wait_for_notification_run(api, "10", HARNESS["ci_workflow"])
        sleep.assert_not_called()

    def test_notification_waits_for_actual_end_without_rerunning_ci(self):
        api = Mock(repository=CONFIG["repository"])
        pending = workflow_run()
        pending["status"] = "in_progress"
        api.repo.side_effect = [pending, workflow_run()]
        with patch.object(mc.time, "sleep") as sleep:
            mc.wait_for_notification_run(api, "10", HARNESS["ci_workflow"])
        sleep.assert_called_once_with(2)
        self.assertTrue(all(not call.kwargs for call in api.repo.call_args_list))

    def test_invalid_notification_id_is_rejected_before_any_api_call(self):
        api = Mock(repository=CONFIG["repository"])
        with self.assertRaises(mc.GateError):
            mc.wait_for_notification_run(api, "../unsafe", HARNESS["ci_workflow"])
        api.repo.assert_not_called()

    def test_foreign_or_candidate_notification_cannot_supply_completion(self):
        for field, value in [
            ("repository", {"full_name": "other/repo"}),
            ("path", ".github/workflows/fake.yml"),
            ("event", "pull_request"),
            ("head_branch", "codex/1-feature"),
        ]:
            api = Mock(repository=CONFIG["repository"])
            run = workflow_run()
            run[field] = value
            api.repo.return_value = run
            with self.subTest(field=field), self.assertRaises(mc.GateError):
                mc.wait_for_notification_run(api, "10", HARNESS["ci_workflow"])

    def test_unfinished_notification_times_out_without_issuing_any_write(self):
        api = Mock(repository=CONFIG["repository"])
        run = workflow_run()
        run["status"] = "in_progress"
        api.repo.return_value = run
        with patch.object(mc.time, "sleep"), self.assertRaises(mc.GateError):
            mc.wait_for_notification_run(api, "10", HARNESS["ci_workflow"])
        self.assertEqual(api.repo.call_count, 30)
        self.assertTrue(all(not call.kwargs for call in api.repo.call_args_list))

    def test_repository_scope_cannot_be_redirected(self):
        api = mc.GitHub(CONFIG["repository"])
        with patch.object(mc.subprocess, "run") as command, self.assertRaises(mc.GateError):
            api.api("repos/unrelated/private/issues")
        command.assert_not_called()

    def test_request_and_response_bodies_use_utf8_regardless_of_os_locale(self):
        api = mc.GitHub(CONFIG["repository"])
        with patch.object(mc.subprocess, "run") as run:
            run.return_value = Mock(returncode=0, stdout='{"body": "한글"}'.encode())
            response = api.repo("issues/1", method="PATCH", data={"body": "한글"})
        self.assertEqual(response, {"body": "한글"})
        sent = run.call_args.kwargs["input"]
        self.assertEqual(json.loads(sent.decode("utf-8")), {"body": "한글"})

    def test_non_utf8_github_response_fails_closed(self):
        api = mc.GitHub(CONFIG["repository"])
        with patch.object(mc.subprocess, "run") as run:
            run.return_value = Mock(returncode=0, stdout=b"\xff")
            with self.assertRaises(mc.GateError):
                api.repo("pulls/1")

    def test_pagination_collects_all_pages(self):
        api = mc.GitHub(CONFIG["repository"])
        with patch.object(api, "repo", side_effect=[[{}] * 100, [{"id": 101}]]) as fetch:
            self.assertEqual(len(api.pages("pulls?state=open")), 101)
        self.assertIn("&per_page=100&page=2", fetch.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

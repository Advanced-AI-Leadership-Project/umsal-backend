"""Regression tests for policy enforcement, including unpublished secret history."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "check_git_policy.py"
SPEC = importlib.util.spec_from_file_location("git_policy", MODULE)
assert SPEC and SPEC.loader
policy_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy_check)

POLICY = {
    "schema_version": 1,
    "repository": "Advanced-AI-Leadership-Project/umsal-backend",
    "base_branch": "main",
    "branch_pattern": r"^codex/[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*$",
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
}
HEAD = "a" * 40


def event(*, draft: bool = False) -> dict:
    return {
        "repository": {"full_name": POLICY["repository"]},
        "pull_request": {
            "head": {"ref": "codex/12-shared-flow", "sha": HEAD},
            "base": {"ref": "main", "sha": "b" * 40},
            "title": "chore(git): 공통 버전 관리 환경 추가",
            "draft": draft,
            "body": (
                "Closes #12\n\n## 변경 내용\n공통 Git 규칙을 적용한다.\n\n"
                "## 검증\n정책 단위 검사를 통과했다.\n\n"
                "## AI 검토\nReviewer: independent-agent-2\nHead-SHA: "
                + HEAD
                + "\n\n## 남은 사항\n실제 기기 검증은 이번 변경에 해당하지 않는다.\n"
            ),
        },
    }


class MetadataTests(unittest.TestCase):
    def test_korean_subject_and_merge_subject_are_allowed(self):
        for subject in (
            "feat(web): 가족 연결 화면 추가",
            "chore(sync): main 변경 반영",
            "fix(api)!: revoke existing grants",
            "docs: 새 에이전트 온보딩",
        ):
            with self.subTest(subject=subject):
                self.assertEqual(policy_check.validate_subject(subject, POLICY), [])

    def test_invalid_type_empty_description_and_overlong_subject(self):
        for subject in ("update: work", "feat: ", "feat: " + "가" * 100, "feat: x\nfix: y"):
            with self.subTest(subject=subject):
                self.assertTrue(policy_check.validate_subject(subject, POLICY))

    def test_branch_names_exclude_main_and_unscoped_branches(self):
        self.assertEqual(policy_check.validate_branch("codex/12-shared-flow", POLICY), [])
        for branch in (
            "main",
            "codex/new-flow",
            "codex/0-feature",
            "codex/12-Shell",
            "codex/12-foo/bar",
        ):
            with self.subTest(branch=branch):
                self.assertTrue(policy_check.validate_branch(branch, POLICY))

    def test_ready_pr_requires_matching_issue_and_full_review_head(self):
        self.assertEqual(policy_check.validate_pr(event(), POLICY), [])
        value = event()
        value["pull_request"]["body"] = value["pull_request"]["body"].replace(
            "Closes #12", "Closes #13"
        )
        self.assertTrue(policy_check.validate_pr(value, POLICY))
        value = event()
        value["pull_request"]["body"] = value["pull_request"]["body"].replace(HEAD, "c" * 40)
        self.assertTrue(policy_check.validate_pr(value, POLICY))

    def test_review_markers_must_be_in_review_section(self):
        value = event()
        value["pull_request"]["body"] = value["pull_request"]["body"].replace(
            "## AI 검토\nReviewer:", "## AI 검토\n대기\n\n## 다른 항목\nReviewer:"
        )
        self.assertTrue(policy_check.validate_pr(value, POLICY))

    def test_draft_can_wait_for_review_but_still_requires_sections(self):
        value = event(draft=True)
        value["pull_request"]["body"] = value["pull_request"]["body"].replace(
            "Reviewer: independent-agent-2\nHead-SHA: " + HEAD,
            "검토 대기\n- [ ] 별도 에이전트 검토",
        )
        self.assertEqual(policy_check.validate_pr(value, POLICY), [])
        value["pull_request"]["draft"] = False
        self.assertTrue(policy_check.validate_pr(value, POLICY))

    def test_ready_pr_rejects_empty_duplicate_or_missing_sections(self):
        for body in ("Closes #12", event()["pull_request"]["body"] + "\n## 검증\n중복"):
            value = event()
            value["pull_request"]["body"] = body
            try:
                errors = policy_check.validate_pr(value, POLICY)
            except policy_check.PolicyError:
                continue
            self.assertTrue(errors)

    def test_wrong_repository_and_base_fail(self):
        value = event()
        value["repository"]["full_name"] = "other/repository"
        value["pull_request"]["base"]["ref"] = "release"
        self.assertEqual(len(policy_check.validate_pr(value, POLICY)), 2)

    def test_malicious_metadata_is_data_and_not_executed(self):
        value = event()
        value["pull_request"]["title"] = "chore: $(touch /tmp/never-execute-policy-metadata)"
        value["pull_request"]["body"] += "\n$(echo data)\n`echo more data`\n"
        self.assertEqual(policy_check.validate_pr(value, POLICY), [])

    def test_missing_pr_payload_is_a_controlled_error(self):
        with self.assertRaises(policy_check.PolicyError):
            policy_check.validate_pr({"repository": {"full_name": POLICY["repository"]}}, POLICY)


class ContentTests(unittest.TestCase):
    def test_sources_and_local_template_are_allowed(self):
        for path in (
            ".env.example",
            "services/api/src/uploads.py",
            "docs/design/설계.png",
            "uv.lock",
        ):
            with self.subTest(path=path):
                self.assertEqual(policy_check.path_violations(path), [])

    def test_raw_data_generated_and_credentials_are_rejected(self):
        for path in (
            ".env",
            "apps/web/.env.local",
            "node_modules/pkg/index.js",
            ".venv/bin/python",
            "apps/web/dist/index.html",
            "data/transcript.txt",
            "weights/model.bin",
            ".aws/config",
            "tests/private.pem",
            "fixture.wav",
            "models.bin.safetensors",
            "artifact.zip",
        ):
            with self.subTest(path=path):
                self.assertTrue(policy_check.path_violations(path))

    def test_secret_families_are_detected_without_exposing_values(self):
        samples = (
            b"AKIA" + b"A" * 16,
            b"ghp_" + b"a" * 36,
            b"github_pat_" + b"a" * 40,
            b"sk-proj-" + b"a" * 36,
            b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
        )
        for sample in samples:
            with self.subTest(family=sample[:4]):
                errors = policy_check.blob_violations(sample)
                self.assertTrue(errors)
                self.assertNotIn(sample.decode(), repr(errors))

    def test_demo_credentials_and_regex_source_are_not_secrets(self):
        self.assertEqual(policy_check.blob_violations(b"POSTGRES_PASSWORD=umsal_local"), [])
        self.assertEqual(policy_check.blob_violations(MODULE.read_bytes()), [])

    def test_unusual_paths_are_escaped_for_logs(self):
        quoted = policy_check.quoted_path("strange\n::error::message\x1b.txt")
        self.assertNotIn("\n", quoted)
        self.assertNotIn("\x1b", quoted)


class GitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="umsal-policy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Policy Test")
        self.run_git("config", "user.email", "policy@example.invalid")
        (self.root / "README.md").write_text("baseline\n", encoding="utf-8")
        self.run_git("add", "README.md")
        self.run_git("commit", "-m", "docs: initial source")
        self.base = self.run_git("rev-parse", "HEAD").strip()
        self.run_git("switch", "-c", "codex/12-shared-flow")

    def run_git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )
        return result.stdout

    def commit(self, filename: str, contents: bytes, message: str):
        target = self.root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        self.run_git("add", "--", filename)
        self.run_git("commit", "-m", message)

    def test_staged_scan_reads_index_not_unstaged_working_copy(self):
        secret = b"ghp_" + b"a" * 36
        (self.root / "config.txt").write_bytes(secret)
        self.run_git("add", "config.txt")
        (self.root / "config.txt").write_text("safe working copy\n")
        errors = policy_check.staged_errors(cwd=self.root, policy=POLICY)
        self.assertTrue(any("GitHub token" in error for error in errors))
        self.assertNotIn(secret.decode(), repr(errors))

    def test_history_finds_secret_added_then_deleted_before_pr(self):
        secret = b"AKIA" + b"Z" * 16
        self.commit("config.txt", secret, "chore: add configuration")
        self.run_git("rm", "config.txt")
        self.run_git("commit", "-m", "chore: remove configuration")
        errors = policy_check.range_errors(self.base, "HEAD", cwd=self.root, policy=POLICY)
        self.assertTrue(any("AWS access key" in error for error in errors))
        self.assertNotIn(secret.decode(), repr(errors))

    def test_range_checks_every_commit_subject(self):
        self.commit("first.txt", b"safe", "Update files")
        self.commit("second.txt", b"safe", "docs: add guide")
        errors = policy_check.range_errors(self.base, "HEAD", cwd=self.root, policy=POLICY)
        self.assertTrue(any("Conventional Commit" in error for error in errors))

    def test_safe_range_rename_and_newline_filename(self):
        self.commit("first.txt", b"safe", "docs: add guide")
        self.run_git("mv", "first.txt", "strange\nfile.txt")
        self.run_git("commit", "-m", "docs: rename guide")
        self.assertEqual(
            policy_check.range_errors(self.base, "HEAD", cwd=self.root, policy=POLICY), []
        )

    def test_submodule_like_tree_entry_is_rejected(self):
        self.run_git("update-index", "--add", "--cacheinfo", "160000", self.base, "dependency")
        self.assertTrue(policy_check.staged_errors(cwd=self.root, policy=POLICY))

    def test_cli_does_not_print_secret_when_rejecting_index(self):
        secret = b"ghp_" + b"b" * 36
        (self.root / "config.txt").write_bytes(secret)
        self.run_git("add", "config.txt")
        config = self.root / "policy.json"
        config.write_text(json.dumps(POLICY), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = policy_check.main(
                ["--repo", str(self.root), "--policy", str(config), "--staged"]
            )
        self.assertEqual(code, 1)
        self.assertIn("GitHub token", output.getvalue())
        self.assertNotIn(secret.decode(), output.getvalue())

    def test_merge_from_main_has_a_conventional_subject(self):
        self.commit("feature.txt", b"safe feature", "feat: add feature")
        self.run_git("switch", "main")
        self.commit("main.txt", b"safe main", "docs: update main")
        current_main = self.run_git("rev-parse", "HEAD").strip()
        self.run_git("switch", "codex/12-shared-flow")
        self.run_git("merge", "main", "-m", "chore(sync): main 변경 반영")
        self.assertEqual(
            policy_check.range_errors(current_main, "HEAD", cwd=self.root, policy=POLICY), []
        )


if __name__ == "__main__":
    unittest.main()

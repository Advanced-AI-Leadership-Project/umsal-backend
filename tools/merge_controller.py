"""Trusted, default-branch-only PR integration controller.

Never installs, imports or executes PR code. A separate read-only workflow tests
the exact main+head tree. GITHUB_TOKEN is ephemeral; no personal/API secret needed.
The executable path is serialized by the workflow's repository-wide concurrency.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import ai_workflow as ai
import check_git_policy as policy

ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- umsal-backend-verification-harness -->"


class GateError(Exception):
    """A fail-closed, public-safe gate diagnostic."""


class GitHub:
    def __init__(self, repository: str):
        self.repository = repository

    def api(self, path: str, *, method: str = "GET", data=None):
        if not path.startswith((f"repos/{self.repository}/", "graphql")):
            raise GateError("API destination is outside this repository.")
        args = ["gh", "api", path, "--method", method]
        if data is not None:
            args += ["--input", "-"]
        result = subprocess.run(
            args,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        if result.returncode:
            # GitHub errors can contain PR bodies or credentials. Do not echo them.
            raise GateError("GitHub request failed; no merge was authorized by this response.")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as error:
            raise GateError("GitHub returned an invalid response.") from error

    def repo(self, path: str, *, method: str = "GET", data=None):
        return self.api(f"repos/{self.repository}/{path}", method=method, data=data)

    def pages(self, path: str, *, key: str | None = None) -> list:
        values = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            response = self.repo(f"{path}{separator}per_page=100&page={page}")
            items = response[key] if key else response
            if not isinstance(items, list):
                raise GateError("Cannot validate all GitHub pagination results.")
            values.extend(items)
            if len(items) < 100:
                return values
        raise GateError("Pagination limit reached; refusing incomplete evidence.")


def integration_title(number: int, head: str, base: str) -> str:
    return f"integrate pr={number} head={head} base={base}"


def trusted_run(run: dict, *, repository: str, path: str, base: str) -> bool:
    return (
        run.get("path") == path
        and run.get("head_sha") == base
        and run.get("head_branch") == "main"
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
        and run.get("event") in {"push", "workflow_dispatch"}
    )


def latest_run(runs: list[dict], *, title: str, repository: str, path: str, base: str):
    matches = [
        run
        for run in runs
        if trusted_run(run, repository=repository, path=path, base=base)
        and run.get("display_title") == title
        and (not title.startswith("integrate ") or run.get("event") == "workflow_dispatch")
    ]
    return (
        max(
            matches,
            key=lambda run: (run.get("run_started_at") or run.get("created_at", ""), run["id"]),
        )
        if matches
        else None
    )


def run_result(api: GitHub, run: dict | None, required: list[str]) -> str:
    if run is None:
        return "missing"
    if run.get("status") != "completed":
        return "pending"
    if run.get("conclusion") != "success":
        return "failed"
    attempt = run.get("run_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise GateError("Cannot identify the current workflow attempt.")
    jobs = api.pages(f"actions/runs/{run['id']}/attempts/{attempt}/jobs", key="jobs")
    for name in required:
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            return "failed"
        if matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
            return "failed"
    return "success"


def wait_for_notification_run(api: GitHub, run_id: str, workflow_path: str) -> None:
    """A notifier dispatch may reach us just before its own CI run closes.

    This is a bounded read-only wait, not a verdict or an artifact trust input.
    All current PR/main evidence is independently queried again by reconcile.
    """
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise GateError("Invalid completion run identifier.")
    for attempt in range(30):
        run = api.repo(f"actions/runs/{run_id}")
        if (
            run.get("repository", {}).get("full_name") != api.repository
            or run.get("path") != workflow_path
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main"
        ):
            raise GateError("Completion notification is not from the trusted main CI.")
        if run.get("status") == "completed":
            return
        if attempt < 29:
            time.sleep(2)
    raise GateError("CI completion notification timed out; no merge was attempted.")


def metadata_gate(config: dict, pr: dict, paths: list[str], harness: dict) -> None:
    try:
        ai.validate_pr(config, pr)
        errors = policy.validate_pr(
            {"repository": {"full_name": config["repository"]}, "pull_request": pr},
            config,
        )
    except (ai.WorkflowError, policy.PolicyError) as error:
        raise GateError(str(error)) from error
    if errors:
        raise GateError(" ".join(errors))
    labels = {label["name"] for label in pr.get("labels", [])}
    if harness["hold_label"] in labels:
        raise GateError("harness:hold가 지정되어 자동 병합을 보류합니다.")
    critical = any(
        path == pattern or (pattern.endswith("/") and path.startswith(pattern))
        for path in paths
        for pattern in harness["critical_paths"]
    )
    sections = policy.body_sections(pr.get("body") or "")
    if critical and not re.search(r"(?m)^Review-Scope: harness\s*$", sections["AI 검토"]):
        raise GateError(
            "검증·운영 규칙 변경은 별도 리뷰 후 Review-Scope: harness를 기록해야 합니다."
        )


def changed_paths(files: list, count: int) -> list[str]:
    # The endpoint caps at 3,000 files. Renaming a guard away is also a change.
    if not isinstance(count, int) or len(files) != count:
        raise GateError("변경 파일 목록이 누락·잘렸습니다. PR을 작은 작업으로 나눠야 합니다.")
    return [
        path
        for item in files
        for path in [item["filename"], item.get("previous_filename")]
        if path is not None
    ]


class Controller:
    def __init__(self, config: dict, harness: dict, api: GitHub, *, execute: bool = False):
        self.config = config
        self.harness = harness
        self.api = api
        self.execute = execute
        self.repository = config["repository"]
        self.base_branch = config["base_branch"]
        self.path = harness["ci_workflow"]
        self.jobs = harness["required_jobs"]

    def base_sha(self) -> str:
        value = self.api.repo(f"git/ref/heads/{self.base_branch}")["object"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise GateError("Cannot identify the current main SHA.")
        return value

    def runs(self, base: str) -> list:
        return self.api.pages(
            f"actions/workflows/ci.yml/runs?branch={self.base_branch}&head_sha={base}",
            key="workflow_runs",
        )

    def find_run(self, runs: list, base: str, title: str):
        return latest_run(runs, title=title, repository=self.repository, path=self.path, base=base)

    def dispatch(self, mode: str, base: str, pr: dict | None = None) -> None:
        inputs = {"mode": mode, "base_sha": base, "pr": "", "head_sha": ""}
        if pr:
            inputs.update(pr=str(pr["number"]), head_sha=pr["head"]["sha"])
        if self.execute:
            self.api.repo(
                "actions/workflows/ci.yml/dispatches",
                method="POST",
                data={"ref": self.base_branch, "inputs": inputs},
            )
        print(f"{'Dispatched' if self.execute else 'Would dispatch'} {mode} for {base[:12]}")

    def feedback(self, pr: dict, message: str, run: dict | None = None) -> None:
        number, head = pr["number"], pr["head"]["sha"]
        body = (
            f"{MARKER}\n## 자동 검증 상태\n\n대상 head: `{head}`\n\n{message}\n\n"
            "실패한 검사는 담당 AI가 로그·아티팩트를 읽고 원인을 수정한 뒤 push합니다. "
            "새 head는 다시 검증하고 독립 리뷰의 `Head-SHA`도 갱신합니다. "
            "실패를 숨기기 위한 검사 삭제·기준 완화·무작정 재실행은 허용하지 않습니다.\n\n"
            "[검증·복구 안내](https://github.com/"
            + self.repository
            + "/blob/main/docs/verification-harness.md) · 일시 보류: `harness:hold`"
        )
        if run:
            body += f"\n\n검사: https://github.com/{self.repository}/actions/runs/{run['id']}"
        print(f"PR #{number}: {message}")
        if not self.execute:
            return
        comments = self.api.pages(f"issues/{number}/comments")
        own = [
            c
            for c in comments
            if c.get("user", {}).get("login") == "github-actions[bot]"
            and (c.get("body") or "").startswith(MARKER)
        ]
        if own:
            comment = max(own, key=lambda item: item["id"])
            if comment["body"] != body:
                self.api.repo(
                    f"issues/comments/{comment['id']}", method="PATCH", data={"body": body}
                )
        else:
            self.api.repo(f"issues/{number}/comments", method="POST", data={"body": body})

    def main_failure(self, base: str, run: dict) -> None:
        marker = f"<!-- umsal-backend-main-failure:{base} -->"
        print(f"MAIN REGRESSION: {base}; all automatic merges are blocked.")
        if not self.execute:
            return
        issues = self.api.pages("issues?state=all&creator=github-actions%5Bbot%5D")
        if any(marker in (issue.get("body") or "") for issue in issues):
            return
        self.api.repo(
            "issues",
            method="POST",
            data={
                "title": f"fix(ci): main 검증 실패 {base[:12]}",
                "body": (
                    f"{marker}\nmain `{base}`의 전체 검증이 실패했습니다. "
                    "일반 PR 자동 병합을 중단합니다. "
                    "담당 AI는 실패 원인을 진단하고 수정 PR을 작성합니다. "
                    "이미 실패한 main을 복구할 때는 아래 안내의 복구 절차를 따릅니다.\n\n"
                    f"검사: https://github.com/{self.repository}/actions/runs/{run['id']}\n\n"
                    "[복구 안내](https://github.com/"
                    + self.repository
                    + "/blob/main/docs/verification-harness.md#main-회귀와-복구)"
                ),
                "labels": ["bug", "area:infra", "priority:P0"],
            },
        )

    def pr_evidence(self, pr: dict) -> None:
        """Use only default-branch policy code; inspect candidate Git objects as data."""
        head = pr["head"]["sha"]
        paths = changed_paths(
            self.api.pages(f"pulls/{pr['number']}/files"),
            pr.get("changed_files"),
        )
        metadata_gate(self.config, pr, paths, self.harness)
        permission = self.api.repo(f"collaborators/{pr['user']['login']}/permission")
        if permission.get("permission") not in {"admin", "maintain", "write"}:
            raise GateError("자동 병합은 저장소 Write 이상 팀원의 내부 PR만 지원합니다.")
        number = int(pr["head"]["ref"].split("/", 1)[1].split("-", 1)[0])
        issue = self.api.repo(f"issues/{number}")
        if issue.get("state") != "open" or "pull_request" in issue:
            raise GateError("연결된 작업 이슈가 열린 상태여야 합니다.")
        try:
            # This trusted checker never executes a candidate script or imports it.
            ai.validator("--base", self.base_sha(), "--head", head, "--branch", pr["head"]["ref"])
            runs = ai.validate_workflow_runs(self.config, pr["number"], head)
            ai.validate_rollup(
                ai.pr_check_rollup(self.config, pr["number"]),
                self.config["required_checks"],
                self.repository,
                runs,
            )
            ai.validate_review_state(self.config, pr["number"], head)
        except ai.WorkflowError as error:
            raise GateError(str(error)) from error

    def process(self, pr: dict, base: str, runs: list, *, main_ok: bool) -> bool:
        if pr.get("base", {}).get("ref") != self.base_branch:
            return False
        if pr.get("draft"):
            self.feedback(
                pr,
                "Draft PR은 자동 병합하지 않습니다. CI 로그·보고서의 실패를 수정하고 "
                "전체 성공 및 현재 head의 독립 AI 리뷰를 확인한 뒤 Ready로 바꿉니다.",
            )
            return False
        try:
            self.pr_evidence(pr)
        except GateError as error:
            self.feedback(pr, f"병합 대기: {error}")
            return False
        if pr.get("mergeable") is not True:
            self.feedback(
                pr,
                "충돌이 있거나 GitHub가 병합 가능 여부를 계산 중입니다. "
                "충돌은 담당 AI가 해결합니다.",
            )
            return False
        title = integration_title(pr["number"], pr["head"]["sha"], base)
        integration = self.find_run(runs, base, title)
        state = run_result(self.api, integration, self.jobs)
        if state == "missing":
            self.dispatch("integration", base, pr)
            self.feedback(pr, f"최신 main `{base}`과 이 PR을 합친 전체 검증을 요청했습니다.")
            return False
        if state != "success":
            message = (
                "통합 검증 실행 중입니다."
                if state == "pending"
                else "통합 검증 실패로 병합을 차단했습니다."
            )
            self.feedback(pr, message, integration)
            return False
        if not main_ok:
            self.feedback(
                pr,
                "PR 통합 검증은 통과했습니다. main의 정상 검증 확인까지 병합을 보류합니다.",
                integration,
            )
            return False
        fresh = self.api.repo(f"pulls/{pr['number']}")
        if any(
            fresh.get(key) != pr.get(key) for key in ["title", "body", "head", "draft", "state"]
        ):
            self.feedback(
                fresh, "검증 중 PR 내용 또는 head가 바뀌었습니다. 새 상태로 다시 확인합니다."
            )
            return False
        if self.base_sha() != base:
            self.feedback(
                pr, "다른 PR이 먼저 반영되어 main이 바뀌었습니다. 새 main으로 자동 재검증합니다."
            )
            return False
        # Repeat mutable evidence immediately before the only merge mutation.
        self.pr_evidence(fresh)
        final_runs = self.runs(base)
        current = self.find_run(final_runs, base, title)
        final_main = self.find_run(final_runs, base, f"verify main={base}")
        if (
            run_result(self.api, current, self.jobs) != "success"
            or run_result(self.api, final_main, self.jobs) != "success"
            or self.base_sha() != base
        ):
            self.feedback(pr, "최종 확인에서 검사 또는 main이 변경되어 병합을 보류합니다.")
            return False
        last = self.api.repo(f"pulls/{pr['number']}")
        if (
            any(last.get(key) != fresh.get(key) for key in ["body", "title", "draft", "state"])
            or last.get("head", {}).get("sha") != pr["head"]["sha"]
            or last.get("mergeable") is not True
        ):
            self.feedback(last, "최종 확인 중 PR 상태가 바뀌어 병합을 보류합니다.")
            return False
        metadata_gate(self.config, last, [], self.harness)
        if not self.execute:
            print(f"Would squash PR #{pr['number']} at {pr['head']['sha']} onto {base}.")
            return False
        result = self.api.repo(
            f"pulls/{pr['number']}/merge",
            method="PUT",
            data={
                "sha": pr["head"]["sha"],
                "merge_method": "squash",
                "commit_title": pr["title"],
                "commit_message": pr["body"],
            },
        )
        if not result or result.get("merged") is not True:
            raise GateError("GitHub did not confirm the merge; do not retry blindly.")
        sha = result.get("sha", "")
        merged = self.api.repo(f"pulls/{pr['number']}")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not merged.get("merged"):
            raise GateError("Merge needs reconciliation; preserve all branches and evidence.")
        # GITHUB_TOKEN push events need an explicit dispatch for post-merge CI.
        self.dispatch("main", sha)
        self.feedback(
            pr,
            f"전체 검증과 독립 AI 리뷰를 확인해 `{sha}`로 자동 squash 병합했습니다. "
            "main 재검증을 요청했습니다.",
            integration,
        )
        return True

    def reconcile(self) -> None:
        base = self.base_sha()
        runs = self.runs(base)
        main = self.find_run(runs, base, f"verify main={base}")
        health = run_result(self.api, main, self.jobs)
        if health == "missing":
            self.dispatch("main", base)
        if health == "failed":
            self.main_failure(base, main)
        prs = self.api.pages(f"pulls?state=open&base={self.base_branch}&sort=created&direction=asc")
        for item in prs:
            pr = self.api.repo(f"pulls/{item['number']}")
            # One merge per invocation. The next main verification wakes the queue.
            if self.process(pr, base, runs, main_ok=health == "success"):
                break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Write feedback, dispatch CI and merge."
    )
    args = parser.parse_args(argv)
    try:
        config = ai.policy()
        harness = json.loads((ROOT / ".github/harness.json").read_text())
        ai.verify_origin(config)
        if args.execute:
            if os.environ.get("UMSAL_AUTO_MERGE") != "true":
                print("Automation paused: repository variable UMSAL_AUTO_MERGE is not true.")
                return 0
            if os.environ.get("GITHUB_REF") != "refs/heads/main":
                raise GateError("Write mode must run from the trusted default branch.")
        api = GitHub(config["repository"])
        notified = os.environ.get("HARNESS_COMPLETED_RUN_ID", "")
        if notified:
            wait_for_notification_run(api, notified, harness["ci_workflow"])
        Controller(config, harness, api, execute=args.execute).reconcile()
    except (
        GateError,
        ai.WorkflowError,
        KeyError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        if isinstance(error, GateError | ai.WorkflowError):
            print(f"BLOCKED: {error}", file=sys.stderr)
        else:
            print("BLOCKED: cannot validate controller inputs or GitHub state.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

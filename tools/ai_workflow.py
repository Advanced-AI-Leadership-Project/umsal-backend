"""Shared AI Git workflow guardrails. Python standard library; no stored credentials.

These local checks are bypassable and do not replace GitHub branch protection.
The merge command defaults to a read-only plan; --execute performs the reviewed
squash merge only after rechecking the current PR, CI and base branch.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / ".github" / "ai-workflow.json"
ZERO = "0" * 40


class WorkflowError(Exception):
    """A safe, user-readable error without subprocess output or credentials."""


def command(
    args: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, cwd=cwd or ROOT, text=True, capture_output=True, check=False)
    except OSError as error:
        raise WorkflowError(f"Required executable is unavailable: {args[0]}") from error
    if check and result.returncode:
        # stderr may include URLs, credentials or body text. Never echo it.
        raise WorkflowError(
            f"{args[0]} command failed; inspect it locally without sharing secrets."
        )
    return result


def git(*args: str, check: bool = True) -> str:
    return command(["git", *args], check=check).stdout.strip()


def policy() -> dict:
    try:
        data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkflowError("Cannot read the shared AI workflow policy.") from error
    required = [
        "repository",
        "base_branch",
        "branch_pattern",
        "required_checks",
        "required_workflows",
    ]
    if data.get("schema_version") != 1 or any(not data.get(key) for key in required):
        raise WorkflowError("Shared workflow policy is incomplete or unsupported.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", data["repository"]):
        raise WorkflowError("Invalid repository in shared policy.")
    return data


def normalized_remote(value: str) -> str | None:
    """Recognize credential-free github.com origin URLs without printing them."""
    if value.startswith("git@github.com:"):
        repository = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment:
            return None
        if parsed.scheme == "https" and parsed.username is None and parsed.password is None:
            repository = parsed.path.lstrip("/")
        elif parsed.scheme == "ssh" and parsed.username == "git" and parsed.password is None:
            repository = parsed.path.lstrip("/")
        else:
            return None
        try:
            if parsed.port is not None:
                return None
        except ValueError:
            return None
    repository = repository.removesuffix(".git")
    return repository if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) else None


def verify_origin(config: dict) -> None:
    top = Path(git("rev-parse", "--show-toplevel")).resolve()
    if top != ROOT.resolve():
        raise WorkflowError("Run this helper from its own Git checkout.")
    urls = git("remote", "get-url", "--all", "origin").splitlines()
    urls += git("remote", "get-url", "--push", "--all", "origin").splitlines()
    expected = config["repository"].lower()
    if not urls or any((normalized_remote(value) or "").lower() != expected for value in urls):
        raise WorkflowError("Origin fetch/push repository does not match the shared policy.")


def active_branch() -> str:
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not branch:
        raise WorkflowError("Detached HEAD: choose the task branch before Git mutations.")
    return branch


def require_clean() -> None:
    if git("status", "--porcelain"):
        raise WorkflowError("Working tree has changes. Preserve them before switching tasks.")


def gh_json(path: str, *, method: str | None = None, fields: dict | None = None):
    args = ["gh", "api", path]
    if method:
        args += ["--method", method]
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    try:
        return json.loads(command(args).stdout)
    except ValueError as error:
        raise WorkflowError("GitHub returned an unexpected response.") from error


def hooks_installed() -> bool:
    return git("config", "--local", "--get", "core.hooksPath", check=False) == ".githooks"


def install_hooks() -> None:
    existing = git("config", "--get", "core.hooksPath", check=False)
    if existing and existing != ".githooks":
        raise WorkflowError(
            "Existing core.hooksPath is preserved. Reconcile hooks before installation."
        )
    for name in ["pre-commit", "commit-msg", "pre-push"]:
        target = ROOT / ".githooks" / name
        if not target.is_file():
            raise WorkflowError("Shared hooks are missing. Update the checkout first.")
        target.chmod(target.stat().st_mode | 0o111)
    git("config", "--local", "core.hooksPath", ".githooks")
    print("Installed repository-local shared hooks; no global Git settings changed.")


def doctor(config: dict) -> None:
    verify_origin(config)
    print(f"Repository: {config['repository']}")
    print(f"Branch: {active_branch()}")
    print(f"Working tree: {'changes present' if git('status', '--porcelain') else 'clean'}")
    print(f"Shared hooks: {'enabled' if hooks_installed() else 'not installed; run hooks'}")
    expected = {}
    python_project = ROOT / "pyproject.toml"
    if python_project.is_file():
        pinned_uv = re.search(r'required-version\s*=\s*"==([^"\s]+)"', python_project.read_text())
        if pinned_uv:
            expected["uv"] = pinned_uv.group(1)
    missing = []
    for tool in ["git", "gh", "uv"]:
        if not shutil.which(tool):
            print(f"{tool}: missing")
            missing.append(tool)
            continue
        version_lines = command([tool, "--version"]).stdout.splitlines()
        first = version_lines[0] if version_lines else ""
        actual = first.removeprefix("v")
        if tool == "uv" and first.startswith("uv "):
            actual = first.split()[1]
        if tool in expected and actual != expected[tool]:
            print(f"{tool}: differs from pinned {expected[tool]}; use onboarding instructions")
            missing.append(tool)
        else:
            print(f"{tool}: available")
    if command(["gh", "auth", "status"], check=False).returncode:
        raise WorkflowError("GitHub authentication is unavailable; run gh auth login locally.")
    print("GitHub authentication: available; credentials not displayed")
    if missing:
        raise WorkflowError(
            "Required developer tools are missing or differ from the pinned environment."
        )


def start(config: dict, issue: int, slug: str) -> None:
    verify_origin(config)
    require_clean()
    if active_branch() != config["base_branch"]:
        raise WorkflowError("Start from the base branch in a separate clean checkout or worktree.")
    branch = f"codex/{issue}-{slug}"
    if not re.fullmatch(config["branch_pattern"], branch):
        raise WorkflowError("Use a positive issue number and a lowercase hyphenated slug.")
    task = gh_json(f"repos/{config['repository']}/issues/{issue}")
    if task.get("state") != "open" or "pull_request" in task:
        raise WorkflowError("The task must be an open GitHub issue, not a pull request.")
    git("fetch", "origin", config["base_branch"])
    existing = command(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        check=False,
    )
    if existing.returncode == 0:
        raise WorkflowError("The remote task branch already exists. Resume its issue/PR instead.")
    if existing.returncode != 2:
        raise WorkflowError("Cannot confirm whether the remote task branch exists.")
    git("merge", "--ff-only", f"origin/{config['base_branch']}")
    if git("rev-parse", "HEAD") != git("rev-parse", f"origin/{config['base_branch']}"):
        raise WorkflowError(
            "Local main contains unpublished commits. Preserve them before a new task."
        )
    git("switch", "--no-track", "-c", branch)
    print(f"Started {branch}. Use a separate checkout/worktree for simultaneous agents.")


def validator(*args: str) -> None:
    result = command(
        [sys.executable, str(ROOT / "tools" / "check_git_policy.py"), *args], check=False
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode:
        # The validator deliberately emits sanitized diagnostics only.
        raise WorkflowError("Shared Git policy check failed.")


def pre_push(config: dict, remote_name: str, remote_url: str, stdin: str) -> None:
    verify_origin(config)
    if (
        remote_name != "origin"
        or (normalized_remote(remote_url) or "").lower() != config["repository"].lower()
    ):
        raise WorkflowError("Push only to the shared repository's origin remote.")
    base_ref = f"origin/{config['base_branch']}"
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4:
            raise WorkflowError("Unexpected pre-push input.")
        local_ref, local_sha, remote_ref, remote_sha = parts
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in [local_sha, remote_sha]):
            raise WorkflowError("Unexpected commit ID in pre-push input.")
        if not remote_ref.startswith("refs/heads/"):
            raise WorkflowError("Tag or non-branch pushes require a separate release workflow.")
        branch = remote_ref.removeprefix("refs/heads/")
        if branch == config["base_branch"] or not re.fullmatch(config["branch_pattern"], branch):
            raise WorkflowError("Direct base-branch pushes and untracked branch names are blocked.")
        if local_sha == ZERO or local_ref == "(delete)":
            raise WorkflowError("Branch deletion is reserved for verified PR merge cleanup.")
        if local_ref != f"refs/heads/{branch}":
            raise WorkflowError("Push the matching task branch; ref redirection is blocked.")
        if remote_sha != ZERO:
            ancestor = command(
                ["git", "merge-base", "--is-ancestor", remote_sha, local_sha], check=False
            )
            if ancestor.returncode:
                raise WorkflowError(
                    "Non-fast-forward pushes are blocked. Merge origin/main instead."
                )
        # Scan every task commit, not only the just-pushed tip, including deleted secrets.
        validator("--base", base_ref, "--head", local_sha, "--branch", branch)


def handoff(config: dict, issue: int) -> None:
    verify_origin(config)
    branch = active_branch()
    if not branch.startswith(f"codex/{issue}-") or not re.fullmatch(
        config["branch_pattern"], branch
    ):
        raise WorkflowError("The issue number must match the active task branch.")
    base = git("rev-parse", f"origin/{config['base_branch']}")
    head = git("rev-parse", "HEAD")
    changed = git("diff", "--name-only", f"{base}...{head}")
    print(f"# AI 인수인계 — #{issue}\n")
    print(
        f"- Repository: {config['repository']}\n- Branch: {branch}"
        f"\n- Head-SHA: {head}\n- Base-SHA: {base}"
    )
    print(
        f"- Working tree: {'변경 있음 — 보존 후 확인' if git('status', '--porcelain') else 'clean'}"
    )
    print("\n## 변경 파일\n")
    print(changed or "아직 커밋된 변경 없음")
    print(
        "\n## 수행한 검증\n\n실제로 실행한 명령·결과를 작성하세요. "
        "이 도구는 테스트 통과를 추정하지 않습니다."
    )
    print(
        "\n## 남은 작업과 주의점\n\n미완료 항목, 미검증 범위, 파일 소유권·충돌 여부를 작성하세요."
    )
    print(
        "\n## 다음 에이전트\n\nAGENTS.md → SPEC.md → docs/ai-collaboration.md → "
        "이슈와 PR을 읽고 현재 SHA를 확인하세요."
    )


def validate_rollup(
    rollup: list, required: list[str], repository: str, current_run_ids: set[int]
) -> None:
    if not current_run_ids:
        raise WorkflowError("No current successful PR workflow runs are selected.")
    by_name: dict[str, list] = {name: [] for name in required}
    run_url = re.compile(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/([1-9][0-9]*)"
        r"(?:/job/[1-9][0-9]*)?(?:\?[^#]*)?"
    )
    for check in rollup:
        name = check.get("name", check.get("context"))
        match = run_url.fullmatch(check.get("detailsUrl", ""))
        # Old edited-event runs and push runs may share names with current PR
        # checks. Only the selected latest runs can satisfy or block these gates.
        if name in by_name and match and int(match.group(1)) in current_run_ids:
            by_name[name].append(check)
    for name, matches in by_name.items():
        if not matches:
            raise WorkflowError(f"Missing CI check: {name}")
        for check in matches:
            if (
                check.get("__typename") != "CheckRun"
                or check.get("status") != "COMPLETED"
                or check.get("conclusion") != "SUCCESS"
                or not check.get("detailsUrl", "").startswith(
                    f"https://github.com/{repository}/actions/runs/"
                )
            ):
                raise WorkflowError(f"CI check is not a successful GitHub Actions run: {name}")


def validate_pr(config: dict, pr: dict) -> tuple[str, str]:
    head = pr.get("head", {}).get("sha", "")
    branch = pr.get("head", {}).get("ref", "")
    if (
        pr.get("state") != "open"
        or pr.get("draft")
        or pr.get("base", {}).get("ref") != config["base_branch"]
        or pr.get("base", {}).get("repo", {}).get("full_name", "").lower()
        != config["repository"].lower()
        or pr.get("head", {}).get("repo", {}).get("full_name", "").lower()
        != config["repository"].lower()
        or not re.fullmatch(config["branch_pattern"], branch)
        or not re.fullmatch(r"[0-9a-f]{40}", head)
    ):
        raise WorkflowError(
            "PR must be open, ready, same-repository and target the shared base branch."
        )
    # Reuse the versioned subject and metadata validator, so local gates and CI agree.
    try:
        from check_git_policy import validate_subject
    except ImportError as error:
        raise WorkflowError("Shared policy validator is unavailable.") from error
    if validate_subject(pr.get("title", ""), config):
        raise WorkflowError("PR title does not follow the shared commit convention.")
    body = pr.get("body") or ""
    number = branch.split("/", 1)[1].split("-", 1)[0]
    if not re.search(rf"(?im)^Closes\s+#{number}\s*$", body):
        raise WorkflowError("PR body must close its matching task issue.")
    for heading in ["변경 내용", "검증", "AI 검토", "남은 사항"]:
        match = re.search(rf"(?m)^## {heading}\s*\n(.*?)(?=^## |\Z)", body, re.S)
        if not match or not match.group(1).strip():
            raise WorkflowError("PR body is missing a required completed section.")
    review = re.search(r"(?m)^## AI 검토\s*\n(.*?)(?=^## |\Z)", body, re.S).group(1)
    reviewers = re.findall(r"(?m)^Reviewer:\s*(\S[^\n]*)$", review)
    heads = re.findall(r"(?m)^Head-SHA:\s*([0-9a-f]{40})\s*$", review)
    if len(reviewers) != 1 or heads != [head] or re.search(r"(?m)^\s*- \[ \]", body):
        raise WorkflowError(
            "Ready PR needs one reviewer, its current Head-SHA and no pending checkboxes."
        )
    return head, branch


def pr_check_rollup(config: dict, number: int) -> list:
    result = command(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            config["repository"],
            "--json",
            "statusCheckRollup",
        ]
    )
    try:
        return json.loads(result.stdout)["statusCheckRollup"]
    except (ValueError, KeyError, TypeError) as error:
        raise WorkflowError("Cannot determine current PR checks.") from error


def validate_workflow_runs(config: dict, number: int, head: str) -> set[int]:
    runs = []
    for page in range(1, 101):
        data = gh_json(
            f"repos/{config['repository']}/actions/runs?head_sha={head}"
            f"&event=pull_request&per_page=100&page={page}"
        )
        items = data.get("workflow_runs")
        if not isinstance(items, list):
            raise WorkflowError("Cannot confirm all current PR workflow runs.")
        runs.extend(items)
        if len(items) < 100:
            if data.get("total_count", len(runs)) > len(runs):
                raise WorkflowError("Workflow result set is truncated; cannot confirm latest CI.")
            break
    else:
        raise WorkflowError("Workflow run pagination exceeded the safe limit.")
    selected = set()
    for path in config["required_workflows"]:
        matches = [
            run
            for run in runs
            if run.get("path", "").split("@", 1)[0] == path
            and run.get("head_sha") == head
            and any(pr.get("number") == number for pr in run.get("pull_requests", []))
        ]
        if not matches:
            raise WorkflowError(f"No current PR workflow run found: {path}")
        latest = max(
            matches,
            key=lambda run: (
                run.get("run_started_at") or run.get("created_at", ""),
                run.get("id", 0),
            ),
        )
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise WorkflowError(f"Latest PR workflow is not successful: {path}")
        actor = latest.get("repository", {}).get("full_name", "")
        if actor.lower() != config["repository"].lower():
            raise WorkflowError("Unexpected repository for workflow run.")
        run_id = latest.get("id")
        if not isinstance(run_id, int) or run_id < 1:
            raise WorkflowError("Cannot identify the current PR workflow run.")
        selected.add(run_id)
    return selected


def validate_review_state(config: dict, number: int, head: str) -> None:
    """Fail closed for unresolved threads and outstanding requested changes.

    GitHub Free private repositories do not enforce these gates server-side.
    Read every page explicitly, keeping PR text out of shell commands.
    """
    owner, name = config["repository"].split("/", 1)
    query = """
    query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$number) {
          headRefOid
          reviewThreads(first:100, after:$cursor) {
            nodes { isResolved }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    cursor = None
    visited = set()
    for _page in range(100):
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor is not None:
            args.extend(["-f", f"cursor={cursor}"])
        try:
            response = json.loads(command(args).stdout)
            if response.get("errors"):
                raise ValueError
            pr = response["data"]["repository"]["pullRequest"]
            connection = pr["reviewThreads"]
            nodes = connection["nodes"]
            info = connection["pageInfo"]
            if pr["headRefOid"] != head or not isinstance(nodes, list):
                raise ValueError
            if any(node.get("isResolved") is not True for node in nodes):
                raise WorkflowError("PR has unresolved review conversations.")
            if info["hasNextPage"] is False:
                break
            cursor = info["endCursor"]
            if not cursor or cursor in visited or info["hasNextPage"] is not True:
                raise ValueError
            visited.add(cursor)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise WorkflowError(
                "Cannot confirm all PR review conversations at the current SHA."
            ) from error
    else:
        raise WorkflowError("Review conversation pagination exceeded the safe limit.")

    decisions = {}
    for page in range(1, 101):
        reviews = gh_json(
            f"repos/{config['repository']}/pulls/{number}/reviews?per_page=100&page={page}"
        )
        if not isinstance(reviews, list):
            raise WorkflowError("Cannot confirm current PR review decisions.")
        for review in reviews:
            try:
                state = review["state"]
                login = review["user"]["login"]
                review_id = review["id"]
                if not isinstance(login, str) or not isinstance(review_id, int):
                    raise ValueError
                if state == "PENDING":
                    raise WorkflowError(
                        "PR has a visible pending review; finish it before merging."
                    )
                if state not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}:
                    raise ValueError
                # A later comment alone cannot erase a previous requested change.
                if state != "COMMENTED" and review_id > decisions.get(login, (-1, ""))[0]:
                    decisions[login] = (review_id, state)
            except (ValueError, KeyError, TypeError) as error:
                raise WorkflowError("Cannot confirm current PR review decisions.") from error
        if len(reviews) < 100:
            break
    else:
        raise WorkflowError("Review decision pagination exceeded the safe limit.")
    if any(state == "CHANGES_REQUESTED" for _, state in decisions.values()):
        raise WorkflowError("PR has outstanding requested changes.")


def same_reviewed_pr(config: dict, current: dict, expected: dict) -> bool:
    return (
        validate_pr(config, current) == validate_pr(config, expected)
        and current.get("body") == expected.get("body")
        and current.get("title") == expected.get("title")
        and current.get("mergeable") is True
        and current.get("mergeable_state") == "clean"
    )


def merge(config: dict, number: int, execute: bool) -> None:
    verify_origin(config)
    pr_path = f"repos/{config['repository']}/pulls/{number}"
    pr = gh_json(pr_path)
    head, branch = validate_pr(config, pr)
    git("fetch", "origin", config["base_branch"], branch)
    ancestor = command(
        ["git", "merge-base", "--is-ancestor", f"origin/{config['base_branch']}", head], check=False
    )
    if ancestor.returncode:
        raise WorkflowError(
            "PR is behind main. Merge current origin/main, retest and review the new SHA."
        )
    current_runs = validate_workflow_runs(config, number, head)
    validate_rollup(
        pr_check_rollup(config, number),
        config["required_checks"],
        config["repository"],
        current_runs,
    )
    validate_review_state(config, number, head)
    # Check review metadata, local policy/history and current branch state again before mutation.
    event = {"repository": {"full_name": config["repository"]}, "pull_request": pr}
    from check_git_policy import validate_pr as validate_event

    errors = validate_event(event, config)
    if errors:
        raise WorkflowError("PR metadata no longer matches the shared policy.")
    validator("--base", f"origin/{config['base_branch']}", "--head", head, "--branch", branch)
    if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
        raise WorkflowError("GitHub has not confirmed a clean, conflict-free merge.")
    print(f"Validated PR #{number}: {branch} at {head}; squash into {config['base_branch']}.")
    if not execute:
        print("Read-only plan. Add --execute to perform this validated merge.")
        return
    latest = gh_json(pr_path)
    if not same_reviewed_pr(config, latest, pr):
        raise WorkflowError("PR changed during verification. Start the merge check again.")
    git("fetch", "origin", config["base_branch"])
    if command(
        ["git", "merge-base", "--is-ancestor", f"origin/{config['base_branch']}", head], check=False
    ).returncode:
        raise WorkflowError("Main moved during verification. Synchronize and recheck the PR.")
    current_runs = validate_workflow_runs(config, number, head)
    validate_rollup(
        pr_check_rollup(config, number),
        config["required_checks"],
        config["repository"],
        current_runs,
    )
    validate_review_state(config, number, head)
    if not same_reviewed_pr(config, gh_json(pr_path), pr):
        raise WorkflowError("PR changed during the final checks. Recheck before merging.")
    result = gh_json(
        pr_path + "/merge",
        method="PUT",
        fields={"merge_method": "squash", "sha": head, "commit_title": pr["title"]},
    )
    if not result.get("merged"):
        raise WorkflowError("GitHub did not confirm the squash merge.")
    merged = gh_json(pr_path)
    if not merged.get("merged") or merged.get("head", {}).get("sha") != head:
        raise WorkflowError("Merge response requires manual reconciliation; do not retry blindly.")
    git("fetch", "origin", config["base_branch"])
    merge_sha = result.get("sha", "")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", merge_sha)
        or command(
            ["git", "merge-base", "--is-ancestor", merge_sha, f"origin/{config['base_branch']}"],
            check=False,
        ).returncode
    ):
        raise WorkflowError(
            "Merged commit is not yet confirmed on origin/main; preserve local work."
        )
    # GitHub's repository setting deletes the merged task branch. If it remains,
    # retain it and report it; do not race a later push with an unconditional DELETE.
    print(f"Merged PR #{number}: {merge_sha}. Local task branch and working tree are preserved.")
    remote = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote:
        print("Remote task branch still exists; preserve it until safe cleanup is confirmed.")
    else:
        print("Remote task branch deletion confirmed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    sub.add_parser("hooks")
    sub.add_parser("check")
    task = sub.add_parser("start")
    task.add_argument("issue", type=int)
    task.add_argument("slug")
    hand = sub.add_parser("handoff")
    hand.add_argument("issue", type=int)
    merger = sub.add_parser("merge")
    merger.add_argument("pr", type=int)
    merger.add_argument("--execute", action="store_true")
    push = sub.add_parser("pre-push", help="Internal Git pre-push hook entry point")
    push.add_argument("remote_name")
    push.add_argument("remote_url")
    args = parser.parse_args(argv)
    try:
        config = policy()
        if args.action == "doctor":
            doctor(config)
        elif args.action == "hooks":
            verify_origin(config)
            install_hooks()
        elif args.action == "start":
            if args.issue < 1:
                raise WorkflowError("Issue number must be positive.")
            start(config, args.issue, args.slug)
        elif args.action == "check":
            verify_origin(config)
            validator("--staged")
        elif args.action == "handoff":
            handoff(config, args.issue)
        elif args.action == "merge":
            if args.pr < 1:
                raise WorkflowError("PR number must be positive.")
            merge(config, args.pr, args.execute)
        elif args.action == "pre-push":
            pre_push(config, args.remote_name, args.remote_url, sys.stdin.read())
    except WorkflowError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Check shared Git rules without executing PR text or printing secret contents."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

MAX_BLOB_BYTES = 5 * 1024 * 1024
BLOCKED_COMPONENTS = {
    ".agent-worktrees",
    ".aws",
    ".cache",
    ".local",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".tools",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}
BLOCKED_ROOTS = {"data", "datasets", "models", "weights", "recordings", "transcripts"}
BLOCKED_SUFFIXES = {
    ".aac",
    ".ckpt",
    ".flac",
    ".gguf",
    ".jks",
    ".key",
    ".keystore",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".onnx",
    ".p12",
    ".pem",
    ".pfx",
    ".pt",
    ".pth",
    ".safetensors",
    ".wav",
    ".webm",
    ".zip",
}
SECRET_PATTERNS = (
    ("AWS access key", re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("GitHub token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile(rb"github_pat_[A-Za-z0-9_]{30,}")),
    ("API secret token", re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    (
        "private key",
        re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ),
)
REQUIRED_HEADINGS = ("변경 내용", "검증", "AI 검토", "남은 사항")


class PolicyError(Exception):
    """An input or Git operation cannot be checked safely."""


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        required = ("repository", "base_branch", "branch_pattern", "commit_types")
        if not isinstance(policy, dict) or policy.get("schema_version") != 1:
            raise ValueError
        if any(not policy.get(key) for key in required):
            raise ValueError
        if not all(isinstance(policy[key], str) for key in required[:3]):
            raise ValueError
        if not isinstance(policy["commit_types"], list) or not all(
            isinstance(item, str) and re.fullmatch(r"[a-z]+", item)
            for item in policy["commit_types"]
        ):
            raise ValueError
        maximum = policy.get("commit_max_length", 100)
        if not isinstance(maximum, int) or maximum < 20:
            raise ValueError
        re.compile(policy["branch_pattern"])
    except (OSError, ValueError, TypeError, re.error) as exc:
        raise PolicyError("Cannot read a valid shared workflow policy.") from exc
    return policy


def validate_subject(subject: str, policy: dict[str, Any]) -> list[str]:
    types = "|".join(re.escape(item) for item in policy["commit_types"])
    pattern = rf"(?:{types})(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*"
    errors = []
    if not re.fullmatch(pattern, subject):
        errors.append("Use a Conventional Commit subject with an allowed type and description.")
    if len(subject) > policy.get("commit_max_length", 100):
        errors.append("Commit/PR subject exceeds the configured character limit.")
    return errors


def validate_branch(branch: str, policy: dict[str, Any]) -> list[str]:
    if not re.fullmatch(policy["branch_pattern"], branch):
        return ["Use the shared codex/<issue-number>-<slug> branch format."]
    return []


def body_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    parts = re.split(r"(?m)^##[ \t]+([^\r\n]+)[ \t]*\r?$", body)
    for index in range(1, len(parts), 2):
        name = parts[index].strip()
        if name in sections:
            raise PolicyError("PR body contains a duplicated section heading.")
        sections[name] = parts[index + 1].strip()
    return sections


def validate_pr(event: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    try:
        pr = event["pull_request"]
        repository = event["repository"]["full_name"]
        branch, base = pr["head"]["ref"], pr["base"]["ref"]
        head = pr["head"]["sha"]
        title = pr["title"]
        body = pr.get("body") or ""
        if not all(isinstance(value, str) for value in (branch, base, head, title, body)):
            raise ValueError
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
            raise ValueError
        draft = pr.get("draft", False)
        if not isinstance(draft, bool):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError("Cannot read valid pull request metadata.") from exc
    errors = []
    if repository != policy["repository"]:
        errors.append("The PR repository does not match the shared workflow policy.")
    if base != policy["base_branch"]:
        errors.append("The PR must target the configured base branch.")
    errors.extend(validate_branch(branch, policy))
    errors.extend(validate_subject(title, policy))
    issue = re.match(r"^codex/([1-9][0-9]*)-", branch)
    closes = re.findall(r"(?im)^[ \t]*Closes[ \t]+#([1-9][0-9]*)[ \t]*\r?$", body)
    if not issue or closes != [issue.group(1)]:
        errors.append("Add exactly one Closes #N line matching the branch issue number.")
    sections = body_sections(body)
    for heading in REQUIRED_HEADINGS:
        if not sections.get(heading):
            errors.append(f"PR body needs a nonempty '{heading}' section.")
    if not draft:
        if re.search(r"(?im)^[ \t]*[-*][ \t]+\[[ \t]\]", body):
            errors.append("Ready PRs cannot contain unchecked checklist items.")
        review = sections.get("AI 검토", "")
        reviewer = re.findall(r"(?m)^[ \t]*Reviewer:[ \t]*(\S[^\r\n]*)[ \t]*\r?$", review)
        reviewed_heads = re.findall(
            r"(?m)^[ \t]*Head-SHA:[ \t]*([0-9a-fA-F]{40})[ \t]*\r?$", review
        )
        if len(reviewer) != 1:
            errors.append("Ready PRs need one Reviewer: agent identifier in the AI review section.")
        if [sha.lower() for sha in reviewed_heads] != [head.lower()]:
            errors.append("AI review Head-SHA must match the current PR head commit.")
    return errors


def path_violations(path: str) -> list[str]:
    parts = PurePosixPath(path).parts
    lower = [part.lower() for part in parts]
    errors = []
    if not parts or any(part in {".", ".."} for part in parts) or path.startswith("/"):
        errors.append("unsafe repository path")
    if any(part in BLOCKED_COMPONENTS for part in lower):
        errors.append("generated/local/credential directory")
    if lower and lower[0] in BLOCKED_ROOTS:
        errors.append("raw data or model directory")
    if lower:
        name = lower[-1]
        if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
            errors.append("personal environment file")
        if name in {".netrc", ".git-credentials", ".pypirc"}:
            errors.append("credential configuration file")
        if PurePosixPath(name).suffix in BLOCKED_SUFFIXES:
            errors.append("raw media, model, archive or credential file type")
    return errors


def blob_violations(blob: bytes) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(blob)]


def git(args: list[str], *, cwd: Path) -> bytes:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        raise PolicyError("A required Git read failed; verify checkout history and references.")
    return result.stdout


def resolve_commit(ref: str, *, cwd: Path) -> str:
    value = git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], cwd=cwd)
    sha = value.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PolicyError("Git did not return a valid commit SHA.")
    return sha


def quoted_path(path: str) -> str:
    return json.dumps(path, ensure_ascii=True)


def scan_blob(spec: str, path: str, *, cwd: Path, seen: set[str]) -> list[str]:
    errors = [f"File {quoted_path(path)}: {reason}." for reason in path_violations(path)]
    oid = git(["rev-parse", "--verify", "--end-of-options", spec], cwd=cwd).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", oid):
        raise PolicyError("Git did not return a valid object SHA.")
    if oid in seen:
        return errors
    seen.add(oid)
    kind = git(["cat-file", "-t", oid], cwd=cwd).decode().strip()
    if kind != "blob":
        return errors + [
            f"File {quoted_path(path)}: submodules/non-blob objects need separate review."
        ]
    size = int(git(["cat-file", "-s", oid], cwd=cwd))
    if size > MAX_BLOB_BYTES:
        return errors + [f"File {quoted_path(path)}: exceeds the 5 MiB source-file limit."]
    blob = git(["cat-file", "blob", oid], cwd=cwd)
    errors.extend(
        f"File {quoted_path(path)}: detected {name}; value omitted."
        for name in blob_violations(blob)
    )
    return errors


def staged_errors(*, cwd: Path, policy: dict[str, Any]) -> list[str]:
    branch = git(["branch", "--show-current"], cwd=cwd).decode().strip()
    errors = validate_branch(branch, policy)
    unresolved = git(["diff", "--name-only", "--diff-filter=U", "-z"], cwd=cwd)
    if unresolved:
        errors.append("Resolve index conflicts before committing.")
        return errors
    paths = git(["diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z"], cwd=cwd)
    seen: set[str] = set()
    for raw in filter(None, paths.split(b"\0")):
        path = raw.decode("utf-8", "surrogateescape")
        errors.extend(scan_blob(":" + path, path, cwd=cwd, seen=seen))
    return errors


def range_errors(base: str, head: str, *, cwd: Path, policy: dict[str, Any]) -> list[str]:
    base_sha, head_sha = resolve_commit(base, cwd=cwd), resolve_commit(head, cwd=cwd)
    commits = git(["rev-list", "--reverse", f"{base_sha}..{head_sha}"], cwd=cwd).decode().split()
    if not commits:
        return ["The PR commit range is empty."]
    errors: list[str] = []
    seen: set[str] = set()
    for sha in commits:
        subject = git(["show", "-s", "--format=%s", sha], cwd=cwd).decode("utf-8").strip()
        errors.extend(
            f"Commit {sha[:12]}: {reason}" for reason in validate_subject(subject, policy)
        )
        paths = git(
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", "-m", sha],
            cwd=cwd,
        )
        for raw in dict.fromkeys(filter(None, paths.split(b"\0"))):
            path = raw.decode("utf-8", "surrogateescape")
            # Deletions are safe here; their earlier additions remain checked in this range.
            spec = sha + ":" + path
            exists = subprocess.run(
                ["git", "cat-file", "-e", spec],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if exists.returncode == 0:
                errors.extend(scan_blob(spec, path, cwd=cwd, seen=seen))
    return list(dict.fromkeys(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path(".github/ai-workflow.json"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--branch")
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--message-file", type=Path)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        errors: list[str] = []
        if args.message_file:
            lines = args.message_file.read_text(encoding="utf-8").splitlines()
            errors.extend(validate_subject(lines[0] if lines else "", policy))
        if args.staged:
            errors.extend(staged_errors(cwd=args.repo, policy=policy))
        if args.branch:
            errors.extend(validate_branch(args.branch, policy))
        base, head = args.base, args.head
        if args.event_file:
            event = json.loads(args.event_file.read_text(encoding="utf-8"))
            errors.extend(validate_pr(event, policy))
            base, head = event["pull_request"]["base"]["sha"], event["pull_request"]["head"]["sha"]
            if not all(re.fullmatch(r"[0-9a-fA-F]{40}", ref) for ref in (base, head)):
                raise PolicyError("The PR event does not contain valid base/head commit SHAs.")
            if resolve_commit("HEAD", cwd=args.repo) != head.lower():
                errors.append("The workflow must check out the exact pull request head commit.")
        if base:
            errors.extend(range_errors(base, head, cwd=args.repo, policy=policy))
        if not any((args.message_file, args.staged, args.branch, args.event_file, args.base)):
            parser.error("choose --staged, --message-file, --branch, --base or --event-file")
        if errors:
            for error in dict.fromkeys(errors):
                print("FAIL: " + error)
            return 1
        print("PASS: shared Git policy checks completed; secret values are never printed.")
        return 0
    except (PolicyError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, PolicyError) else "Cannot safely read policy inputs."
        print("FAIL: " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

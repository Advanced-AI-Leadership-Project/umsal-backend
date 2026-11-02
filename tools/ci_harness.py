"""Reproducible validation entry point. No cloud credentials or production data.

CI prepares an actual base+head merge before installing candidate dependencies.
This script is invoked from the trusted default branch for dispatched runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / ".reports"
SHA = re.compile(r"[0-9a-f]{40}")


def run(args: list[str], *, cwd: Path | None = None) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd or ROOT, check=True, timeout=900)


def prepare() -> None:
    """Validate dispatch inputs, then integrate the exact head into the exact base.

    No network, hooks, submodules, candidate commands or writable token are used.
    Ordinary PR and push runs already have their GitHub-provided checkout.
    """
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        return
    mode = os.environ.get("HARNESS_MODE", "")
    base = os.environ.get("HARNESS_BASE", "")
    head = os.environ.get("HARNESS_HEAD", "")
    number = os.environ.get("HARNESS_PR", "")
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if (
        mode not in {"integration", "main"}
        or not SHA.fullmatch(base)
        or base != current
        or base != os.environ.get("GITHUB_SHA")
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("Dispatch must check out its exact declared main SHA.")
    if mode == "integration":
        if not SHA.fullmatch(head) or not re.fullmatch(r"[1-9][0-9]*", number):
            raise ValueError("Integration dispatch needs a numeric PR and a full head SHA.")
        # Even --no-commit requires a committer identity on a clean runner.
        # These per-command synthetic values never create a commit or change Git config.
        run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=UMSAL integration check",
                "-c",
                "user.email=integration@example.invalid",
                "merge",
                "--no-ff",
                "--no-commit",
                head,
            ]
        )
    elif head or number:
        raise ValueError("Main verification cannot declare a PR or candidate head.")
    REPORTS.mkdir(exist_ok=True)
    tree = subprocess.check_output(["git", "write-tree"], cwd=ROOT, text=True).strip()
    (REPORTS / "integration.json").write_text(
        json.dumps({"mode": mode, "pr": number, "base": base, "head": head, "tree": tree}) + "\n"
    )
    print(f"Prepared {mode}: base={base} head={head or 'none'} tree={tree}")


def quality() -> None:
    """Scan every tracked source, not only the current PR diff."""
    from check_git_policy import MAX_BLOB_BYTES, blob_violations, path_violations, quoted_path

    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    errors = []
    paths = [ROOT / name.decode() for name in raw.split(b"\0") if name]
    for path in paths:
        # Removed tracked files are absent from an uncommitted integration tree.
        if not path.exists() and not path.is_symlink():
            continue
        relative = path.relative_to(ROOT).as_posix()
        reasons = path_violations(relative)
        if path.is_symlink():
            reasons.append("symbolic links require separate policy support")
        elif path.is_file():
            if path.stat().st_size > MAX_BLOB_BYTES:
                reasons.append("source exceeds the 5 MiB policy limit")
            else:
                reasons.extend(blob_violations(path.read_bytes()))
        for reason in reasons:
            errors.append(f"{quoted_path(relative)}: {reason}; values omitted")
    if errors:
        raise ValueError("\n".join(errors))
    run([sys.executable, "-m", "unittest", "discover", "-s", "tools/tests", "-v"])
    print(f"PASS: full tracked-tree policy scan ({len(paths)} paths).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["prepare", "quality", "python", "security"])
    args = parser.parse_args(argv)
    REPORTS.mkdir(exist_ok=True)
    uv = ["uv", "run", "--locked"]
    # Keep runtime/cache files inside this checkout on every OS.
    os.environ.setdefault("UV_CACHE_DIR", str(ROOT / ".cache" / "uv"))
    os.environ.setdefault("UV_PYTHON_INSTALL_DIR", str(ROOT / ".tools" / "python"))
    try:
        if args.suite == "prepare":
            prepare()
        elif args.suite == "quality":
            quality()
        elif args.suite == "python":
            run([*uv, "ruff", "check", "."])
            run([*uv, "ruff", "format", "--check", "."])
            run([*uv, "pytest", "--junitxml=.reports/python.xml", "-q"])
        elif args.suite == "security":
            # Fail on any known PyPI advisory. Advisory-service/network failures
            # also fail; there is no blanket ignore list.
            run([*uv, "pip-audit", "--local", "--format=json", "--output=.reports/pip-audit.json"])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, ValueError):
            print(f"FAIL: {error}", file=sys.stderr)
        else:
            print(f"FAIL: {args.suite}; inspect the failing step/report.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

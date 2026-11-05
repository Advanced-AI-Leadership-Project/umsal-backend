"""Build the Lambda zip from uv.lock with Linux x86_64 wheels, never local Windows/macOS ones.

Usage: python infra/aws/build_lambda.py  ->  .local/lambda/<commit-sha>.zip
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / ".local" / "lambda-build"


def _commit_sha() -> str:
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("Commit src, pyproject.toml and uv.lock first; the zip is named by SHA.")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    sha = _commit_sha()
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    requirements = BUILD / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    package = BUILD / "package"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python-version",
            "3.12",
            "--python-platform",
            "x86_64-manylinux_2_34",
            "--only-binary",
            ":all:",
            "--no-deps",
            "--target",
            str(package),
            "--requirements",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
    )
    # Copy only git-tracked app files so ignored local files (.env etc.) never ship.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "src/umsal_backend"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    for name in filter(None, tracked):
        relative = Path(name).relative_to("src")
        (package / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, package / relative)
    out_dir = ROOT / ".local" / "lambda"
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{sha}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                output.write(path, path.relative_to(package).as_posix())
    print(f"Built {archive.relative_to(ROOT).as_posix()} ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

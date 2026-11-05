"""Build Lambda zips from uv.lock with Linux x86_64 wheels, never local Windows/macOS ones.

Usage:
  python infra/aws/build_lambda.py               ->  .local/lambda/<sha>.zip          (API)
  python infra/aws/build_lambda.py --kind migrate ->  .local/lambda/<sha>-migrate.zip

Both zips contain the git-tracked ``src/umsal_backend`` and the RDS global CA bundle
(``umsal_backend/certs/rds-global-bundle.pem``) used for ``sslmode=verify-full``. The
migrate zip additionally contains the ``migrations`` dependency group (Alembic,
SQLAlchemy), ``alembic.ini`` and the tracked ``migrations/`` directory.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / ".local" / "lambda-build"
RDS_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
CA_PATH_IN_ZIP = Path("umsal_backend") / "certs" / "rds-global-bundle.pem"


def _commit_sha() -> str:
    dirty = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "src",
            "migrations",
            "alembic.ini",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("Commit src, migrations, pyproject.toml and uv.lock first.")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _tracked(*paths: str) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--", *paths],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [name for name in output.split("\0") if name]


def _download_ca_bundle(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(RDS_CA_BUNDLE_URL, timeout=30) as response:  # noqa: S310
        body = response.read()
    if b"BEGIN CERTIFICATE" not in body:
        raise SystemExit("Downloaded RDS CA bundle does not look like PEM certificates.")
    target.write_bytes(body)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["api", "migrate"], default="api")
    args = parser.parse_args(argv)

    sha = _commit_sha()
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    requirements = BUILD / "requirements.txt"
    export = ["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--no-hashes"]
    if args.kind == "migrate":
        export += ["--group", "migrations"]
    subprocess.run(
        [*export, "--output-file", str(requirements)],
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
    # Copy only git-tracked files so ignored local files (.env etc.) never ship.
    for name in _tracked("src/umsal_backend"):
        relative = Path(name).relative_to("src")
        (package / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, package / relative)
    if args.kind == "migrate":
        for name in _tracked("alembic.ini", "migrations"):
            (package / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, package / name)
    _download_ca_bundle(package / CA_PATH_IN_ZIP)

    out_dir = ROOT / ".local" / "lambda"
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / (f"{sha}.zip" if args.kind == "api" else f"{sha}-migrate.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                output.write(path, path.relative_to(package).as_posix())
    print(f"Built {archive.relative_to(ROOT).as_posix()} ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

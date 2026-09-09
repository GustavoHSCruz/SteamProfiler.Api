#!/usr/bin/env python3
"""Reject common release-tree mistakes before they become Git history."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIRED = {
    ".dockerignore",
    ".env.example",
    ".github/workflows/ci.yml",
    ".gitignore",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "SECURITY.md",
}
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(\.env$|\.env\.(?!example$)|id_(rsa|dsa|ecdsa|ed25519)$|"
    r".*\.(pem|p12|pfx|key|sqlite|sqlite3|db)$)",
    re.IGNORECASE,
)
SECRET_PATTERNS = {
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(
        rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)"
    ),
    "OpenAI key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "machine-specific home": re.compile(
        rb"/home/" + rb"gcruz" + rb"(?:/|\b)"
    ),
}


def candidates() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return sorted(set(result.stdout.splitlines()))


def main() -> int:
    files = candidates()
    problems: list[str] = []
    missing = sorted(REQUIRED - set(files))
    if missing:
        problems.append("missing release files: " + ", ".join(missing))

    for name in files:
        if FORBIDDEN_NAMES.search(name):
            problems.append(f"sensitive filename: {name}")
            continue
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(f"cannot read {name}: {exc}")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                problems.append(f"{label} pattern in {name}")

    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    if ".env" not in dockerignore:
        problems.append(".dockerignore does not exclude .env")
    key = (ROOT / ".env.example").read_text().split("STEAM_API_KEY=", 1)[1]
    if key.splitlines()[0]:
        problems.append("STEAM_API_KEY must be empty in .env.example")

    if problems:
        print("\n".join(problems))
        return 1
    print(f"{len(files)} publishable files checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())

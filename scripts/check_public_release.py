"""Bounded current-tree hygiene scan; this cannot establish clean Git history."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (
    ".github", "docs", "examples", "fixtures", "schemas", "scripts",
    "src", "supabase", "tests",
)
ROOT_FILES = {
    ".env.example", "CONTRIBUTING.md", "DATA_POLICY.md", "DISCLAIMER.md", "LICENSE",
    "NOTICE", "PRIVACY.md", "README.md", "SECURITY.md",
    "THIRD_PARTY_NOTICES.md", "pyproject.toml",
}
TEXT_SUFFIXES = {".json", ".md", ".py", ".sql", ".toml", ".txt", ".yaml", ".yml"}
MAX_FILE_BYTES = 2_000_000
SECRET_PATTERNS = {
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Supabase service-role assignment": re.compile(
        r"(?i)\bservice_role\s*[:=]\s*[\"'][A-Za-z0-9_-]{24,}[\"']"
    ),
}
FORBIDDEN_PUBLIC_REFERENCES = {
    # Join terms so the scan does not flag its own source.
    "private issuer symbol 1": "MS" + "FT",
    "private issuer symbol 2": "A" + "MZN",
    "private issuer symbol 3": "M" + "U",
    "private issuer name 1": "Micro" + "soft",
    "private issuer name 2": "Ama" + "zon",
    "private issuer name 3": "Mic" + "ron",
    "personal name": "Tho" + "mas",
}


def current_tree_text_files() -> tuple[list[Path], dict[str, int]]:
    """Return bounded text candidates and counts for skipped candidates."""
    listing = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z", "--", *SCANNED_ROOTS, *sorted(ROOT_FILES)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    selected = []
    skipped = {"non_text": 0, "symlink": 0, "oversized": 0, "missing": 0}
    for item in listing.split(b"\0"):
        if not item:
            continue
        path = ROOT / item.decode("utf-8")
        if path.name not in ROOT_FILES and path.suffix.lower() not in TEXT_SUFFIXES:
            skipped["non_text"] += 1
            continue
        if path.is_symlink():
            skipped["symlink"] += 1
        elif not path.is_file():
            skipped["missing"] += 1
        elif path.stat().st_size > MAX_FILE_BYTES:
            skipped["oversized"] += 1
        else:
            selected.append(path)
    return sorted(selected), skipped


def main() -> int:
    failures: list[str] = []
    scanned, skipped = current_tree_text_files()
    for path in scanned:
        relative = path.relative_to(ROOT)
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError) as exc:
            failures.append(f"{relative}: unreadable text file ({type(exc).__name__})")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"{relative}: resembles {label}")
        upper = content.upper()
        for label, term in FORBIDDEN_PUBLIC_REFERENCES.items():
            if re.search(
                rf"(?<![A-Z0-9]){re.escape(term.upper())}(?![A-Z0-9])",
                upper,
            ):
                failures.append(f"{relative}: contains {label}")

    env_listing = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z", "--", ".env", ".env.*"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.split(b"\0")
    unexpected_env = [
        item.decode("utf-8") for item in env_listing
        if item and item.decode("utf-8") != ".env.example"
    ]
    failures.extend(f"{path}: environment file must not be tracked" for path in unexpected_env)

    if failures:
        print("Current-tree public-release scan failed:")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print(f"Working-tree public-release scan passed ({len(scanned)} text files).")
    print(
        "Scope: bounded roots, tracked and non-ignored untracked files, UTF-8 "
        "text-like suffixes, and credential patterns. Skipped candidates: "
        f"{skipped}. Binary/unrecognized files, ignored files, and files over "
        f"{MAX_FILE_BYTES} bytes are outside the content scan. This scan does "
        "not inspect prior commits, prove clean history, detect all sensitive "
        "content, or replace human review."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys


def extract_release_notes(changelog_path: pathlib.Path, version: str) -> str:
    lines = changelog_path.read_text(encoding="utf-8").splitlines()
    start_header = f"## [{version}]"
    collected: list[str] = []
    in_section = False

    for line in lines:
        if line.startswith(start_header):
            in_section = True
            collected.append(line)
            continue

        if in_section and line.startswith("## ["):
            break

        if in_section:
            collected.append(line)

    if not collected:
        raise SystemExit(f"Could not find release notes for version {version} in {changelog_path}.")

    return "\n".join(collected).strip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: extract_release_notes.py <version>")

    version = sys.argv[1]
    changelog_path = pathlib.Path("CHANGELOG.md")
    sys.stdout.write(extract_release_notes(changelog_path, version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

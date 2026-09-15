#!/usr/bin/env python3
"""Check the user's Markdown style; no core validation and no file writes.

Keep these mechanical checks aligned with ../references/formatting.md. This script uses
only the standard library so the installed skill can run independently of Enso.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")


def body_lines(lines: list[str]) -> set[int]:
    """Indices outside YAML frontmatter and fenced/indented code blocks."""
    start = 0
    if lines and lines[0] == "---":
        for index, line in enumerate(lines[1:], 1):
            if line in ("---", "..."):
                start = index + 1
                break
        else:
            return set()  # A malformed header is the core validator's responsibility.
    visible = set()
    fence_char, fence_length = "", 0
    indented = False
    for index in range(start, len(lines)):
        line = lines[index]
        match = FENCE.match(line)
        if fence_char:
            if (
                match
                and match[1][0] == fence_char
                and len(match[1]) >= fence_length
                and not match[2].strip()
            ):
                fence_char, fence_length = "", 0
            continue
        if line.startswith(("    ", "\t")):
            indented = True
            continue
        if indented and not line.strip():
            continue
        indented = False
        if match and not (match[1][0] == "`" and "`" in match[2]):
            fence_char, fence_length = match[1][0], len(match[1])
        else:
            visible.add(index)
    return visible


def check_text(text: str) -> list[tuple[int, str, str]]:
    """Return deterministic one-based line, rule, message findings without rewriting."""
    findings = []
    lines = text.splitlines()
    if "\r" in text:
        line_number = text[: text.index("\r")].count("\n") + 1
        findings.append((line_number, "line-endings", "use LF line endings"))
    if text and not text.endswith("\n"):
        findings.append((max(1, len(lines)), "final-newline", "end the file with a newline"))
    visible = body_lines(lines)
    for index in sorted(visible):
        line = lines[index]
        suffix = line[len(line.rstrip(" \t")) :]
        if suffix and not (suffix == "  " and line.strip()):
            findings.append((index + 1, "trailing-whitespace", "remove trailing whitespace"))
        if not line.strip() and index - 1 in visible and not lines[index - 1].strip():
            findings.append((index + 1, "blank-lines", "use at most one blank line"))
        if HEADING.match(line):
            if index - 1 in visible and lines[index - 1].strip():
                findings.append((index + 1, "heading-spacing", "add a blank line before heading"))
            if index + 1 in visible and lines[index + 1].strip():
                findings.append((index + 1, "heading-spacing", "add a blank line after heading"))
    return sorted(findings)


def markdown_files(paths: Sequence[Path]) -> Iterable[Path]:
    """Walk supplied files/directories once, without hidden entries or symbolic links."""
    seen = set()
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"{path}: symbolic links are not checked")
        if path.is_file():
            if path.suffix.lower() != ".md":
                raise ValueError(f"{path}: expected a Markdown file")
            candidates = [path]
        elif path.is_dir():
            candidates = []
            for directory, dirs, files in os.walk(path):
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if not name.startswith(".") and not (Path(directory) / name).is_symlink()
                )
                candidates.extend(
                    Path(directory) / name
                    for name in sorted(files)
                    if not name.startswith(".")
                    and Path(name).suffix.lower() == ".md"
                    and not (Path(directory) / name).is_symlink()
                )
        else:
            raise ValueError(f"{path}: expected an existing file or directory")
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield candidate


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 when clean, 1 for style findings, 2 for invalid inputs or unreadable files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="+", type=Path, help="Markdown files or directories to check"
    )
    args = parser.parse_args(argv)
    count, failures, errors = 0, 0, 0
    try:
        for path in markdown_files(args.paths):
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError) as exc:
                print(f"{path}:1: input: {exc}")
                errors += 1
                continue
            count += 1
            for line, rule, message in check_text(text):
                print(f"{path}:{line}: {rule}: {message}")
                failures += 1
    except (OSError, ValueError) as exc:
        print(f"input: {exc}")
        errors += 1
    print(f"Checked {count} Markdown files: {failures} style findings, {errors} input errors.")
    return 2 if errors else 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

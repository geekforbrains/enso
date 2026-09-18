#!/usr/bin/env python3
"""Copy a Markdown vault into an empty knowledge root, preserving the original vault."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from enso.knowledge import normalize_text
from enso.maintenance import write_json


def _validate_destination(source: Path, destination: Path, receipt: Path) -> None:
    if not source.is_dir():
        raise ValueError("The source must be an existing directory.")
    if destination == source or destination.is_relative_to(source):
        raise ValueError("The destination must be outside the source vault.")
    if source.is_relative_to(destination):
        raise ValueError("The source must not be inside the destination.")
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError("The destination and its parents must not be symbolic links.")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("The destination must be absent or an empty directory.")
    if receipt.exists() or receipt.is_symlink():
        raise ValueError("The import receipt already exists; choose a new receipt path.")
    if receipt.is_relative_to(destination) or receipt.is_relative_to(source):
        raise ValueError("The receipt must be outside both the source and destination.")
    if any(path.is_symlink() for path in receipt.parents):
        raise ValueError("The receipt's parents must not be symbolic links.")


def _inventory(source: Path) -> tuple[list[Path], list[Path], list[str]]:
    directories: list[Path] = []
    files: list[Path] = []
    skipped: list[str] = []
    for current, child_dirs, child_files in os.walk(source, followlinks=False):
        parent = Path(current)
        retained = []
        for name in sorted(child_dirs):
            path = parent / name
            relative = path.relative_to(source)
            if name.startswith(".") or path.is_symlink():
                skipped.append(relative.as_posix())
            else:
                directories.append(relative)
                retained.append(name)
        child_dirs[:] = retained
        for name in sorted(child_files):
            path = parent / name
            relative = path.relative_to(source)
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                skipped.append(relative.as_posix())
            else:
                files.append(relative)
    return directories, files, skipped


def _open_source(source: Path, relative: Path) -> BinaryIO:
    """Open only regular files without following a replaced path component."""
    directory = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f"Source is not a regular file: {relative}")
        return os.fdopen(fd, "rb")
    finally:
        os.close(directory)


def _copy_file(source: Path, relative: Path, stage: Path) -> dict[str, Any]:
    destination = stage / relative
    source_hash = hashlib.sha256()
    target_hash = hashlib.sha256()
    # Only .md files are notes to Enso; anything else is copied byte for byte.
    markdown = relative.suffix.lower() == ".md"
    with _open_source(source, relative) as original, destination.open("xb") as target:
        before = os.fstat(original.fileno())
        if markdown:
            data = original.read()
            source_hash.update(data)
            normalized = normalize_text(data.decode("utf-8-sig")).encode("utf-8")
            target.write(normalized)
            target_hash.update(normalized)
        else:
            while data := original.read(1024 * 1024):
                source_hash.update(data)
                target_hash.update(data)
                target.write(data)
        after = os.fstat(original.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Source changed while being copied: {relative}")
        target.flush()
        os.fsync(target.fileno())
    destination.chmod(0o600)
    os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
    return {
        "path": relative.as_posix(),
        "markdown": markdown,
        "source_bytes": before.st_size,
        "source_mtime_ns": before.st_mtime_ns,
        "source_sha256": source_hash.hexdigest(),
        "imported_sha256": target_hash.hexdigest(),
    }


def import_vault(source: Path, destination: Path, receipt: Path) -> dict[str, Any]:
    """Stage the full copy, then publish atomically; never merge into an existing vault."""
    source = source.expanduser().resolve(strict=True)
    destination = Path(os.path.abspath(destination.expanduser()))
    receipt = Path(os.path.abspath(receipt.expanduser()))
    _validate_destination(source, destination, receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = None
    try:
        directories, files, skipped = _inventory(source)
        stage = Path(tempfile.mkdtemp(prefix=".knowledge-import-", dir=destination.parent))
        for relative in directories:
            (stage / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        entries = [_copy_file(source, relative, stage) for relative in files]
        report: dict[str, Any] = {
            "version": 1,
            "state": "prepared",
            "source": str(source),
            "destination": str(destination),
            "created_at": datetime.now(UTC).isoformat(),
            "files": entries,
            "skipped": skipped,
        }
        write_json(receipt, report)
        # rename refuses to replace a nonempty directory, so concurrent imports never merge.
        if destination.is_symlink():
            raise ValueError("The destination became a symbolic link during import.")
        os.rename(stage, destination)
        stage = None
        report["state"] = "complete"
        write_json(receipt, report)
        return report
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = import_vault(args.source, args.destination, args.receipt)
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "destination": report["destination"],
                "files": len(report["files"]),
                "notes": sum(entry["markdown"] for entry in report["files"]),
                "skipped": len(report["skipped"]),
                "receipt": str(args.receipt.expanduser().absolute()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

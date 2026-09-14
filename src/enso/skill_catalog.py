"""The fixed official skill catalog and non-executing, add-only home installation.

One Git commit pins the catalog, Git tree and downloaded files. Existing directories
belong to the operator; installing a skill never updates or repairs one in place.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import frontmatter, locks, skills, workspaces
from .config import Paths
from .maintenance import sync_directory

SOURCE = "geekforbrains/enso-skills"
API = f"https://api.github.com/repos/{SOURCE}"
RAW = f"https://raw.githubusercontent.com/{SOURCE}"
RECEIPT = ".enso-skill.json"
MAX_CATALOG_BYTES = 512 * 1024
MAX_TREE_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_SKILL_BYTES = 8 * 1024 * 1024
MAX_SKILLS = 128
MAX_FILES = 128
FETCH_TIMEOUT = 60.0
_NAME = re.compile(r"enso-[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA = re.compile(r"[0-9a-f]{40}")
_HASH = re.compile(r"[0-9a-f]{64}")
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class SkillError(Exception):
    """The official catalog or an installation could not safely complete."""


@dataclass(frozen=True)
class Entry:
    """A catalog entry; paths are relative to its named skill directory."""

    name: str
    description: str
    files: tuple[str, ...]
    requires: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "files": list(self.files),
            "requires": list(self.requires),
        }


@dataclass(frozen=True)
class Catalog:
    """One validated catalog and the Git tree at its resolved commit."""

    commit: str
    entries: tuple[Entry, ...]
    tree: dict[str, dict[str, Any]]


def _name(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 64 and _NAME.fullmatch(value) is not None


def _path(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 240 or not value:
        return False
    parts = value.split("/")
    return all(_PART.fullmatch(part) is not None for part in parts)


def _description(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 1024
        and bool(value.strip())
        and all(char.isprintable() or char in "\n\r\t" for char in value)
    )


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError, UnicodeError, RecursionError:
        raise SkillError(f"{label} is not valid JSON") from None
    if not isinstance(value, dict):
        raise SkillError(f"{label} must be a JSON object")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _download(url: str, limit: int, deadline: float) -> bytes:
    """Read a bounded HTTPS response; fixed-source requests never follow redirects."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SkillError("official skill download timed out")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "enso-skills/1", "Accept-Encoding": "identity"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=min(20.0, remaining)) as response:
            result = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise SkillError("official skill download timed out")
                chunk = response.read1(min(65536, limit + 1 - len(result)))
                if not chunk:
                    return bytes(result)
                result.extend(chunk)
                if len(result) > limit:
                    raise SkillError("official skill download exceeds its size limit")
    except urllib.error.HTTPError as exc:
        raise SkillError(f"official skill download failed (HTTP {exc.code})") from None
    except urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException:
        raise SkillError("official skill download failed or timed out") from None


def _entry(raw: object, index: int) -> Entry:
    label = f"catalog skills[{index}]"
    if not isinstance(raw, dict):
        raise SkillError(f"{label} must be an object")
    problems: list[str] = []
    name, description = raw.get("name"), raw.get("description")
    if not _name(name) or name in workspaces.BUNDLED_SKILLS:
        problems.append(f"{label}.name must be an unbundled enso-* name (at most 64 characters)")
    if not _description(description):
        problems.append(f"{label}.description must be nonempty text of at most 1024 characters")
    files, requires = raw.get("files"), raw.get("requires", [])
    if (
        not isinstance(files, list)
        or not 1 <= len(files) <= MAX_FILES
        or not all(_path(item) for item in files)
    ):
        problems.append(f"{label}.files must list 1-{MAX_FILES} safe relative file paths")
    elif len(set(files)) != len(files) or "SKILL.md" not in files:
        problems.append(f"{label}.files must be unique and include SKILL.md")
    elif len({item.casefold() for item in files}) != len(files) or any(
        str(parent).casefold() in {item.casefold() for item in files}
        for item in files
        for parent in PurePosixPath(item).parents
    ):
        problems.append(f"{label}.files contain colliding file paths")
    if (
        not isinstance(requires, list)
        or len(requires) > MAX_SKILLS
        or not all(isinstance(item, str) and item in workspaces.BUNDLED_SKILLS for item in requires)
    ):
        problems.append(f"{label}.requires must list bundled Enso skill names")
    elif len(set(requires)) != len(requires) or name in requires:
        problems.append(f"{label}.requires must be unique and cannot include the skill itself")
    if set(raw) - {"name", "description", "files", "requires"}:
        problems.append(f"{label} has unsupported fields")
    if problems:
        raise SkillError("; ".join(problems))
    assert isinstance(name, str) and isinstance(description, str) and isinstance(files, list)
    return Entry(name, description, tuple(files), tuple(requires))


def _entries(raw: bytes) -> tuple[Entry, ...]:
    document = _json(raw, "official skill catalog")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise SkillError("official skill catalog requires schema_version 1")
    items = document.get("skills")
    if not isinstance(items, list) or len(items) > MAX_SKILLS:
        raise SkillError(f"official skill catalog must list at most {MAX_SKILLS} skills")
    entries: list[Entry] = []
    problems: list[str] = []
    for index, raw_entry in enumerate(items):
        try:
            entries.append(_entry(raw_entry, index))
        except SkillError as exc:
            problems.append(str(exc))
    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        problems.append("official skill catalog has duplicate skill names")
    if problems:
        raise SkillError("; ".join(problems))
    return tuple(entries)


def _blob(tree: dict[str, dict[str, Any]], path: str) -> dict[str, Any]:
    entry = tree.get(path, {})
    if (
        entry.get("type") != "blob"
        or entry.get("mode") not in ("100644", "100755")
        or not isinstance(entry.get("sha"), str)
        or _SHA.fullmatch(entry["sha"]) is None
        or type(entry.get("size")) is not int
        or entry["size"] < 0
    ):
        raise SkillError(f"{path} must be a regular Git file; links and submodules are refused")
    for parent in PurePosixPath(path).parents:
        if str(parent) == ".":
            continue
        ancestor = tree.get(str(parent), {})
        if ancestor.get("type") != "tree" or ancestor.get("mode") != "040000":
            raise SkillError(f"{path} must be inside regular Git directories")
    return entry


def _file(catalog: Catalog, path: str, limit: int, deadline: float) -> bytes:
    blob = _blob(catalog.tree, path)
    if blob["size"] > limit:
        raise SkillError(f"{path} exceeds its size limit")
    raw = _download(f"{RAW}/{catalog.commit}/{path}", limit, deadline)
    # Verify the raw response against the regular blob in the pinned Git tree.
    digest = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    if len(raw) != blob["size"] or digest != blob["sha"]:
        raise SkillError(f"{path} does not match the pinned Git tree")
    return raw


def _catalog(deadline: float) -> Catalog:
    commit = _json(_download(f"{API}/commits/main", MAX_CATALOG_BYTES, deadline), "Git commit")
    sha = commit.get("sha")
    try:
        tree_sha = commit["commit"]["tree"]["sha"]
    except KeyError, TypeError:
        raise SkillError("official repository returned an invalid Git commit") from None
    if not all(isinstance(value, str) and _SHA.fullmatch(value) for value in (sha, tree_sha)):
        raise SkillError("official repository returned an invalid Git commit")
    assert isinstance(sha, str)
    tree = _json(
        _download(f"{API}/git/trees/{tree_sha}?recursive=1", MAX_TREE_BYTES, deadline), "Git tree"
    )
    entries = tree.get("tree")
    if (
        tree.get("sha") != tree_sha
        or tree.get("truncated") is not False
        or not isinstance(entries, list)
    ):
        raise SkillError("official repository returned an incomplete Git tree")
    index: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SkillError("official repository returned an invalid Git tree")
        if entry["path"] in index:
            raise SkillError("official repository returned duplicate Git tree paths")
        index[entry["path"]] = entry
    catalog = Catalog(sha, (), index)
    parsed = _entries(_file(catalog, "catalog.json", MAX_CATALOG_BYTES, deadline))
    for skill in parsed:
        for relative in skill.files:
            _blob(index, f"{skill.name}/{relative}")
    return Catalog(sha, parsed, index)


def available(paths: Paths) -> dict[str, Any]:
    """Fetch the fixed official catalog, resolving main once to an immutable commit."""
    catalog = _catalog(time.monotonic() + FETCH_TIMEOUT)
    return {
        "source": SOURCE,
        "commit": catalog.commit,
        "skills": [
            {**entry.as_dict(), "installed": os.path.lexists(paths.skills / entry.name)}
            for entry in catalog.entries
        ],
    }


def _find(catalog: Catalog, name: str) -> Entry:
    for entry in catalog.entries:
        if entry.name == name:
            return entry
    raise SkillError(f"{name} is not in the official catalog; use enso skill list --available")


def _validate_name(name: str) -> None:
    if not _name(name):
        raise SkillError("skill names must be lowercase enso-* names of at most 64 characters")
    if name in workspaces.BUNDLED_SKILLS:
        raise SkillError(
            f"{name} is bundled with Enso; use enso init to seed missing bundled skills"
        )


def show(name: str) -> dict[str, Any]:
    """Show one available skill's metadata and its pinned source."""
    _validate_name(name)
    catalog = _catalog(time.monotonic() + FETCH_TIMEOUT)
    return {"source": SOURCE, "commit": catalog.commit, **_find(catalog, name).as_dict()}


def _receipt(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            return None
        fd = os.open(path / RECEIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            raw = file.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            return None
        value = _json(raw, "skill receipt")
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("source") != SOURCE
            or value.get("name") != path.name
            or not _name(value.get("name"))
            or not isinstance(value.get("commit"), str)
            or _SHA.fullmatch(value["commit"]) is None
        ):
            return None
        files = value.get("files")
        if (
            not isinstance(files, dict)
            or not 1 <= len(files) <= MAX_FILES
            or "SKILL.md" not in files
            or not all(
                _path(name) and isinstance(digest, str) and _HASH.fullmatch(digest)
                for name, digest in files.items()
            )
        ):
            return None
        return value
    except OSError, SkillError:
        return None


def official_installation(path: Path) -> bool:
    """Recognize recorded official origin offline, including later operator edits.

    This is provenance for the reserved-name audit, not permission to overwrite files.
    """
    return _receipt(path) is not None


def installed(paths: Paths) -> list[dict[str, Any]]:
    """List home skills without configuration, database access, network or user-scope reads."""
    result = []
    for skill in skills.resolve(paths, user_dirs=[]):
        receipt = _receipt(skill.path)
        origin = "bundled" if skill.name in workspaces.BUNDLED_SKILLS else "manual"
        if receipt is not None:
            origin = "official"
        result.append(
            {
                **skill.as_dict(),
                "origin": origin,
                "source": SOURCE if receipt else None,
                "commit": receipt["commit"] if receipt else None,
            }
        )
    return result


def _validate_skill(raw: bytes, entry: Entry) -> None:
    try:
        document, problem = frontmatter.parse(raw.decode("utf-8"))
    except UnicodeError:
        raise SkillError(f"{entry.name}/SKILL.md must be UTF-8") from None
    except RecursionError:
        raise SkillError(f"{entry.name}/SKILL.md frontmatter is nested too deeply") from None
    if document is None:
        raise SkillError(f"{entry.name}/SKILL.md {problem}")
    fields = document.fields
    problems = []
    if fields.get("name") != entry.name:
        problems.append("name must match its catalog and directory name")
    if not _description(fields.get("description")):
        problems.append("description must be nonempty text of at most 1024 characters")
    elif fields["description"] != entry.description:
        problems.append("description must match the catalog")
    if set(fields) - {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
    }:
        problems.append("frontmatter has unsupported fields; use metadata for additional fields")
    for key in ("license", "allowed-tools"):
        if key in fields and not isinstance(fields[key], str):
            problems.append(f"{key} must be text")
    if "compatibility" in fields and (
        not isinstance(fields["compatibility"], str) or not 1 <= len(fields["compatibility"]) <= 500
    ):
        problems.append("compatibility must be text of 1-500 characters")
    if "metadata" in fields and (
        not isinstance(fields["metadata"], dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields["metadata"].items())
    ):
        problems.append("metadata must map text keys to text values")
    if problems:
        raise SkillError(f"{entry.name}/SKILL.md: " + "; ".join(problems))


def _no_links(path: Path) -> None:
    for entry in (path.absolute(), *path.absolute().parents):
        if entry.is_symlink():
            raise SkillError(f"skill installation path must not contain a symbolic link: {entry}")


@contextmanager
def _install_lock(paths: Paths) -> Iterator[None]:
    _no_links(paths.skills)
    paths.skills.mkdir(parents=True, exist_ok=True)
    try:
        fd = locks.acquire(paths.skill_lock)
    except locks.LockPathError as exc:
        raise SkillError(f"the skill installation {exc}") from None
    except BlockingIOError:
        raise SkillError("another skill installation is in progress") from None
    try:
        yield
    finally:
        os.close(fd)


def _destination(paths: Paths, name: str) -> Path:
    target = paths.skills / name
    _no_links(target)
    if target.exists():
        raise SkillError(f"{name} already exists; existing skills are never overwritten")
    return target


def _required(paths: Paths, entry: Entry) -> None:
    usable = {skill.name for skill in skills.resolve(paths, user_dirs=[]) if skill.ok}
    missing = [name for name in entry.requires if name not in usable]
    if missing:
        raise SkillError("required home skills are missing or invalid: " + ", ".join(missing))


def _stage_file(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as file:
        file.write(raw)
        file.flush()
        os.fchmod(file.fileno(), mode)
        os.fsync(file.fileno())


def install(paths: Paths, name: str) -> dict[str, Any]:
    """Download and atomically add one official skill, preserving every existing destination."""
    # The selected home may use an OS alias such as macOS /tmp. Resolve that root
    # once; directories and destinations below it still must not be symbolic links.
    paths = Paths(paths.home.resolve())
    _validate_name(name)
    _destination(paths, name)
    deadline = time.monotonic() + FETCH_TIMEOUT
    catalog = _catalog(deadline)
    entry = _find(catalog, name)
    _required(paths, entry)
    contents: dict[str, bytes] = {}
    total = 0
    for relative in entry.files:
        raw = _file(catalog, f"{name}/{relative}", MAX_FILE_BYTES, deadline)
        total += len(raw)
        if total > MAX_SKILL_BYTES:
            raise SkillError(f"{name} exceeds the skill size limit")
        contents[relative] = raw
    _validate_skill(contents["SKILL.md"], entry)
    receipt = {
        "schema_version": 1,
        "source": SOURCE,
        "commit": catalog.commit,
        "name": name,
        "files": {relative: hashlib.sha256(raw).hexdigest() for relative, raw in contents.items()},
    }
    with _install_lock(paths):
        target = _destination(paths, name)
        _required(paths, entry)
        with tempfile.TemporaryDirectory(dir=paths.skills, prefix=".install-") as temporary:
            staged = Path(temporary) / name
            staged.mkdir()
            for relative, raw in contents.items():
                file = staged / relative
                blob = _blob(catalog.tree, f"{name}/{relative}")
                _stage_file(file, raw, 0o755 if blob["mode"] == "100755" else 0o644)
            _stage_file(staged / RECEIPT, (json.dumps(receipt, indent=2) + "\n").encode(), 0o600)
            for directory, _, _ in os.walk(staged, topdown=False):
                sync_directory(Path(directory))
            _destination(paths, name)
            os.rename(staged, target)
            sync_directory(paths.skills)
    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "source": SOURCE,
        "commit": catalog.commit,
        "files": list(entry.files),
        "requires": list(entry.requires),
    }

"""Canonical, conservative filesystem scaffolding for Enso content.

Seeding and repair are intentionally separate operations.  Fresh setup and
workspace creation may copy bundled user-owned content once; repair owns only
directories and discovery links and never recreates or rewrites that content.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import importlib.resources
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

_DIRECTORY_MODE = 0o700
_CONTENT_MODE = 0o600
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_STARTER_DOC_MAPPINGS: tuple[
    tuple[tuple[str, ...], tuple[str, ...]],
    ...,
] = (
    (("enso", "content_model.md"), ("docs", "enso", "content_model.md")),
    (("enso", "layout.md"), ("docs", "enso", "layout.md")),
    (("operator.md",), ("docs", "operator.md")),
)


class ScaffoldError(RuntimeError):
    """A canonical scaffold cannot be created or repaired safely."""


class LinkState(str, Enum):
    """Result of reconciling one managed discovery link."""

    CREATED = "created"
    CORRECT = "correct"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ManagedLinkResult:
    """Observed outcome for a managed relative symlink."""

    path: Path
    target: str
    state: LinkState
    warning: str | None = None


@dataclass(frozen=True)
class ScaffoldReport:
    """Changes and non-destructive conflicts found by a scaffold operation."""

    workspace: Path | None = None
    created: tuple[Path, ...] = ()
    links: tuple[ManagedLinkResult, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationReport:
    """Read-only structural validation result."""

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


class _Resource(Protocol):
    """Small subset of ``importlib.resources.abc.Traversable`` we consume."""

    @property
    def name(self) -> str: ...

    def is_dir(self) -> bool: ...

    def is_file(self) -> bool: ...

    def iterdir(self) -> Iterator[_Resource]: ...

    def read_bytes(self) -> bytes: ...

    def read_text(self, encoding: str | None = None) -> str: ...


def _validated_workspace_name(name: str) -> str:
    """Adapt the shared configuration error into this service's error type."""
    from .config import ConfigError, validate_workspace_name

    try:
        return validate_workspace_name(name)
    except ConfigError as exc:
        raise ScaffoldError(str(exc)) from exc


def _publish_exclusive(source: Path, destination: Path) -> None:
    """Atomically rename a directory without replacing any destination.

    POSIX ``rename`` may replace an existing empty directory, which violates
    workspace creation's refuse-existing contract.  Linux and macOS both
    expose a no-replace extension; failing closed on other platforms is safer
    than publishing with overwrite semantics.
    """
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is not None:
        result = renameat2(
            _AT_FDCWD,
            ctypes.c_char_p(source_bytes),
            _AT_FDCWD,
            ctypes.c_char_p(destination_bytes),
            _RENAME_NOREPLACE,
        )
    else:
        renamex = getattr(library, "renamex_np", None)
        if renamex is None:
            raise OSError(
                errno.ENOTSUP,
                "exclusive atomic directory publication is not supported",
                os.fspath(destination),
            )
        result = renamex(
            ctypes.c_char_p(source_bytes),
            ctypes.c_char_p(destination_bytes),
            _RENAME_EXCL,
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            os.fspath(destination),
        )


class ScaffoldService:
    """Create, repair, and validate the canonical Enso content tree."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        if root is None:
            from . import config as config_module

            root = config_module.CONFIG_DIR
        self.root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))

    @property
    def workspaces_root(self) -> Path:
        return self.root / "workspaces"

    def workspace_path(self, name: str) -> Path:
        """Return this service's validated, name-derived workspace path."""
        from .config import CONFIG_DIR, managed_workspace_path

        name = _validated_workspace_name(name)
        configured_root = Path(os.path.abspath(os.path.expanduser(CONFIG_DIR)))
        if self.root == configured_root:
            return Path(managed_workspace_path(name))
        return self.workspaces_root / name

    def seed_fresh_global(self) -> ScaffoldReport:
        """Seed bundled global content during an explicit fresh setup only.

        Existing regular files are user-owned and remain byte-for-byte intact.
        Missing bundled files are copied with exclusive, non-following creates;
        a symlink or non-regular collision fails rather than being traversed.
        """
        created: list[Path] = []
        self._ensure_global_directories(created)

        prompts = importlib.resources.files("enso").joinpath("prompts")
        self._seed_resource_file(prompts.joinpath("AGENTS.md"), self.root / "AGENTS.md", created)

        bundled_skills = importlib.resources.files("enso").joinpath("skills")
        if not bundled_skills.is_dir():
            raise ScaffoldError("bundled Enso skills resource is missing")
        self._seed_resource_tree(bundled_skills, self.root / "skills", created)

        links, warnings = self._reconcile_global_links()
        created.extend(link.path for link in links if link.state is LinkState.CREATED)
        return ScaffoldReport(
            created=tuple(created),
            links=tuple(links),
            warnings=tuple(warnings),
        )

    def repair_global(self) -> ScaffoldReport:
        """Repair global directories and links without seeding content."""
        created: list[Path] = []
        self._ensure_global_directories(created)
        links, warnings = self._reconcile_global_links()
        created.extend(link.path for link in links if link.state is LinkState.CREATED)
        return ScaffoldReport(
            created=tuple(created),
            links=tuple(links),
            warnings=tuple(warnings),
        )

    def seed_fresh_starter_docs(self) -> ScaffoldReport:
        """Seed the fixed starter documents during explicit fresh setup only.

        Existing regular files are user-owned and remain byte-for-byte intact,
        so an interrupted setup rerun fills only the missing documents.
        Missing documents are copied with exclusive, non-following creates; a
        symlink or non-regular collision fails rather than being traversed.
        """
        bundled_root = importlib.resources.files("enso").joinpath("starter_docs")
        created: list[Path] = []
        self._ensure_root(created)
        for source_parts, destination_parts in _STARTER_DOC_MAPPINGS:
            parent = self.root
            for part in destination_parts[:-1]:
                parent = parent / part
                self._ensure_physical_directory(parent, create=True, created=created)
            self._seed_resource_file(
                bundled_root.joinpath(*source_parts),
                self.root.joinpath(*destination_parts),
                created,
            )
        return ScaffoldReport(created=tuple(created))

    def create_workspace(self, name: str) -> ScaffoldReport:
        """Build a complete new workspace beside its destination and publish it."""
        name = _validated_workspace_name(name)
        created_global: list[Path] = []
        self._ensure_root(created_global)
        self._ensure_physical_directory(self.workspaces_root, create=True, created=created_global)
        destination = self.workspace_path(name)
        if os.path.lexists(destination):
            raise ScaffoldError(f"workspace destination already exists: {destination}")

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{name}.tmp-",
                dir=self.workspaces_root,
            )
        )
        try:
            staged_created, staged_links = self._seed_workspace(staging, name)
            if os.path.lexists(destination):
                raise ScaffoldError(f"workspace destination already exists: {destination}")
            try:
                _publish_exclusive(staging, destination)
            except OSError as exc:
                raise ScaffoldError(f"could not publish workspace {name!r}: {exc}") from exc
        except BaseException:
            if os.path.lexists(staging):
                shutil.rmtree(staging)
            raise

        def published(path: Path) -> Path:
            return destination / path.relative_to(staging)

        links = tuple(
            ManagedLinkResult(
                path=published(link.path),
                target=link.target,
                state=link.state,
                warning=link.warning,
            )
            for link in staged_links
        )
        created = [*created_global, destination]
        created.extend(published(path) for path in staged_created if path != staging)
        return ScaffoldReport(
            workspace=destination,
            created=tuple(created),
            links=links,
        )

    def repair_workspace(self, name: str) -> ScaffoldReport:
        """Repair structural workspace paths while preserving all content."""
        name = _validated_workspace_name(name)
        created: list[Path] = []
        self._require_global_container()
        workspace = self.workspace_path(name)
        self._ensure_physical_directory(workspace, create=False, created=created)
        self._reject_workspace_git_entry(workspace)

        for relative in ("skills", "knowledge", "drafts", "uploads", ".agents", ".claude"):
            self._ensure_physical_directory(
                workspace / relative,
                create=True,
                created=created,
            )

        links, warnings = self._reconcile_workspace_links(workspace)
        created.extend(link.path for link in links if link.state is LinkState.CREATED)
        knowledge_index = workspace / "knowledge" / "README.md"
        if not self._is_regular_file(knowledge_index):
            warnings.append(
                f"seeded content {knowledge_index} is missing or is not a physical regular "
                "file; preserving it without recreation"
            )
        duplicates = self.duplicate_skill_names(name)
        if duplicates:
            warnings.append(self._duplicate_skill_message(name, duplicates))
        return ScaffoldReport(
            workspace=workspace,
            created=tuple(created),
            links=tuple(links),
            warnings=tuple(warnings),
        )

    def duplicate_skill_names(self, name: str) -> tuple[str, ...]:
        """Return skill directory names present at both global and workspace scope."""
        workspace = self.workspace_path(name)
        global_skills = self.root / "skills"
        global_names = (
            self._physical_child_directories(global_skills)
            if os.path.lexists(global_skills)
            else set()
        )
        workspace_names = self._physical_child_directories(workspace / "skills")
        return tuple(sorted(global_names & workspace_names))

    def validate_global(self) -> ValidationReport:
        """Validate global structure without creating or repairing anything."""
        errors: list[str] = []
        for path in (
            self.root,
            self.workspaces_root,
            self.root / "docs",
            self.root / "jobs",
            self.root / "skills",
            self.root / ".agents",
            self.root / ".claude",
        ):
            self._validate_physical_directory(path, errors)
        self._validate_regular_content(self.root / "AGENTS.md", errors)
        self._validate_link(self.root / "CLAUDE.md", "AGENTS.md", errors)
        self._validate_link(self.root / ".agents" / "skills", "../skills", errors)
        self._validate_link(self.root / ".claude" / "skills", "../skills", errors)
        try:
            self._validate_skill_scope(self.root / "skills")
        except ScaffoldError as exc:
            errors.append(str(exc))
        return ValidationReport(errors=tuple(errors))

    def validate_workspace(self, name: str) -> ValidationReport:
        """Validate a managed workspace and root/workspace skill uniqueness."""
        name = _validated_workspace_name(name)
        errors: list[str] = []
        workspace = self.workspace_path(name)
        self._validate_physical_directory(self.root, errors)
        self._validate_physical_directory(self.workspaces_root, errors)
        self._validate_physical_directory(workspace, errors)
        if os.path.lexists(workspace / ".git"):
            errors.append(
                f"workspace {name!r} has a forbidden .git entry at {workspace / '.git'}"
            )
        for relative in ("skills", "knowledge", "drafts", "uploads", ".agents", ".claude"):
            self._validate_physical_directory(workspace / relative, errors)
        self._validate_regular_content(workspace / "AGENTS.md", errors)
        self._validate_link(workspace / "CLAUDE.md", "AGENTS.md", errors)
        self._validate_link(workspace / ".agents" / "skills", "../skills", errors)
        self._validate_link(workspace / ".claude" / "skills", "../skills", errors)
        try:
            self._validate_skill_scope(workspace / "skills")
        except ScaffoldError as exc:
            errors.append(str(exc))

        try:
            duplicates = self.duplicate_skill_names(name)
        except ScaffoldError as exc:
            errors.append(str(exc))
        else:
            if duplicates:
                errors.append(self._duplicate_skill_message(name, duplicates))
        return ValidationReport(errors=tuple(errors))

    def _ensure_root(self, created: list[Path]) -> None:
        if not os.path.lexists(self.root):
            parent = self.root.parent
            self._ensure_physical_directory(parent, create=False, created=[])
        self._ensure_physical_directory(self.root, create=True, created=created)

    def _ensure_global_directories(self, created: list[Path]) -> None:
        self._ensure_root(created)
        for relative in ("docs", "jobs", "skills", "workspaces", ".agents", ".claude"):
            self._ensure_physical_directory(
                self.root / relative,
                create=True,
                created=created,
            )

    def _require_global_container(self) -> None:
        self._ensure_physical_directory(self.root, create=False, created=[])
        self._ensure_physical_directory(self.workspaces_root, create=False, created=[])

    @staticmethod
    def _ensure_physical_directory(
        path: Path,
        *,
        create: bool,
        created: list[Path],
    ) -> None:
        absolute = Path(os.path.abspath(path))
        if os.path.lexists(absolute):
            try:
                path_stat = os.lstat(absolute)
            except OSError as exc:
                raise ScaffoldError(f"could not inspect managed path {absolute}: {exc}") from exc
            if not stat.S_ISDIR(path_stat.st_mode) or os.path.realpath(absolute) != str(absolute):
                raise ScaffoldError(f"managed path must be a physical directory: {absolute}")
            return
        if not create:
            raise ScaffoldError(f"managed physical directory is missing: {absolute}")
        try:
            os.mkdir(absolute, mode=_DIRECTORY_MODE)
        except FileExistsError:
            ScaffoldService._ensure_physical_directory(
                absolute,
                create=False,
                created=created,
            )
        except OSError as exc:
            raise ScaffoldError(f"could not create managed directory {absolute}: {exc}") from exc
        else:
            created.append(absolute)

    def _seed_workspace(
        self,
        staging: Path,
        name: str,
    ) -> tuple[list[Path], list[ManagedLinkResult]]:
        created = [staging]
        for relative in ("skills", "knowledge", "drafts", "uploads", ".agents", ".claude"):
            self._ensure_physical_directory(
                staging / relative,
                create=True,
                created=created,
            )

        prompts = importlib.resources.files("enso").joinpath("prompts")
        workspace_prompt = prompts.joinpath("WORKSPACE_AGENTS.md")
        if not workspace_prompt.is_file():
            raise ScaffoldError("bundled workspace instruction resource is missing")
        self._create_exclusive_text(
            staging / "AGENTS.md",
            self._workspace_instructions(workspace_prompt, name),
        )
        created.append(staging / "AGENTS.md")

        knowledge_source = prompts.joinpath("WORKSPACE_KNOWLEDGE_README.md")
        self._seed_resource_file(
            knowledge_source,
            staging / "knowledge" / "README.md",
            created,
        )

        links = [
            self._ensure_managed_link(staging / "CLAUDE.md", "AGENTS.md"),
            self._ensure_managed_link(staging / ".agents" / "skills", "../skills"),
            self._ensure_managed_link(staging / ".claude" / "skills", "../skills"),
        ]
        created.extend(link.path for link in links if link.state is LinkState.CREATED)
        return created, links

    @staticmethod
    def _workspace_instructions(source: _Resource, name: str) -> str:
        template = source.read_text(encoding="utf-8")
        if "{{workspace_name}}" not in template:
            raise ScaffoldError(
                "bundled workspace instruction resource is missing its "
                "{{workspace_name}} placeholder"
            )
        return template.replace("{{workspace_name}}", name)

    def _seed_resource_tree(
        self,
        source: _Resource,
        destination: Path,
        created: list[Path],
    ) -> None:
        if not source.is_dir():
            raise ScaffoldError(f"bundled directory resource is missing: {source.name}")
        self._ensure_physical_directory(destination, create=True, created=created)
        for child in sorted(source.iterdir(), key=lambda entry: entry.name):
            target = destination / child.name
            if child.is_dir():
                self._seed_resource_tree(child, target, created)
            elif child.is_file():
                self._seed_resource_file(child, target, created)

    def _seed_resource_file(
        self,
        source: _Resource,
        destination: Path,
        created: list[Path],
    ) -> None:
        if not source.is_file():
            raise ScaffoldError(f"bundled file resource is missing: {source.name}")
        if os.path.lexists(destination):
            try:
                destination_stat = os.lstat(destination)
            except OSError as exc:
                raise ScaffoldError(f"could not inspect seed path {destination}: {exc}") from exc
            if not stat.S_ISREG(destination_stat.st_mode):
                raise ScaffoldError(
                    f"seed path {destination} must be a regular file, not a symlink or directory"
                )
            return
        self._create_exclusive_bytes(destination, source.read_bytes())
        created.append(destination)

    @staticmethod
    def _stat_identity(file_stat: os.stat_result) -> tuple[int, int]:
        return file_stat.st_dev, file_stat.st_ino

    @staticmethod
    def _create_exclusive_text(path: Path, content: str) -> None:
        ScaffoldService._create_exclusive_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _create_exclusive_bytes(path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, _CONTENT_MODE)
        except OSError as exc:
            raise ScaffoldError(f"could not create seed file {path} exclusively: {exc}") from exc
        try:
            with os.fdopen(descriptor, "wb") as seed_file:
                seed_file.write(content)
                seed_file.flush()
                os.fsync(seed_file.fileno())
        except BaseException as exc:
            with contextlib.suppress(OSError):
                os.unlink(path)
            if isinstance(exc, OSError):
                raise ScaffoldError(f"could not write seed file {path}: {exc}") from exc
            raise

    def _reconcile_global_links(
        self,
    ) -> tuple[list[ManagedLinkResult], list[str]]:
        links: list[ManagedLinkResult] = []
        warnings: list[str] = []
        agents = self.root / "AGENTS.md"
        self._reconcile_content_link(
            agents,
            self.root / "CLAUDE.md",
            "AGENTS.md",
            links,
            warnings,
        )
        for parent in (".agents", ".claude"):
            result = self._ensure_managed_link(self.root / parent / "skills", "../skills")
            links.append(result)
            if result.warning:
                warnings.append(result.warning)
        return links, warnings

    def _reconcile_workspace_links(
        self,
        workspace: Path,
    ) -> tuple[list[ManagedLinkResult], list[str]]:
        links: list[ManagedLinkResult] = []
        warnings: list[str] = []
        self._reconcile_content_link(
            workspace / "AGENTS.md",
            workspace / "CLAUDE.md",
            "AGENTS.md",
            links,
            warnings,
        )
        for parent in (".agents", ".claude"):
            result = self._ensure_managed_link(workspace / parent / "skills", "../skills")
            links.append(result)
            if result.warning:
                warnings.append(result.warning)
        return links, warnings

    def _reconcile_content_link(
        self,
        source: Path,
        link_path: Path,
        target: str,
        links: list[ManagedLinkResult],
        warnings: list[str],
    ) -> None:
        if not self._is_regular_file(source):
            warning = (
                f"content source {source} is missing or is not a physical regular file; "
                f"preserving {link_path} and not creating a discovery link"
            )
            warnings.append(warning)
            if os.path.lexists(link_path):
                result = self._inspect_managed_link(link_path, target)
                links.append(result)
                if result.warning:
                    warnings.append(result.warning)
            return
        result = self._ensure_managed_link(link_path, target)
        links.append(result)
        if result.warning:
            warnings.append(result.warning)

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            return stat.S_ISREG(os.lstat(path).st_mode)
        except OSError:
            return False

    @staticmethod
    def _ensure_managed_link(path: Path, target: str) -> ManagedLinkResult:
        if os.path.isabs(target):
            raise ScaffoldError(f"managed link target must be relative: {target}")
        if os.path.lexists(path):
            return ScaffoldService._inspect_managed_link(path, target)
        try:
            os.symlink(target, path)
        except FileExistsError:
            return ScaffoldService._inspect_managed_link(path, target)
        except OSError as exc:
            raise ScaffoldError(f"could not create managed link {path} -> {target}: {exc}") from exc
        return ManagedLinkResult(path=path, target=target, state=LinkState.CREATED)

    @staticmethod
    def _inspect_managed_link(path: Path, target: str) -> ManagedLinkResult:
        if os.path.islink(path):
            try:
                actual = os.readlink(path)
            except OSError as exc:
                warning = f"could not inspect managed link {path}: {exc}"
            else:
                if actual == target:
                    return ManagedLinkResult(path=path, target=target, state=LinkState.CORRECT)
                warning = (
                    f"preserving unmanaged link {path} -> {actual}; "
                    f"expected relative target {target}"
                )
        else:
            warning = f"preserving unexpected path {path}; expected relative symlink to {target}"
        return ManagedLinkResult(
            path=path,
            target=target,
            state=LinkState.CONFLICT,
            warning=warning,
        )

    @staticmethod
    def _reject_workspace_git_entry(workspace: Path) -> None:
        git_entry = workspace / ".git"
        if os.path.lexists(git_entry):
            raise ScaffoldError(
                f"managed workspace root may not contain a .git entry: {git_entry}"
            )

    @staticmethod
    def _effective_permission_bits(file_stat: os.stat_result) -> int:
        """Return the rwx bits that apply to the current effective user."""
        if file_stat.st_uid == os.geteuid():
            return (stat.S_IMODE(file_stat.st_mode) >> 6) & 0o7
        groups = {*os.getgroups(), os.getegid()}
        if file_stat.st_gid in groups:
            return (stat.S_IMODE(file_stat.st_mode) >> 3) & 0o7
        return stat.S_IMODE(file_stat.st_mode) & 0o7

    @classmethod
    def _require_readable_regular_file(cls, path: Path, *, description: str) -> None:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError as exc:
            raise ScaffoldError(f"{description} is missing: {path}") from exc
        except OSError as exc:
            raise ScaffoldError(f"could not inspect {description} {path}: {exc}") from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise ScaffoldError(f"{description} must be a physical regular file: {path}")
        if not cls._effective_permission_bits(path_stat) & 0o4:
            raise ScaffoldError(f"{description} is not readable by current user: {path}")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ScaffoldError(f"{description} is not safely readable: {path}: {exc}") from exc
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or cls._stat_identity(opened_stat) != cls._stat_identity(path_stat)
            ):
                raise ScaffoldError(f"{description} changed while it was inspected: {path}")
            try:
                final_path_stat = os.lstat(path)
            except OSError as exc:
                raise ScaffoldError(
                    f"{description} changed while it was inspected: {path}"
                ) from exc
            if cls._stat_identity(opened_stat) != cls._stat_identity(final_path_stat):
                raise ScaffoldError(f"{description} changed while it was inspected: {path}")
        finally:
            os.close(descriptor)

    @classmethod
    def _physical_child_directories(cls, parent: Path) -> set[str]:
        """Enumerate physical skill directories or fail on inaccessible state."""
        try:
            parent_stat = os.lstat(parent)
        except FileNotFoundError as exc:
            raise ScaffoldError(f"skill directory is missing: {parent}") from exc
        except OSError as exc:
            raise ScaffoldError(f"could not inspect skill directory {parent}: {exc}") from exc
        if not stat.S_ISDIR(parent_stat.st_mode) or os.path.realpath(parent) != str(parent):
            raise ScaffoldError(f"skill directory must be a physical directory: {parent}")
        if cls._effective_permission_bits(parent_stat) & 0o5 != 0o5:
            raise ScaffoldError(
                f"skill directory is not readable and searchable by current user: {parent}"
            )

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(parent, flags)
        except OSError as exc:
            raise ScaffoldError(
                f"skill directory is not safely readable and searchable: {parent}: {exc}"
            ) from exc
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened_stat.st_mode)
                or cls._stat_identity(opened_stat) != cls._stat_identity(parent_stat)
            ):
                raise ScaffoldError(f"skill directory changed while inspected: {parent}")

            names: set[str] = set()
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        if not entry.is_dir(follow_symlinks=True):
                            continue
                        child = parent / entry.name
                        child_stat = os.lstat(child)
                        if (
                            not stat.S_ISDIR(child_stat.st_mode)
                            or os.path.realpath(child) != str(child)
                        ):
                            raise ScaffoldError(
                                f"skill must be a physical directory: {child}"
                            )
                        if cls._effective_permission_bits(child_stat) & 0o5 != 0o5:
                            raise ScaffoldError(
                                "skill directory is not readable and searchable by "
                                f"current user: {child}"
                            )
                        names.add(entry.name)
            except ScaffoldError:
                raise
            except OSError as exc:
                raise ScaffoldError(f"could not enumerate skill directory {parent}: {exc}") from exc

            final_stat = os.fstat(descriptor)
            try:
                final_path_stat = os.lstat(parent)
            except OSError as exc:
                raise ScaffoldError(f"skill directory changed while inspected: {parent}") from exc
            if (
                cls._stat_identity(opened_stat) != cls._stat_identity(final_stat)
                or cls._stat_identity(final_stat) != cls._stat_identity(final_path_stat)
            ):
                raise ScaffoldError(f"skill directory changed while inspected: {parent}")
            return names
        finally:
            os.close(descriptor)

    @classmethod
    def _validate_skill_scope(cls, parent: Path) -> None:
        for name in cls._physical_child_directories(parent):
            cls._require_readable_regular_file(
                parent / name / "SKILL.md",
                description="skill definition",
            )

    @staticmethod
    def _duplicate_skill_message(name: str, duplicates: tuple[str, ...]) -> str:
        rendered = ", ".join(duplicates)
        return (
            f"workspace {name!r} has duplicate global/workspace skill names: {rendered}; "
            "rename or remove one scope before launch"
        )

    @staticmethod
    def _validate_physical_directory(path: Path, errors: list[str]) -> None:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            errors.append(f"required physical directory is missing: {path}")
        except OSError as exc:
            errors.append(f"could not inspect required directory {path}: {exc}")
        else:
            if not stat.S_ISDIR(path_stat.st_mode) or os.path.realpath(path) != str(path):
                errors.append(f"required path is not a physical directory: {path}")

    @classmethod
    def _validate_regular_content(cls, path: Path, errors: list[str]) -> None:
        try:
            cls._require_readable_regular_file(
                path,
                description="required instruction source",
            )
        except ScaffoldError as exc:
            errors.append(str(exc))

    @staticmethod
    def _validate_link(path: Path, target: str, errors: list[str]) -> None:
        if not os.path.islink(path):
            errors.append(f"required discovery link is missing or not a symlink: {path}")
            return
        try:
            actual = os.readlink(path)
        except OSError as exc:
            errors.append(f"could not inspect discovery link {path}: {exc}")
            return
        if actual != target:
            errors.append(
                f"discovery link {path} targets {actual!r}; expected relative target {target!r}"
            )

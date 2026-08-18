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
import secrets
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


@dataclass(frozen=True)
class _DirectoryHandle:
    """One held directory and the held parent/name that must still reach it."""

    path: Path
    descriptor: int
    identity: tuple[int, int]
    parent: _DirectoryHandle | None = None
    name: str | None = None


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


def _publish_exclusive_at(
    source: str,
    destination: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically move one relative file without replacing a destination."""
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is not None:
        result = renameat2(
            src_dir_fd,
            ctypes.c_char_p(source_bytes),
            dst_dir_fd,
            ctypes.c_char_p(destination_bytes),
            _RENAME_NOREPLACE,
        )
    else:
        renameatx = getattr(library, "renameatx_np", None)
        if renameatx is None:
            raise OSError(
                errno.ENOTSUP,
                "exclusive atomic file publication is not supported",
                destination,
            )
        result = renameatx(
            src_dir_fd,
            ctypes.c_char_p(source_bytes),
            dst_dir_fd,
            ctypes.c_char_p(destination_bytes),
            _RENAME_EXCL,
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


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

        An exact existing copy is accepted as an interrupted-setup retry.  Any
        customized, linked, or non-regular destination is a collision and is
        left untouched.
        """
        bundled_root = importlib.resources.files("enso").joinpath("starter_docs")
        documents: list[tuple[tuple[str, ...], Path, bytes]] = []
        for source_parts, destination_parts in _STARTER_DOC_MAPPINGS:
            source = bundled_root.joinpath(*source_parts)
            if not source.is_file():
                raise ScaffoldError(
                    f"bundled starter document resource is missing: {'/'.join(source_parts)}"
                )
            documents.append(
                (destination_parts, self.root.joinpath(*destination_parts), source.read_bytes())
            )

        created: list[Path] = []
        with self._open_starter_doc_directories(created) as (staging, parents):
            pending: list[tuple[_DirectoryHandle, Path, bytes]] = []
            for destination_parts, destination, content in documents:
                parent = parents[destination_parts[:-1]]
                existing = self._read_starter_doc_at(parent, destination)
                if existing is None:
                    pending.append((parent, destination, content))
                elif existing != content:
                    raise ScaffoldError(
                        f"starter document {destination} collision: existing bytes differ "
                        "from the bundled document"
                    )

            for parent, destination, content in pending:
                if self._create_atomic_starter_doc(staging, parent, destination, content):
                    created.append(destination)
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
        global_names = self._physical_child_directories(self.root / "skills")
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

        if not errors:
            duplicates = self.duplicate_skill_names(name)
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
        if "{{workspace_name}}" in template:
            return template.replace("{{workspace_name}}", name)
        lines = template.splitlines(keepends=True)
        if lines and lines[0].startswith("# "):
            lines[0] = f"# {name} workspace\n"
        else:
            lines.insert(0, f"# {name} workspace\n\n")
        charter = (
            "\nThis managed workspace is named `"
            + name
            + "`. If its purpose and scope or critical approval rules are not "
            "documented here, ask the user for them. Use `knowledge/README.md` (when "
            "present) as the path-and-when-to-read index for deferred detail.\n"
        )
        lines.insert(1, charter)
        return "".join(lines)

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
    def _directory_open_flags() -> int:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise ScaffoldError("starter document seeding requires no-follow filesystem opens")
        flags = os.O_RDONLY | os.O_NONBLOCK | no_follow
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        return flags

    @classmethod
    def _verify_directory_handle(cls, handle: _DirectoryHandle) -> None:
        """Require every held parent/name edge back to the filesystem root."""
        current: _DirectoryHandle | None = handle
        while current is not None:
            try:
                held_stat = os.fstat(current.descriptor)
                if (
                    not stat.S_ISDIR(held_stat.st_mode)
                    or cls._stat_identity(held_stat) != current.identity
                ):
                    raise ScaffoldError(
                        f"managed directory ancestry changed: {current.path}"
                    )
                if current.parent is not None and current.name is not None:
                    named_stat = os.stat(
                        current.name,
                        dir_fd=current.parent.descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(named_stat.st_mode)
                        or cls._stat_identity(named_stat) != current.identity
                    ):
                        raise ScaffoldError(
                            f"managed directory ancestry changed: {current.path}"
                        )
            except ScaffoldError:
                raise
            except OSError as exc:
                raise ScaffoldError(
                    f"managed directory ancestry changed: {current.path}: {exc}"
                ) from exc
            current = current.parent

    @classmethod
    def _open_directory_component(
        cls,
        parent: _DirectoryHandle,
        name: str,
        path: Path,
        *,
        create: bool,
        created: list[Path],
        handles: list[_DirectoryHandle],
    ) -> _DirectoryHandle:
        """Open one physical child relative to a still-attached held parent."""
        cls._verify_directory_handle(parent)
        made_directory = False
        try:
            before = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise ScaffoldError(f"managed physical directory is missing: {path}") from None
            try:
                os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=parent.descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ScaffoldError(f"could not create managed directory {path}: {exc}") from exc
            else:
                made_directory = True
            try:
                before = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
            except OSError as exc:
                raise ScaffoldError(
                    f"managed path must be a physical directory: {path}: {exc}"
                ) from exc
        except OSError as exc:
            raise ScaffoldError(f"could not inspect managed path {path}: {exc}") from exc

        if not stat.S_ISDIR(before.st_mode):
            raise ScaffoldError(f"managed path must be a physical directory: {path}")
        try:
            descriptor = os.open(
                name,
                cls._directory_open_flags(),
                dir_fd=parent.descriptor,
            )
        except OSError as exc:
            raise ScaffoldError(
                f"managed path must be a physical directory: {path}: {exc}"
            ) from exc

        try:
            held_stat = os.fstat(descriptor)
            after = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
            identity = cls._stat_identity(held_stat)
            if (
                not stat.S_ISDIR(held_stat.st_mode)
                or not stat.S_ISDIR(after.st_mode)
                or identity != cls._stat_identity(before)
                or identity != cls._stat_identity(after)
            ):
                raise ScaffoldError(f"managed directory ancestry changed: {path}")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            raise

        handle = _DirectoryHandle(
            path=path,
            descriptor=descriptor,
            identity=identity,
            parent=parent,
            name=name,
        )
        handles.append(handle)
        if made_directory:
            created.append(path)
        cls._verify_directory_handle(handle)
        return handle

    def _open_anchored_root(
        self,
        created: list[Path],
        handles: list[_DirectoryHandle],
    ) -> _DirectoryHandle:
        """Walk from the immutable filesystem root without following components."""
        anchor_path = Path(self.root.anchor)
        descriptor: int | None = None
        try:
            descriptor = os.open(anchor_path, self._directory_open_flags())
            anchor_stat = os.fstat(descriptor)
        except OSError as exc:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise ScaffoldError(f"could not anchor the Enso root safely: {exc}") from exc
        if not stat.S_ISDIR(anchor_stat.st_mode):
            os.close(descriptor)
            raise ScaffoldError(f"managed path must be a physical directory: {anchor_path}")
        current = _DirectoryHandle(
            path=anchor_path,
            descriptor=descriptor,
            identity=self._stat_identity(anchor_stat),
        )
        handles.append(current)

        components = self.root.parts[1:]
        for index, component in enumerate(components):
            path = current.path / component
            current = self._open_directory_component(
                current,
                component,
                path,
                create=index == len(components) - 1,
                created=created,
                handles=handles,
            )
        return current

    @contextlib.contextmanager
    def _open_starter_doc_directories(
        self,
        created: list[Path],
    ) -> Iterator[tuple[_DirectoryHandle, dict[tuple[str, ...], _DirectoryHandle]]]:
        """Hold an attached root, destination directories, and protected staging."""
        handles: list[_DirectoryHandle] = []
        try:
            root = self._open_anchored_root(created, handles)
            docs = self._open_directory_component(
                root,
                "docs",
                self.root / "docs",
                create=True,
                created=created,
                handles=handles,
            )
            enso_docs = self._open_directory_component(
                docs,
                "enso",
                self.root / "docs" / "enso",
                create=True,
                created=created,
                handles=handles,
            )
            staging = self._open_directory_component(
                root,
                "runtime",
                self.root / "runtime",
                create=True,
                created=created,
                handles=handles,
            )
            self._verify_directory_handle(enso_docs)
            self._verify_directory_handle(staging)
            yield staging, {("docs",): docs, ("docs", "enso"): enso_docs}
        finally:
            for handle in reversed(handles):
                with contextlib.suppress(OSError):
                    os.close(handle.descriptor)

    @staticmethod
    def _validate_starter_doc_stat(destination: Path, file_stat: os.stat_result) -> None:
        if not stat.S_ISREG(file_stat.st_mode):
            raise ScaffoldError(
                f"starter document {destination} collision: destination is not a physical "
                "regular file"
            )
        if file_stat.st_nlink != 1:
            raise ScaffoldError(
                f"starter document {destination} collision: destination has multiple hard links"
            )
        if file_stat.st_uid != os.geteuid():
            raise ScaffoldError(
                f"starter document {destination} collision: destination is not owned by "
                "the current user"
            )

    @classmethod
    def _read_starter_doc_at(
        cls,
        parent: _DirectoryHandle,
        destination: Path,
    ) -> bytes | None:
        """Read an owned single-link file through an attached destination parent."""
        cls._verify_directory_handle(parent)
        try:
            before = os.stat(
                destination.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            cls._verify_directory_handle(parent)
            return None
        except OSError as exc:
            raise ScaffoldError(
                f"starter document {destination} collision: could not inspect it safely: {exc}"
            ) from exc
        cls._validate_starter_doc_stat(destination, before)

        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(destination.name, flags, dir_fd=parent.descriptor)
        except FileNotFoundError:
            raise ScaffoldError(
                f"starter document {destination} collision: destination changed while "
                "being inspected"
            ) from None
        except OSError as exc:
            raise ScaffoldError(
                f"starter document {destination} collision: could not open it safely: {exc}"
            ) from exc
        try:
            held_stat = os.fstat(descriptor)
            after_open = os.stat(
                destination.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            identity = cls._stat_identity(held_stat)
            if (
                identity != cls._stat_identity(before)
                or identity != cls._stat_identity(after_open)
            ):
                raise ScaffoldError(
                    f"starter document {destination} collision: destination changed while "
                    "being inspected"
                )
            cls._validate_starter_doc_stat(destination, held_stat)
            cls._validate_starter_doc_stat(destination, after_open)
            cls._verify_directory_handle(parent)

            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)

            after_read = os.fstat(descriptor)
            named_after_read = os.stat(
                destination.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if (
                cls._stat_identity(after_read) != identity
                or cls._stat_identity(named_after_read) != identity
            ):
                raise ScaffoldError(
                    f"starter document {destination} collision: destination changed while "
                    "being inspected"
                )
            cls._validate_starter_doc_stat(destination, after_read)
            cls._validate_starter_doc_stat(destination, named_after_read)
            cls._verify_directory_handle(parent)
            return b"".join(chunks)
        except ScaffoldError:
            raise
        except OSError as exc:
            raise ScaffoldError(
                f"starter document {destination} collision: could not read it safely: {exc}"
            ) from exc
        finally:
            os.close(descriptor)

    @classmethod
    def _unlink_if_identity(
        cls,
        parent_descriptor: int,
        name: str,
        identity: tuple[int, int],
    ) -> None:
        """Best-effort cleanup without knowingly unlinking a replacement name."""
        try:
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if cls._stat_identity(current) != identity:
                return
            os.unlink(name, dir_fd=parent_descriptor)
        except OSError:
            return

    @classmethod
    def _require_renamed_publication(
        cls,
        staging: _DirectoryHandle,
        temporary_name: str,
        descriptor: int,
        destination_parent: _DirectoryHandle,
        destination: Path,
        identity: tuple[int, int],
    ) -> None:
        try:
            held = os.fstat(descriptor)
            published = os.stat(
                destination.name,
                dir_fd=destination_parent.descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ScaffoldError(
                f"starter document {destination} changed during atomic publication: {exc}"
            ) from exc
        try:
            os.stat(
                temporary_name,
                dir_fd=staging.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ScaffoldError(
                f"starter document {destination} staging state could not be verified: {exc}"
            ) from exc
        else:
            raise ScaffoldError(
                f"starter document {destination} staging file remained after publication"
            )
        for file_stat in (held, published):
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or cls._stat_identity(file_stat) != identity
                or file_stat.st_nlink != 1
                or file_stat.st_uid != os.geteuid()
            ):
                raise ScaffoldError(
                    f"starter document {destination} changed during atomic publication"
                )

    @classmethod
    def _reserve_starter_staging_file(
        cls,
        staging: _DirectoryHandle,
        destination: Path,
    ) -> tuple[int, str, tuple[int, int]]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        temporary_name: str | None = None
        identity: tuple[int, int] | None = None
        try:
            for _ in range(100):
                candidate = f".{destination.name}.tmp-{secrets.token_hex(8)}"
                try:
                    descriptor = os.open(
                        candidate,
                        flags,
                        _CONTENT_MODE,
                        dir_fd=staging.descriptor,
                    )
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise ScaffoldError(
                        f"could not reserve an atomic seed file for {destination}: {exc}"
                    ) from exc
                temporary_name = candidate
                break
            if descriptor is None or temporary_name is None:
                raise ScaffoldError(f"could not reserve an atomic seed file for {destination}")

            held = os.fstat(descriptor)
            staged = os.stat(
                temporary_name,
                dir_fd=staging.descriptor,
                follow_symlinks=False,
            )
            identity = cls._stat_identity(held)
            if (
                not stat.S_ISREG(held.st_mode)
                or not stat.S_ISREG(staged.st_mode)
                or cls._stat_identity(staged) != identity
                or held.st_nlink != 1
                or staged.st_nlink != 1
                or held.st_uid != os.geteuid()
                or staged.st_uid != os.geteuid()
            ):
                raise ScaffoldError(
                    f"could not reserve an owned single-link seed file for {destination}"
                )
            cls._verify_directory_handle(staging)
            return descriptor, temporary_name, identity
        except BaseException:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if temporary_name is not None:
                if identity is None:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary_name, dir_fd=staging.descriptor)
                else:
                    cls._unlink_if_identity(staging.descriptor, temporary_name, identity)
            raise

    @classmethod
    def _create_atomic_starter_doc(
        cls,
        staging: _DirectoryHandle,
        destination_parent: _DirectoryHandle,
        destination: Path,
        content: bytes,
    ) -> bool:
        """Publish complete bytes exclusively from protected runtime staging."""
        cls._verify_directory_handle(staging)
        cls._verify_directory_handle(destination_parent)
        temporary_name: str | None = None
        descriptor: int | None = None
        temporary_identity: tuple[int, int] | None = None
        published = False
        try:
            descriptor, temporary_name, temporary_identity = (
                cls._reserve_starter_staging_file(staging, destination)
            )

            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError(errno.EIO, "short write while seeding starter document")
                remaining = remaining[written:]
            os.fsync(descriptor)

            held = os.fstat(descriptor)
            staged = os.stat(
                temporary_name,
                dir_fd=staging.descriptor,
                follow_symlinks=False,
            )
            if (
                cls._stat_identity(held) != temporary_identity
                or cls._stat_identity(staged) != temporary_identity
                or held.st_nlink != 1
                or staged.st_nlink != 1
            ):
                raise ScaffoldError(
                    f"starter document {destination} staging file changed before publication"
                )
            cls._verify_directory_handle(staging)
            cls._verify_directory_handle(destination_parent)

            try:
                _publish_exclusive_at(
                    temporary_name,
                    destination.name,
                    src_dir_fd=staging.descriptor,
                    dst_dir_fd=destination_parent.descriptor,
                )
            except FileExistsError:
                existing = cls._read_starter_doc_at(destination_parent, destination)
                if existing == content:
                    return False
                raise ScaffoldError(
                    f"starter document {destination} collision: destination appeared "
                    "during atomic publication"
                ) from None
            except OSError as exc:
                raise ScaffoldError(
                    f"could not publish starter document {destination} atomically: {exc}"
                ) from exc
            published = True

            cls._require_renamed_publication(
                staging,
                temporary_name,
                descriptor,
                destination_parent,
                destination,
                temporary_identity,
            )
            cls._verify_directory_handle(staging)
            cls._verify_directory_handle(destination_parent)
            os.fsync(destination_parent.descriptor)
            os.fsync(staging.descriptor)
            cls._verify_directory_handle(destination_parent)
            final_stat = os.stat(
                destination.name,
                dir_fd=destination_parent.descriptor,
                follow_symlinks=False,
            )
            cls._validate_starter_doc_stat(destination, final_stat)
            if cls._stat_identity(final_stat) != temporary_identity:
                raise ScaffoldError(
                    f"starter document {destination} changed during atomic publication"
                )
            cls._verify_directory_handle(destination_parent)
            published = False
            temporary_name = None
            return True
        except ScaffoldError:
            raise
        except OSError as exc:
            raise ScaffoldError(
                f"could not create starter document {destination} atomically: {exc}"
            ) from exc
        finally:
            if published and temporary_identity is not None:
                cls._unlink_if_identity(
                    destination_parent.descriptor,
                    destination.name,
                    temporary_identity,
                )
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if temporary_name is not None and temporary_identity is not None:
                cls._unlink_if_identity(
                    staging.descriptor,
                    temporary_name,
                    temporary_identity,
                )

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
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
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
    def _physical_child_directories(parent: Path) -> set[str]:
        try:
            parent_stat = os.lstat(parent)
        except OSError:
            return set()
        if not stat.S_ISDIR(parent_stat.st_mode) or os.path.realpath(parent) != str(parent):
            return set()
        try:
            with os.scandir(parent) as entries:
                return {
                    entry.name
                    for entry in entries
                    if entry.is_dir(follow_symlinks=True)
                }
        except OSError:
            return set()

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

    @staticmethod
    def _validate_regular_content(path: Path, errors: list[str]) -> None:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            errors.append(f"required instruction source is missing: {path}")
        except OSError as exc:
            errors.append(f"could not inspect instruction source {path}: {exc}")
        else:
            if not stat.S_ISREG(path_stat.st_mode):
                errors.append(
                    f"required instruction source must be a physical regular file: {path}"
                )

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

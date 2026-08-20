"""Native policy selection and launch construction for policy-controlled work.

Enso does not compile or grade provider policy. The operator authors each
CLI's native files under a policy's ``policy_dir``; this module
verifies the plumbing against the selected workspace, computes the
``policy_revision`` digest, and builds the launch inputs (arguments live in
each provider class; the minimal child environment and staged runtime home
live here). Anything it cannot verify fails closed with a specific diagnostic.
See docs/specs/permissions.md.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass

from .teams import POLICY_FILES, Policy, Workspace

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# Bump when the launch contract (flags, env construction) changes, so a new
# contract produces a new policy_revision and therefore a fresh execution key.
LAUNCH_CONTRACT_VERSION = "6"
UNRESTRICTED_REVISION = f"unrestricted:v{LAUNCH_CONTRACT_VERSION}"

# Environment kept for policy-controlled provider subprocesses. Everything
# else — 1Password service tokens, transport credentials, secrets/*.env
# projections — is withheld unless the policy's env_passthrough names it;
# allowlisting means a newly added secret can never leak by omission.
_KEEP_ENV = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "SHELL")
_SNAPSHOT_MANIFEST = ".enso-policy-manifest.json"
# Files launch-time staging owns inside a staged-home (codex/grok) policy tree.
_STAGED_SOURCE_RESERVED = {"auth.json", _SNAPSHOT_MANIFEST}
# Names one provider's policy tree may not carry on top of the shared set.
# Grok reads folder trust from trusted_folders.toml inside GROK_HOME, and an
# untrusted workspace is what keeps a workspace-planted .grok/config.toml or
# .claude/settings.json from contributing rules, hooks, or MCP servers to the
# launch. A staged copy would pre-trust the workspace and open that path, so
# the tree may not ship one — trust stays absent by construction.
_PROVIDER_SOURCE_RESERVED = {"grok": frozenset({"trusted_folders.toml"})}
# Keys the Grok CLI recognizes in [permission]; anything else is silently
# dropped at load time, leaving the agent with zero rules (fail-open).
_GROK_PERMISSION_KEYS = ("allow", "deny", "ask", "rules")
# The stanza the Grok CLI appends to config.toml after a run (replace-by-
# rename, so a read-only staged file does not stop it). Staging pre-seeds it
# so the published bytes stay stable and the manifest verifies every launch.
_GROK_MARKETPLACE_STANZA = (
    "[marketplace]\n"
    "default_skills_installs_purged = true\n"
    "official_marketplace_auto_installed = true\n"
    "\n"
    "[[marketplace.sources]]\n"
    'name = "xAI Official"\n'
    'git = "https://github.com/xai-org/plugin-marketplace.git"\n'
)
# Header/env keys in mcp.json that look credential-bearing; a literal value
# (one with no ${...} reference) under such a key draws a warning.
_SECRET_KEY_RE = re.compile(r"(?i)(auth|token|secret|key|password|bearer)")
_KNOWN_PROVIDERS = frozenset((*POLICY_FILES, "agy"))


class PolicyError(Exception):
    """A policy-controlled launch cannot be constructed; dispatch must refuse."""

    def __init__(self, provider: str, problems: tuple[str, ...]):
        self.provider = provider
        self.problems = problems
        super().__init__(f"{provider}: " + "; ".join(problems))


@dataclass(frozen=True)
class PolicyCheck:
    """Result of checking one workspace/policy/provider binding."""

    provider: str
    ok: bool
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    policy_path: str | None = None
    policy_revision: str | None = None
    mcp_servers: tuple[str, ...] = ()  # claude: server names resolved from mcp.json


@dataclass(frozen=True)
class PolicySourceValidation:
    """Read-only validation of every native source selected by one policy."""

    ok: bool
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provider_checks: tuple[PolicyCheck, ...] = ()


@dataclass(frozen=True)
class Launch:
    """Everything a provider spawn needs beyond the prompt and model."""

    mode: str  # "unrestricted" | "policy"
    provider: str
    policy_path: str | None
    home: str | None  # revision-keyed CODEX_HOME/GROK_HOME for codex and grok policy launches
    policy_revision: str
    env: dict[str, str] | None  # None → inherit the parent environment
    ignore_rules: bool = True  # codex: no .rules files were configured
    mcp_config: str | None = None  # claude: conventional mcp.json, passed as --mcp-config


UNRESTRICTED_LAUNCH_BY_PROVIDER = {
    name: Launch(
        mode="unrestricted",
        provider=name,
        policy_path=None,
        home=None,
        policy_revision=UNRESTRICTED_REVISION,
        env=None,
    )
    for name in ("claude", "codex", "agy", "grok")
}


def policy_path(policy: Policy, provider: str) -> str | None:
    """Canonical native-policy path for a provider, or None if it has none."""
    rel = POLICY_FILES.get(provider)
    if policy.policy_dir is None or rel is None:
        return None
    return os.path.join(policy.policy_dir, rel)


def _claude_mcp_path(policy: Policy) -> str:
    """Conventional Claude MCP server file; its presence turns MCP on for the policy."""
    assert policy.policy_dir is not None
    return os.path.join(policy.policy_dir, "claude", "mcp.json")


def _provider_source_root(policy: Policy, provider: str) -> str:
    assert policy.policy_dir is not None
    return os.path.join(policy.policy_dir, provider)


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + os.sep)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)


def _physical_chain_problems(path: str, label: str) -> list[str]:
    """Reject missing, case-aliased, or symlinked components anywhere in a path.

    The workspace-overlap checks compare path strings, so every component must
    carry the exact on-disk spelling (the default macOS filesystem is
    case-insensitive) and be a physical directory rather than a symlink alias.
    """
    absolute = os.path.abspath(os.path.expanduser(path))
    traversed = os.path.sep
    for name in (part for part in absolute.split(os.sep) if part):
        parent = traversed
        traversed = os.path.join(traversed, name)
        try:
            with os.scandir(parent) as iterator:
                entry_names = {entry.name for entry in iterator}
        except OSError as exc:
            return [f"cannot enumerate {label} path component {traversed}: {exc}"]
        if name not in entry_names:
            aliases = sorted(
                candidate
                for candidate in entry_names
                if candidate.casefold() == name.casefold()
            )
            if not aliases:
                return [f"{label} does not exist at {traversed}"]
            return [
                f"{label} path component spelling does not exactly match an "
                f"on-disk entry at {traversed} (found {aliases[0]!r})"
            ]
        try:
            metadata = os.lstat(traversed)
        except OSError as exc:
            return [f"cannot inspect {label} path component {traversed}: {exc}"]
        if stat.S_ISLNK(metadata.st_mode):
            return [f"{label} path component {traversed} must not be a symlink"]
        if not stat.S_ISDIR(metadata.st_mode):
            return [f"{label} path component {traversed} must be a directory"]
    return []


def _directory_metadata_problems(
    path: str,
    label: str,
    *,
    missing_ok: bool = False,
) -> tuple[os.stat_result | None, list[str]]:
    """Validate one physical current-user directory without changing it."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None, []
        return None, [f"{label} does not exist at {path}"]
    except OSError as exc:
        return None, [f"cannot inspect {label}: {exc}"]
    if stat.S_ISLNK(metadata.st_mode):
        return None, [f"{label} must be a physical directory, not a symlink"]
    if not stat.S_ISDIR(metadata.st_mode):
        return None, [f"{label} must be a directory"]
    return metadata, _directory_stat_problems(metadata, label)


def _directory_stat_problems(metadata: os.stat_result, label: str) -> list[str]:
    problems: list[str] = []
    if metadata.st_uid != os.geteuid():
        problems.append(f"{label} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        problems.append(f"{label} must not be group/other-writable")
    return problems


def _file_metadata_problems(metadata: os.stat_result, label: str) -> list[str]:
    problems: list[str] = []
    if not stat.S_ISREG(metadata.st_mode):
        return [f"{label} must be a regular file"]
    if metadata.st_uid != os.geteuid():
        problems.append(f"{label} must be owned by the current user")
    if metadata.st_nlink != 1:
        problems.append(f"{label} must not have hard links")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        problems.append(f"{label} must be owner-only (chmod 600 or 400)")
    return problems


def _read_descriptor(descriptor: int, label: str) -> tuple[bytes | None, list[str]]:
    """Read one no-follow-opened regular owner-only file completely."""
    problems = _file_metadata_problems(os.fstat(descriptor), label)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
    except OSError as exc:
        problems.append(f"could not read {label}: {exc}")
    return (None if problems else b"".join(chunks)), problems


def _tree_snapshot_descriptor(
    root_descriptor: int,
    label: str,
) -> tuple[dict[str, bytes], list[str]]:
    """Read a complete tree relative to a pinned physical root descriptor."""
    files: dict[str, bytes] = {}
    problems: list[str] = []

    def visit(directory_descriptor: int, prefix: str = "") -> None:
        try:
            with os.scandir(directory_descriptor) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            problems.append(f"cannot inspect {label}: {exc}")
            return
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            item_label = f"{label} entry {relative}"
            try:
                metadata = os.stat(
                    entry.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                problems.append(f"cannot inspect {item_label}: {exc}")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                if relative == "mcp.json":
                    problems.append("mcp.json must be a regular file, not a symlink")
                else:
                    problems.append(f"{item_label} must not be a symlink")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                problems.extend(_directory_stat_problems(metadata, item_label))
                try:
                    child = os.open(
                        entry.name,
                        _directory_open_flags(),
                        dir_fd=directory_descriptor,
                    )
                except OSError as exc:
                    problems.append(f"could not open {item_label}: {exc}")
                    continue
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                problems.append(f"{item_label} must be a regular file or directory")
                continue
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
                os, "O_NOFOLLOW", 0
            )
            try:
                descriptor = os.open(entry.name, flags, dir_fd=directory_descriptor)
            except OSError as exc:
                problems.append(f"could not open {item_label} safely: {exc}")
                continue
            try:
                content, read_problems = _read_descriptor(descriptor, item_label)
            finally:
                os.close(descriptor)
            problems.extend(read_problems)
            if content is not None:
                files[relative] = content

    visit(root_descriptor)
    return files, problems


def _tree_snapshot(root: str, label: str) -> tuple[dict[str, bytes], list[str]]:
    """Return the bytes of every file below one physical provider tree."""
    problems = _physical_chain_problems(root, label)
    if problems:
        return {}, problems
    absolute = os.path.abspath(os.path.expanduser(root))
    try:
        descriptor = os.open(absolute, _directory_open_flags())
    except OSError as exc:
        return {}, [f"could not open {label}: {exc}"]
    try:
        problems.extend(_directory_stat_problems(os.fstat(descriptor), label))
        files, tree_problems = _tree_snapshot_descriptor(descriptor, label)
        problems.extend(tree_problems)
        return files, problems
    finally:
        os.close(descriptor)


def _policy_directory_problems(
    policy: Policy,
    workspaces: tuple[Workspace, ...],
) -> list[str]:
    if policy.policy_dir is None:
        return ["policy_dir is required for a restricted policy"]
    problems = _physical_chain_problems(policy.policy_dir, "policy_dir")
    policy_lexical = os.path.abspath(os.path.expanduser(policy.policy_dir))
    if not problems:
        try:
            problems.extend(
                _directory_stat_problems(os.lstat(policy_lexical), "policy_dir")
            )
        except OSError as exc:
            problems.append(f"cannot inspect policy_dir: {exc}")
    policy_real = os.path.realpath(policy_lexical)
    for workspace in workspaces:
        workspace_lexical = os.path.abspath(os.path.expanduser(workspace.path))
        if _within(policy_lexical, workspace_lexical) or _within(
            workspace_lexical,
            policy_lexical,
        ):
            problems.append(
                f"policy_dir lexically overlaps workspace {workspace.name!r}; "
                "the workspace could rewrite protected policy content"
            )
            continue
        workspace_real = os.path.realpath(workspace_lexical)
        if _within(policy_real, workspace_real) or _within(workspace_real, policy_real):
            problems.append(
                f"policy_dir overlaps workspace {workspace.name!r}; "
                "the workspace could rewrite protected policy content"
            )
    return problems


def _runtime_topology_problems(policy: Policy, provider: str) -> list[str]:
    """Inspect an existing staged-home tree without creating or repairing it."""
    assert policy.policy_dir is not None
    runtime = os.path.join(policy.policy_dir, ".runtime")
    _metadata, problems = _directory_metadata_problems(
        runtime,
        ".runtime directory",
        missing_ok=True,
    )
    if _metadata is None:
        return problems
    homes = os.path.join(runtime, f"{provider}-home")
    if not os.path.lexists(homes):
        return problems
    _files, home_problems = _tree_snapshot(homes, f".runtime/{provider}-home")
    problems.extend(home_problems)
    return problems


def _validate_native_format(
    provider: str,
    raw: bytes,
    files: dict[str, bytes],
) -> tuple[list[str], list[str], dict | None, dict, str | None]:
    """Validate stable provider bytes and return Claude cross-check inputs."""
    if provider == "claude":
        problems, warnings, settings = _check_claude_settings_bytes(raw)
        servers: dict = {}
        mcp_digest: str | None = None
        mcp_raw = files.get("mcp.json")
        if mcp_raw is not None:
            mcp_problems, servers, mcp_digest = _check_claude_mcp_bytes(mcp_raw)
            problems.extend(mcp_problems)
        return problems, warnings, settings, servers, mcp_digest
    if provider == "codex":
        return _check_codex_config_bytes(raw), [], None, {}, None
    problems, warnings = _check_grok_config_bytes(raw)
    return problems, warnings, None, {}, None


def _provider_source_check(
    policy: Policy,
    provider: str,
    workspaces: tuple[Workspace, ...],
) -> PolicyCheck:
    if provider not in _KNOWN_PROVIDERS:
        return PolicyCheck(
            provider=provider,
            ok=False,
            problems=(f"unknown provider {provider!r}",),
        )
    if policy.unrestricted:
        if policy.policy_dir is not None:
            return PolicyCheck(
                provider=provider,
                ok=False,
                problems=("unrestricted policy must not define policy_dir",),
            )
        return PolicyCheck(provider=provider, ok=True, policy_revision=UNRESTRICTED_REVISION)

    path = policy_path(policy, provider)
    problems = _policy_directory_problems(policy, workspaces)
    if provider == "agy":
        problems.append(
            "agy has no verified Enso launch contract and requires an unrestricted workspace"
        )
        return PolicyCheck(provider=provider, ok=False, problems=tuple(problems))
    if path is None or policy.policy_dir is None:
        return PolicyCheck(provider=provider, ok=False, problems=tuple(problems))

    root = _provider_source_root(policy, provider)
    files, tree_problems = _tree_snapshot(root, f"{provider} policy directory")
    problems.extend(tree_problems)
    relative_path = os.path.relpath(path, root).replace(os.sep, "/")
    raw = files.get(relative_path)
    if raw is None and not any(relative_path in problem for problem in tree_problems):
        problems.append(f"native policy not found at {path}")

    reserved = _STAGED_SOURCE_RESERVED | _PROVIDER_SOURCE_RESERVED.get(
        provider, frozenset()
    )
    for relative in sorted(files):
        if relative in reserved:
            problems.append(f"{provider} policy tree reserves {relative}")

    if provider in ("codex", "grok"):
        problems.extend(_runtime_topology_problems(policy, provider))

    warnings: list[str] = []
    servers: dict = {}
    mcp_digest: str | None = None
    if raw is not None:
        format_problems, warnings, settings, servers, mcp_digest = _validate_native_format(
            provider,
            raw,
            files,
        )
        problems.extend(format_problems)
        if provider == "claude" and not problems and settings is not None:
            known_servers = tuple(sorted(servers))
            warnings.extend(_claude_mcp_rule_warnings(settings, known_servers))
            warnings.extend(_claude_mcp_secret_warnings(servers))

    if problems:
        return PolicyCheck(
            provider=provider,
            ok=False,
            problems=tuple(dict.fromkeys(problems)),
            warnings=tuple(warnings),
            policy_path=path,
            mcp_servers=tuple(sorted(servers)),
        )
    revision = _source_revision(provider, files, mcp_digest=mcp_digest)
    return PolicyCheck(
        provider=provider,
        ok=True,
        warnings=tuple(warnings),
        policy_path=path,
        policy_revision=revision,
        mcp_servers=tuple(sorted(servers)),
    )


def check_policy_sources(
    policy: Policy,
    workspaces: Iterable[Workspace],
) -> PolicySourceValidation:
    """Validate every selected provider source without writing runtime state."""
    workspace_items = tuple(workspaces)
    checks = tuple(
        _provider_source_check(policy, provider, workspace_items)
        for provider in policy.providers
    )
    problems = tuple(
        dict.fromkeys(
            f"{check.provider}: {problem}"
            for check in checks
            for problem in check.problems
        )
    )
    warnings = tuple(
        dict.fromkeys(
            f"{check.provider}: {warning}"
            for check in checks
            for warning in check.warnings
        )
    )
    if not checks:
        problems = ("policy selects no providers",)
    return PolicySourceValidation(
        ok=bool(checks) and not problems,
        problems=problems,
        warnings=warnings,
        provider_checks=checks,
    )


def check_provider(workspace: Workspace, policy: Policy, provider: str) -> PolicyCheck:
    """Statically validate one workspace/policy/provider binding."""
    return _provider_source_check(policy, provider, (workspace,))


def _check_claude_settings_bytes(raw: bytes) -> tuple[list[str], list[str], dict | None]:
    """Validate settings.json, returning it parsed for further cross-checks."""
    try:
        settings = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["settings.json does not parse as UTF-8 JSON"], [], None
    if not isinstance(settings, dict):
        return ["settings.json must be a JSON object"], [], None
    problems = []
    # Hooks in a workspace .claude/settings.json execute outside the permission
    # system and outside the sandbox, and the agent can write that file itself.
    # This key is Enso's launch floor, not a judgement of the operator's policy.
    if settings.get("disableAllHooks") is not True:
        problems.append(
            'must set "disableAllHooks": true — without it a workspace '
            ".claude/settings.json can run arbitrary commands outside the sandbox"
        )
    warnings = []
    sandbox = settings.get("sandbox")
    if not (isinstance(sandbox, dict) and sandbox.get("enabled") is True):
        warnings.append(
            "sandbox.enabled is not true: permission rules govern Claude's own "
            "tools only; subprocesses need the OS sandbox or outer isolation"
        )
    return problems, warnings, settings


def _check_claude_mcp_bytes(raw: bytes) -> tuple[list[str], dict, str | None]:
    """Validate mcp.json bytes and bind the parsed servers to their digest."""
    try:
        mcp = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["mcp.json does not parse as UTF-8 JSON"], {}, None
    if not isinstance(mcp, dict):
        return ["mcp.json must be a JSON object"], {}, None
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict):
        return ['mcp.json must define "mcpServers" as an object'], {}, None
    if not servers:
        return ['mcp.json "mcpServers" is empty; delete the file to disable MCP'], {}, None
    return [], servers, hashlib.sha256(raw).hexdigest()


def _claude_mcp_rule_warnings(settings: dict, known: tuple[str, ...]) -> list[str]:
    """Cross-check mcp__ permission rules against the policy's resolved servers.

    Both directions are silent no-ops rather than access widenings, so they
    warn: a rule naming an undefined server can never match (with no mcp.json
    every mcp__ rule is inert), and a server no allow rule admits has every
    tool denied under dontAsk — deny or ask references cannot make it usable.
    """
    permissions = settings.get("permissions")
    rules: list[tuple[str, str]] = []
    if isinstance(permissions, dict):
        for list_name in ("allow", "ask", "deny"):
            values = permissions.get(list_name)
            if not isinstance(values, list):
                continue
            rules.extend(
                (list_name, rule)
                for rule in values
                if isinstance(rule, str) and rule.startswith("mcp__")
            )
    warnings: list[str] = []
    allowed: set[str] = set()
    for list_name, rule in rules:
        candidate = rule[len("mcp__"):]
        # Server names may themselves contain "__", so match whole known names
        # instead of splitting the rule.
        matched = {
            server for server in known if candidate == server or candidate.startswith(server + "__")
        }
        if not matched:
            warnings.append(
                "permission rule matches no configured MCP server in claude/mcp.json "
                "and can never apply"
            )
        elif list_name == "allow":
            allowed.update(matched)
    for server in known:
        if server not in allowed:
            warnings.append(
                f'no allow rule references MCP server "{server}"; '
                "every tool on it will be denied under dontAsk"
            )
    return warnings


def _claude_mcp_secret_warnings(servers: dict) -> list[str]:
    """Flag credential-shaped literals in mcp.json; values belong in the environment."""
    affected: set[tuple[str, str]] = set()
    for name in sorted(servers):
        server = servers[name]
        if not isinstance(server, dict):
            continue
        for section in ("headers", "env"):
            block = server.get(section)
            if not isinstance(block, dict):
                continue
            for key, value in block.items():
                if not _SECRET_KEY_RE.search(key):
                    continue
                if isinstance(value, str) and value and "${" not in value:
                    affected.add((name, section))
    return [
        f'mcp.json server "{name}" has a secret-shaped literal in {section}; '
        "use a ${VAR} reference"
        for name, section in sorted(affected)
    ]


def _check_codex_config_bytes(raw: bytes) -> list[str]:
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ["config.toml does not parse as UTF-8 TOML"]
    if "developer_instructions" in config:
        return [
            "config.toml developer_instructions would duplicate Codex's native "
            "AGENTS.md discovery; move policy-specific guidance into the workspace "
            "AGENTS.md"
        ]
    has_profiles = "default_permissions" in config or "permissions" in config
    has_legacy = "sandbox_mode" in config or "sandbox_workspace_write" in config
    if has_profiles and has_legacy:
        return [
            "config.toml mixes permission profiles with legacy sandbox settings; "
            "Codex would silently use the legacy sandbox and ignore the profile"
        ]
    return []


def _check_grok_config_bytes(raw: bytes) -> tuple[list[str], list[str]]:
    """Statically reject config shapes the Grok CLI loads as zero rules.

    The CLI reports no error and no skipped entry for a missing or misspelled
    [permission] table, a wrong-shaped key, or an unknown one — it just loads
    zero rules and runs wide open, so every such shape is a problem here.

    Folder trust is checked alongside the rules because it is the gate, not a
    rule: disabling it admits workspace-planted config, hooks, and MCP servers
    into the launch, which is the one way a policy file can widen itself.
    """
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ["config.toml does not parse as UTF-8 TOML"], []
    warnings: list[str] = []
    problems: list[str] = _grok_folder_trust_problems(config)
    if "marketplace" in config:
        warnings.append(
            "config.toml has its own [marketplace] stanza; the Grok CLI rewrites "
            "it after a run, which would fail the next snapshot verification — "
            "omit it so staging can pre-seed the canonical one"
        )
    trust_problems = len(problems)
    permission = config.get("permission")
    if not isinstance(permission, dict):
        problems.append(
            "config.toml must define a [permission] table with at least one of "
            "allow/deny/ask/rules; without one the Grok CLI silently loads zero rules"
        )
        return problems, warnings
    declared = 0
    for key in sorted(permission):
        value = permission[key]
        if key not in _GROK_PERMISSION_KEYS:
            if key == "folder_trust":
                problems.append(
                    "config.toml must not configure folder_trust inside [permission]; "
                    "the Grok CLI would silently drop it"
                )
            else:
                problems.append(
                    "config.toml [permission] contains an unrecognized key; only "
                    "allow/deny/ask/rules; the Grok CLI would silently drop it"
                )
        elif key == "rules":
            if isinstance(value, list) and all(
                isinstance(entry, dict) and "action" in entry and "tool" in entry
                for entry in value
            ):
                declared += len(value)
                if value:
                    warnings.append(
                        "config.toml [permission] rules uses the array-of-tables "
                        "form, which the Grok CLI loads inconsistently; prefer "
                        "allow/deny/ask string arrays"
                    )
            else:
                problems.append(
                    "config.toml [permission] rules is not a well-formed rules "
                    "array; prefer allow/deny/ask string arrays (the Grok CLI "
                    "silently drops wrong-shaped rules)"
                )
        elif isinstance(value, list) and all(isinstance(rule, str) for rule in value):
            declared += len(value)
        else:
            problems.append(
                f"config.toml [permission] {key} must be an array of rule strings; "
                "the Grok CLI silently loads zero rules from any other shape"
            )
    if len(problems) == trust_problems and declared == 0:
        problems.append(
            "config.toml [permission] declares no rules; the Grok CLI would run "
            "wide open under a dontAsk launch"
        )
    return problems, warnings


def _grok_folder_trust_problems(config: dict) -> list[str]:
    """Reject a policy that turns Grok's folder-trust gate off.

    Folder trust only ever loosens: with it disabled the CLI applies a
    workspace's own ``.grok/config.toml`` and vendor-compat settings, so an
    agent-writable workspace could grant itself rules, hooks, and MCP servers
    the policy never declared. A fresh staged home leaves the workspace
    untrusted; only this key (or ``GROK_FOLDER_TRUST``, which env_passthrough
    reserves) can undo that, so the policy file may not carry it. An explicit
    ``enabled = true`` restates the default and is allowed.
    """
    if "folder_trust" not in config:
        return []
    trust = config["folder_trust"]
    if isinstance(trust, dict) and set(trust) <= {"enabled"} and trust.get("enabled") is True:
        return []
    return [
        "config.toml must not configure [folder_trust]; disabling folder trust "
        "ungates workspace-planted config, hooks, and MCP servers, letting a "
        "writable workspace widen its own policy (only 'enabled = true', the "
        "default, is accepted)"
    ]


def _grok_rule_count(config: dict) -> int:
    """Number of rules a well-shaped [permission] table declares."""
    permission = config.get("permission")
    if not isinstance(permission, dict):
        return 0
    return sum(
        len(value)
        for key, value in permission.items()
        if key in _GROK_PERMISSION_KEYS and isinstance(value, list)
    )


def _grok_effective_config(raw: bytes) -> bytes:
    """The config bytes as the Grok CLI leaves them after its first run.

    The CLI appends a [marketplace] stanza to config.toml post-run; staging
    publishes these effective bytes and the revision hashes them, so the
    snapshot manifest still verifies on every later launch. An operator who
    authors their own [marketplace] keeps it verbatim (check_provider warns
    that the write-back will then mutate the snapshot).
    """
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return raw
    if "marketplace" in config:
        return raw
    stanza = _GROK_MARKETPLACE_STANZA.encode("utf-8")
    if raw and not raw.endswith(b"\n"):
        return raw + b"\n\n" + stanza
    return raw + b"\n" + stanza


def _manifest_revision(provider: str, manifest: dict[str, str]) -> str:
    payload = json.dumps(
        {"contract": LAUNCH_CONTRACT_VERSION, "provider": provider, "files": manifest},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_revision(
    provider: str,
    files: dict[str, bytes],
    *,
    mcp_digest: str | None = None,
) -> str:
    """Hash exactly the stable source bytes used for native-format validation."""
    if provider == "claude":
        manifest = {"settings.json": hashlib.sha256(files["settings.json"]).hexdigest()}
        if mcp_digest is not None:
            manifest["mcp.json"] = mcp_digest
    else:
        manifest = {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in files.items()
            if relative != _SNAPSHOT_MANIFEST
        }
        if provider == "grok":
            manifest["config.toml"] = hashlib.sha256(
                _grok_effective_config(files["config.toml"])
            ).hexdigest()
    return _manifest_revision(provider, manifest)


def prepare_launch(workspace: Workspace, policy: Policy, provider: str) -> Launch:
    """Build the launch for one provider, failing closed on any problem."""
    check = check_provider(workspace, policy, provider)
    if not check.ok:
        raise PolicyError(provider, check.problems)
    if policy.unrestricted:
        return UNRESTRICTED_LAUNCH_BY_PROVIDER.get(
            provider,
            Launch(
                mode="unrestricted",
                provider=provider,
                policy_path=None,
                home=None,
                policy_revision=UNRESTRICTED_REVISION,
                env=None,
            ),
        )

    assert check.policy_path is not None and check.policy_revision is not None
    env = _minimal_env(provider, policy.env_passthrough)
    home: str | None = None
    ignore_rules = True
    mcp_config: str | None = None
    if provider == "claude" and check.mcp_servers:
        mcp_config = _claude_mcp_path(policy)
    if provider in ("codex", "grok"):
        try:
            if provider == "codex":
                home, ignore_rules = _stage_codex_home(policy, check.policy_revision)
                env["CODEX_HOME"] = home
            else:
                home = _stage_grok_home(policy, check.policy_revision)
                env["GROK_HOME"] = home
        except PolicyError:
            raise
        except OSError as exc:
            # A read-only or unwritable policy dir must refuse this turn, not
            # escape as an unhandled error after the delivery was claimed.
            raise PolicyError(
                provider, (f"could not stage the {provider.capitalize()} runtime home: {exc}",)
            ) from exc
    log.info(
        "Policy launch for %s: MCP servers [%s], passthrough [%s]",
        provider,
        ", ".join(check.mcp_servers),
        ", ".join(name for name in policy.env_passthrough if name in env),
    )
    return Launch(
        mode="policy",
        provider=provider,
        policy_path=check.policy_path,
        home=home,
        policy_revision=check.policy_revision,
        env=env,
        ignore_rules=ignore_rules,
        mcp_config=mcp_config,
    )


def _materialize_snapshot_at(
    root: int,
    files: dict[str, bytes],
    provider: str,
) -> None:
    """Write already-validated bytes below one agent-created temp descriptor."""
    for relative, content in sorted(files.items()):
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise PolicyError(provider, (f"policy contains unsafe path {relative!r}",))
        parent = os.dup(root)
        try:
            for index, name in enumerate(parts[:-1]):
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                child = _open_directory_at(
                    parent,
                    name,
                    f"temporary policy directory {'/'.join(parts[: index + 1])}",
                    provider,
                )
                os.close(parent)
                parent = child
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(parts[-1], flags, 0o600, dir_fd=parent)
            try:
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, content)
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)


def _grok_inspection_problems(
    completed: subprocess.CompletedProcess[str],
    declared: int,
) -> list[str]:
    """Compare a content-redacted Grok inspection report with the policy."""
    if completed.returncode != 0:
        return [f"grok inspect exited {completed.returncode}"]
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ["grok inspect --json output is not valid JSON"]
    permissions = report.get("permissions") if isinstance(report, dict) else None
    loaded = permissions.get("loaded") if isinstance(permissions, dict) else None
    if isinstance(loaded, bool) or not isinstance(loaded, int):
        return ["grok inspect --json reported no permissions.loaded count"]
    if loaded < declared:
        return [
            f"grok inspect loaded {loaded} permission rules but the policy declares "
            f"{declared}; grok silently ignores wrong-shaped rules"
        ]
    if loaded > declared:
        return [
            f"grok inspect loaded {loaded} permission rules but the policy declares "
            f"{declared}; rules are reaching the launch from outside the policy"
        ]
    return []


def verify_grok_rules(workspace: Workspace, policy: Policy, grok_path: str) -> list[str]:
    """Dynamically confirm Grok loads the checked policy, without persistent writes.

    Stable checked bytes go into a disposable GROK_HOME, and inspection gets a
    separate scratch HOME. Canonical policy ``.runtime`` and user auth remain
    untouched. Failures come back as problems; this never raises.
    """
    check = check_provider(workspace, policy, "grok")
    if not check.ok:
        return list(check.problems)
    if policy.unrestricted:
        return []
    assert policy.policy_dir is not None and check.policy_revision is not None

    source = _provider_source_root(policy, "grok")
    files, source_problems = _tree_snapshot(source, "grok policy directory")
    if source_problems:
        return list(dict.fromkeys(source_problems))
    if _source_revision("grok", files) != check.policy_revision:
        return ["grok policy changed between static validation and dynamic inspection"]

    raw = files.get("config.toml")
    if raw is None:
        return ["native policy not found at grok/config.toml"]
    effective = _grok_effective_config(raw)
    effective_files = {**files, "config.toml": effective}
    try:
        effective_config = tomllib.loads(effective.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ["grok config does not parse as UTF-8 TOML"]
    declared = _grok_rule_count(effective_config)
    try:
        with tempfile.TemporaryDirectory(prefix="enso-grok-home-inspect-") as grok_home:
            grok_descriptor = os.open(grok_home, _directory_open_flags())
            try:
                _materialize_snapshot_at(grok_descriptor, effective_files, "grok")
            finally:
                os.close(grok_descriptor)
            env = _minimal_env("grok", policy.env_passthrough)
            env["GROK_HOME"] = grok_home
            with tempfile.TemporaryDirectory(prefix="enso-grok-inspect-") as scratch_home:
                completed = subprocess.run(
                    [grok_path, "inspect", "--json"],
                    cwd=workspace.path,
                    env={**env, "HOME": scratch_home},
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
    except PolicyError as exc:
        return list(exc.problems)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"could not run grok inspect: {exc}"]
    return _grok_inspection_problems(completed, declared)


def _minimal_env(provider: str, passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    """Allowlisted child environment: locale, passthrough, controlled PATH, provider auth.

    Profile ``env_passthrough`` names are copied before the launch-controlled
    assignments (PATH, provider auth keys, CODEX_HOME/GROK_HOME) so a
    launch-controlled value always wins even if validation of the reserved
    names is bypassed.
    """
    from .providers import PROVIDER_CLASSES

    env = {key: os.environ[key] for key in _KEEP_ENV if key in os.environ}
    missing = [name for name in passthrough if name not in os.environ]
    if missing:
        log.warning(
            "env_passthrough names not set in the service environment: %s", ", ".join(missing)
        )
    for name in passthrough:
        if name in os.environ:
            env[name] = os.environ[name]
    env["PATH"] = _filtered_path()
    provider_cls = PROVIDER_CLASSES.get(provider)
    for key in provider_cls.env_keys if provider_cls else ():
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _filtered_path() -> str:
    """The parent PATH minus every directory holding an ``enso`` executable.

    Friction, not a boundary — an absolute path still reaches the CLI, which
    is why the operator's policy must deny it too (permissions.md).
    """
    enso_dirs = set()
    found = shutil.which("enso")
    if found:
        enso_dirs.add(os.path.dirname(os.path.realpath(found)))
    enso_dirs.add(os.path.dirname(os.path.realpath(sys.argv[0])))
    parts = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and os.path.realpath(part) not in enso_dirs
    ]
    return os.pathsep.join(parts)


def _user_codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _user_grok_home() -> str:
    return os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _open_directory_at(parent: int, name: str, label: str, provider: str) -> int:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise PolicyError(provider, (f"cannot inspect {label}: {exc}",)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyError(provider, (f"{label} must be a physical directory",))
    problems = _directory_stat_problems(metadata, label)
    if problems:
        raise PolicyError(provider, tuple(problems))
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent)
    except OSError as exc:
        raise PolicyError(provider, (f"could not open {label} safely: {exc}",)) from exc


def _ensure_runtime_directory_at(parent: int, name: str, label: str, provider: str) -> int:
    """Create one private child relative to a pinned parent, or reject it."""
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise PolicyError(provider, (f"could not create {label}: {exc}",)) from exc
    descriptor = _open_directory_at(parent, name, label, provider)
    if created:
        os.fchmod(descriptor, 0o700)
    return descriptor


def _copy_codex_tree(source: int, destination: int) -> None:
    """Copy a pinned native source tree into a pinned unpublished directory.

    Keep this two-argument seam for the concurrent-publish tests; provider
    attribution is applied by the caller that owns the publication attempt.
    """
    _copy_policy_tree(source, destination)


def _copy_policy_tree(source: int, destination: int) -> None:
    """Descriptor-relative implementation shared by Codex and Grok."""
    provider = "codex"
    with os.scandir(source) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        name = entry.name
        try:
            metadata = os.stat(name, dir_fd=source, follow_symlinks=False)
        except OSError as exc:
            raise PolicyError(provider, (f"could not inspect policy entry {name}: {exc}",)) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PolicyError(provider, (f"policy entry {name} must not be a symlink",))
        if stat.S_ISDIR(metadata.st_mode):
            problems = _directory_stat_problems(metadata, f"policy directory {name}")
            if problems:
                raise PolicyError(provider, tuple(problems))
            source_child = _open_directory_at(source, name, f"policy directory {name}", provider)
            try:
                os.mkdir(name, mode=0o700, dir_fd=destination)
                destination_child = _open_directory_at(
                    destination,
                    name,
                    f"staged directory {name}",
                    provider,
                )
                try:
                    os.fchmod(destination_child, 0o700)
                    _copy_policy_tree(source_child, destination_child)
                finally:
                    os.close(destination_child)
            finally:
                os.close(source_child)
            continue
        problems = _file_metadata_problems(metadata, f"policy file {name}")
        if problems:
            raise PolicyError(provider, tuple(problems))
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        source_file = os.open(name, source_flags, dir_fd=source)
        try:
            content, read_problems = _read_descriptor(source_file, f"policy file {name}")
        finally:
            os.close(source_file)
        if content is None:
            raise PolicyError(provider, tuple(read_problems))
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        destination_file = os.open(name, destination_flags, 0o600, dir_fd=destination)
        try:
            os.fchmod(destination_file, 0o600)
            _write_all(destination_file, content)
            os.fsync(destination_file)
            os.fchmod(destination_file, 0o400)
        finally:
            os.close(destination_file)


def _write_snapshot_manifest_at(home: int, manifest: dict[str, str]) -> None:
    content = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(_SNAPSHOT_MANIFEST, flags, 0o600, dir_fd=home)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)


def _verify_staged_snapshot_at(
    home: int,
    revision: str,
    provider: str,
) -> frozenset[str]:
    """Verify one published revision through its pinned directory descriptor."""
    files, topology_problems = _tree_snapshot_descriptor(home, "staged policy revision")
    if topology_problems:
        raise PolicyError(provider, tuple(topology_problems))
    manifest_raw = files.get(_SNAPSHOT_MANIFEST)
    if manifest_raw is None:
        raise PolicyError(provider, ("staged policy manifest is missing",))
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError(provider, (f"could not read staged policy manifest: {exc}",)) from exc
    if not isinstance(manifest, dict) or not all(
        isinstance(relative, str) and isinstance(digest, str)
        for relative, digest in manifest.items()
    ):
        raise PolicyError(provider, ("staged policy manifest is invalid",))
    config_name = os.path.basename(POLICY_FILES[provider])
    if config_name not in manifest or _manifest_revision(provider, manifest) != revision:
        raise PolicyError(provider, ("staged policy manifest has the wrong revision",))

    for relative, expected in manifest.items():
        if os.path.isabs(relative) or ".." in relative.split("/"):
            raise PolicyError(provider, ("staged policy manifest contains an unsafe path",))
        content = files.get(relative)
        if content is None or hashlib.sha256(content).hexdigest() != expected:
            raise PolicyError(provider, (f"staged {relative} digest does not match its manifest",))

    allowed_runtime_files = {"auth.json", _SNAPSHOT_MANIFEST}
    staged_policy_files = {
        relative
        for relative in files
        if relative not in allowed_runtime_files and not relative.startswith(".auth-")
    }
    if staged_policy_files != set(manifest):
        raise PolicyError(provider, ("staged files do not match the policy manifest",))
    return frozenset(files)


def _preseed_grok_marketplace_at(home: int) -> None:
    """Rewrite an unpublished config.toml copy with its effective bytes."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("config.toml", flags, dir_fd=home)
    try:
        raw, problems = _read_descriptor(descriptor, "config.toml")
        if raw is None:
            raise PolicyError("grok", tuple(problems))
        effective = _grok_effective_config(raw)
        if effective == raw:
            return
        os.fchmod(descriptor, 0o600)
        write_flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        writer = os.open("config.toml", write_flags, dir_fd=home)
        try:
            os.ftruncate(writer, 0)
            _write_all(writer, effective)
            os.fsync(writer)
        finally:
            os.fchmod(writer, 0o400)
            os.close(writer)
    finally:
        os.fchmod(descriptor, 0o400)
        os.close(descriptor)


def _remove_tree_at(parent: int, name: str) -> None:
    """Remove one agent-created temporary tree without following entries."""
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    descriptor = os.open(name, _directory_open_flags(), dir_fd=parent)
    try:
        with os.scandir(descriptor) as iterator:
            entries = list(iterator)
        for entry in entries:
            _remove_tree_at(descriptor, entry.name)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent)


def _publish_staged_snapshot_at(
    source: int,
    snapshots: int,
    revision: str,
    provider: str,
) -> None:
    """Publish one immutable revision entirely below pinned descriptors."""
    temporary = f".{revision[:12]}-{secrets.token_hex(8)}"
    os.mkdir(temporary, mode=0o700, dir_fd=snapshots)
    temporary_descriptor = _open_directory_at(
        snapshots,
        temporary,
        "temporary staged policy revision",
        provider,
    )
    published = False
    try:
        os.fchmod(temporary_descriptor, 0o700)
        try:
            _copy_codex_tree(source, temporary_descriptor)
        except PolicyError as exc:
            raise PolicyError(provider, exc.problems) from exc
        if provider == "grok":
            _preseed_grok_marketplace_at(temporary_descriptor)
        files, problems = _tree_snapshot_descriptor(
            temporary_descriptor,
            "temporary staged policy revision",
        )
        if problems:
            raise PolicyError(provider, tuple(problems))
        manifest = {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in files.items()
            if relative != _SNAPSHOT_MANIFEST
        }
        if _manifest_revision(provider, manifest) != revision:
            raise PolicyError(
                provider,
                ("staged policy digest does not match the checked revision",),
            )
        _write_snapshot_manifest_at(temporary_descriptor, manifest)
        try:
            os.rename(
                temporary,
                revision,
                src_dir_fd=snapshots,
                dst_dir_fd=snapshots,
            )
            published = True
        except OSError as exc:
            concurrent_publish = exc.errno in {errno.EEXIST, errno.ENOTEMPTY}
            if not concurrent_publish:
                raise
    finally:
        os.close(temporary_descriptor)
        if not published:
            _remove_tree_at(snapshots, temporary)


def _read_provider_auth(path: str, provider: str) -> bytes | None:
    label = provider.capitalize()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PolicyError(provider, (f"could not open {label} auth safely: {exc}",)) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PolicyError(provider, (f"{label} auth must be a regular file",))
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PolicyError(provider, (f"{label} auth changed while it was being read",))
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _stage_provider_auth_at(home: int, provider: str, user_home: str) -> None:
    """Atomically refresh auth without ever exposing a partial credential file."""
    source = os.path.join(user_home, "auth.json")
    content = _read_provider_auth(source, provider)
    if content is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink("auth.json", dir_fd=home)
        return
    temporary = f".auth-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=home)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, "auth.json", src_dir_fd=home, dst_dir_fd=home)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=home)


def _stage_provider_home(
    policy: Policy,
    revision: str,
    provider: str,
    user_home: str,
) -> tuple[str, frozenset[str]]:
    """Select an immutable, revision-keyed policy snapshot and safe auth."""
    assert policy.policy_dir is not None
    problems = _physical_chain_problems(policy.policy_dir, "policy_dir")
    if problems:
        raise PolicyError(provider, tuple(problems))
    try:
        root = os.open(
            os.path.abspath(os.path.expanduser(policy.policy_dir)),
            _directory_open_flags(),
        )
    except OSError as exc:
        raise PolicyError(provider, (f"could not open policy_dir safely: {exc}",)) from exc
    opened: list[int] = [root]
    home = os.path.join(
        policy.policy_dir,
        ".runtime",
        f"{provider}-home",
        revision,
    )
    try:
        root_problems = _directory_stat_problems(os.fstat(root), "policy_dir")
        if root_problems:
            raise PolicyError(provider, tuple(root_problems))
        source = _open_directory_at(
            root,
            provider,
            f"{provider} policy directory",
            provider,
        )
        opened.append(source)
        runtime = _ensure_runtime_directory_at(
            root,
            ".runtime",
            ".runtime directory",
            provider,
        )
        opened.append(runtime)
        snapshots = _ensure_runtime_directory_at(
            runtime,
            f"{provider}-home",
            f".runtime/{provider}-home",
            provider,
        )
        opened.append(snapshots)
        try:
            os.stat(revision, dir_fd=snapshots, follow_symlinks=False)
        except FileNotFoundError:
            _publish_staged_snapshot_at(source, snapshots, revision, provider)
        home_descriptor = _open_directory_at(
            snapshots,
            revision,
            "staged policy revision",
            provider,
        )
        opened.append(home_descriptor)
        staged_files = _verify_staged_snapshot_at(home_descriptor, revision, provider)
        _stage_provider_auth_at(home_descriptor, provider, user_home)
        log.debug("Selected %s home at %s (revision %s)", provider, home, revision[:12])
        return home, staged_files
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _stage_codex_home(policy: Policy, revision: str) -> tuple[str, bool]:
    """Codex snapshot selection plus whether any .rules files were staged."""
    home, staged_files = _stage_provider_home(
        policy,
        revision,
        "codex",
        _user_codex_home(),
    )
    has_rules = any(
        relative.startswith("rules/") and relative.endswith(".rules")
        for relative in staged_files
    )
    return home, not has_rules


def _stage_grok_home(policy: Policy, revision: str) -> str:
    """Grok snapshot selection; grok has no .rules concept to toggle."""
    home, _staged_files = _stage_provider_home(policy, revision, "grok", _user_grok_home())
    return home

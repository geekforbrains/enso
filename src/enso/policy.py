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
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass

from .fsutil import regular_file_sha256
from .teams import POLICY_FILES, Policy, Workspace

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# Bump when the launch contract (flags, env construction) changes, so a new
# contract produces a new policy_revision and therefore a fresh execution key.
LAUNCH_CONTRACT_VERSION = "4"
UNRESTRICTED_REVISION = f"unrestricted:v{LAUNCH_CONTRACT_VERSION}"

# Environment kept for policy-controlled provider subprocesses. Everything
# else — 1Password service tokens, transport credentials, secrets/*.env
# projections — is withheld unless the policy's env_passthrough names it;
# allowlisting means a newly added secret can never leak by omission.
_KEEP_ENV = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "SHELL")
_SNAPSHOT_MANIFEST = ".enso-policy-manifest.json"
_CODEX_SOURCE_RESERVED = {"auth.json", _SNAPSHOT_MANIFEST}
# Header/env keys in mcp.json that look credential-bearing; a literal value
# (one with no ${...} reference) under such a key draws a warning.
_SECRET_KEY_RE = re.compile(r"(?i)(auth|token|secret|key|password|bearer)")


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
class Launch:
    """Everything a provider spawn needs beyond the prompt and model."""

    mode: str  # "unrestricted" | "policy"
    provider: str
    policy_path: str | None
    home: str | None  # revision-keyed CODEX_HOME for codex policy launches
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
    for name in ("claude", "codex", "agy")
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


def _codex_source_root(policy: Policy) -> str:
    assert policy.policy_dir is not None
    return os.path.join(policy.policy_dir, "codex")


def check_provider(workspace: Workspace, policy: Policy, provider: str) -> PolicyCheck:
    """Statically validate one workspace/policy/provider binding.

    Verifies selection and integrity, not semantics: a file that parses and
    deliberately grants broad access is still the operator's policy.
    """
    if policy.unrestricted:
        return PolicyCheck(provider=provider, ok=True, policy_revision=UNRESTRICTED_REVISION)

    path = policy_path(policy, provider)
    if path is None:
        reason = (
            "agy has no verified Enso launch contract and requires an unrestricted workspace"
            if provider == "agy"
            else f"unknown provider {provider!r}"
        )
        return PolicyCheck(provider=provider, ok=False, problems=(reason,))

    problems = _file_problems(path, workspace)
    if problems:
        return PolicyCheck(provider=provider, ok=False, problems=tuple(problems), policy_path=path)

    warnings: list[str] = []
    mcp_servers: tuple[str, ...] = ()
    mcp_digest: str | None = None
    if provider == "claude":
        problems, warnings, settings = _check_claude_settings(path)
        mcp_problems, servers, mcp_digest = _check_claude_mcp(policy, workspace)
        problems.extend(mcp_problems)
        mcp_servers = tuple(sorted(servers))
        if not problems and settings is not None:
            warnings.extend(_claude_mcp_rule_warnings(settings, mcp_servers))
            warnings.extend(_claude_mcp_secret_warnings(servers))
    elif provider == "codex":
        problems = _check_codex_config(path)
        problems.extend(_codex_tree_problems(policy, workspace, skip=path))

    if problems:
        return PolicyCheck(provider=provider, ok=False, problems=tuple(problems), policy_path=path)
    try:
        revision = _policy_revision(policy, provider, path, claude_mcp_digest=mcp_digest)
    except OSError as exc:
        return PolicyCheck(
            provider=provider,
            ok=False,
            problems=(f"could not hash native policy: {exc}",),
            policy_path=path,
        )
    return PolicyCheck(
        provider=provider,
        ok=True,
        warnings=tuple(warnings),
        policy_path=path,
        policy_revision=revision,
        mcp_servers=mcp_servers,
    )


def _file_problems(path: str, workspace: Workspace) -> list[str]:
    """Integrity checks every policy source file must pass."""
    label = os.path.basename(path)
    if not os.path.lexists(path):
        return [f"native policy not found at {path}"]
    if os.path.islink(path):
        return [f"{label} must be a regular file, not a symlink"]
    try:
        file_stat = os.stat(path)
    except OSError as exc:
        return [f"cannot stat {label}: {exc}"]
    if not stat.S_ISREG(file_stat.st_mode):
        return [f"{label} must be a regular file"]
    if file_stat.st_nlink != 1:
        return [f"{label} must not have hard links"]
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        return [f"{label} must be owner-only (chmod 600)"]
    real = os.path.realpath(path)
    ws_real = os.path.realpath(workspace.path)
    if real == ws_real or real.startswith(ws_real + os.sep):
        return [f"{label} resolves inside the workspace; the agent could rewrite it"]
    return []


def _codex_tree_files(root: str) -> list[tuple[str, str]]:
    """Return every regular source path as a stable relative-path list."""
    files: list[tuple[str, str]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            files.append((relative, path))
    return files


def _codex_tree_problems(policy: Policy, workspace: Workspace, *, skip: str) -> list[str]:
    """Validate every file copied into the immutable Codex policy snapshot."""
    root = _codex_source_root(policy)
    problems: list[str] = []
    if os.path.islink(root):
        problems.append("codex policy directory must not be a symlink")
        return problems
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            path = os.path.join(directory, name)
            if os.path.islink(path):
                relative = os.path.relpath(path, root)
                problems.append(f"{relative} must be a directory, not a symlink")
                dirnames.remove(name)
        for name in filenames:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative in _CODEX_SOURCE_RESERVED:
                problems.append(f"codex policy tree reserves {relative}")
            if os.path.abspath(path) != os.path.abspath(skip):
                problems.extend(_file_problems(path, workspace))
    return problems


def _check_claude_settings(path: str) -> tuple[list[str], list[str], dict | None]:
    """Validate settings.json, returning it parsed for further cross-checks."""
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"settings.json does not parse: {exc}"], [], None
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


def _check_claude_mcp(
    policy: Policy, workspace: Workspace
) -> tuple[list[str], dict, str | None]:
    """Validate the conventional claude/mcp.json when present.

    Returns (problems, servers, digest) where servers is the parsed mcpServers
    object and digest is the sha256 of the bytes those servers were parsed
    from, so the policy revision and the launched server set come from a
    single read; both are empty/None when the file is absent or unusable.
    Presence is tested with lexists, so a symlink at the conventional path is
    an integrity error, never absence.
    """
    path = _claude_mcp_path(policy)
    if not os.path.lexists(path):
        return [], {}, None
    problems = _file_problems(path, workspace)
    if problems:
        return problems, {}, None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        mcp = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"mcp.json does not parse: {exc}"], {}, None
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
                f'permission rule "{rule}" matches no MCP server in claude/mcp.json '
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
    warnings: list[str] = []
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
                    warnings.append(
                        f'mcp.json server "{name}" has a secret-shaped literal in '
                        f"{section}.{key}; use a ${{VAR}} reference"
                    )
    return warnings


def _check_codex_config(path: str) -> list[str]:
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"config.toml does not parse: {exc}"]
    if "developer_instructions" in config:
        return [
            "config.toml developer_instructions is reserved for Enso's shared "
            "instructions; move policy-specific guidance into the workspace AGENTS.md"
        ]
    has_profiles = "default_permissions" in config or "permissions" in config
    has_legacy = "sandbox_mode" in config or "sandbox_workspace_write" in config
    if has_profiles and has_legacy:
        return [
            "config.toml mixes permission profiles with legacy sandbox settings; "
            "Codex would silently use the legacy sandbox and ignore the profile"
        ]
    return []


def _manifest_revision(provider: str, manifest: dict[str, str]) -> str:
    payload = json.dumps(
        {"contract": LAUNCH_CONTRACT_VERSION, "provider": provider, "files": manifest},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _codex_manifest(root: str) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative, path in _codex_tree_files(root):
        if relative == _SNAPSHOT_MANIFEST:
            continue
        digest = regular_file_sha256(path)
        if digest is None:
            raise OSError(f"could not hash {path}")
        manifest[relative] = digest
    return manifest


def _policy_revision(
    policy: Policy, provider: str, path: str, *, claude_mcp_digest: str | None = None
) -> str:
    """Digest of the complete policy source tree plus the launch contract.

    ``claude_mcp_digest`` is the hash computed by ``_check_claude_mcp`` from
    the same bytes the resolved server set was parsed from; re-reading
    mcp.json here could hash a file that appeared or changed since the check.
    """
    digest = regular_file_sha256(path)
    if digest is None:
        raise OSError(f"could not hash {path}")
    manifest = {os.path.basename(path): digest}
    if provider == "claude":
        if claude_mcp_digest is not None:
            manifest["mcp.json"] = claude_mcp_digest
    elif provider == "codex":
        manifest = _codex_manifest(_codex_source_root(policy))
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
    if provider == "codex":
        try:
            home, ignore_rules = _stage_codex_home(policy, check.policy_revision)
        except PolicyError:
            raise
        except OSError as exc:
            # A read-only or unwritable policy dir must refuse this turn, not
            # escape as an unhandled error after the delivery was claimed.
            raise PolicyError(
                provider, (f"could not stage the Codex runtime home: {exc}",)
            ) from exc
        env["CODEX_HOME"] = home
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


def _minimal_env(provider: str, passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    """Allowlisted child environment: locale, passthrough, controlled PATH, provider auth.

    Profile ``env_passthrough`` names are copied before the launch-controlled
    assignments (PATH, provider auth keys, CODEX_HOME) so a launch-controlled
    value always wins even if validation of the reserved names is bypassed.
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


def _copy_codex_tree(source: str, destination: str) -> None:
    """Copy a validated native Codex tree into an unpublished directory."""
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames.sort()
        filenames.sort()
        relative_dir = os.path.relpath(directory, source)
        target_dir = destination if relative_dir == "." else os.path.join(destination, relative_dir)
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        os.chmod(target_dir, 0o700)
        for name in filenames:
            source_file = os.path.join(directory, name)
            target_file = os.path.join(target_dir, name)
            shutil.copyfile(source_file, target_file)
            os.chmod(target_file, 0o400)


def _write_snapshot_manifest(home: str, manifest: dict[str, str]) -> None:
    path = os.path.join(home, _SNAPSHOT_MANIFEST)
    with open(path, "x", encoding="utf-8") as file:
        json.dump(manifest, file, sort_keys=True, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.chmod(path, 0o400)


def _verify_codex_snapshot(home: str, revision: str) -> None:
    """Ensure a published revision still contains its original policy bytes."""
    manifest_path = os.path.join(home, _SNAPSHOT_MANIFEST)
    if os.path.islink(manifest_path):
        raise PolicyError("codex", ("staged policy manifest must not be a symlink",))
    try:
        with open(manifest_path, encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError("codex", (f"could not read staged policy manifest: {exc}",)) from exc
    if not isinstance(manifest, dict) or not all(
        isinstance(relative, str) and isinstance(digest, str)
        for relative, digest in manifest.items()
    ):
        raise PolicyError("codex", ("staged policy manifest is invalid",))
    if "config.toml" not in manifest or _manifest_revision("codex", manifest) != revision:
        raise PolicyError("codex", ("staged policy manifest has the wrong revision",))

    for relative, expected in manifest.items():
        if os.path.isabs(relative) or ".." in relative.split("/"):
            raise PolicyError("codex", ("staged policy manifest contains an unsafe path",))
        staged = os.path.join(home, *relative.split("/"))
        if regular_file_sha256(staged) != expected:
            raise PolicyError("codex", (f"staged {relative} digest does not match its manifest",))

    configured_rules = {
        relative
        for relative in manifest
        if relative.startswith("rules/") and relative.endswith(".rules")
    }
    staged_rules = {
        relative
        for relative, _path in _codex_tree_files(os.path.join(home, "rules"))
        if relative.endswith(".rules")
    }
    staged_rules = {f"rules/{relative}" for relative in staged_rules}
    if staged_rules != configured_rules:
        raise PolicyError("codex", ("staged rules do not match the policy manifest",))


def _publish_codex_snapshot(source: str, home: str, revision: str) -> None:
    """Atomically publish one immutable revision, tolerating a concurrent winner."""
    parent = os.path.dirname(home)
    temporary = tempfile.mkdtemp(prefix=f".{revision[:12]}-", dir=parent)
    try:
        _copy_codex_tree(source, temporary)
        manifest = _codex_manifest(temporary)
        if _manifest_revision("codex", manifest) != revision:
            raise PolicyError(
                "codex", ("staged policy digest does not match the checked revision",)
            )
        _write_snapshot_manifest(temporary, manifest)
        try:
            os.rename(temporary, home)
        except OSError as exc:
            concurrent_publish = exc.errno in {errno.EEXIST, errno.ENOTEMPTY}
            if not concurrent_publish or not os.path.isdir(home) or os.path.islink(home):
                raise
    finally:
        if os.path.isdir(temporary):
            shutil.rmtree(temporary)


def _read_codex_auth(path: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PolicyError("codex", (f"could not open Codex auth safely: {exc}",)) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PolicyError("codex", ("Codex auth must be a regular file",))
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PolicyError("codex", ("Codex auth changed while it was being read",))
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _stage_codex_auth(home: str) -> None:
    """Atomically refresh auth without ever exposing a partial credential file."""
    source = os.path.join(_user_codex_home(), "auth.json")
    destination = os.path.join(home, "auth.json")
    content = _read_codex_auth(source)
    if content is None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(destination)
        return

    descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=home)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.remove(temporary)


def _stage_codex_home(policy: Policy, revision: str) -> tuple[str, bool]:
    """Select an immutable, revision-keyed Codex policy snapshot and safe auth."""
    assert policy.policy_dir is not None
    source = _codex_source_root(policy)
    snapshots = os.path.join(policy.policy_dir, ".runtime", "codex-home")
    os.makedirs(snapshots, mode=0o700, exist_ok=True)
    os.chmod(snapshots, 0o700)
    home = os.path.join(snapshots, revision)
    if os.path.lexists(home):
        if os.path.islink(home) or not os.path.isdir(home):
            raise PolicyError("codex", ("staged policy revision is not a directory",))
    else:
        _publish_codex_snapshot(source, home, revision)
    _verify_codex_snapshot(home, revision)
    _stage_codex_auth(home)

    log.debug("Selected Codex home at %s (revision %s)", home, revision[:12])
    staged_rules = os.path.join(home, "rules")
    has_rules = any(
        relative.endswith(".rules") for relative, _path in _codex_tree_files(staged_rules)
    )
    return home, not has_rules

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
import subprocess
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
        problems.extend(_tree_problems(policy, workspace, "codex", skip=path))
    elif provider == "grok":
        problems, warnings = _check_grok_config(path)
        problems.extend(_tree_problems(policy, workspace, "grok", skip=path))

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


def _tree_files(root: str) -> list[tuple[str, str]]:
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


def _tree_problems(policy: Policy, workspace: Workspace, provider: str, *, skip: str) -> list[str]:
    """Validate every file copied into a provider's immutable policy snapshot."""
    root = _provider_source_root(policy, provider)
    reserved = _STAGED_SOURCE_RESERVED | _PROVIDER_SOURCE_RESERVED.get(provider, frozenset())
    problems: list[str] = []
    if os.path.islink(root):
        problems.append(f"{provider} policy directory must not be a symlink")
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
            if relative in reserved:
                problems.append(f"{provider} policy tree reserves {relative}")
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


def _check_grok_config(path: str) -> tuple[list[str], list[str]]:
    """Statically reject config shapes the Grok CLI loads as zero rules.

    The CLI reports no error and no skipped entry for a missing or misspelled
    [permission] table, a wrong-shaped key, or an unknown one — it just loads
    zero rules and runs wide open, so every such shape is a problem here.

    Folder trust is checked alongside the rules because it is the gate, not a
    rule: disabling it admits workspace-planted config, hooks, and MCP servers
    into the launch, which is the one way a policy file can widen itself.
    """
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"config.toml does not parse: {exc}"], []
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
            problems.append(
                f"config.toml [permission] key {key!r} is not one of "
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


def _tree_manifest(root: str) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative, path in _tree_files(root):
        if relative == _SNAPSHOT_MANIFEST:
            continue
        digest = regular_file_sha256(path)
        if digest is None:
            raise OSError(f"could not hash {path}")
        manifest[relative] = digest
    return manifest


def _grok_manifest(root: str) -> dict[str, str]:
    """Tree manifest hashing the effective (pre-seeded) config.toml bytes.

    The revision must cover exactly what staging publishes, so the config
    entry digests the bytes after the marketplace pre-seed, never the raw
    operator file.
    """
    manifest = _tree_manifest(root)
    config_path = os.path.join(root, "config.toml")
    with open(config_path, "rb") as file:
        raw = file.read()
    manifest["config.toml"] = hashlib.sha256(_grok_effective_config(raw)).hexdigest()
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
        manifest = _tree_manifest(_provider_source_root(policy, "codex"))
    elif provider == "grok":
        manifest = _grok_manifest(_provider_source_root(policy, "grok"))
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


def verify_grok_rules(workspace: Workspace, policy: Policy, grok_path: str) -> list[str]:
    """Dynamically confirm the Grok CLI loads the staged policy's rules.

    A wrong-shaped permission config loads zero rules with no error and no
    skipped entry, so the static checks are backed by running ``grok inspect
    --json`` exactly as a launch would — workspace cwd, staged revision home
    in GROK_HOME — and requiring the reported ``permissions.loaded`` to equal
    the rule count the effective config declares. The inspect run gets a
    scratch HOME: grok counts the operator's always-trusted home-scope
    vendor-compat rules (``~/.claude/settings.json``) into the total, which
    would both false-fail the equality and let ambient rules mask a silently
    dropped policy rule. Failures come back as problems; this never raises.
    """
    try:
        launch = prepare_launch(workspace, policy, "grok")
    except PolicyError as exc:
        return list(exc.problems)
    if launch.home is None or launch.env is None:
        return []  # unrestricted launches have no staged policy to verify
    try:
        with open(os.path.join(launch.home, "config.toml"), "rb") as file:
            declared = _grok_rule_count(tomllib.load(file))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"could not read the staged grok config: {exc}"]
    try:
        with tempfile.TemporaryDirectory(prefix="enso-grok-inspect-") as scratch_home:
            completed = subprocess.run(
                [grok_path, "inspect", "--json"],
                cwd=workspace.path,
                env={**launch.env, "HOME": scratch_home},
                capture_output=True,
                text=True,
                timeout=60,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"could not run grok inspect: {exc}"]
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return [f"grok inspect exited {completed.returncode}: {detail}"]
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return [f"grok inspect --json output does not parse: {exc}"]
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
        # More rules than the policy wrote means something outside it reached
        # the launch — a trusted workspace contributing its own config is the
        # path that matters, since that is a policy widening itself.
        sources = permissions.get("sources") if isinstance(permissions, dict) else None
        detail = ""
        if isinstance(sources, list) and sources:
            named = ", ".join(str(source) for source in sources)
            detail = f" (sources: {named})"
        return [
            f"grok inspect loaded {loaded} permission rules but the policy declares "
            f"{declared}; rules are reaching the launch from outside the policy"
            f"{detail}"
        ]
    return []


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


def _copy_codex_tree(source: str, destination: str) -> None:
    """Copy a validated native policy tree into an unpublished directory."""
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


def _verify_staged_snapshot(home: str, revision: str, provider: str) -> None:
    """Ensure a published revision still contains its original policy bytes."""
    manifest_path = os.path.join(home, _SNAPSHOT_MANIFEST)
    if os.path.islink(manifest_path):
        raise PolicyError(provider, ("staged policy manifest must not be a symlink",))
    try:
        with open(manifest_path, encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
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
        staged = os.path.join(home, *relative.split("/"))
        if regular_file_sha256(staged) != expected:
            raise PolicyError(provider, (f"staged {relative} digest does not match its manifest",))

    if provider != "codex":
        return  # .rules files are a codex concept; nothing further to cross-check
    configured_rules = {
        relative
        for relative in manifest
        if relative.startswith("rules/") and relative.endswith(".rules")
    }
    staged_rules = {
        relative
        for relative, _path in _tree_files(os.path.join(home, "rules"))
        if relative.endswith(".rules")
    }
    staged_rules = {f"rules/{relative}" for relative in staged_rules}
    if staged_rules != configured_rules:
        raise PolicyError(provider, ("staged rules do not match the policy manifest",))


def _preseed_grok_marketplace(home: str) -> None:
    """Rewrite an unpublished config.toml copy with its effective bytes."""
    path = os.path.join(home, "config.toml")
    with open(path, "rb") as file:
        raw = file.read()
    effective = _grok_effective_config(raw)
    if effective == raw:
        return
    os.chmod(path, 0o600)
    with open(path, "wb") as file:
        file.write(effective)
    os.chmod(path, 0o400)


def _publish_staged_snapshot(source: str, home: str, revision: str, provider: str) -> None:
    """Atomically publish one immutable revision, tolerating a concurrent winner."""
    parent = os.path.dirname(home)
    temporary = tempfile.mkdtemp(prefix=f".{revision[:12]}-", dir=parent)
    try:
        _copy_codex_tree(source, temporary)
        if provider == "grok":
            _preseed_grok_marketplace(temporary)
        manifest = _tree_manifest(temporary)
        if _manifest_revision(provider, manifest) != revision:
            raise PolicyError(
                provider, ("staged policy digest does not match the checked revision",)
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


def _stage_provider_auth(home: str, provider: str, user_home: str) -> None:
    """Atomically refresh auth without ever exposing a partial credential file."""
    source = os.path.join(user_home, "auth.json")
    destination = os.path.join(home, "auth.json")
    content = _read_provider_auth(source, provider)
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


def _stage_provider_home(policy: Policy, revision: str, provider: str, user_home: str) -> str:
    """Select an immutable, revision-keyed policy snapshot and safe auth."""
    assert policy.policy_dir is not None
    source = _provider_source_root(policy, provider)
    snapshots = os.path.join(policy.policy_dir, ".runtime", f"{provider}-home")
    os.makedirs(snapshots, mode=0o700, exist_ok=True)
    os.chmod(snapshots, 0o700)
    home = os.path.join(snapshots, revision)
    if os.path.lexists(home):
        if os.path.islink(home) or not os.path.isdir(home):
            raise PolicyError(provider, ("staged policy revision is not a directory",))
    else:
        _publish_staged_snapshot(source, home, revision, provider)
    _verify_staged_snapshot(home, revision, provider)
    _stage_provider_auth(home, provider, user_home)

    log.debug("Selected %s home at %s (revision %s)", provider, home, revision[:12])
    return home


def _stage_codex_home(policy: Policy, revision: str) -> tuple[str, bool]:
    """Codex snapshot selection plus whether any .rules files were staged."""
    home = _stage_provider_home(policy, revision, "codex", _user_codex_home())
    staged_rules = os.path.join(home, "rules")
    has_rules = any(
        relative.endswith(".rules") for relative, _path in _tree_files(staged_rules)
    )
    return home, not has_rules


def _stage_grok_home(policy: Policy, revision: str) -> str:
    """Grok snapshot selection; grok has no .rules concept to toggle."""
    return _stage_provider_home(policy, revision, "grok", _user_grok_home())

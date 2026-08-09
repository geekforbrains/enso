"""Native policy selection and launch construction for policy-controlled work.

Enso does not compile or grade provider policy. The operator authors each
CLI's native file under the workspace's ``policy_dir``; this module verifies
the plumbing — the file exists, is a regular owner-only file outside the
workspace, and parses — computes the ``policy_revision`` digest, and builds
the launch inputs (arguments live in each provider class, the minimal child
environment and staged runtime home live here). Anything it cannot verify
fails closed with a specific diagnostic. See docs/specs/permissions.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass

from .fsutil import regular_file_sha256
from .teams import POLICY_FILES, Workspace

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# Bump when the launch contract (flags, env construction) changes, so a new
# contract produces a new policy_revision and therefore a fresh execution key.
LAUNCH_CONTRACT_VERSION = "1"
UNRESTRICTED_REVISION = f"unrestricted:v{LAUNCH_CONTRACT_VERSION}"

# Environment kept for policy-controlled provider subprocesses. Everything
# else — 1Password service tokens, transport credentials, secrets/*.env
# projections — is withheld; allowlisting means a newly added secret can
# never leak by omission.
_KEEP_ENV = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "SHELL")


class PolicyError(Exception):
    """A policy-controlled launch cannot be constructed; dispatch must refuse."""

    def __init__(self, provider: str, problems: tuple[str, ...]):
        self.provider = provider
        self.problems = problems
        super().__init__(f"{provider}: " + "; ".join(problems))


@dataclass(frozen=True)
class PolicyCheck:
    """Result of statically checking one workspace/provider pair."""

    provider: str
    ok: bool
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    policy_path: str | None = None
    policy_revision: str | None = None


@dataclass(frozen=True)
class Launch:
    """Everything a provider spawn needs beyond the prompt and model."""

    mode: str  # "unrestricted" | "policy"
    provider: str
    policy_path: str | None
    home: str | None  # staged CODEX_HOME for codex policy launches
    policy_revision: str
    env: dict[str, str] | None  # None → inherit the parent environment
    ignore_rules: bool = True  # codex: no .rules files were configured


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


def policy_path(workspace: Workspace, provider: str) -> str | None:
    """Canonical native-policy path for a provider, or None if it has none."""
    rel = POLICY_FILES.get(provider)
    if workspace.policy_dir is None or rel is None:
        return None
    return os.path.join(workspace.policy_dir, rel)


def _codex_rules_dir(workspace: Workspace) -> str:
    assert workspace.policy_dir is not None
    return os.path.join(workspace.policy_dir, "codex", "rules")


def _codex_rules_files(workspace: Workspace) -> list[str]:
    rules_dir = _codex_rules_dir(workspace)
    if not os.path.isdir(rules_dir):
        return []
    return sorted(
        os.path.join(rules_dir, name)
        for name in os.listdir(rules_dir)
        if name.endswith(".rules")
    )


def check_provider(workspace: Workspace, provider: str) -> PolicyCheck:
    """Statically validate the launch plumbing for one provider.

    Verifies selection and integrity, not semantics: a file that parses and
    deliberately grants broad access is still the operator's policy.
    """
    if workspace.unrestricted:
        return PolicyCheck(
            provider=provider, ok=True, policy_revision=UNRESTRICTED_REVISION
        )

    path = policy_path(workspace, provider)
    if path is None:
        reason = (
            "agy has no permission model and requires an unrestricted workspace"
            if provider == "agy"
            else f"unknown provider {provider!r}"
        )
        return PolicyCheck(provider=provider, ok=False, problems=(reason,))

    problems = _file_problems(path, workspace)
    if problems:
        return PolicyCheck(
            provider=provider, ok=False, problems=tuple(problems), policy_path=path
        )

    warnings: list[str] = []
    if provider == "claude":
        problems, warnings = _check_claude_settings(path)
    elif provider == "codex":
        problems = _check_codex_config(path)
        for rules_file in _codex_rules_files(workspace):
            problems.extend(_file_problems(rules_file, workspace))

    if problems:
        return PolicyCheck(
            provider=provider, ok=False, problems=tuple(problems), policy_path=path
        )
    return PolicyCheck(
        provider=provider,
        ok=True,
        warnings=tuple(warnings),
        policy_path=path,
        policy_revision=_policy_revision(workspace, provider, path),
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
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        return [f"{label} must be owner-only (chmod 600)"]
    real = os.path.realpath(path)
    ws_real = os.path.realpath(workspace.path)
    if real == ws_real or real.startswith(ws_real + os.sep):
        return [f"{label} resolves inside the workspace; the agent could rewrite it"]
    return []


def _check_claude_settings(path: str) -> tuple[list[str], list[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"settings.json does not parse: {exc}"], []
    if not isinstance(settings, dict):
        return ["settings.json must be a JSON object"], []
    warnings = []
    sandbox = settings.get("sandbox")
    if not (isinstance(sandbox, dict) and sandbox.get("enabled") is True):
        warnings.append(
            "sandbox.enabled is not true: permission rules govern Claude's own "
            "tools only; subprocesses need the OS sandbox or outer isolation"
        )
    return [], warnings


def _check_codex_config(path: str) -> list[str]:
    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"config.toml does not parse: {exc}"]
    has_profiles = "default_permissions" in config or "permissions" in config
    has_legacy = "sandbox_mode" in config or "sandbox_workspace_write" in config
    if has_profiles and has_legacy:
        return [
            "config.toml mixes permission profiles with legacy sandbox settings; "
            "Codex would silently use the legacy sandbox and ignore the profile"
        ]
    return []


def _policy_revision(workspace: Workspace, provider: str, path: str) -> str:
    """Digest of the complete policy source tree plus the launch contract."""
    manifest: dict[str, str | None] = {os.path.basename(path): regular_file_sha256(path)}
    if provider == "codex":
        for rules_file in _codex_rules_files(workspace):
            manifest[f"rules/{os.path.basename(rules_file)}"] = regular_file_sha256(
                rules_file
            )
    payload = json.dumps(
        {"contract": LAUNCH_CONTRACT_VERSION, "provider": provider, "files": manifest},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def prepare_launch(workspace: Workspace, provider: str) -> Launch:
    """Build the launch for one provider, failing closed on any problem."""
    check = check_provider(workspace, provider)
    if not check.ok:
        raise PolicyError(provider, check.problems)
    if workspace.unrestricted:
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
    env = _minimal_env(provider)
    home: str | None = None
    ignore_rules = True
    if provider == "codex":
        home, ignore_rules = _stage_codex_home(workspace, check.policy_revision)
        env["CODEX_HOME"] = home
    return Launch(
        mode="policy",
        provider=provider,
        policy_path=check.policy_path,
        home=home,
        policy_revision=check.policy_revision,
        env=env,
        ignore_rules=ignore_rules,
    )


def _minimal_env(provider: str) -> dict[str, str]:
    """Allowlisted child environment: locale, controlled PATH, provider auth."""
    from .providers import PROVIDER_CLASSES

    env = {key: os.environ[key] for key in _KEEP_ENV if key in os.environ}
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


def _stage_codex_home(workspace: Workspace, revision: str) -> tuple[str, bool]:
    """Stage a byte-for-byte copy of the Codex policy into a service-owned home.

    Codex reads ``$CODEX_HOME/config.toml``; pointing CODEX_HOME at a staged
    tree both selects the operator's policy and keeps the ambient user config,
    hooks, and rules out. Configuration plumbing, not compilation: staged
    bytes must equal the source or the launch fails. Auth is copied from the
    user Codex home so the CLI can authenticate; session state lives in the
    staged home, giving per-workspace isolation.
    """
    assert workspace.policy_dir is not None
    home = os.path.join(workspace.policy_dir, ".runtime", "codex-home")
    os.makedirs(home, mode=0o700, exist_ok=True)

    source = policy_path(workspace, "codex")
    assert source is not None
    staged_config = os.path.join(home, "config.toml")
    shutil.copyfile(source, staged_config)
    os.chmod(staged_config, 0o600)
    if regular_file_sha256(staged_config) != regular_file_sha256(source):
        raise PolicyError("codex", ("staged config digest does not match the source",))

    rules_files = _codex_rules_files(workspace)
    staged_rules = os.path.join(home, "rules")
    if os.path.isdir(staged_rules):
        shutil.rmtree(staged_rules)
    if rules_files:
        os.makedirs(staged_rules, mode=0o700)
        for rules_file in rules_files:
            staged = os.path.join(staged_rules, os.path.basename(rules_file))
            shutil.copyfile(rules_file, staged)
            os.chmod(staged, 0o600)
            if regular_file_sha256(staged) != regular_file_sha256(rules_file):
                raise PolicyError(
                    "codex", ("staged rules digest does not match the source",)
                )

    auth = os.path.join(_user_codex_home(), "auth.json")
    if os.path.isfile(auth):
        staged_auth = os.path.join(home, "auth.json")
        shutil.copyfile(auth, staged_auth)
        os.chmod(staged_auth, 0o600)

    log.debug("Staged Codex home at %s (revision %s)", home, revision[:12])
    return home, not rules_files

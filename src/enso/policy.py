"""Restricted workspaces: the provider policy file a launch must find before it runs.

Enso does not define a permission language of its own. A workspace marked ``restricted``
in ``config.json`` must instead hold the CLI's own project-level policy file, in the
place that CLI reads it from the working directory, and the launch must not carry the
flag that tells the CLI to bypass its own policy. Codex and Grok read a project file only
below a root they trust, so for them the CLI's own user config must also trust the home.
Enso checks only that much: the file exists, the bypass flag is absent, and the CLI will
look at the file. What the file says, and whether the CLI honours it, stays the CLI's
business. See ``docs/configuration.md`` § Restricted workspaces.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .config import Config

# The project-level policy file each CLI reads from its working directory, relative to the
# workspace. A provider absent here has no workspace-scoped policy mechanism, so a
# restricted workspace cannot run it at all.
POLICY_FILES: dict[str, str] = {
    "claude": ".claude/settings.json",
    "codex": ".codex/config.toml",
    "grok": ".grok/config.toml",
    "opencode": "opencode.json",
}
# The flags that make a CLI discard its policy file outright. The other unattended flags
# ``enso setup`` writes (Claude's ``--dangerously-skip-permissions``, Grok's
# ``--always-approve``, OpenCode's ``--auto``) were verified to leave the file's deny rules
# in force, so a policy still means something under them and they are not refused.
BYPASS_FLAGS: dict[str, tuple[str, ...]] = {
    "codex": ("--dangerously-bypass-approvals-and-sandbox", "--yolo"),
}


class PolicyError(Exception):
    """A restricted workspace cannot launch this provider; the message is user-facing."""


def policy_file(workspace_dir: Path, provider: str) -> Path | None:
    """Where ``provider``'s policy file lives in the workspace, or None when it has none."""
    relative = POLICY_FILES.get(provider)
    return workspace_dir / relative if relative else None


def _trust_entry(file: Path, table: str, keys: tuple[str, ...]) -> dict[str, object] | None:
    """The first ``[<table>."<key>"]`` entry of a CLI's TOML file, or None when it has none.

    ``keys`` are the spellings of the root that CLI itself matches; a spelling it ignores
    must not count. A missing, unreadable, or malformed file trusts nothing.
    """
    try:
        entries = tomllib.loads(file.read_text()).get(table)
    except OSError, ValueError:  # ValueError covers a bad encoding and bad TOML
        return None
    if not isinstance(entries, dict):
        return None
    for key in keys:
        entry = entries.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def check(config: Config, workspace: str, provider: str) -> None:
    """Raise PolicyError when ``workspace`` is restricted and ``provider`` may not run in it.

    Nothing is checked for an unrestricted workspace. A restricted one needs the provider's
    policy file present as a regular file, its effective arguments free of a flag that
    would make the CLI discard that file, and, for a CLI that reads the file only below a
    trusted root, that root — Enso's home — trusted in the CLI's user config.
    """
    override = config.workspaces.get(workspace)
    if override is None or not override.restricted:
        return
    fix = 'or set "restricted": false for the workspace in config.json'
    path = policy_file(config.paths.workspace(workspace), provider)
    if path is None:
        raise PolicyError(
            f"workspace {workspace} is restricted, but {provider} has no workspace policy "
            f"file; use another provider {fix}"
        )
    if not path.is_file():
        raise PolicyError(
            f"workspace {workspace} is restricted and has no {provider} policy file; add "
            f"{POLICY_FILES[provider]} to the workspace {fix}"
        )
    args = config.provider_args(workspace, provider)
    bypass = BYPASS_FLAGS.get(provider, ())
    found = [arg for arg in args if arg in bypass]
    if found:
        raise PolicyError(
            f"workspace {workspace} is restricted, but its {provider} arguments include "
            f"{found[0]}, which discards the policy file; override providers.{provider}.args "
            f"for the workspace {fix}"
        )
    # The home, not the workspace, is the root these CLIs trust: it is the Git root above
    # every workspace, and it is what they record when they trust a project themselves, as
    # the absolute, resolved path without a trailing slash. Codex matches that key exactly;
    # Grok also matches the slash spelling its inspect output prints, which an operator may
    # have copied.
    root = str(config.paths.home.resolve())
    if provider == "codex":
        file = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"
        entry = _trust_entry(file, "projects", (root,))
        if entry is None or entry.get("trust_level") != "trusted":
            raise PolicyError(
                f"workspace {workspace} is restricted, but codex applies the policy file only "
                f'under a trusted root; add [projects."{root}"] trust_level = "trusted" to '
                f"{file} {fix}"
            )
    elif provider == "grok" and "--trust" not in args:
        file = Path(os.environ.get("GROK_HOME") or "~/.grok").expanduser()
        file /= "trusted_folders.toml"
        entry = _trust_entry(file, "folders", (root, root + "/"))
        if entry is None or entry.get("trusted") is not True:
            raise PolicyError(
                f"workspace {workspace} is restricted, but grok loads the policy file only "
                f"under a trusted root; add --trust to providers.grok.args, or "
                f'[folders."{root}"] trusted = true to {file}, {fix}'
            )

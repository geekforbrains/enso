"""The restricted-workspace check: a policy file present, no bypass flag, a trusted home."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from enso import policy
from enso.config import Config, Paths, WorkspaceConfig

FIX = 'or set "restricted": false for the workspace in config.json'
# The CLI's own user-level trust file, and the entry that trusts a root, per provider.
TRUST_FILES = {"codex": "config.toml", "grok": "trusted_folders.toml"}
TRUST_ENTRIES = {
    "codex": '[projects."{root}"]\ntrust_level = "trusted"\n',
    "grok": '[folders."{root}"]\ntrusted = true\n',
}


def restricted(config: Config, provider_args: dict[str, tuple[str, ...]] | None = None) -> Config:
    override = WorkspaceConfig(restricted=True, provider_args=provider_args or {})
    return replace(config, workspaces={"default": override})


@pytest.fixture(autouse=True)
def cli_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Empty scratch ``CODEX_HOME`` and ``GROK_HOME``: no test here reads the real ones."""
    homes = {}
    for provider in TRUST_FILES:
        homes[provider] = tmp_path / provider
        homes[provider].mkdir()
        monkeypatch.setenv(f"{provider.upper()}_HOME", str(homes[provider]))
    return homes


@pytest.fixture
def policed(enso_home: Paths, config: Config) -> Config:
    """A restricted ``default`` that holds a Codex and a Grok policy file."""
    for provider in TRUST_FILES:
        path = policy.policy_file(enso_home.workspace("default"), provider)
        assert path is not None
        path.parent.mkdir()
        path.write_text("[permission]\n")
    return restricted(config)


def trust(cli_homes: dict[str, Path], provider: str, root: str) -> Path:
    """Write the entry the CLI itself records when it trusts ``root``; returns the file."""
    file = cli_homes[provider] / TRUST_FILES[provider]
    file.write_text(TRUST_ENTRIES[provider].format(root=root))
    return file


def test_an_unrestricted_workspace_is_never_checked(config: Config) -> None:
    policy.check(config, "default", "claude")  # no file, no problem
    policy.check(config, "default", "agy")  # not even a provider without a mechanism


@pytest.mark.parametrize(
    ("provider", "relative"),
    [("claude", ".claude/settings.json"), ("codex", ".codex/config.toml")],
)
def test_a_restricted_workspace_needs_the_providers_own_file(
    enso_home: Paths, config: Config, cli_homes: dict[str, Path], provider: str, relative: str
) -> None:
    config = restricted(config)
    with pytest.raises(policy.PolicyError) as missing:
        policy.check(config, "default", provider)
    assert str(missing.value) == (
        f"workspace default is restricted and has no {provider} policy file; add {relative} "
        f"to the workspace {FIX}"
    )
    path = enso_home.workspace("default") / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # a directory in the file's place is not a policy
    with pytest.raises(policy.PolicyError):
        policy.check(config, "default", provider)
    path.rmdir()
    path.write_text("{}")
    if provider in TRUST_FILES:
        trust(cli_homes, provider, str(enso_home.home))
    policy.check(config, "default", provider)


def test_a_flag_that_discards_the_file_is_refused(policed: Config) -> None:
    yolo = restricted(policed, provider_args={"codex": ("--yolo",)})
    with pytest.raises(policy.PolicyError) as flagged:
        policy.check(yolo, "default", "codex")
    assert str(flagged.value) == (
        "workspace default is restricted, but its codex arguments include --yolo, "
        f"which discards the policy file; override providers.codex.args for the workspace {FIX}"
    )


def test_a_provider_without_a_workspace_policy_cannot_run_restricted(config: Config) -> None:
    with pytest.raises(policy.PolicyError) as refused:
        policy.check(restricted(config), "default", "agy")
    assert str(refused.value).startswith(
        "workspace default is restricted, but agy has no workspace policy file; use another"
    )


@pytest.mark.parametrize(
    "content",
    [
        None,  # no user config at all
        "[projects\n",  # malformed
        "projects = 1\n",  # not a table
        '[projects."{root}"]\ntrust_level = "untrusted"\n',
        '[projects."/somewhere/else"]\ntrust_level = "trusted"\n',
        # Codex matches its key exactly; the slash spelling Grok tolerates is not read.
        '[projects."{root}/"]\ntrust_level = "trusted"\n',
    ],
    ids=["missing", "malformed", "not-a-table", "untrusted", "other-root", "trailing-slash"],
)
def test_codex_needs_the_home_trusted_in_its_user_config(
    enso_home: Paths, policed: Config, cli_homes: dict[str, Path], content: str | None
) -> None:
    file = cli_homes["codex"] / "config.toml"
    if content is not None:
        file.write_text(content.format(root=enso_home.home))
    with pytest.raises(policy.PolicyError) as refused:
        policy.check(policed, "default", "codex")
    assert str(refused.value) == (
        "workspace default is restricted, but codex applies the policy file only under a "
        f'trusted root; add [projects."{enso_home.home}"] trust_level = "trusted" to {file} '
        f"{FIX}"
    )


def test_a_trusted_home_is_resolved_the_way_the_clis_key_it(
    enso_home: Paths, tmp_path: Path, policed: Config, cli_homes: dict[str, Path]
) -> None:
    trust(cli_homes, "codex", str(enso_home.home))
    policy.check(policed, "default", "codex")
    # The CLIs record the real path, so a home reached through a symlink still matches.
    link = tmp_path / "link"
    link.symlink_to(enso_home.home)
    policy.check(replace(policed, paths=Paths(link)), "default", "codex")
    # Grok's inspect output prints the root with a trailing slash; copied verbatim, it counts.
    trust(cli_homes, "grok", f"{enso_home.home}/")
    policy.check(policed, "default", "grok")  # --always-approve in the global args: allowed


def test_grok_needs_the_home_trusted_or_the_trust_flag(
    enso_home: Paths, policed: Config, cli_homes: dict[str, Path]
) -> None:
    file = cli_homes["grok"] / "trusted_folders.toml"
    with pytest.raises(policy.PolicyError) as refused:
        policy.check(policed, "default", "grok")
    assert str(refused.value) == (
        "workspace default is restricted, but grok loads the policy file only under a "
        f'trusted root; add --trust to providers.grok.args, or [folders."{enso_home.home}"] '
        f"trusted = true to {file}, {FIX}"
    )
    file.write_text(f'[folders."{enso_home.home}"]\ntrusted = false\n')
    with pytest.raises(policy.PolicyError):
        policy.check(policed, "default", "grok")
    file.write_text("[folders\n")
    with pytest.raises(policy.PolicyError):
        policy.check(policed, "default", "grok")
    # --trust makes the launch itself trust the root (and record it), so nothing to check.
    flagged = restricted(policed, provider_args={"grok": ("--always-approve", "--trust")})
    policy.check(flagged, "default", "grok")
    assert file.read_text() == "[folders\n"  # Enso never writes the entry itself

"""Home seeding and the setup wizard's transport paths with the platform calls stubbed."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import service, workspaces
from enso.cli import app
from enso.cli import setup as wizard
from enso.config import Agent, Config, Paths, load_config
from enso.jobs import find_job
from enso.runtime import ORIGIN_HEADER


def test_seed_home_writes_once_and_refreshes_skills_with_a_backup(enso_home: Paths) -> None:
    done = workspaces.seed_home(enso_home)
    assert enso_home.agents_md.read_text().startswith("# Enso")
    assert os.readlink(enso_home.home / "CLAUDE.md") == "AGENTS.md"
    assert sorted(p.name for p in enso_home.skills.iterdir()) == list(workspaces.BUNDLED_SKILLS)
    for link in (enso_home.home / ".claude" / "skills", enso_home.home / ".agents" / "skills"):
        assert os.readlink(link) == "../skills" and (link / "enso-jobs" / "SKILL.md").is_file()
    # AGENTS.md, bundled files and skills, shared knowledge, the three links, and git init.
    assert (enso_home.home / ".git").is_dir() and enso_home.knowledge.is_dir()
    assert (
        len(done) == 1 + len(workspaces.BUNDLED_FILES) + len(workspaces.bundled_skill_files()) + 5
    )
    assert workspaces.seed_home(enso_home) == []
    skill = enso_home.skills / "enso-slack" / "SKILL.md"
    skill.write_text("edited")
    assert workspaces.seed_home(enso_home) == []  # never touched without refresh_skills
    workspaces.seed_home(enso_home, refresh_skills=True)
    assert (
        skill.read_text().startswith("---") and skill.with_suffix(".md.bak").read_text() == "edited"
    )


def test_seed_jobs_stamps_the_agent_and_writes_once(enso_home: Paths) -> None:
    job_dir = enso_home.workspace_jobs("default") / "enso-audit"
    done = workspaces.seed_jobs(enso_home, Agent("claude", "opus", "high"))
    assert done == [
        f"wrote {enso_home.workspace_jobs('default') / job / name}"
        for job in workspaces.BUNDLED_JOBS
        for name in ("JOB.md", "prerun.sh")
    ]
    text = (job_dir / "JOB.md").read_text()
    assert 'provider: "claude"\nmodel: "opus"\neffort: "high"\n' in text
    assert re.findall(r"\{\{\w+\}\}", text) == ["{{prerun_output}}"]  # the runner's, kept
    assert workspaces.seed_jobs(enso_home, Agent("claude", "opus", "high")) == []
    (job_dir / "JOB.md").write_text(text.replace("enabled: true", "enabled: false"))
    (job_dir / "prerun.sh").unlink()
    assert workspaces.seed_jobs(enso_home, Agent("codex", "sol", "low")) == []
    assert "enabled: false" in (job_dir / "JOB.md").read_text()  # the directory is the operator's
    assert not (job_dir / "prerun.sh").exists()  # a deleted script is not put back either
    workspaces.seed_home(enso_home, refresh_skills=True)  # a skills refresh never reaches jobs
    assert [path.name for path in job_dir.iterdir()] == ["JOB.md"]


def test_bundled_content_mirrors_the_home(enso_home: Paths) -> None:
    bundled = Path(workspaces.__file__).parent / "bundled"
    files = sorted(
        path.relative_to(bundled)
        for path in bundled.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    template = Path("workspace") / "AGENTS.md"
    assert template in files and len(files) > 2
    jobs = {path.parts[1] for path in files if path.parts[0] == "jobs"}
    assert jobs == set(workspaces.BUNDLED_JOBS)  # dropped in but unlisted: never seeded or exempt
    assert {str(path) for path in files if path.parts[0] == "skills"} == set(
        workspaces.bundled_skill_files()
    )
    stamps = {"{{provider}}": "claude", "{{model}}": "opus", "{{effort}}": "high"}

    workspaces.seed_home(enso_home)
    workspaces.seed_jobs(enso_home, Agent("claude", "opus", "high"))
    root = workspaces.create_workspace(enso_home, "meteor")

    for relative in files:
        if relative == template:
            continue
        expected = (bundled / relative).read_text()
        if relative.parts[0] == "jobs":
            for placeholder, value in stamps.items():
                expected = expected.replace(placeholder, value)
            relative = Path("workspaces/default") / relative
        assert (enso_home.home / relative).read_text() == expected
        assert set(re.findall(r"\{\{\w+\}\}", expected)) <= {"{{prerun_output}}"}
    stamped = (bundled / template).read_text().replace("{{workspace_name}}", "meteor")
    assert (root / "AGENTS.md").read_text() == stamped and "{{" not in stamped


# Inline-code paths the templates point the agent at: directories, and the two instruction files.
MENTIONED_PATHS = re.compile(r"`([\w.-]+/|AGENTS\.md|CLAUDE\.md)`")


def test_templates_mention_only_paths_that_exist(enso_home: Paths) -> None:
    workspaces.seed_home(enso_home)
    root = workspaces.create_workspace(enso_home, "meteor")
    stamped = (root / "AGENTS.md").read_text()
    assert stamped.startswith("# meteor\n") and "## Purpose" in stamped
    for text in (enso_home.agents_md.read_text(), stamped):
        mentioned = set(MENTIONED_PATHS.findall(text))
        assert {"knowledge/", "drafts/", "uploads/"} <= mentioned
        assert [path for path in sorted(mentioned) if not (root / path).exists()] == []


# The general prose must not tell the agent which platform this turn came from; the origin block
# does. A platform name survives only where it identifies a real field, skill, or contract.
PLATFORM_NAME = re.compile(r"[Ss]lack|[Tt]elegram")
PLATFORM_SPECIFIC = re.compile(r"ENSO_ORIGIN_|enso-slack|rich-format contract")


def test_home_instructions_leave_the_current_platform_to_the_origin_block(
    enso_home: Paths,
) -> None:
    workspaces.seed_home(enso_home)
    general, marker, turn = enso_home.agents_md.read_text().partition("## The turn")

    assert marker and not PLATFORM_NAME.search(general)  # voice and behaviour assume no platform
    assert ORIGIN_HEADER.split("—")[0].strip() in turn  # they point at the block instead
    for line in turn.splitlines():
        if PLATFORM_NAME.search(line):
            assert PLATFORM_SPECIFIC.search(line), line

    root = workspaces.create_workspace(enso_home, "meteor")
    assert not PLATFORM_NAME.search((root / "AGENTS.md").read_text())  # the template names none


def test_ensure_layout_creates_and_repoints_but_never_removes(enso_home: Paths) -> None:
    root = enso_home.workspace("default")  # the fixture makes a bare directory
    (root / "AGENTS.md").write_text("# default\n")
    (root / "drafts").mkdir()
    (root / "drafts" / "post.md").write_text("keep me")
    (root / ".claude").mkdir()
    os.symlink("/nowhere", root / ".claude" / "skills")  # points elsewhere: repointed
    (root / ".agents" / "skills").mkdir(parents=True)  # a real directory: left alone

    done = workspaces.ensure_layout(root)

    verbs = [line.split(" ", 1)[0] for line in done]
    assert verbs == ["created"] * 6 + ["linked", "repointed"]
    assert all((root / name).is_dir() for name in workspaces.WORKSPACE_DIRS)
    assert (root / "drafts" / "post.md").read_text() == "keep me"
    assert os.readlink(root / "CLAUDE.md") == "AGENTS.md"
    assert os.readlink(root / ".claude" / "skills") == "../skills"
    assert (root / ".agents" / "skills").is_dir() and not (root / ".agents" / "skills").is_symlink()
    assert workspaces.ensure_layout(root) == []


def test_setup_wizard_slack_path(enso_home: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wizard.shutil, "which", lambda name: sys.executable if name == "claude" else None
    )
    monkeypatch.setattr(wizard, "pair_in_terminal", lambda *args: wizard.PairedIdentity("U1", "D1"))
    installs: list[Paths] = []
    monkeypatch.setattr(service, "install", lambda paths, config: installs.append(paths) or ["ok"])
    tested: list[tuple[str, str] | None] = []
    monkeypatch.setattr(
        wizard, "_send_test", lambda paths, config: tested.append(config.default_notify())
    )
    answers = ["", "", "", "", "xoxb-1", "xapp-1", "y"]
    result = CliRunner().invoke(app, ["setup"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.output
    assert "Your chat is connected." in result.output
    assert "Your Slack user id" not in result.output
    config = load_config(enso_home)
    assert config.bindings == {"slack:dm:U1": "default"}
    assert (config.defaults.provider, config.defaults.model, config.defaults.effort) == (
        "claude", "opus", "high",
    )  # fmt: skip
    assert config.slack is not None and config.slack.app_token == "xapp-1"
    assert config.slack.notify == "D1" and tested == [("slack", "D1")]
    assert enso_home.agents_md.exists()
    job, problems = find_job(enso_home, config, "default:enso-audit")
    assert job is not None and problems == []
    # Audit notifications use the private chat just paired.
    summary = next(
        line
        for line in result.output.splitlines()
        if line.startswith("installed the enso-audit job")
    )
    assert "reports problems to your notify target" in summary
    assert (job.provider, job.model, job.effort) == ("claude", "opus", "high")  # the wizard's
    assert job.enabled and job.workspace == "default" and job.prerun == "prerun.sh"
    assert installs == [enso_home]
    assert CliRunner().invoke(app, ["setup"]).exit_code == 1  # fresh homes only


def test_setup_wizard_telegram_path(enso_home: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wizard's prompts and the first config come from the transport's declaration."""
    monkeypatch.setattr(
        wizard.shutil, "which", lambda name: sys.executable if name == "claude" else None
    )
    paired: list[tuple[str, dict[str, str]]] = []

    def pair_in_terminal(paths, transport, credentials, ready):
        paired.append((transport, credentials))
        return wizard.PairedIdentity("123", "123")

    monkeypatch.setattr(wizard, "pair_in_terminal", pair_in_terminal)
    monkeypatch.setattr(wizard, "_send_test", lambda paths, config: None)
    # Provider, model, effort, transport, then the one Telegram token; no background service.
    answers = ["", "", "", "telegram", "123:abc", "n"]
    result = CliRunner().invoke(app, ["setup"], input="\n".join(answers) + "\n")

    assert result.exit_code == 0, result.output
    assert "BotFather" in result.output and "Create your Slack app" not in result.output
    assert paired == [("telegram", {"bot_token": "123:abc"})]
    config = load_config(enso_home)
    assert config.raw["transports"]["telegram"] == {"bot_token": "123:abc", "notify": "123"}
    assert config.telegram is not None
    assert config.telegram.notify == "123" and config.bindings == {"telegram:123": "default"}


def test_send_test_names_the_extra_when_the_transport_cannot_be_built(
    enso_home: Paths,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing extra used to raise StopIteration after config.json was already written."""
    monkeypatch.setattr("enso.cli.build_transports", lambda config: [])
    wizard._send_test(enso_home, config)
    assert capsys.readouterr().out == (
        "no test message: slack transport unavailable; install enso[slack]\n"
    )


def test_setup_detects_antigravity(enso_home: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """agy is found by its own name like the other three, and seeded with its own flags."""
    monkeypatch.setattr(
        wizard.shutil, "which", lambda name: "/opt/bin/agy" if name == "agy" else None
    )
    assert wizard.detect_providers() == {
        "agy": {
            "path": "/opt/bin/agy",
            "models": [
                "gemini-3.8-flash-high",
                "gemini-3.8-flash-medium",
                "gemini-3.8-flash-low",
                "gemini-3.1-pro-high",
                "gemini-3.1-pro-low",
                "claude-sonnet-4-6",
                "claude-opus-4-6-thinking",
            ],
            "args": ["--dangerously-skip-permissions"],
        }
    }
    monkeypatch.setattr(wizard, "pair_in_terminal", lambda *args: wizard.PairedIdentity("U1", "D1"))
    monkeypatch.setattr(wizard, "_send_test", lambda paths, config: None)
    # Provider, model, effort, transport, then the Slack step; no background service.
    answers = ["", "", "", "", "xoxb-1", "xapp-1", "n"]
    result = CliRunner().invoke(app, ["setup"], input="\n".join(answers) + "\n")

    assert result.exit_code == 0, result.output
    assert "found agy at /opt/bin/agy" in result.output
    config = load_config(enso_home)
    assert config.providers["agy"].path == "/opt/bin/agy"
    assert config.providers["agy"].args == ("--dangerously-skip-permissions",)
    assert (config.defaults.provider, config.defaults.model, config.defaults.effort) == (
        "agy", "gemini-3.8-flash-high", "high",
    )  # fmt: skip


def test_setup_detects_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wizard.shutil, "which", lambda name: "/opt/bin/opencode" if name == "opencode" else None
    )
    assert wizard.detect_providers() == {
        "opencode": {
            "path": "/opt/bin/opencode",
            "models": ["openrouter/deepseek/deepseek-v4-flash"],
            "args": ["--auto"],
        }
    }


def test_docs_link_only_to_pages_git_will_commit() -> None:
    """A page ignored by an unanchored pattern would vanish from a fresh checkout.

    Regression: `TASKS.md` in `.gitignore` matched `docs/tasks.md` on a case-insensitive
    filesystem, so every link to the feature's owning page dangled once committed.
    """
    repo = Path(__file__).resolve().parent.parent
    docs = repo / "docs"
    targets = {
        (docs / match.group(1)).resolve()
        for page in docs.glob("*.md")
        for match in re.finditer(r"\]\(([a-z0-9_./-]+\.md)(?:#[a-z0-9-]+)?\)", page.read_text())
    }
    assert targets and all(target.is_file() for target in targets), sorted(
        str(t) for t in targets if not t.is_file()
    )
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    check = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "--no-index", "--", *map(str, targets)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert check.returncode == 1, f"ignored docs pages:\n{check.stdout}"

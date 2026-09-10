"""Upgrade shipped content using recorded baselines while preserving operator choices."""

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from enso import workspaces
from enso.config import Agent
from enso.maintenance import read_json


@pytest.fixture
def bundle_home(enso_home, tmp_path, monkeypatch):
    package = tmp_path / "package"
    bundled = package / "bundled"
    files = {
        "AGENTS.md": "Home instructions one\n",
        "slack/manifest.json": '{"version": "one"}\n',
        "skills/enso/SKILL.md": "Core skill one\n",
        "jobs/enso-audit/JOB.md": (
            "---\nname: enso-audit\nenabled: true\nschedule: '0 3 * * *'\n"
            'workspace: default\nprovider: "{{provider}}"\nmodel: "{{model}}"\n'
            'effort: "{{effort}}"\n---\nAudit prompt one\n'
        ),
        "jobs/enso-audit/prerun.sh": "#!/bin/sh\nprintf old\n",
    }
    for name, content in files.items():
        path = bundled / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(workspaces.resources, "files", lambda package_name: package)
    monkeypatch.setattr(workspaces, "BUNDLED_SKILLS", ("enso",))
    monkeypatch.setattr(workspaces, "BUNDLED_JOBS", ("enso-audit",))
    agent = Agent("claude", "opus", "high")
    return SimpleNamespace(paths=enso_home, bundled=bundled, agent=agent)


def seed(home):
    workspaces.seed_home(home.paths)
    workspaces.seed_jobs(home.paths, home.agent)


def test_untouched_bundles_refresh_and_keep_original_job_agent(bundle_home):
    home = bundle_home
    seed(home)
    (home.bundled / "AGENTS.md").write_text("Home instructions two\n")
    (home.bundled / "skills/enso/SKILL.md").write_text("Core skill two\n")
    job_source = home.bundled / "jobs/enso-audit/JOB.md"
    job_source.write_text(job_source.read_text().replace("prompt one", "prompt two"))
    changed = workspaces.reconcile_bundles(home.paths, Agent("codex", "gpt-5", "xhigh"))
    assert "AGENTS.md" in changed
    assert (home.paths.home / "AGENTS.md").read_text() == "Home instructions two\n"
    assert (home.paths.home / "skills/enso/SKILL.md").read_text() == "Core skill two\n"
    job = (home.paths.jobs / "enso-audit/JOB.md").read_text()
    assert "Audit prompt two" in job
    assert 'provider: "claude"' in job and 'model: "opus"' in job and 'effort: "high"' in job
    receipt = read_json(home.paths.home / ".bundles.json")
    assert receipt["files"]["AGENTS.md"] == hashlib.sha256(b"Home instructions two\n").hexdigest()


def test_operator_edits_disabled_jobs_and_deleted_bundles_survive(bundle_home):
    home = bundle_home
    seed(home)
    instructions = home.paths.home / "AGENTS.md"
    instructions.write_text("My instructions\n")
    skill = home.paths.home / "skills/enso/SKILL.md"
    skill.unlink()
    job = home.paths.jobs / "enso-audit/JOB.md"
    customized = job.read_text().replace("enabled: true", "enabled: false")
    job.write_text(customized)
    (home.bundled / "AGENTS.md").write_text("New shipped instructions\n")
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert instructions.read_text() == "My instructions\n"
    assert not skill.exists()
    assert job.read_text() == customized
    shutil.rmtree(job.parent)
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert not job.parent.exists()


def test_historical_existing_bundle_is_preserved_and_new_bundle_names_are_seeded(
    bundle_home, monkeypatch
):
    home = bundle_home
    instructions = home.paths.home / "AGENTS.md"
    instructions.write_text("Historical instructions without a receipt\n")
    job = home.paths.jobs / "enso-audit/JOB.md"
    job.parent.mkdir(parents=True)
    historical = workspaces._stamp(
        (home.bundled / "jobs/enso-audit/JOB.md").read_text(),
        {"provider": "claude", "model": "opus", "effort": "high"},
    )
    job.write_text(historical)
    extra = home.bundled / "skills/enso-new/SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("New release skill\n")
    monkeypatch.setattr(workspaces, "BUNDLED_SKILLS", ("enso", "enso-new"))
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert instructions.read_text() == "Historical instructions without a receipt\n"
    assert job.read_text() == historical
    assert not (job.parent / "prerun.sh").exists()
    assert (home.paths.home / "skills/enso-new/SKILL.md").read_text() == "New release skill\n"


def test_new_helper_is_added_to_tracked_job_but_deleted_old_helper_stays_deleted(bundle_home):
    home = bundle_home
    seed(home)
    deleted = home.paths.jobs / "enso-audit/prerun.sh"
    deleted.unlink()
    (home.bundled / "jobs/enso-audit/new-helper.sh").write_text("#!/bin/sh\nprintf new\n")
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert not deleted.exists()
    assert (home.paths.jobs / "enso-audit/new-helper.sh").read_text() == "#!/bin/sh\nprintf new\n"


def test_reconcile_does_not_follow_user_skill_symlinks(bundle_home, tmp_path):
    home = bundle_home
    seed(home)
    skill = home.paths.home / "skills/enso/SKILL.md"
    original = skill.read_text()
    outside = tmp_path / "user-owned-skill.md"
    outside.write_text(original)
    skill.unlink()
    skill.symlink_to(outside)
    (home.bundled / "skills/enso/SKILL.md").write_text("New shipped skill\n")
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert skill.is_symlink()
    assert outside.read_text() == original


def test_historical_job_without_receipt_retains_missing_script_across_repeated_upgrades(
    bundle_home,
):
    home = bundle_home
    seed(home)
    # Simulate an older Enso home created before baseline receipts existed.
    (home.paths.home / ".bundles.json").unlink()
    script = home.paths.jobs / "enso-audit/prerun.sh"
    script.unlink()
    for _ in range(2):
        workspaces.reconcile_bundles(home.paths, home.agent)
        assert not script.exists()
    baseline = json.loads((home.paths.home / ".bundles.json").read_text())
    assert "jobs/enso-audit/JOB.md" not in baseline["files"]


def test_skill_support_files_seed_refresh_and_preserve_edits_and_deletions(
    bundle_home, monkeypatch
):
    home = bundle_home
    helpers = ("scripts/tool.py", "references/guide.md", "assets/example.txt")
    monkeypatch.setattr(workspaces, "BUNDLED_SKILL_SUPPORT", {"enso": helpers})
    for name in helpers:
        source = home.bundled / "skills/enso" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("original\n")
    seed(home)
    root = home.paths.skills / "enso"
    (root / helpers[1]).write_text("my guidance\n")
    (root / helpers[2]).unlink()
    for name in helpers:
        (home.bundled / "skills/enso" / name).write_text("new\n")
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert (root / helpers[0]).read_text() == "new\n"
    assert (root / helpers[1]).read_text() == "my guidance\n"
    assert not (root / helpers[2]).exists()


def test_seed_support_files_does_not_follow_user_directory_link(bundle_home, monkeypatch, tmp_path):
    home = bundle_home
    seed(home)
    source = home.bundled / "skills/enso/scripts/tool.py"
    source.parent.mkdir()
    source.write_text("helper\n")
    monkeypatch.setattr(workspaces, "BUNDLED_SKILL_SUPPORT", {"enso": ("scripts/tool.py",)})
    outside = tmp_path / "authored-scripts"
    outside.mkdir()
    (home.paths.skills / "enso/scripts").symlink_to(outside, target_is_directory=True)
    workspaces.seed_home(home.paths)
    workspaces.seed_home(home.paths, refresh_skills=True)
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert list(outside.iterdir()) == []


def test_new_skill_helper_added_to_tracked_but_not_historical_bundle(bundle_home, monkeypatch):
    home = bundle_home
    seed(home)
    source = home.bundled / "skills/enso/reference.md"
    source.write_text("new helper\n")
    monkeypatch.setattr(workspaces, "BUNDLED_SKILL_SUPPORT", {"enso": ("reference.md",)})
    workspaces.reconcile_bundles(home.paths, home.agent)
    target = home.paths.skills / "enso/reference.md"
    assert target.read_text() == "new helper\n"
    target.unlink()
    (home.paths.home / ".bundles.json").unlink()
    workspaces.reconcile_bundles(home.paths, home.agent)
    assert not target.exists()

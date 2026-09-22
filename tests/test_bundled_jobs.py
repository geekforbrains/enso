"""Bundled maintenance jobs run deterministic commands without launching an agent."""

from __future__ import annotations

import os

import pytest
from conftest import load_job, write_config
from typer.testing import CliRunner

from enso import db, runs, workspaces
from enso.cli import app
from enso.jobs import schedule_problem, validate
from enso.jobs.runner import JobRunner


def test_audit_template_parses_and_validates(enso_home, config, raw_config):
    workspaces.seed_jobs(enso_home, config.defaults)
    job = load_job(enso_home, config, "enso-audit")
    assert validate(job, config) == [] and schedule_problem(job.schedule) is None
    assert (job.name, job.schedule, job.workspace) == ("Enso audit", "0 3 * * *", "default")
    assert job.agent is None and job.gate is None
    assert job.command == "enso doctor --attention --notify --quiet"
    assert job.enabled and job.catch_up and job.notify is None
    assert "{{" not in job.prompt
    assert not (job.job_dir / "prerun.sh").exists()

    write_config(enso_home, raw_config)
    db.initialize(enso_home)
    shown = CliRunner().invoke(app, ["job", "show", "default:enso-audit"])
    assert shown.exit_code == 0, shown.output
    assert "problem:" not in shown.output and "enabled: True" in shown.output
    listed = CliRunner().invoke(app, ["job", "list", "--workspace", "default"])
    assert listed.exit_code == 0 and "enso-audit" in listed.output


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("enso-audit", "doctor --attention --notify --quiet"),
        ("enso-update", "update check --notify --quiet"),
    ],
)
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_maintenance_job_runs_as_a_command(
    enso_home, fake_config, tmp_path, monkeypatch, name, command, exit_code
):
    workspaces.seed_jobs(enso_home, fake_config.defaults)
    job = load_job(enso_home, fake_config, name)
    assert job.agent is None and job.gate is None and job.command == f"enso {command}"
    assert not (job.job_dir / "prerun.sh").exists()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "enso"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'[[ "$*" == "{command}" ]] || exit 99\n'
        'printf "checked\\n"\n'
        f"exit {exit_code}\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = await JobRunner(fake_config).run(job, trigger="schedule")
    assert result.run_id is not None
    saved = runs.get(enso_home, result.run_id)
    assert saved is not None and saved.kind == "command"
    assert saved.provider is None and saved.model is None
    assert result.status == ("ok" if exit_code == 0 else "error")
    assert result.output == "checked\n"

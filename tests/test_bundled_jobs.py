"""The bundled ``enso-audit`` job end to end, with a stub ``enso`` first on PATH.

Its prerun calls ``enso doctor --json``; a stub that exits 0, 1, or 2 stands in for it, so
the real doctor never runs and the real home is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeTransport, load_job, write_config
from typer.testing import CliRunner

from enso import db, doctor, runs, workspaces
from enso.cli import app
from enso.config import Config, Paths
from enso.jobs import Job, schedule_problem, validate
from enso.jobs.runner import JobRunner

BUNDLED = Path(workspaces.__file__).parent / "bundled" / "jobs" / "enso-audit"
DOCTOR_FAILED = "enso doctor exited with status"


def report(*, ok: bool) -> str:
    """The doctor's own ``--json`` shape: one repairable home problem unless ``ok``."""
    problems = [] if ok else ["CLAUDE.md is missing (a symlink to AGENTS.md)" + doctor.FIXABLE_MARK]
    sections = [doctor.Section("home", problems=problems)]
    return json.dumps(doctor.Report(Path("/home/x/.enso"), sections).as_dict())


HEALTHY = report(ok=True)
BROKEN = report(ok=False)

Stub = Callable[..., None]


@pytest.fixture
def stub_enso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    """``stub(exit_code, stdout, stderr)`` puts a ``enso`` first on PATH.

    It answers ``doctor --json`` with exactly that and refuses anything else, so the venv's
    real ``enso`` behind it is never reached.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def stub(exit_code: int, stdout: str = "", stderr: str = "") -> None:
        # Newline-terminated like a real program's, so the prerun's own line stays a line.
        (bin_dir / "stdout").write_text(f"{stdout}\n" if stdout else "")
        (bin_dir / "stderr").write_text(f"{stderr}\n" if stderr else "")
        script = bin_dir / "enso"
        script.write_text(
            "#!/usr/bin/env bash\n"
            '[[ "$*" == "doctor --json" ]] || exit 99\n'
            f"cat '{bin_dir / 'stdout'}'\n"
            f"cat '{bin_dir / 'stderr'}' >&2\n"
            f"exit {exit_code}\n"
        )
        script.chmod(0o755)

    return stub


def seeded(paths: Paths, config: Config) -> Job:
    """The job as ``enso setup`` installs it, stamped with the config's default agent."""
    workspaces.seed_jobs(paths, config.defaults)
    return load_job(paths, config, "enso-audit")


def prerun(job: Job, paths: Paths, *, path: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the prerun the way the runner does: bash, from the job directory, ENSO_HOME set."""
    env = {**os.environ, "ENSO_HOME": str(paths.home)}
    if path is not None:
        env["PATH"] = path
    bash = shutil.which("bash")  # resolved here, since the script's PATH may hold nothing
    assert bash is not None
    return subprocess.run(
        [bash, str(job.job_dir / "prerun.sh")],
        cwd=job.job_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_template_parses_and_validates(enso_home: Paths, config: Config, raw_config: dict) -> None:
    job = seeded(enso_home, config)  # load_job asserts there are no problems

    assert validate(job, config) == [] and schedule_problem(job.schedule) is None
    assert (job.name, job.schedule, job.workspace) == ("Enso audit", "0 3 * * *", "default")
    assert (job.provider, job.model, job.effort) == ("claude", "opus", "xhigh")
    assert job.enabled and job.catch_up and job.prerun == "prerun.sh" and job.notify is None
    assert job.prompt.count("{{prerun_output}}") == 1
    assert "{{" not in job.prompt.replace("{{prerun_output}}", "")
    assert (job.job_dir / "prerun.sh").is_file()
    for command in ("enso message send", "enso workspace audit --fix"):
        assert command in job.prompt
    assert doctor.FIXABLE_MARK.strip() in job.prompt  # the marker the prompt tells the agent about
    # A job problem is repaired by hand, so the prompt has to point at the file doctor names.
    assert "JOB.md" in job.prompt and "never guessed or rewritten" in job.prompt

    write_config(enso_home, raw_config)
    db.initialize(enso_home)
    shown = CliRunner().invoke(app, ["job", "show", "default:enso-audit"])
    assert shown.exit_code == 0, shown.output
    assert "problem:" not in shown.output and "enabled: True" in shown.output
    listed = CliRunner().invoke(app, ["job", "list"])
    assert listed.exit_code == 0 and "enso-audit" in listed.output


@pytest.mark.parametrize(
    ("doctor_exit", "stdout", "stderr", "expect_rc", "expect_stdout"),
    [
        (0, HEALTHY, "", 1, ""),  # healthy: the report is dropped, nothing to do
        (1, BROKEN, "", 0, BROKEN + "\n"),  # problems: the report opens the gate
        (1, "", "Traceback (most recent call last):", 2, ""),  # a crash exits 1 too: no report
        (2, "", "boom", 2, ""),  # the doctor itself failed
    ],
)
def test_prerun_inverts_the_doctor_exit(
    enso_home: Paths,
    config: Config,
    stub_enso: Stub,
    doctor_exit: int,
    stdout: str,
    stderr: str,
    expect_rc: int,
    expect_stdout: str,
) -> None:
    job = seeded(enso_home, config)
    stub_enso(doctor_exit, stdout=stdout, stderr=stderr)
    done = prerun(job, enso_home)
    assert (done.returncode, done.stdout) == (expect_rc, expect_stdout)
    if expect_rc == 2:  # the doctor's own stderr passes through, then the alert line
        detail = "1 without a report" if doctor_exit == 1 else str(doctor_exit)
        assert done.stderr.splitlines() == [stderr, f"ENSO_ERROR: {DOCTOR_FAILED} {detail}"]
    else:
        assert done.stderr == ""


def test_prerun_without_enso_on_path_is_a_prerun_error(
    enso_home: Paths, config: Config, tmp_path: Path
) -> None:
    job = seeded(enso_home, config)
    (tmp_path / "empty").mkdir()
    done = prerun(job, enso_home, path=str(tmp_path / "empty"))
    assert (done.returncode, done.stdout) == (2, "")
    assert f"ENSO_ERROR: {DOCTOR_FAILED} 127" in done.stderr


async def test_seeded_job_runs_end_to_end(
    enso_home: Paths, fake_config: Config, stub_enso: Stub
) -> None:
    transport = FakeTransport("slack")
    runner = JobRunner(fake_config, {"slack": transport})
    job = seeded(enso_home, fake_config)

    stub_enso(0, stdout=HEALTHY)
    healthy = await runner.run(job, trigger="schedule")
    run = runs.get(enso_home, healthy.run_id or "")
    assert (healthy.status, healthy.output) == ("no_work", "") and run is not None
    assert run.status == "no_work" and transport.sent == []

    stub_enso(1, stdout=BROKEN)
    broken = await runner.run(job, trigger="schedule")
    # The fake CLI echoes the substituted prompt: the report reached the agent, fenced.
    assert broken.status == "ok" and f"```json\n{BROKEN}\n```" in broken.output
    assert transport.sent == []  # the agent sends the summary; the runner alerts on failure only

    stub_enso(1, stderr="Traceback (most recent call last):")  # a crash: exit 1, no report
    crashed = await runner.run(job, trigger="schedule")
    assert (crashed.status, crashed.error) == (
        "prerun_error",
        f"{DOCTOR_FAILED} 1 without a report",
    )

    stub_enso(2, stderr="launchctl: boom")
    failed = await runner.run(job, trigger="schedule")
    assert (failed.status, failed.error) == ("prerun_error", f"{DOCTOR_FAILED} 2")
    assert transport.sent == [  # each distinct prerun failure is alerted once
        ("C1", f"⚠️ [default:enso-audit] prerun failed\n{DOCTOR_FAILED} 1 without a report"),
        ("C1", f"⚠️ [default:enso-audit] prerun failed\n{DOCTOR_FAILED} 2"),
    ]

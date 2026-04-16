"""Tests for the job system."""

from __future__ import annotations

import os

from enso.jobs import Job, create_job, load_jobs, parse_job


def test_parse_job(tmp_path):
    """Parse a well-formed JOB.md."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Test Job
schedule: "0 9 * * *"
provider: claude
model: sonnet
enabled: true
prerun: check.sh
---

Do the thing. {{prerun_output}}
""")
    job = parse_job("test-job", str(job_file))
    assert job is not None
    assert job.name == "Test Job"
    assert job.schedule == "0 9 * * *"
    assert job.provider == "claude"
    assert job.model == "sonnet"
    assert job.enabled is True
    assert job.prerun == "check.sh"
    assert "{{prerun_output}}" in job.prompt


def test_parse_job_disabled(tmp_path):
    """Disabled jobs parse correctly."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Disabled
schedule: "0 0 * * *"
provider: gemini
model: gemini-2.5-pro
enabled: false
---

Nope.
""")
    job = parse_job("disabled", str(job_file))
    assert job is not None
    assert job.enabled is False


def test_parse_job_missing_fields(tmp_path):
    """Missing required fields returns None."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Incomplete
---

Missing schedule/provider/model.
""")
    assert parse_job("bad", str(job_file)) is None


def test_parse_job_bad_frontmatter(tmp_path):
    """No frontmatter delimiters returns None."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("Just some text with no frontmatter.")
    assert parse_job("bad", str(job_file)) is None


def test_create_job(tmp_enso):
    """create_job scaffolds a JOB.md file with enabled: false."""
    job = create_job("my-job", "My Job", "claude", "opus", "30 6 * * *")
    assert os.path.isfile(job.path)
    assert job.dir_name == "my-job"
    assert job.name == "My Job"
    assert job.schedule == "30 6 * * *"

    # Verify it round-trips through parse
    parsed = parse_job("my-job", job.path)
    assert parsed is not None
    assert parsed.name == "My Job"
    assert parsed.provider == "claude"
    assert parsed.enabled is False


def test_load_jobs(tmp_enso):
    """load_jobs finds all jobs in the jobs directory."""
    create_job("alpha", "Alpha", "claude", "sonnet", "0 9 * * *")
    create_job("beta", "Beta", "gemini", "gemini-2.5-pro", "0 12 * * *")
    jobs = load_jobs()
    assert len(jobs) == 2
    names = {j.dir_name for j in jobs}
    assert names == {"alpha", "beta"}


def test_load_jobs_empty(tmp_enso):
    """load_jobs returns empty when no jobs directory exists."""
    assert load_jobs() == []


def test_parse_job_with_notify(tmp_path):
    """Jobs with a notify field parse correctly."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Notify Job
schedule: "0 9 * * *"
provider: claude
model: sonnet
notify: alerts
---

Check things.
""")
    job = parse_job("notify-job", str(job_file))
    assert job is not None
    assert job.notify == "alerts"


def test_parse_job_without_notify(tmp_path):
    """Jobs without a notify field default to None."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: No Notify
schedule: "0 9 * * *"
provider: claude
model: sonnet
---

Do stuff.
""")
    job = parse_job("no-notify", str(job_file))
    assert job is not None
    assert job.notify is None


def test_job_dir_property():
    """Job.job_dir computes correctly."""
    job = Job(dir_name="foo", name="Foo", schedule="* * * * *", provider="claude", model="sonnet")
    assert job.job_dir.endswith("/foo")

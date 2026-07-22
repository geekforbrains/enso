"""Tests for the job system."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enso import frontmatter
from enso.config import load_config, save_config
from enso.jobs import Job, create_job, job_config_error, load_jobs, parse_job


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
provider: codex
model: luna
enabled: false
---

Nope.
""")
    job = parse_job("disabled", str(job_file))
    assert job is not None
    assert job.enabled is False


def test_parse_job_boolean_formatting_and_inline_comments(tmp_path):
    """Legacy parsing accepts harmless YAML whitespace and comments."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Formatted
schedule: "0 0 * * *"
provider: claude
model: sonnet
enabled : false  # temporarily paused
catch_up: true  # run a missed invocation
---

Prompt.
""")

    job = parse_job("formatted", str(job_file))

    assert job is not None
    assert job.enabled is False
    assert job.catch_up is True


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
    assert job.enabled is False
    assert job.prompt == "Your prompt here."

    # Verify it round-trips through parse
    parsed = parse_job("my-job", job.path)
    assert parsed is not None
    assert parsed.name == "My Job"
    assert parsed.provider == "claude"
    assert parsed.enabled is False
    assert parsed.prompt == job.prompt


def _configure_model(model: str) -> None:
    """Register a custom model name so create_job validation accepts it."""
    config = load_config()
    config["providers"]["claude"]["models"].append(model)
    save_config(config)


def test_create_job_quotes_yaml_sensitive_values(tmp_enso):
    """New scaffolds are valid YAML and remain compatible with the loader."""
    _configure_model("null")  # YAML-sensitive scalar as a model name
    job = create_job(
        "daily-review",
        "Daily: Review",
        "claude",
        "null",
        "* * * * *",
    )

    meta, body = frontmatter.read(job.path)
    assert meta == {
        "name": "Daily: Review",
        "schedule": "* * * * *",
        "provider": "claude",
        "model": "null",
        "enabled": False,
    }
    assert body == "Your prompt here.\n"

    parsed = parse_job("daily-review", job.path)
    assert parsed is not None
    assert parsed.name == "Daily: Review"
    assert parsed.schedule == "* * * * *"
    assert parsed.provider == "claude"
    assert parsed.model == "null"


def test_create_job_and_loader_handle_safe_dump_edge_values(tmp_enso):
    """Quoted apostrophes, colons, and fence-like lines round-trip exactly."""
    _configure_model("null")
    name = "Bob's: Review\n---\ncontinued"
    job = create_job(
        "yaml-edge",
        name,
        "claude",
        "null",
        "* * * * *",
    )

    parsed = parse_job("yaml-edge", job.path)

    assert parsed is not None
    assert parsed.name == name
    assert parsed.schedule == "* * * * *"
    assert parsed.provider == "claude"
    assert parsed.model == "null"
    assert parsed.enabled is False


def test_parse_job_falls_back_for_legacy_non_yaml_frontmatter(tmp_path):
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Daily: Review
schedule: "0 9 * * *"
provider: claude
model: sonnet
enabled: true  # legacy comment
---

Prompt.
""")

    parsed = parse_job("legacy", str(job_file))

    assert parsed is not None
    assert parsed.name == "Daily: Review"
    assert parsed.enabled is True


@pytest.mark.parametrize(
    "dir_name",
    [
        "",
        ".",
        "..",
        "../escape",
        "nested/job",
        r"nested\job",
        " padded ",
        "line\nbreak",
        "drive:name",
    ],
)
def test_create_job_rejects_unsafe_directory_names(tmp_enso, dir_name):
    with pytest.raises(ValueError, match="non-empty slug"):
        create_job(dir_name, "Unsafe", "claude", "sonnet", "0 9 * * *")

    assert not os.path.exists(os.path.join(tmp_enso, "jobs"))


def test_job_config_error_messages():
    models = {"claude": ["opus"], "codex": []}
    assert job_config_error("claude", "opus", models) is None
    assert "Unknown provider 'gemini'" in job_config_error("gemini", "opus", models)
    assert "Unknown claude model 'bogus'" in job_config_error("claude", "bogus", models)
    assert "none configured" in job_config_error("codex", "sol", models)


def test_create_job_rejects_unknown_provider(tmp_enso):
    with pytest.raises(ValueError, match="Unknown provider 'gemini'"):
        create_job("bad", "Bad", "gemini", "gemini-pro", "0 0 * * *")
    # Validation fails before anything touches disk.
    assert not os.path.isdir(os.path.join(tmp_enso, "jobs", "bad"))


def test_create_job_rejects_unknown_model(tmp_enso):
    with pytest.raises(ValueError, match="Unknown claude model 'bogus'"):
        create_job("bad", "Bad", "claude", "bogus", "0 0 * * *")
    assert not os.path.isdir(os.path.join(tmp_enso, "jobs", "bad"))


def test_create_job_with_malformed_models_config_rejects_cleanly(tmp_enso):
    """A non-list models value must not enable substring matches or crash."""
    config = load_config()
    config["providers"]["claude"]["models"] = "sonnet"
    save_config(config)

    # "son" would pass a naive `in` check against the string "sonnet".
    with pytest.raises(ValueError, match="none configured"):
        create_job("bad", "Bad", "claude", "son", "0 0 * * *")


def test_create_job_accepts_custom_configured_model(tmp_enso):
    """Models the user added to config are valid job targets."""
    config = load_config()
    config["providers"]["claude"]["models"].append("my-custom-model")
    save_config(config)

    job = create_job("custom", "Custom", "claude", "my-custom-model", "0 0 * * *")

    assert job.model == "my-custom-model"


def test_create_job_refuses_to_overwrite_existing_job(tmp_enso):
    job = create_job("daily", "Daily", "claude", "sonnet", "0 9 * * *")
    with open(job.path, "a", encoding="utf-8") as file:
        file.write("User customization.\n")
    original = Path(job.path).read_bytes()

    with pytest.raises(FileExistsError, match="Job 'daily' already exists"):
        create_job("daily", "Replacement", "codex", "luna", "0 0 * * *")

    assert Path(job.path).read_bytes() == original


def test_create_job_refuses_existing_symlink_directory(tmp_enso, tmp_path):
    jobs_dir = Path(tmp_enso) / "jobs"
    jobs_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (jobs_dir / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileExistsError, match="Job 'linked' already exists"):
        create_job("linked", "Linked", "claude", "sonnet", "0 9 * * *")

    assert list(outside.iterdir()) == []


def test_parse_job_skips_non_utf8_file(tmp_path):
    job_file = tmp_path / "JOB.md"
    job_file.write_bytes(b"---\nname: invalid\n---\n\xff")

    assert parse_job("invalid", str(job_file)) is None


def test_load_jobs(tmp_enso):
    """load_jobs finds all jobs in the jobs directory."""
    create_job("alpha", "Alpha", "claude", "sonnet", "0 9 * * *")
    create_job("beta", "Beta", "codex", "luna", "0 12 * * *")
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


def test_parse_job_runtime_controls(tmp_path):
    """Jobs can override timeout and catch-up controls."""
    job_file = tmp_path / "JOB.md"
    job_file.write_text("""\
---
name: Controlled Job
schedule: "*/15 * * * *"
provider: codex
model: gpt-5.5
timeout: 1800
prerun_timeout: 45
catch_up: true
misfire_grace_seconds: 900
---

Check things.
""")
    job = parse_job("controlled-job", str(job_file))
    assert job is not None
    assert job.timeout == 1800
    assert job.prerun_timeout == 45
    assert job.catch_up is True
    assert job.misfire_grace_seconds == 900


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

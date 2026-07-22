"""Job system — scheduled background tasks parsed from JOB.md files."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass

import yaml

from . import frontmatter
from .config import JOBS_DIR, load_config, provider_models

log = logging.getLogger(__name__)

_DEFAULT_PROMPT = "Your prompt here."


@dataclass
class Job:
    """A background job parsed from a JOB.md file."""

    dir_name: str
    name: str
    schedule: str
    provider: str
    model: str
    enabled: bool = True
    prerun: str | None = None
    notify: str | None = None
    timeout: int = 15 * 60
    prerun_timeout: int = 120
    catch_up: bool = False
    misfire_grace_seconds: int = 5 * 60
    prompt: str = ""
    path: str = ""

    @property
    def job_dir(self) -> str:
        """Absolute path to the job's directory."""
        return os.path.join(JOBS_DIR, self.dir_name)


def load_jobs() -> list[Job]:
    """Load all jobs from ~/.enso/jobs/."""
    if not os.path.isdir(JOBS_DIR):
        return []
    jobs = []
    for entry in sorted(os.listdir(JOBS_DIR)):
        job_file = os.path.join(JOBS_DIR, entry, "JOB.md")
        if os.path.isfile(job_file):
            job = parse_job(entry, job_file)
            if job:
                jobs.append(job)
    return jobs


def parse_job(dir_name: str, path: str) -> Job | None:
    """Parse a JOB.md file into a Job dataclass.

    Expected format: YAML-like frontmatter between --- delimiters,
    followed by the prompt body.
    """
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeError):
        log.warning("Could not read %s", path)
        return None

    parts = frontmatter.split_raw(content)
    if parts is None:
        log.warning("Invalid frontmatter in %s", path)
        return None

    raw_meta, prompt = parts
    fields = _parse_frontmatter(raw_meta)
    required = ("name", "schedule", "provider", "model")
    if not all(k in fields for k in required):
        log.warning("Missing required fields in %s", path)
        return None

    return Job(
        dir_name=dir_name,
        name=fields["name"],
        schedule=fields["schedule"],
        provider=fields["provider"],
        model=fields["model"],
        enabled=_parse_bool(fields.get("enabled"), True),
        prerun=fields.get("prerun"),
        notify=fields.get("notify"),
        timeout=_parse_int(fields.get("timeout"), 15 * 60),
        prerun_timeout=_parse_int(fields.get("prerun_timeout"), 120),
        catch_up=_parse_bool(fields.get("catch_up"), False),
        misfire_grace_seconds=_parse_int(
            fields.get("misfire_grace_seconds"),
            5 * 60,
        ),
        prompt=prompt.strip(),
        path=path,
    )


def job_config_error(
    provider: str, model: str, models_by_provider: dict[str, list[str]],
) -> str | None:
    """Explain why a job's provider/model pair can't run, or None when valid."""
    if provider not in models_by_provider:
        valid = ", ".join(models_by_provider) or "none configured"
        return f"Unknown provider '{provider}' (valid: {valid})"
    models = models_by_provider[provider]
    if model not in models:
        valid = ", ".join(models) or "none configured"
        return f"Unknown {provider} model '{model}' (valid: {valid})"
    return None


def create_job(
    dir_name: str,
    name: str,
    provider: str,
    model: str,
    schedule: str,
) -> Job:
    """Create a new job directory with a scaffolded JOB.md file.

    The prompt body is left as a placeholder for the caller to fill in.
    """
    _validate_dir_name(dir_name)
    error = job_config_error(provider, model, provider_models(load_config()))
    if error:
        raise ValueError(error)
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_dir = os.path.join(JOBS_DIR, dir_name)
    try:
        os.mkdir(job_dir)
    except FileExistsError:
        raise FileExistsError(f"Job '{dir_name}' already exists") from None

    job_file = os.path.join(job_dir, "JOB.md")
    try:
        frontmatter.write(
            job_file,
            {
                "name": name,
                "schedule": schedule,
                "provider": provider,
                "model": model,
                "enabled": False,
            },
            _DEFAULT_PROMPT,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.rmdir(job_dir)
        raise

    return Job(
        dir_name=dir_name,
        name=name,
        schedule=schedule,
        provider=provider,
        model=model,
        enabled=False,
        prompt=_DEFAULT_PROMPT,
        path=job_file,
    )


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse YAML scalars as strings, falling back to the legacy parser."""
    try:
        loaded = yaml.load(text, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        loaded = None
    if isinstance(loaded, dict):
        return {
            key: value
            for key, value in loaded.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }

    # Older Enso versions emitted unquoted values such as
    # ``name: Daily: Review``. They are not valid YAML, but remain supported.
    fields: dict[str, str] = {}
    for line in text.strip().splitlines():
        match = re.match(r"^(\w+)\s*:\s*(.+)$", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip().strip("\"'")
            fields[key] = value
    return fields


def _validate_dir_name(dir_name: str) -> None:
    """Require a portable slug-like directory name, never a path."""
    if (
        not isinstance(dir_name, str)
        or re.fullmatch(r"[\w.-]+", dir_name) is None
        or dir_name in {os.curdir, os.pardir}
    ):
        raise ValueError(
            "Job directory name must be a non-empty slug containing only "
            "letters, numbers, dots, underscores, or hyphens"
        )


def _parse_bool(value: str | None, default: bool) -> bool:
    """Parse a YAML-like boolean, tolerating a trailing inline comment."""
    if value is None:
        return default
    token = value.partition("#")[0].strip().strip("\"'")
    return token.lower() == "true"


def _parse_int(value: str | None, default: int) -> int:
    """Parse a positive integer field with a conservative fallback."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default

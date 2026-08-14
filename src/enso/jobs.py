"""Job system — scheduled background tasks parsed from JOB.md files."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml
from croniter import croniter

from . import frontmatter
from .config import JOBS_DIR, load_config, provider_models

if TYPE_CHECKING:
    from .teams import ExecutionCatalog

log = logging.getLogger(__name__)

_DEFAULT_PROMPT = "Your prompt here."
_REQUIRED_FIELDS = ("name", "schedule", "provider", "model", "workspace")


@dataclass
class Job:
    """A background job parsed from a JOB.md file."""

    dir_name: str
    name: str
    schedule: str
    provider: str
    model: str
    workspace: str
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
    jobs, _errors = load_jobs_with_errors()
    return jobs


def load_jobs_with_errors(
    config: dict | None = None,
) -> tuple[list[Job], dict[str, tuple[str, ...]]]:
    """Load jobs plus actionable errors keyed by job directory name.

    Parse errors include jobs that ``load_jobs`` must skip. When ``config`` is
    supplied, parsed jobs are also checked against their schedule, configured
    provider/model, and named workspace-policy binding.
    """
    if not os.path.isdir(JOBS_DIR):
        return [], {}
    jobs: list[Job] = []
    errors: dict[str, tuple[str, ...]] = {}
    for entry in sorted(os.listdir(JOBS_DIR)):
        job_file = os.path.join(JOBS_DIR, entry, "JOB.md")
        if os.path.isfile(job_file):
            job, problems = _parse_job(entry, job_file)
            if problems:
                errors[entry] = problems
                for problem in problems:
                    log.warning("%s in %s", problem, job_file)
            if job is None:
                continue
            jobs.append(job)
            if config is not None:
                validation = job_validation_errors(job, config)
                if validation:
                    errors[entry] = (*errors.get(entry, ()), *validation)
    return jobs, errors


def parse_job(dir_name: str, path: str) -> Job | None:
    """Parse a JOB.md file into a Job dataclass.

    Expected format: YAML-like frontmatter between --- delimiters,
    followed by the prompt body.
    """
    job, problems = _parse_job(dir_name, path)
    for problem in problems:
        log.warning("%s in %s", problem, path)
    return job


def _parse_job(dir_name: str, path: str) -> tuple[Job | None, tuple[str, ...]]:
    """Parse one job while preserving diagnostics for validation commands."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeError:
        return None, ("Could not read JOB.md as UTF-8",)
    except OSError as exc:
        return None, (f"Could not read JOB.md: {exc}",)

    parts = frontmatter.split_raw(content)
    if parts is None:
        return None, ("Invalid or missing frontmatter",)

    raw_meta, prompt = parts
    fields = _parse_frontmatter(raw_meta)
    for forbidden in ("access", "policy"):
        if forbidden in fields:
            return None, (
                f"Field '{forbidden}' is not supported; jobs derive policy from workspace",
            )
    missing = tuple(field for field in _REQUIRED_FIELDS if field not in fields)
    if missing:
        return None, (f"Missing required fields: {', '.join(missing)}",)

    return (
        Job(
            dir_name=dir_name,
            name=fields["name"],
            schedule=fields["schedule"],
            provider=fields["provider"],
            model=fields["model"],
            workspace=fields["workspace"],
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
        ),
        (),
    )


def job_config_error(
    provider: str,
    model: str,
    models_by_provider: dict[str, list[str]],
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


def job_binding_error(
    workspace: str,
    provider: str,
    catalog: ExecutionCatalog,
) -> str | None:
    """Explain why a job's workspace-policy binding is unusable."""
    if not workspace:
        return "workspace is required"
    if catalog.errors:
        return "Invalid execution catalog: " + "; ".join(catalog.errors)

    if workspace not in catalog.workspaces:
        valid = ", ".join(catalog.workspaces) or "none configured"
        return f"Unknown workspace '{workspace}' (valid: {valid})"
    workspace_problems = catalog.workspace_errors.get(workspace)
    if workspace_problems:
        return f"Invalid workspace '{workspace}': " + "; ".join(workspace_problems)

    policy = catalog.policy_for(workspace)
    policy_problems = catalog.policy_errors.get(policy.name)
    if policy_problems:
        return f"Invalid policy '{policy.name}': " + "; ".join(policy_problems)
    if not policy.allows_provider(provider):
        allowed = ", ".join(policy.providers) or "none"
        return f"Policy '{policy.name}' does not allow provider '{provider}' (allowed: {allowed})"
    return None


def schedule_error(schedule: str) -> str | None:
    """Explain why a cron schedule can't be parsed, or None when valid."""
    if isinstance(schedule, str) and croniter.is_valid(schedule):
        return None
    return f"Invalid cron schedule {schedule!r} (expected e.g. '0 9 * * *')"


def job_validation_errors(job: Job, config: dict) -> tuple[str, ...]:
    """Return every static schedule and execution error for a parsed job."""
    from .teams import load_catalog

    problems: list[str] = []
    if error := schedule_error(job.schedule):
        problems.append(error)
    if error := job_config_error(job.provider, job.model, provider_models(config)):
        problems.append(error)
    if error := job_binding_error(
        job.workspace,
        job.provider,
        load_catalog(config),
    ):
        problems.append(error)
    return tuple(problems)


def create_job(
    dir_name: str,
    name: str,
    provider: str,
    model: str,
    schedule: str,
    *,
    workspace: str,
) -> Job:
    """Create a new job directory with a scaffolded JOB.md file.

    The prompt body is left as a placeholder for the caller to fill in.
    """
    _validate_dir_name(dir_name)
    config = load_config()
    error = job_config_error(provider, model, provider_models(config))
    if error:
        raise ValueError(error)
    error = schedule_error(schedule)
    if error:
        raise ValueError(error)
    from .teams import load_catalog

    error = job_binding_error(
        workspace,
        provider,
        load_catalog(config),
    )
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
                "workspace": workspace,
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
        workspace=workspace,
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

"""Job system — scheduled background tasks parsed from JOB.md files."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from .config import JOBS_DIR

log = logging.getLogger(__name__)


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
        with open(path) as f:
            content = f.read()
    except OSError:
        log.warning("Could not read %s", path)
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        log.warning("Invalid frontmatter in %s", path)
        return None

    fields = _parse_frontmatter(parts[1])
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
        enabled=fields.get("enabled", "true").lower() == "true",
        prerun=fields.get("prerun"),
        prompt=parts[2].strip(),
        path=path,
    )


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
    job_dir = os.path.join(JOBS_DIR, dir_name)
    os.makedirs(job_dir, exist_ok=True)

    job_file = os.path.join(job_dir, "JOB.md")
    content = f"""\
---
name: {name}
schedule: "{schedule}"
provider: {provider}
model: {model}
enabled: false
---

Your prompt here.
"""
    with open(job_file, "w") as f:
        f.write(content)

    return Job(
        dir_name=dir_name,
        name=name,
        schedule=schedule,
        provider=provider,
        model=model,
        prompt="",
        path=job_file,
    )


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple YAML-like key: value pairs from frontmatter text."""
    fields: dict[str, str] = {}
    for line in text.strip().splitlines():
        match = re.match(r"^(\w+):\s*(.+)$", line)
        if match:
            key = match.group(1)
            value = match.group(2).strip().strip("\"'")
            fields[key] = value
    return fields

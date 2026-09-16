"""Jobs: the ``JOB.md`` model, its closed field schema, validation, and scaffolding.

``frontmatter`` decides the syntax; the schema below is the whole of what a ``JOB.md``
may say. See ``docs/jobs.md``, which owns the file format.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .. import frontmatter
from ..config import Config, Paths, require_workspace, split_job_ref, valid_workspace_name
from ..providers import PROVIDER_CLASSES
from ..scheduling import CRON_FIELDS as CRON_FIELDS
from ..scheduling import next_cron
from ..scheduling import schedule_problem as schedule_problem

TEXT, INTEGER, NONNEGATIVE, FLAG = "text", "integer", "nonnegative", "flag"
# Every field a JOB.md may carry, in the order docs/jobs.md lists them, by the YAML type it
# must hold. The names are the Job dataclass's own, so a validated mapping constructs one.
FIELDS: dict[str, str] = {
    "name": TEXT,
    "schedule": TEXT,
    "provider": TEXT,
    "model": TEXT,
    "effort": TEXT,
    "project": TEXT,
    "stage": TEXT,
    "concurrency_group": TEXT,
    "enabled": FLAG,
    "prerun": TEXT,
    "prerun_timeout": INTEGER,
    "postrun": TEXT,
    "postrun_timeout": INTEGER,
    "max_followups": NONNEGATIVE,
    "timeout": INTEGER,
    "notify": TEXT,
    "catch_up": FLAG,
    "misfire_grace_seconds": INTEGER,
}
# ``schedule`` is required unless the job serves a stage, whose readiness is its own trigger.
REQUIRED = ("name", "schedule", "provider", "model", "effort", "enabled")
_TYPE_PROBLEMS = {
    TEXT: "must be non-empty text",
    INTEGER: "must be a positive integer",
    NONNEGATIVE: "must be a nonnegative integer",
    FLAG: "must be true or false",
}
DEFAULT_TIMEOUT = 900
DEFAULT_PRERUN_TIMEOUT = 120
DEFAULT_POSTRUN_TIMEOUT = 120
DEFAULT_MAX_FOLLOWUPS = 2
DEFAULT_MISFIRE_GRACE = 300
PLACEHOLDER_PROMPT = "Your prompt here. {{prerun_output}} is replaced with the prerun's stdout."


@dataclass(frozen=True)
class Job:
    """One ``workspaces/<workspace>/jobs/<dir_name>/JOB.md``."""

    dir_name: str
    path: Path
    name: str
    schedule: str | None  # cron, local time; None on a stage job that fires when work is ready
    provider: str
    model: str
    effort: str
    workspace: str
    enabled: bool
    prompt: str
    project: str | None = None  # with ``stage``: the task board stage this job serves
    stage: str | None = None
    concurrency_group: str | None = None  # serialize provider execution and postrun checks
    prerun: str | None = None  # script in the job directory, run with bash
    prerun_timeout: int = DEFAULT_PRERUN_TIMEOUT
    postrun: str | None = None  # likewise, given the output on stdin and the outcome in env
    postrun_timeout: int = DEFAULT_POSTRUN_TIMEOUT
    max_followups: int = DEFAULT_MAX_FOLLOWUPS
    timeout: int = DEFAULT_TIMEOUT
    notify: str | None = None  # failure alerts go here instead of the transport default
    catch_up: bool = False  # run a missed slot late
    misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE

    @property
    def ref(self) -> str:
        return f"{self.workspace}:{self.dir_name}"

    @property
    def job_dir(self) -> Path:
        return self.path.parent

    @property
    def group(self) -> str | None:
        """An explicit shared-resource group; project capacity is enforced separately."""
        return self.concurrency_group

    def next_run(self, after: datetime) -> datetime:
        """The first slot after ``after`` as a local instant; slots are wall-clock times."""
        if self.schedule is None:
            raise ValueError(f"job {self.ref} has no schedule; it fires when work is ready")
        return next_cron(self.schedule, after)

    def as_dict(self) -> dict:
        return {**asdict(self), "ref": self.ref, "path": str(self.path), "group": self.group}


# -- Parsing and validation ---------------------------------------------------


render = frontmatter.render


def _holds(kind: str, value: object) -> bool:
    """Whether one frontmatter value is the YAML type its field is declared to hold.

    A bool is an ``int`` in Python and would otherwise pass as a timeout, and a quoted
    number stays the string it was written as rather than being coerced back.
    """
    if kind == TEXT:
        return isinstance(value, str) and bool(value.strip())
    if kind in (INTEGER, NONNEGATIVE):
        minimum = 1 if kind == INTEGER else 0
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum
    return isinstance(value, bool)


def _job_path(paths: Paths, reference: str) -> Path:
    """Validate ownership before reading definitions, publishing files, or opening locks."""
    workspace, _ = split_job_ref(reference)
    require_workspace(paths, workspace)
    path = paths.job(reference)
    if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
        raise ValueError("job path must not contain symbolic links")
    return path


def parse_job(path: Path, config: Config | None = None) -> tuple[Job | None, list[str]]:
    """Read one JOB.md: the job when its schema is whole, and everything wrong with it.

    Frontmatter syntax is settled first and alone, because a block that is unusable or
    answers a key twice has no fields worth judging. Once one mapping exists, the fields,
    the body, and — given a config — everything ``config.json`` can see are checked
    together, so a broken file reports all of it at once, in the order docs/jobs.md lists
    the fields. A schema fault yields no ``Job``; a job that parsed but cannot run comes
    back with its problems beside it, so the CLI, doctor, and the web viewer can show what
    the file says next to what is wrong with it. Neither one is ever scheduled.
    """
    if (
        len(path.parents) < 5
        or path.parents[3].name != "workspaces"
        or path.parent.parent.name != "jobs"
        or path.name != "JOB.md"
    ):
        return None, ["job must be workspaces/<workspace>/jobs/<job>/JOB.md"]
    workspace = path.parents[2].name
    reference = f"{workspace}:{path.parent.name}"
    try:
        paths = config.paths if config is not None else Paths(path.parents[4])
        if path != _job_path(paths, reference):
            raise ValueError("job path does not match its owning workspace")
    except ValueError as exc:
        return None, [str(exc)]
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        return None, [f"could not read JOB.md: {exc}"]
    document, problem = frontmatter.parse(text)
    if document is None:
        return None, [f"JOB.md {problem}"]
    fields = document.fields
    problems = [
        f"{frontmatter.key_text(key)} is not a recognized field"
        for key in fields
        if key not in FIELDS
    ]
    command_stage = _command_stage(fields, config)
    problems += [
        f"{key} is required"
        for key in REQUIRED
        if key not in fields
        and not (key == "schedule" and "stage" in fields)
        and not (command_stage and key in ("provider", "model", "effort"))
    ]
    problems += [
        f"{key} {_TYPE_PROBLEMS[kind]}"
        for key, kind in FIELDS.items()
        if key in fields and not _holds(kind, fields[key])
    ]
    if ("project" in fields) != ("stage" in fields):
        problems.append("project and stage go together; give both or neither")
    if not document.body and not command_stage:
        problems.append("the prompt body is empty")
    schema_holds = not problems
    if config is not None:
        problems += _config_problems(fields, config, workspace)
    if not schema_holds:
        return None, problems
    given = {key: fields[key] for key in FIELDS if key in fields}
    given.setdefault("schedule", None)  # a stage job may leave it out
    if command_stage:
        given.setdefault("provider", "command")
        given.setdefault("model", "command")
        given.setdefault("effort", "none")
    return Job(
        dir_name=path.parent.name, workspace=workspace, path=path, prompt=document.body, **given
    ), problems


def _usable(fields: Mapping[str, object], key: str) -> str | None:
    """One text field, when it holds what the schema would accept, else ``None``.

    A field that is absent, null, or the wrong type has already reported itself; reading it
    anyway would only add a second problem about the same fault.
    """
    value = fields.get(key)
    return value if isinstance(value, str) and _holds(TEXT, value) else None


def _command_stage(fields: Mapping[str, object], config: Config | None) -> bool:
    """Whether configuration assigns this stage to Enso instead of a provider."""
    if config is None:
        return False
    project = config.projects.get(_usable(fields, "project") or "")
    stage = project.stage(_usable(fields, "stage") or "") if project else None
    return stage is not None and (stage.command is not None or stage.integrate)


def command_stage(job: Job, config: Config) -> bool:
    return _command_stage({"project": job.project, "stage": job.stage}, config)


def _agent_problems(fields: Mapping[str, object], config: Config) -> list[str]:
    """What config.json says about provider, model, and effort, each judged on its own.

    A model and an effort are only meaningful against the provider that offers them, so an
    unusable or unconfigured provider leaves nothing to judge them by and they are left to
    the problem already reported. Given a configured provider both are judged separately: a
    wrong model never hides a wrong effort, and either is still reported when the other is
    the field that is missing.
    """
    if _command_stage(fields, config):
        return []
    name = _usable(fields, "provider")
    if name is None:
        return []
    provider = config.providers.get(name)
    if provider is None:
        return [f"JOB.md.provider {name!r} is not configured"]
    problems: list[str] = []
    model = _usable(fields, "model")
    if model is not None and model not in provider.models:
        problems.append(f"JOB.md.model {model!r} is not in providers.{name}.models")
    effort = _usable(fields, "effort")
    levels = PROVIDER_CLASSES[name].effort_levels
    if effort is not None and effort not in levels:
        problems.append(f"JOB.md.effort must be one of {', '.join(levels)}")
    return problems


def _stage_problems(fields: Mapping[str, object], config: Config) -> list[str]:
    """Whether ``project`` names a configured project and ``stage`` one of its agent stages."""
    key = _usable(fields, "project")
    if key is None:
        return []
    project = config.projects.get(key)
    if project is None:
        return [f"JOB.md.project {key!r} is not configured"]
    stage = _usable(fields, "stage")
    if stage is None:
        return []
    if stage in project.human_stages:
        return [f"{stage} is a human stage; a job cannot serve it"]
    if stage not in project.stage_names:
        return [
            f"JOB.md.stage {stage!r} is not a stage of {key}; use one of "
            f"{', '.join(project.agent_stages)}"
        ]
    return []


def _config_problems(fields: Mapping[str, object], config: Config, workspace: str) -> list[str]:
    """Everything the configuration snapshot can say about usable fields.

    Each check stands alone and reads only usable values, so a file with an unknown field or
    a mistyped timeout still reports its unrunnable schedule, agent, workspace, and notify
    target in the same pass rather than hiding them behind the first fault.
    """
    problems: list[str] = []
    schedule = _usable(fields, "schedule")
    if schedule is not None:
        problem = schedule_problem(schedule)
        if problem:
            problems.append(problem)
    problems += _agent_problems(fields, config)
    problems += _stage_problems(fields, config)
    notify = _usable(fields, "notify")
    if notify is not None:
        try:
            config.resolve_target(notify)
        except ValueError as exc:
            problems.append(f"notify: {exc}")
    project = config.projects.get(_usable(fields, "project") or "")
    if project is not None and workspace != project.workspace:
        problems.append(
            f"JOB.md.project {project.key} belongs to workspace {project.workspace}; "
            "stage jobs must share its workspace"
        )
    return problems


def validate(job: Job, config: Config) -> list[str]:
    """Validate a job against installation, workspace, and project settings."""
    problems = _config_problems({key: getattr(job, key) for key in FIELDS}, config, job.workspace)
    try:
        if job.path != _job_path(config.paths, job.ref):
            raise ValueError("job path does not match its owning workspace")
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def load_jobs(paths: Paths, config: Config | None = None) -> tuple[list[Job], dict[str, list[str]]]:
    """Discover workspace jobs, with problems keyed by their qualified reference."""
    jobs: list[Job] = []
    problems: dict[str, list[str]] = {}
    if not paths.workspaces.is_dir() or paths.workspaces.is_symlink():
        return jobs, problems
    for workspace in sorted(paths.workspaces.iterdir()):
        if not valid_workspace_name(workspace.name) or not workspace.is_dir():
            continue
        root = paths.workspace_jobs(workspace.name)
        if workspace.is_symlink() or root.is_symlink() or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            path = entry / "JOB.md"
            if not path.exists() and not path.is_symlink():
                continue
            reference = f"{workspace.name}:{entry.name}"
            job, found = parse_job(path, config)
            if job is not None:
                jobs.append(job)
            if found:
                problems[reference] = found
    return jobs, problems


def find_job(paths: Paths, config: Config, name: str) -> tuple[Job | None, list[str]]:
    """Read exactly one qualified job; never fall back to another workspace."""
    try:
        path = paths.job(name)
    except ValueError as exc:
        return None, [str(exc)]
    if not path.exists() and not path.is_symlink():
        return None, [f"no job named {name}"]
    return parse_job(path, config)


def create_job(
    paths: Paths,
    config: Config,
    *,
    name: str,
    provider: str,
    model: str,
    effort: str,
    schedule: str | None,
    workspace: str,
    project: str | None = None,
    stage: str | None = None,
) -> Job:
    """Write a disabled JOB.md scaffold under a slug of ``name``; validated first."""
    dir_name = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not dir_name:
        raise ValueError("the job name needs at least one letter or digit")
    if (project is None) != (stage is None):
        raise ValueError("--project and --stage go together; give both or neither")
    if schedule is None and stage is None:
        raise ValueError("--schedule is required unless --project and --stage are given")
    job = Job(
        dir_name=dir_name,
        path=paths.job(f"{workspace}:{dir_name}"),
        name=name,
        schedule=schedule,
        provider=provider,
        model=model,
        effort=effort,
        workspace=workspace,
        enabled=False,
        prompt=PLACEHOLDER_PROMPT,
        project=project,
        stage=stage,
    )
    problems = validate(job, config)
    if problems:
        raise ValueError("; ".join(problems))
    if job.job_dir.exists():
        raise FileExistsError(f"job {job.ref} already exists at {job.job_dir}")
    job.job_dir.mkdir(parents=True)
    fields: dict[str, object] = {"name": name}
    if schedule is not None:
        fields["schedule"] = schedule
    fields |= {
        "provider": provider,
        "model": model,
        "effort": effort,
    }
    if project is not None and stage is not None:
        fields |= {"project": project, "stage": stage}
    fields["enabled"] = False
    job.path.write_text(render(fields, PLACEHOLDER_PROMPT), "utf-8")
    return job

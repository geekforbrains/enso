"""``enso doctor``: is this machine healthy, in one pass with one exit code.

Composes what already exists rather than checking anything twice: ``config.check_config``,
the workspace audit, the provider paths, the transport extras, ``service.status``, and
``jobs.load_jobs``, and the Markdown note audits. An error-level health finding makes doctor
exit 1 but does not imply that every Enso operation is blocked; warnings alone exit 0. ``--json`` is
``Report.as_dict``.

``ok`` answers "is this machine healthy". ``attention`` answers the wider question the
nightly audit job asks — "is there anything to tell the operator" — so a healthy but untidy
installation can be reported without a warning anywhere becoming a health failure.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import audit, db, heartbeat, knowledge, service, web
from .config import Config, Paths, check_config
from .formatting import preview
from .jobs import load_jobs
from .transport_registry import TRANSPORTS
from .web import service as viewer_service

SECTIONS = (
    "config",
    "home",
    "workspaces",
    "providers",
    "transports",
    "service",
    "viewer_service",
    "jobs",
    "heartbeat",
    "knowledge",
)
SKIPPED = "skipped"
# Appended to a finding the workspace audit can repair; the enso-audit prompt quotes it, so
# the agent can tell the operator which lines `--fix` covers.
FIXABLE_MARK = " (repairable with `enso workspace audit --fix`)"


@dataclass
class Section:
    """One area of the report: its problems, its warnings, and a line of context."""

    name: str
    note: str = ""  # context for the healthy case: paths, names, the pid
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)  # the facts, for scripts and the viewer
    skipped: bool = False  # could not run until something else is fixed
    # Some warning here is worth an unprompted report; see ``Report.attention``.
    attention: bool = False

    @property
    def status(self) -> str:
        """``error``, ``warning``, ``skipped``, or ``ok``."""
        if self.problems:
            return audit.ERROR
        if self.warnings:
            return audit.WARNING
        return SKIPPED if self.skipped else "ok"

    @property
    def summary(self) -> str:
        if self.skipped and not self.problems and not self.warnings:
            return SKIPPED
        return audit.count_summary(len(self.problems), len(self.warnings))

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "note": self.note,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "attention": self.attention,
            "details": dict(self.details),
        }


@dataclass
class Report:
    home: Path
    sections: list[Section]

    @property
    def ok(self) -> bool:
        """No problems anywhere; warnings and skipped sections do not fail the doctor."""
        return not any(section.problems for section in self.sections)

    @property
    def attention(self) -> bool:
        """Whether anything here is worth reporting, health problem or not.

        Every problem, plus the warnings a section marked: today the installation-hygiene
        findings, which are portable facts about the layout rather than matters of taste.
        An orphan workspace or an unedited ``AGENTS.md`` stays quiet.
        """
        return not self.ok or any(section.attention for section in self.sections)

    def section(self, name: str) -> Section:
        return next(section for section in self.sections if section.name == name)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "attention": self.attention,
            "home": str(self.home),
            "sections": [section.as_dict() for section in self.sections],
        }


def run(paths: Paths) -> Report:
    """Every section, in the order ``SECTIONS`` lists them."""
    config, problems, warnings = check_config(paths)
    report = audit.audit(paths, config=config)
    sections = [
        _config(paths, problems, warnings),
        _home(paths, report.home),
        _workspaces(report.workspaces),
        _providers(config),
        _transports(config),
        _service(),
        _viewer_service(paths),
        _jobs(paths, config),
        _heartbeat(paths, config),
        _knowledge(paths),
    ]
    return Report(paths.home, sections)


def _knowledge(paths: Paths) -> Section:
    """Bound the health summary while leaving detailed findings to scoped audit commands."""
    section = Section("knowledge")
    try:
        catalog = knowledge.scan(paths)
        findings = catalog.audit()
        section.note = f"{len(catalog.notes)} notes, {len(findings)} findings"
        section.details = {
            "notes": len(catalog.notes),
            "findings": len(findings),
            "roots": {root.scope: str(root.path) for root in catalog.roots},
        }
        for finding in findings[:10]:
            scope, relative = finding["scope"], finding["path"]
            path = str(catalog.root(scope).path / relative) if scope else str(paths.home)
            section.problems.append(f"{path}: {preview(finding['problem'], width=500)}")
        if findings:
            section.note += "; details: enso knowledge audit --workspace NAME or --shared"
        if len(findings) > 10:
            section.problems.append(f"{len(findings) - 10} more findings; use the scoped audit")
    except (OSError, ValueError, knowledge.KnowledgeError) as exc:
        section.problems.append(f"could not read knowledge at {paths.home}: {exc}")
    return section


def _skipped(name: str) -> Section:
    return Section(name, note="not checked until config.json is valid", skipped=True)


def _heartbeat(paths: Paths, config: Config | None) -> Section:
    """Read current beat health without running gates, migrating state, or loading history."""
    if config is None:
        return _skipped("heartbeat")
    section = Section("heartbeat", details={"enabled": config.heartbeat.enabled})
    counts = {"active": 0, "paused": 0}
    attention: list[str] = []
    offset = 0
    try:
        while found := heartbeat.list_beats(paths, limit=500, offset=offset):
            for beat in found:
                counts[beat.state] += 1
                if beat.attention:
                    attention.append(beat.ref)
                    if config.heartbeat.enabled and beat.state == "active":
                        if beat.last_check_status == "error":
                            section.problems.append(
                                f"{beat.ref}: checks are failing; "
                                f"read `enso heartbeat show {beat.ref}`"
                            )
                        else:
                            section.warnings.append(
                                f"{beat.ref}: needs attention; "
                                f"read `enso heartbeat show {beat.ref}`"
                            )
            offset += len(found)
    except (
        heartbeat.HeartbeatError,
        db.UnreadableDatabaseError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        section.problems.append(f"could not read heartbeat state: {exc}")
    section.details.update({**counts, "attention": attention})
    section.note = (
        f"{counts['active']} active, {counts['paused']} paused"
        if config.heartbeat.enabled
        else f"disabled; {sum(counts.values())} saved beats"
    )
    return section


def _config(paths: Paths, problems: list[str], warnings: list[str]) -> Section:
    return Section(
        "config",
        note=str(paths.config),
        problems=list(problems),
        warnings=list(warnings),
        details={"path": str(paths.config)},
    )


def _home(paths: Paths, home: audit.HomeAudit) -> Section:
    git_root = (paths.home / ".git").exists()
    section = Section(
        "home",
        note=f"Git root at {paths.home}" if git_root else str(paths.home),
        problems=[_finding_text(finding) for finding in home.errors],
        warnings=[_finding_text(finding) for finding in home.warnings],
        attention=any(finding.attention for finding in home.warnings),
        details={
            "path": str(paths.home),
            "git_root": git_root,
            "git": shutil.which("git"),
            "layout": dict(home.layout),
        },
    )
    if not section.details["git"]:
        section.warnings.append("git is not on PATH; the provider CLIs and `--fix` expect it")
    return section


def _workspaces(found: list[audit.WorkspaceAudit]) -> Section:
    section = Section(
        "workspaces",
        note=", ".join(workspace.name for workspace in found) or "none yet",
        details={workspace.name: workspace.status for workspace in found},
    )
    for workspace in found:
        section.problems.extend(f"{workspace.name}: {_finding_text(f)}" for f in workspace.errors)
        section.warnings.extend(f"{workspace.name}: {_finding_text(f)}" for f in workspace.warnings)
        if any(finding.attention for finding in workspace.warnings):
            section.attention = True
    return section


def _finding_text(finding: audit.Finding) -> str:
    return finding.message + (FIXABLE_MARK if finding.fixable else "")


def _providers(config: Config | None) -> Section:
    if config is None:
        return _skipped("providers")
    section = Section("providers", note=", ".join(config.providers))
    for name, provider in config.providers.items():
        executable = shutil.which(provider.path) is not None
        section.details[name] = {
            "path": provider.path,
            "executable": executable,
            "models": list(provider.models),
        }
        if not executable:
            section.problems.append(f"{name}: {provider.path} is not an executable on this machine")
    return section


def _transports(config: Config | None) -> Section:
    if config is None:
        return _skipped("transports")
    section = Section("transports", note=", ".join(config.transports) or "none configured")
    for name, spec in TRANSPORTS.items():
        configured = name in config.transports
        installed = spec.installed()  # absent means `serve` skips that transport
        section.details[name] = {"configured": configured, "installed": installed}
        if configured and not installed:
            section.problems.append(
                f"{name} is configured but its extra is not installed; "
                f"reinstall with `uv tool install -e './enso[{name}]'`"
            )
    return section


def _service() -> Section:
    section = Section("service")
    try:
        status = service.status()
    except service.ServiceError as exc:
        section.warnings.append(str(exc))
        return section
    section.details = {
        "platform": status.platform,
        "unit": str(status.unit),
        "installed": status.installed,
        "loaded": status.loaded,
        "pid": status.pid,
    }
    if not status.installed:
        section.note = f"{status.platform}, not installed"
        section.warnings.append(
            "the service is not installed; run `enso service install`, or `enso serve` yourself"
        )
    elif status.running:
        section.note = f"{status.platform}, running pid {status.pid}"
        binary = _binary()
        if binary and binary not in _read(status.unit):
            section.warnings.append(
                f"the unit does not run {binary}; run `enso service install` after an upgrade"
            )
    else:
        section.note = f"{status.platform}, {'loaded but stopped' if status.loaded else 'stopped'}"
        section.problems.append(
            "the service is installed but not running; `enso service start`, then `enso logs`"
        )
    return section


def _binary() -> str | None:
    try:
        return service.enso_binary()
    except service.ServiceError:
        return None


def _viewer_service(paths: Paths) -> Section:
    section = Section("viewer_service")
    try:
        state = service.status(definition=service.VIEWER)
        section.details = {
            "platform": state.platform,
            "unit": str(state.unit),
            "installed": state.installed,
            "loaded": state.loaded,
            "pid": state.pid,
        }
        if not state.installed:
            section.note = "not installed (optional)"
            return section
        home = viewer_service.unit_home(state)
        section.details["home"] = str(home)
        if home != paths.home.resolve():
            section.note = "installed for another home"
            return section
        current = web.status(paths)
        if state.running and current.running and current.pid == state.pid:
            section.note = f"{state.platform}, running pid {state.pid}"
        else:
            section.note = f"{state.platform}, stopped or not serving this home"
            section.problems.append(
                f"the viewer service is installed but not serving this home; "
                f"run `enso web start`, then check {paths.web_service_log}"
            )
    except service.ServiceError as exc:
        section.warnings.append(str(exc))
    return section


def _read(path: Path) -> str:
    try:
        return path.read_text("utf-8")
    except OSError, UnicodeError:
        return ""


def _jobs(paths: Paths, config: Config | None) -> Section:
    if config is None:
        return _skipped("jobs")
    jobs, problems = load_jobs(paths, config)
    enabled = [job.ref for job in jobs if job.enabled]
    plural = "s" if len(jobs) != 1 else ""
    section = Section(
        "jobs",
        note=f"{len(jobs)} job{plural}, {len(enabled)} enabled" if jobs else "none yet",
        details={"jobs": [job.ref for job in jobs], "enabled": enabled},
    )
    for name, found in problems.items():
        # The file, not just the directory name: nothing here repairs a job, so the report
        # has to hand the operator the exact path to open.
        workspace, _, local = name.partition(":")
        path = paths.workspace_jobs(workspace) / local / "JOB.md"
        section.problems.append(f"{name} ({path}): {'; '.join(found)}")
    return section

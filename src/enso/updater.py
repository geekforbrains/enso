"""Deterministic self-update support for Enso.

The updater follows one fixed stable channel, resolves it to an immutable Git
commit, validates a wheel built from that commit in an isolated virtualenv,
and only then installs the exact same wheel into the running Python
environment.  No provider/model is involved in the update path.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
import venv
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from . import __version__
from . import config as config_module

log = logging.getLogger(__name__)

UPDATE_REPOSITORY = "https://github.com/geekforbrains/enso.git"
UPDATE_BRANCH = "main"
_LAUNCHD_AGENT = "com.enso.agent"
_LAUNCHD_WEB = "com.enso.web"
_SYSTEMD_AGENT = "enso.service"
_SYSTEMD_WEB = "enso-web.service"

UpdateStatus = Literal["current", "updated", "blocked", "failed"]
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class UpdateResult:
    """Outcome returned to a chat transport after an update attempt."""

    status: UpdateStatus
    message: str
    revision: str = ""
    version: str = ""

    @property
    def restart_required(self) -> bool:
        return self.status == "updated"


class UpdateError(RuntimeError):
    """An update stage failed with a user-safe summary."""

    def __init__(self, stage: str, detail: str, *, install_started: bool = False):
        super().__init__(detail)
        self.stage = stage
        self.detail = detail
        self.install_started = install_started


def _state_path() -> str:
    return os.path.join(config_module.CONFIG_DIR, "update.json")


def _lock_path() -> str:
    return os.path.join(config_module.CONFIG_DIR, "update.lock")


def _load_state() -> dict:
    try:
        with open(_state_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """Atomically persist updater-owned metadata, separate from config.json."""
    os.makedirs(config_module.CONFIG_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="update-", dir=config_module.CONFIG_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, _state_path())
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)


@contextlib.contextmanager
def _update_lock():
    """Hold a non-blocking process-wide update lock."""
    os.makedirs(config_module.CONFIG_DIR, exist_ok=True)
    with open(_lock_path(), "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("lock", "Another Enso update is already running.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _safe_detail(output: str) -> str:
    """Return a short diagnostic without dumping a subprocess transcript."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "command exited unsuccessfully"
    home = os.path.expanduser("~")
    detail = " | ".join(lines[-3:]).replace(home, "~")
    detail = re.sub(r"(https?://)[^\s/:]+:[^\s@]+@", r"\1***:***@", detail)
    return detail[:600]


def _run_checked(
    cmd: list[str],
    *,
    stage: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    install_started: bool = False,
) -> str:
    """Run a deterministic argv command and convert failures to UpdateError."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(
            stage, str(exc), install_started=install_started,
        ) from exc
    if result.returncode != 0:
        log.warning("Enso update stage %s failed (exit=%d)", stage, result.returncode)
        raise UpdateError(
            stage,
            _safe_detail(result.stdout),
            install_started=install_started,
        )
    return result.stdout.strip()


def _sanitized_env(home: str) -> dict[str, str]:
    """Build an environment that does not expose user credentials to tests."""
    allowed = {
        "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["HOME"] = home
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _resolve_remote_revision() -> str:
    output = _run_checked(
        [
            "git", "ls-remote", "--exit-code", UPDATE_REPOSITORY,
            f"refs/heads/{UPDATE_BRANCH}",
        ],
        stage="checking GitHub",
        timeout=60,
    )
    revision = output.split()[0] if output else ""
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        raise UpdateError("checking GitHub", "GitHub returned an invalid revision.")
    return revision.lower()


def _git_revision(path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    revision = result.stdout.strip().lower()
    return revision if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", revision) else ""


def _installed_direct_url() -> dict:
    """Read standard PEP 610 installation-origin metadata for Enso."""
    try:
        text = importlib.metadata.distribution("enso").read_text("direct_url.json")
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError, importlib.metadata.PackageNotFoundError):
        return {}


def _editable_install_path() -> str:
    """Return the local checkout behind an editable install, if present."""
    direct_url = _installed_direct_url()
    if not direct_url.get("dir_info", {}).get("editable"):
        return ""
    parsed = urlparse(direct_url.get("url", ""))
    return unquote(parsed.path) if parsed.scheme == "file" else ""


def _checkout_contains_revision(path: str, revision: str) -> bool:
    """Return whether a local checkout already contains the stable commit."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "merge-base", "--is-ancestor", revision, "HEAD"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _installed_revision() -> str:
    """Read the installed VCS revision, with updater state as a fallback."""
    direct_url = _installed_direct_url()
    vcs_revision = direct_url.get("vcs_info", {}).get("commit_id", "")
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", vcs_revision):
        return vcs_revision.lower()

    editable_path = _editable_install_path()
    if editable_path:
        revision = _git_revision(editable_path)
        if revision:
            return revision

    state = _load_state()
    if state.get("version") == __version__:
        revision = state.get("revision", "")
        if isinstance(revision, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            return revision.lower()
    return ""


def _checkout_revision(source: str, revision: str, env: dict[str, str]) -> None:
    _run_checked(["git", "init", "--quiet", source], stage="preparing source", env=env)
    _run_checked(
        ["git", "-C", source, "remote", "add", "origin", UPDATE_REPOSITORY],
        stage="preparing source",
        env=env,
    )
    _run_checked(
        ["git", "-C", source, "fetch", "--quiet", "--depth", "1", "origin", revision],
        stage="fetching source",
        env=env,
        timeout=180,
    )
    _run_checked(
        ["git", "-C", source, "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        stage="checking out source",
        env=env,
    )
    if _git_revision(source) != revision:
        raise UpdateError(
            "checking out source", "Fetched source did not match the pinned revision."
        )


def _source_version(source: str) -> str:
    try:
        text = Path(source, "pyproject.toml").read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError("reading version", "pyproject.toml is missing.") from exc
    project = text.split("[project]", 1)[-1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', project, re.MULTILINE)
    if not match:
        raise UpdateError("reading version", "The source version is invalid.")
    return match.group(1)


def _build_wheel(source: str, wheel_dir: str, env: dict[str, str]) -> str:
    _run_checked(
        [
            sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps",
            "--wheel-dir", wheel_dir, source,
        ],
        stage="building package",
        env=env,
        timeout=600,
    )
    wheels = list(Path(wheel_dir).glob("enso-*.whl"))
    if len(wheels) != 1:
        raise UpdateError("building package", "Expected exactly one Enso wheel.")
    return str(wheels[0])


def _validate_wheel(wheel: str, source: str, root: str, env: dict[str, str]) -> None:
    venv_dir = os.path.join(root, "validation")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    except Exception as exc:
        raise UpdateError("creating validation environment", str(exc)) from exc
    python = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin", "python")
    all_extras = f"{wheel}[dev,telegram,slack,web]"
    _run_checked(
        [python, "-m", "pip", "install", "--quiet", all_extras],
        stage="installing validation package",
        env=env,
        timeout=900,
    )
    _run_checked(
        [python, "-m", "enso.cli", "--version"],
        stage="smoke testing package",
        cwd=root,
        env=env,
        timeout=60,
    )
    tests = os.path.join(source, "tests")
    if not os.path.isdir(tests):
        raise UpdateError("running tests", "The stable source did not contain its test suite.")
    _run_checked(
        [python, "-m", "pytest", "-q", tests],
        stage="running tests",
        cwd=root,
        env=env,
        timeout=1200,
    )


def _live_extras(config: dict) -> list[str]:
    extras: list[str] = []
    transport = config.get("transport")
    if transport in {"telegram", "slack"}:
        extras.append(transport)
    web = config.get("web")
    if isinstance(web, dict) and web.get("enabled", True):
        extras.append("web")
    return extras


def _install_wheel(wheel: str, config: dict, root: str) -> None:
    extras = _live_extras(config)
    requirement = f"{wheel}[{','.join(extras)}]" if extras else wheel
    # First satisfy any dependencies introduced by this revision. pip may
    # legitimately skip the Enso wheel here when its version number did not
    # change between commits.
    _run_checked(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", requirement],
        stage="installing update dependencies",
        timeout=900,
        install_started=True,
    )
    # Revision identity is a Git SHA, not only the package version. Force just
    # the already-validated Enso wheel so same-version source updates land,
    # without needlessly reinstalling its full dependency graph.
    _run_checked(
        [
            sys.executable, "-m", "pip", "install", "--quiet",
            "--force-reinstall", "--no-deps", wheel,
        ],
        stage="installing update",
        timeout=900,
        install_started=True,
    )
    _run_checked(
        [sys.executable, "-m", "enso.cli", "--version"],
        stage="checking installed update",
        cwd=root,
        timeout=60,
        install_started=True,
    )


def update_enso(config: dict, progress: ProgressCallback | None = None) -> UpdateResult:
    """Check stable main, validate it, and install it when the SHA differs."""
    report = progress or (lambda _message: None)
    try:
        with _update_lock():
            report("Checking the stable Enso source on GitHub…")
            remote_revision = _resolve_remote_revision()
            current_revision = _installed_revision()
            if current_revision == remote_revision:
                state = _load_state()
                state.update({
                    "repository": UPDATE_REPOSITORY,
                    "branch": UPDATE_BRANCH,
                    "revision": remote_revision,
                    "version": __version__,
                })
                _save_state(state)
                return UpdateResult(
                    "current",
                    f"Already up to date — Enso v{__version__} ({remote_revision[:8]}).",
                    remote_revision,
                    __version__,
                )

            editable_path = _editable_install_path()
            if editable_path and _checkout_contains_revision(
                editable_path, remote_revision,
            ):
                return UpdateResult(
                    "current",
                    f"No update needed — this checkout ({current_revision[:8]}) "
                    f"is ahead of stable main ({remote_revision[:8]}).",
                    current_revision,
                    __version__,
                )

            with tempfile.TemporaryDirectory(prefix="enso-update-") as root:
                home = os.path.join(root, "home")
                source = os.path.join(root, "source")
                wheel_dir = os.path.join(root, "wheel")
                os.makedirs(home)
                os.makedirs(wheel_dir)
                env = _sanitized_env(home)

                report(f"Fetching stable revision {remote_revision[:8]}…")
                _checkout_revision(source, remote_revision, env)
                version = _source_version(source)

                report(f"Building Enso v{version}…")
                wheel = _build_wheel(source, wheel_dir, env)

                report("Validating the package and running its tests…")
                _validate_wheel(wheel, source, root, env)

                report("Installing the validated package…")
                _install_wheel(wheel, config, root)

            _save_state({
                "repository": UPDATE_REPOSITORY,
                "branch": UPDATE_BRANCH,
                "revision": remote_revision,
                "version": version,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            previous = current_revision[:8] if current_revision else f"v{__version__}"
            return UpdateResult(
                "updated",
                f"Validated and installed Enso v{version} ({previous} → {remote_revision[:8]}). "
                "Restarting Enso services…",
                remote_revision,
                version,
            )
    except UpdateError as exc:
        if exc.stage == "lock":
            return UpdateResult("blocked", exc.detail)
        if exc.install_started:
            message = (
                f"Update failed while {exc.stage}; services were not restarted. "
                f"Check the install from a terminal. ({exc.detail})"
            )
        else:
            message = (
                f"Update stopped while {exc.stage}; the installed Enso was not changed. "
                f"({exc.detail})"
            )
        return UpdateResult("failed", message)
    except Exception:
        log.exception("Unexpected Enso update failure")
        return UpdateResult(
            "failed",
            "Update failed unexpectedly; services were not restarted. "
            "Check ~/.enso/enso.log.",
        )


def installed_service_names() -> list[str]:
    """Return managed Enso services that should survive an update restart."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/LaunchAgents")
        services = []
        if os.path.exists(os.path.join(base, f"{_LAUNCHD_AGENT}.plist")):
            services.append("agent")
        if os.path.exists(os.path.join(base, f"{_LAUNCHD_WEB}.plist")):
            services.append("web")
        return services
    if sys.platform == "linux":
        base = os.path.expanduser("~/.config/systemd/user")
        services = []
        if os.path.exists(os.path.join(base, _SYSTEMD_AGENT)):
            services.append("agent")
        if os.path.exists(os.path.join(base, _SYSTEMD_WEB)):
            services.append("web")
        return services
    return []


def queue_update_confirmation(
    result: UpdateResult,
    *,
    transport: str,
    channel: str,
    thread: str = "",
) -> None:
    """Persist the success message that the restarted process must deliver."""
    state = _load_state()
    state["pending_confirmation"] = {
        "id": uuid.uuid4().hex,
        "transport": transport,
        "channel": channel,
        "thread": thread,
        "revision": result.revision,
        "version": result.version,
        "services": installed_service_names(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)


def pending_update_confirmation(transport: str) -> dict | None:
    pending = _load_state().get("pending_confirmation")
    if isinstance(pending, dict) and pending.get("transport") == transport:
        return pending
    return None


def clear_update_confirmation(confirmation_id: str) -> None:
    state = _load_state()
    pending = state.get("pending_confirmation")
    if isinstance(pending, dict) and pending.get("id") == confirmation_id:
        state.pop("pending_confirmation", None)
        _save_state(state)


def _service_running(service: str) -> bool:
    """Check a managed service without treating the current process specially."""
    try:
        if sys.platform == "darwin":
            label = _LAUNCHD_AGENT if service == "agent" else _LAUNCHD_WEB
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return result.returncode == 0 and re.search(
                r"^\s*state\s*=\s*running\s*$", result.stdout, re.MULTILINE,
            ) is not None
        if sys.platform == "linux":
            unit = _SYSTEMD_AGENT if service == "agent" else _SYSTEMD_WEB
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def update_confirmation_message(pending: dict) -> str:
    """Build the post-restart health confirmation sent by the new process."""
    services = [s for s in pending.get("services", []) if isinstance(s, str)]
    unhealthy = [service for service in services if not _service_running(service)]
    version = pending.get("version", "?")
    revision = str(pending.get("revision", ""))[:8]
    if unhealthy:
        names = ", ".join(unhealthy)
        return (
            f"Enso v{version} ({revision}) started, but these services are not healthy: {names}. "
            "Check ~/.enso/enso.log and ~/.enso/web.log."
        )
    checked = ", ".join(services) if services else "Enso"
    return f"Update complete — Enso v{version} ({revision}) restarted successfully ({checked})."


def restart_services() -> None:
    """Restart dashboard first, then replace the bot process via its manager."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/LaunchAgents")
        domain = f"gui/{os.getuid()}"
        if os.path.exists(os.path.join(base, f"{_LAUNCHD_WEB}.plist")):
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"{domain}/{_LAUNCHD_WEB}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if os.path.exists(os.path.join(base, f"{_LAUNCHD_AGENT}.plist")):
            os.execvp(
                "launchctl",
                ["launchctl", "kickstart", "-k", f"{domain}/{_LAUNCHD_AGENT}"],
            )
    elif sys.platform == "linux":
        base = os.path.expanduser("~/.config/systemd/user")
        xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        systemd_env = {
            **os.environ,
            "XDG_RUNTIME_DIR": xdg,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg}/bus",
        }
        if os.path.exists(os.path.join(base, _SYSTEMD_WEB)):
            subprocess.run(
                ["systemctl", "--user", "restart", _SYSTEMD_WEB],
                env=systemd_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if os.path.exists(os.path.join(base, _SYSTEMD_AGENT)):
            os.execvpe(
                "systemctl",
                ["systemctl", "--user", "restart", _SYSTEMD_AGENT],
                systemd_env,
            )

    # Foreground/manual `enso serve`: replace only this process.
    os.execv(sys.executable, [sys.executable, "-m", "enso.cli", "serve"])


def schedule_service_restart(delay: float = 3.0) -> None:
    """Schedule restart after the transport has had time to send its reply."""
    import asyncio

    asyncio.get_running_loop().call_later(delay, restart_services)


async def wait_for_service_settle() -> None:
    """Give launchd/systemd enough time to expose crash-on-start failures."""
    import asyncio

    await asyncio.sleep(3)

"""Read trusted release manifests and prepare isolated, verified uv installations.

This module uses only the standard library so the published shell installer can embed it,
and it imports nothing from the package. `fetch` is the one bounded, redirect-refusing HTTP
read; the skill catalog imports it rather than keeping a copy. The module never selects a
release or changes a running Enso home; the updater owns that transition.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

MANIFEST_LIMIT = 64 * 1024
CONSTRAINTS_LIMIT = 2 * 1024 * 1024
WHEEL_LIMIT = 80 * 1024 * 1024
DOWNLOAD_TIMEOUT = 60
INSTALL_TIMEOUT = 600
VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(VERSION_PATTERN + r"\Z")
_PYTHON = re.compile(r">=3\.(?:14|15|16|17|18|19)(?:\.[0-9]+)?(?:,<4(?:\.0)?)?\Z")
_EXTRAS = frozenset({"slack", "telegram", "web"})


class ReleaseError(Exception):
    """A release could not be safely read or prepared."""


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "sha256": self.sha256}


@dataclass(frozen=True)
class Release:
    version: str
    commit: str
    requires_python: str
    wheel: Artifact
    constraints: Artifact
    source: str
    release_notes_url: str | None = None

    @property
    def release_id(self) -> str:
        """A stable directory component; artifact changes never overwrite another release."""
        return f"{self.version}-{self.wheel.sha256[:12]}"

    def to_dict(self, *, resolved: bool = False) -> dict[str, Any]:
        def artifact_dict(artifact: Artifact) -> dict[str, str]:
            return {
                "url": _artifact_location(self.source, artifact.url) if resolved else artifact.url,
                "sha256": artifact.sha256,
            }

        result: dict[str, Any] = {
            "schema_version": 1,
            "version": self.version,
            "commit": self.commit,
            "requires_python": self.requires_python,
            "wheel": artifact_dict(self.wheel),
            "constraints": artifact_dict(self.constraints),
        }
        if self.release_notes_url is not None:
            result["release_notes_url"] = self.release_notes_url
        return result


def _remote(source: str) -> bool:
    return bool(_split_url(source).scheme)


def _split_url(url: str) -> urllib.parse.SplitResult:
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        raise ReleaseError("Release URL is malformed.") from None


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = _split_url(url)
    try:
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname, port
    except ValueError:
        raise ReleaseError("Release URL has an invalid port.") from None


def _validate_url(url: str) -> None:
    parsed = _split_url(url)
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ReleaseError("Release URLs must not contain whitespace or control characters.")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ReleaseError("Release URLs must not contain credentials or fragments.")
    _origin(url)
    if parsed.scheme == "https":
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "http" or not loopback:
        raise ReleaseError(
            "Release URLs require HTTPS (HTTP is allowed only on loopback addresses)."
        )


def _artifact_location(source: str, value: str) -> str:
    parsed = _split_url(value)
    if parsed.scheme:
        _validate_url(value)
        return value
    if not _remote(source) and not parsed.netloc and Path(value).is_absolute():
        return str(Path(value).resolve())
    decoded = urllib.parse.unquote(parsed.path)
    if (
        parsed.netloc
        or parsed.query
        or parsed.fragment
        or not decoded
        or decoded.startswith("/")
        or "\\" in decoded
        or any(part in {".", "..", ""} for part in decoded.split("/"))
    ):
        raise ReleaseError("Release artifact paths must stay inside the manifest directory.")
    if _remote(source):
        result = urllib.parse.urljoin(source, value)
        _validate_url(result)
        return result
    directory = Path(source).parent.resolve()
    result_path = (directory / decoded).resolve()
    if not result_path.is_relative_to(directory):
        raise ReleaseError("Release artifact paths must stay inside the manifest directory.")
    return str(result_path)


def _read_token(token_file: Path | str | None) -> str | None:
    if token_file is None:
        return None
    try:
        with Path(token_file).open("r", encoding="utf-8") as handle:
            token = handle.read(8193).strip()
    except OSError, UnicodeError:
        raise ReleaseError("Cannot read the release token file.") from None
    if not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ReleaseError("The release token file must contain one nonempty bearer token.")
    return token


class FetchError(Exception):
    """A bounded fetch did not complete.

    `redirect` is the absolute target of a redirect that was not followed, so a caller with
    a redirect policy can decide whether to continue; every other failure leaves it `None`.
    """

    def __init__(self, message: str, *, redirect: str | None = None) -> None:
        super().__init__(message)
        self.redirect = redirect


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def fetch(url: str, limit: int, deadline: float, *, headers: dict[str, str]) -> bytes:
    """Read at most `limit` bytes from one URL before the monotonic `deadline`.

    Redirects are never followed; the `FetchError` names the target instead. The response
    is read uncompressed in bounded chunks so the limit and the deadline apply to bytes on
    the wire, and each socket wait is capped at 20 seconds so a silent peer overshoots the
    deadline by at most that much.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError("timed out")
    request = urllib.request.Request(url, headers={**headers, "Accept-Encoding": "identity"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=min(20.0, remaining)) as response:
            result = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise FetchError("timed out")
                chunk = response.read1(min(65536, limit + 1 - len(result)))
                if not chunk:
                    return bytes(result)
                result.extend(chunk)
                if len(result) > limit:
                    raise FetchError("exceeds its size limit")
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.code in {301, 302, 303, 307, 308} else None
        redirect = urllib.parse.urljoin(url, location) if location else None
        raise FetchError(f"failed (HTTP {exc.code})", redirect=redirect) from None
    except urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException:
        raise FetchError("failed or timed out") from None


def _read_bytes(source: str, limit: int, *, token: str | None, auth_source: str) -> bytes:
    if not _remote(source):
        try:
            with Path(source).open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError:
            raise ReleaseError("Cannot read the local release artifact.") from None
        if len(data) > limit:
            raise ReleaseError("Release artifact exceeds its download size limit.")
        return data
    return _download(source, limit, token=token, auth_source=auth_source)


def _download(source: str, limit: int, *, token: str | None, auth_source: str) -> bytes:
    """Follow a bounded redirect chain, sending the bearer token only to the feed's origin."""
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    authorized = bool(token and _remote(auth_source) and _origin(source) == _origin(auth_source))
    for _ in range(6):
        _validate_url(source)
        headers = {"User-Agent": "enso-release/1"}
        if authorized:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return fetch(source, limit, deadline, headers=headers)
        except FetchError as exc:
            if exc.redirect is None:
                raise ReleaseError(f"Release download {exc}.") from None
            target = exc.redirect
        _validate_url(target)
        authorized = authorized and _origin(target) == _origin(source)
        source = target
    raise ReleaseError("Release download exceeded the redirect limit.")


def normalize_source(source: str | Path) -> str:
    """Validate a persisted manifest/feed source and make local paths independent of cwd.

    Feed authentication belongs in a token file. Artifact URLs may use signed queries,
    but a manifest/feed URL must not retain credentials in a query string.
    """
    source = str(source)
    if not source.strip():
        raise ReleaseError("A release manifest source must not be empty.")
    parsed = _split_url(source)
    if _remote(source):
        _validate_url(source)
        if parsed.query:
            raise ReleaseError(
                "Release manifest URLs must use token-file authentication, not queries."
            )
        return source
    if parsed.netloc:
        raise ReleaseError("Release URLs require an explicit HTTPS scheme.")
    return str(Path(source).expanduser().resolve())


def load_release(source: str | Path, *, token_file: str | Path | None = None) -> Release:
    """Load an explicitly trusted local/HTTPS manifest without leaking its authentication."""
    source = normalize_source(source)
    data = _read_bytes(source, MANIFEST_LIMIT, token=_read_token(token_file), auth_source=source)
    try:
        raw = json.loads(data)
    except ValueError, UnicodeError:
        raise ReleaseError("Release manifest is not valid JSON.") from None
    return parse_release(raw, source)


def _parse_artifact(entry: Any, name: str, source: str, problems: list[str]) -> Artifact:
    if not isinstance(entry, dict):
        problems.append(f"{name} must contain url and sha256")
        return Artifact("", "")
    if set(entry) != {"url", "sha256"}:
        problems.append(f"{name} must contain only url and sha256")
    url, checksum = entry.get("url"), entry.get("sha256")
    if not isinstance(url, str) or not url:
        problems.append(f"{name}.url must be nonempty")
    else:
        try:
            _artifact_location(source, url)
        except ReleaseError as exc:
            problems.append(f"{name}: {exc}")
    if not isinstance(checksum, str) or not _HASH.fullmatch(checksum):
        problems.append(f"{name}.sha256 must be a lowercase SHA256 hash")
    return Artifact(
        url if isinstance(url, str) else "", checksum if isinstance(checksum, str) else ""
    )


def parse_release(raw: Any, source: str) -> Release:
    """Validate every independent manifest field, retaining relative artifact references."""
    source = normalize_source(source)
    if not isinstance(raw, dict):
        raise ReleaseError("Release manifest must be an object.")
    problems: list[str] = []
    allowed = {
        "schema_version",
        "version",
        "commit",
        "requires_python",
        "wheel",
        "constraints",
        "release_notes_url",
    }
    if raw.keys() - allowed:
        problems.append("unknown manifest fields")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        problems.append("schema_version must be 1")
    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        problems.append("version must be a canonical major.minor.patch release version")
    commit = raw.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        problems.append("commit must be a full 40-character Git commit")
    python = raw.get("requires_python")
    if not isinstance(python, str) or not _PYTHON.fullmatch(python):
        problems.append("requires_python must select Python >=3.14 or newer, optionally <4")
    artifacts = {
        name: _parse_artifact(raw.get(name), name, source, problems)
        for name in ("wheel", "constraints")
    }
    notes = raw.get("release_notes_url")
    if notes is not None:
        try:
            if not isinstance(notes, str):
                raise ReleaseError("must be an HTTPS URL")
            _validate_url(notes)
        except ReleaseError as exc:
            problems.append(f"release_notes_url: {exc}")
    if problems:
        raise ReleaseError("Invalid release manifest: " + "; ".join(problems) + ".")
    assert isinstance(version, str) and isinstance(commit, str) and isinstance(python, str)
    return Release(
        version, commit, python, artifacts["wheel"], artifacts["constraints"], source, notes
    )


def download_artifact(
    release: Release,
    artifact: Artifact,
    *,
    token_file: str | Path | None = None,
    limit: int = WHEEL_LIMIT,
) -> bytes:
    """Resolve a manifest artifact and verify its bytes before they reach an installer."""
    source = _artifact_location(release.source, artifact.url)
    data = _read_bytes(source, limit, token=_read_token(token_file), auth_source=release.source)
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ReleaseError("Release artifact SHA256 verification failed.")
    return data


def _validate_wheel(path: Path, release: Release) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1 or archive.getinfo(metadata[0]).file_size > MANIFEST_LIMIT:
                raise ReleaseError("Release wheel has invalid package metadata.")
            message = BytesParser().parsebytes(archive.read(metadata[0]))
    except OSError, zipfile.BadZipFile, KeyError:
        raise ReleaseError("Release wheel is not a readable Python wheel.") from None
    if message["Name"] != "enso" or message["Version"] != release.version:
        raise ReleaseError("Release wheel package name or version does not match the manifest.")
    if message["Requires-Python"] != release.requires_python:
        raise ReleaseError("Release wheel Python requirement does not match the manifest.")


def _package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_constraints(data: bytes) -> set[tuple[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ReleaseError("Release constraints are not UTF-8.") from None
    pins = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9_.!+-]+)(?:\s*;\s*[^\r\n]+)?\Z")
    versions: set[tuple[str, str]] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = pins.fullmatch(line)
        if not match:
            raise ReleaseError("Release constraints must contain only exact package version pins.")
        versions.add((_package_name(match[1]), match[2]))
    return versions


def _validate_installed(output: str, versions: set[tuple[str, str]], version: str) -> None:
    try:
        packages = json.loads(output)
        if not isinstance(packages, list):
            raise ValueError
        for package in packages:
            name, installed_version = package["name"], package["version"]
            if not isinstance(name, str) or not isinstance(installed_version, str):
                raise ValueError
            normalized = _package_name(name)
            if normalized == "enso" and installed_version == version:
                continue
            if (normalized, installed_version) not in versions:
                raise ReleaseError(
                    "Installed dependencies are not fully pinned by release constraints."
                )
    except ValueError, KeyError, TypeError:
        raise ReleaseError("Cannot verify the installed dependency versions.") from None


def run_bounded(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = INSTALL_TIMEOUT,
) -> str:
    """Run an installer with a bounded output tail, deadline, and child-process cleanup."""
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        raise ReleaseError("Cannot start the release installer; ensure uv is installed.") from None
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReleaseError("Release installation timed out.")
                for key, _ in selector.select(min(remaining, 0.5)):
                    chunk = os.read(key.fd, 8192)
                    if chunk:
                        output.extend(chunk)
                        del output[:-16384]
                    else:
                        selector.unregister(key.fileobj)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if code:
            # Installer diagnostics can include index credentials; expose only a bounded code.
            raise ReleaseError(f"Release installation failed (exit {code}).")
        return output.decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        raise ReleaseError("Release installation timed out.") from None
    finally:
        # A grandchild may keep stdout open after its parent exits; clean the whole group.
        try:
            _kill_installer_group(process)
            process.wait()
        finally:
            process.stdout.close()


def _kill_installer_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as denied:
        # macOS excludes zombie members from signal delivery and can report EPERM
        # until the child is reaped. Suppress only a subsequently proven absent group.
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise denied from None
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            raise denied from None
        raise denied


def _receipt(release: Release, extras: tuple[str, ...]) -> dict[str, Any]:
    return {
        "version": release.version,
        "commit": release.commit,
        "requires_python": release.requires_python,
        "wheel_sha256": release.wheel.sha256,
        "constraints_sha256": release.constraints.sha256,
        "extras": sorted(extras),
    }


def _reuse_prepared(release: Release, release_dir: Path, extras: tuple[str, ...]) -> bool:
    if not release_dir.exists() and not release_dir.is_symlink():
        return False
    try:
        valid = (
            not release_dir.is_symlink()
            and (release_dir / "bin/enso").is_file()
            and json.loads((release_dir / "installation_receipt.json").read_text())
            == _receipt(release, extras)
        )
    except OSError, ValueError:
        valid = False
    if not valid:
        raise ReleaseError("Release directory already exists without a matching completed install.")
    installed = run_bounded(
        [
            str(release_dir / "bin/python"),
            "-c",
            "from importlib.metadata import version; print(version('enso'))",
        ],
        cwd=release_dir,
    )
    if installed != release.version:
        raise ReleaseError("Prepared release version does not match its installation receipt.")
    return True


def prepare_release(
    release: Release,
    release_dir: Path,
    *,
    extras: tuple[str, ...] = ("slack", "telegram", "web"),
    uv: str = "uv",
    token_file: str | Path | None = None,
) -> Path:
    """Create a new venv at its final path; remove only this attempt after any failure.

    The caller owns locking. A verified completed install can be reused, never overwritten.
    A venv cannot be renamed after creation because installed console scripts use absolute paths.
    """
    if set(extras) - _EXTRAS or len(set(extras)) != len(extras):
        raise ReleaseError("Release extras must be unique names from slack, telegram, web.")
    release_dir = release_dir.absolute()
    if _reuse_prepared(release, release_dir, extras):
        return release_dir
    wheel_name = Path(urllib.parse.unquote(_split_url(release.wheel.url).path)).name
    if not re.fullmatch(r"enso-[A-Za-z0-9_.+-]+\.whl", wheel_name):
        raise ReleaseError("Release wheel must have an enso Python wheel filename.")
    wheel = download_artifact(release, release.wheel, token_file=token_file)
    constraints = download_artifact(
        release, release.constraints, token_file=token_file, limit=CONSTRAINTS_LIMIT
    )
    versions = _validate_constraints(constraints)
    release_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        release_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise ReleaseError(
            "Release directory already exists; it will not be overwritten."
        ) from None
    try:
        with tempfile.TemporaryDirectory(prefix=".download-", dir=release_dir.parent) as temporary:
            artifact_dir = Path(temporary)
            wheel_path = artifact_dir / wheel_name
            wheel_path.write_bytes(wheel)
            _validate_wheel(wheel_path, release)
            constraints_path = artifact_dir / "constraints.txt"
            constraints_path.write_bytes(constraints)
            env = dict(os.environ)
            env.pop("ENSO_RELEASE_TOKEN", None)
            env["UV_PYTHON_INSTALL_DIR"] = str(release_dir.parent.parent / "python")
            env["UV_CACHE_DIR"] = str(release_dir.parent.parent / "cache" / "uv")
            run_bounded(
                [
                    uv,
                    "--no-config",
                    "venv",
                    "--python",
                    release.requires_python.removeprefix(">=").split(",")[0],
                    str(release_dir),
                ],
                cwd=artifact_dir,
                env=env,
            )
            requirement = str(wheel_path) + (f"[{','.join(extras)}]" if extras else "")
            run_bounded(
                [
                    uv,
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    str(release_dir / "bin/python"),
                    "--no-build",
                    "--constraint",
                    str(constraints_path),
                    requirement,
                ],
                cwd=artifact_dir,
                env=env,
            )
            run_bounded(
                [uv, "--no-config", "pip", "check", "--python", str(release_dir / "bin/python")],
                cwd=artifact_dir,
                env=env,
            )
            dependencies = run_bounded(
                [
                    uv,
                    "--quiet",
                    "--no-config",
                    "pip",
                    "list",
                    "--format",
                    "json",
                    "--python",
                    str(release_dir / "bin/python"),
                ],
                cwd=artifact_dir,
                env=env,
            )
            _validate_installed(dependencies, versions, release.version)
            installed = run_bounded(
                [
                    str(release_dir / "bin/python"),
                    "-c",
                    "from importlib.metadata import version; print(version('enso'))",
                ],
                cwd=artifact_dir,
                env=env,
            )
            if installed != release.version:
                raise ReleaseError("Installed Enso version does not match the release manifest.")
        (release_dir / "release.json").write_text(
            json.dumps(release.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        receipt = release_dir / ".installation_receipt.json.tmp"
        receipt.write_text(json.dumps(_receipt(release, extras)) + "\n", encoding="utf-8")
        receipt.replace(release_dir / "installation_receipt.json")
        return release_dir
    except BaseException:
        shutil.rmtree(release_dir)
        raise

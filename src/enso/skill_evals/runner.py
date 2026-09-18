"""Run installed Codex/Claude CLIs against isolated, disposable skill fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from enso.execution import kill_process_group
from enso.skill_evals.events import measure
from enso.skill_evals.scenarios import check_results, prepare

MAX_OUTPUT = 32 * 1024 * 1024
MAX_PACKAGE = 10 * 1024 * 1024
type Package = dict[str, tuple[bytes, bool]]


def save_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def package(repo: Path, skill: str, source: str) -> Package:
    """Freeze a complete package from a Git revision or the current working tree."""
    relative = f"src/enso/bundled/skills/{skill}"
    files: Package = {}
    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        source_path = repo / source_path
    if source == "working-tree" or source_path.is_dir():
        root = repo / relative if source == "working-tree" else source_path
        if root.is_symlink():
            raise ValueError(f"Skill package root must not be a symlink: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Skill packages must not contain symlinks: {path}")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[str(path.relative_to(root))] = (
                    path.read_bytes(),
                    bool(path.stat().st_mode & 0o111),
                )
    else:
        revision = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{source}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        names = subprocess.run(
            ["git", "ls-tree", "-r", "-z", revision, "--", relative],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        for entry in names.split(b"\0"):
            if not entry:
                continue
            info, filename = entry.decode().split("\t", 1)
            mode, kind, sha = info.split()
            if kind != "blob" or mode == "120000":
                raise ValueError(f"Expected ordinary package file: {filename}")
            data = subprocess.run(
                ["git", "cat-file", "blob", sha],
                cwd=repo,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout
            files[str(Path(filename).relative_to(relative))] = (data, mode == "100755")
    if "SKILL.md" not in files:
        raise ValueError(f"{source}: no {relative}/SKILL.md")
    if sum(len(data) for data, _ in files.values()) > MAX_PACKAGE:
        raise ValueError("Skill package exceeds 10 MB")
    return files


def package_hash(files: Package) -> str:
    digest = hashlib.sha256()
    for name, (data, executable) in sorted(files.items()):
        digest.update(name.encode() + bytes([executable]) + hashlib.sha256(data).digest())
    return digest.hexdigest()


def write_package(root: Path, files: Package) -> None:
    for name, (data, executable) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755 if executable else 0o644)


def environment(scratch: Path, home: Path, provider: str) -> dict[str, str]:
    """Allow only basic process settings and the selected CLI's authentication."""
    env = {
        k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "USER", "LOGNAME"}
    }
    for name in ("tmp", "user", "cache", "config"):
        (scratch / name).mkdir(exist_ok=True)
    env.update(
        {
            "HOME": str(scratch / "user"),
            "TMPDIR": str(scratch / "tmp"),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "XDG_CONFIG_HOME": str(scratch / "config"),
            "ENSO_HOME": str(home),
            "ENSO_WORKSPACE": "eval",
            "SHELL": "/bin/bash",
            "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", os.defpath),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "NO_COLOR": "1",
        }
    )
    if provider == "codex":
        auth_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        isolated = scratch / "codex"
        isolated.mkdir(mode=0o700)
        auth = auth_root / "auth.json"
        if auth.is_file():
            shutil.copyfile(auth, isolated / "auth.json")
            (isolated / "auth.json").chmod(0o600)
        env["CODEX_HOME"] = str(isolated)
        for name in ("CODEX_API_KEY", "OPENAI_API_KEY"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    else:
        # Claude's subscription login can depend on the OS keychain and ~/.claude.json.
        # Keep its native auth lookup; --setting-sources project excludes user guidance.
        env["HOME"] = str(Path.home())
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        env.update(
            {
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
    return env


def command(
    executable: str,
    provider: str,
    model: str,
    effort: str,
    home: Path,
    workspace: Path,
    scratch: Path,
) -> list[str]:
    """Use the installed CLIs' structured modes, native skill discovery and sandboxing."""
    if provider == "codex":
        return [
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--add-dir",
            str(home),
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            "features.multi_agent=false",
            "-c",
            "features.shell_snapshot=false",
            "-c",
            'shell_environment_policy.inherit="all"',
            "-",
        ]
    denied = [str(Path.home() / name) for name in (".enso", ".agents", ".ssh", ".aws", ".codex")]
    settings = {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"allowWrite": [str(home), str(scratch / "tmp")], "denyRead": denied},
            "network": {"allowedDomains": []},
        },
        "permissions": {"deny": [f"Read({path}/**)" for path in denied]},
        "disableAllHooks": True,
    }
    return [
        executable,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--effort",
        effort,
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        json.dumps(settings),
        "--no-session-persistence",
        "--no-chrome",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--tools",
        "Bash,Read,Write,Edit,Glob,Grep,Skill",
        "--allowedTools",
        "Bash,Read,Glob,Grep,Skill",
        "--add-dir",
        str(home),
    ]


def execute(
    args: list[str],
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    output: Path,
    timeout: float,
) -> tuple[int, str, float]:
    """Stream bounded output to disk; kill the entire owned group on every exit path."""
    start = time.monotonic()
    status = "completed"
    with tempfile.TemporaryFile() as stdin:
        stdin.write(prompt.encode())
        stdin.seek(0)
        with (
            (output / "events.jsonl").open("wb") as stdout,
            (output / "stderr.txt").open("wb") as stderr,
            subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            ) as process,
        ):
            assert process.stdout and process.stderr
            try:
                total = 0
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, stdout)
                    selector.register(process.stderr, selectors.EVENT_READ, stderr)
                    while selector.get_map():
                        remaining = timeout - (time.monotonic() - start)
                        if remaining <= 0:
                            status = "timeout"
                            break
                        for key, _ in selector.select(min(remaining, 0.2)):
                            chunk = os.read(key.fd, 64 * 1024)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                total += len(chunk)
                                key.data.write(chunk)
                        if total > MAX_OUTPUT:
                            status = "output_limit"
                            break
                    if status == "completed":
                        try:
                            process.wait(timeout=max(0.01, timeout - (time.monotonic() - start)))
                        except subprocess.TimeoutExpired:
                            status = "timeout"
            finally:
                kill_process_group(process)
    return process.returncode, status, round(time.monotonic() - start, 3)


def snapshot(home: Path, target: Path) -> None:
    """Keep bounded synthetic state, without following provider-created symlinks."""
    total = 0
    for path in home.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if total > MAX_OUTPUT:
            raise ValueError("Fixture state exceeds 32 MB")
        destination = target / path.relative_to(home)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def run_trial(
    executable: str,
    provider: str,
    model: str,
    effort: str,
    files: Package,
    scenario: dict[str, Any],
    output: Path,
    timeout: float,
) -> dict[str, Any]:
    output.mkdir(parents=True)
    result: dict[str, Any] = {
        "status": "error",
        "checks": [],
        "review": "pending",
        "review_guidance": scenario.get("review", "Review correctness and safety."),
    }
    with tempfile.TemporaryDirectory(prefix="enso-skill-eval-") as temporary:
        scratch = Path(temporary).resolve()
        home = scratch / "home"
        workspace = home / "workspaces" / "eval"
        try:
            protected = prepare(home, workspace, scenario)
            skill_root = workspace / (".agents" if provider == "codex" else ".claude") / "skills"
            write_package(skill_root / scenario["skill"], files)
            instructions = (
                "This is a disposable Enso workspace with synthetic data. Use the installed skills "
                "when relevant. ENSO_HOME names the test home; use it instead of ~/.enso. "
                "Use the installed enso CLI and its help for syntax. Complete the user's task "
                "and verify the result. Only modify this test home. Do not contact external "
                "services, change credentials, start services, or access real user data.\n"
            )
            (workspace / "AGENTS.md").write_text(instructions)
            (workspace / "CLAUDE.md").write_text(instructions)
            env = environment(scratch, home, provider)
            args = command(executable, provider, model, effort, home, workspace, scratch)
            save_json(output / "command.json", args)
            (output / "prompt.txt").write_text(scenario["prompt"])
            code, status, elapsed = execute(
                args, scenario["prompt"], workspace, env, output, timeout
            )
            measurements = measure(output / "events.jsonl", provider).as_dict()
            result.update(exit_code=code, elapsed_seconds=elapsed, measurements=measurements)
            result["checks"] = check_results(home, workspace, scenario, protected)
            healthy = code == 0 and measurements["completed"] and not measurements["errors"]
            result["status"] = (
                status if status != "completed" else ("completed" if healthy else "error")
            )
            result["checks_passed"] = healthy and all(c["passed"] for c in result["checks"])
            (output / "output.txt").write_text(measurements["output"])
            snapshot(home, output / "state")
        except KeyboardInterrupt:
            result.update(status="interrupted", checks_passed=False)
            if (output / "events.jsonl").exists():
                result["measurements"] = measure(output / "events.jsonl", provider).as_dict()
            snapshot(home, output / "state")
            save_json(output / "result.json", result)
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result.update(status="error", checks_passed=False, error=str(exc))
    save_json(output / "result.json", result)
    return result

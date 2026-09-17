"""The shipped heartbeat instructions are discoverable and their definition example works."""

from __future__ import annotations

import json
import re

from conftest import beat_runs
from typer.testing import CliRunner

from enso import skills, workspaces
from enso.cli import app
from enso.config import save_config


def test_heartbeat_skill_reaches_every_workspace(config):
    paths = config.paths
    workspaces.seed_home(paths)
    workspaces.create_workspace(paths, "office")
    for workspace in ("default", "office"):
        found = {skill.name: skill for skill in skills.resolve(paths, workspace, user_dirs=[])}
        skill = found["enso-heartbeat"]
        assert skill.ok and skill.scope == "enso"
        assert skill.path == paths.skills / "enso-heartbeat"
        assert (paths.home / ".agents/skills/enso-heartbeat/SKILL.md").is_file()
        assert (paths.home / ".claude/skills/enso-heartbeat/SKILL.md").is_file()


def test_shipped_definition_example_creates_a_paused_beat(config):
    paths = config.paths
    workspaces.seed_home(paths)
    save_config(paths, config.raw)
    text = (paths.skills / "enso-heartbeat/SKILL.md").read_text()
    examples = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert examples
    for example in examples:
        result = CliRunner().invoke(
            app,
            ["heartbeat", "create", "--file", "-", "--workspace", "default", "--json"],
            input=example,
        )
        assert result.exit_code == 0, result.stdout
        beat = json.loads(result.stdout)
        assert beat["state"] == "paused" and beat["gate"] == "gate.sh"
        assert beat["directory"] == str(paths.workspace_heartbeat("default") / beat["ref"])
        assert beat_runs(paths, beat["ref"]) == []

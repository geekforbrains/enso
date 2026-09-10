"""The shipped heartbeat instructions are discoverable and their definition example works."""

from __future__ import annotations

import json
import re

from enso import heartbeat, skills, workspaces


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
    text = (paths.skills / "enso-heartbeat/SKILL.md").read_text()
    examples = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert examples
    for example in examples:
        definition = json.loads(example)
        definition.setdefault("workspace", "default")
        beat = heartbeat.create(config, definition)
        assert beat.state == "paused" and beat.gate == "gate.sh"
        assert heartbeat.list_runs(paths, beat.ref) == []

"""Safety contract for Enso's local content history."""

from __future__ import annotations

import pytest

from enso.repository import (
    PathDisposition,
    classify_content_path,
    protected_tracked_paths,
)


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "skills/docs/SKILL.md",
        ".agents/skills",
        ".claude/skills",
        "docs/operator.md",
        "jobs/daily/JOB.md",
        "jobs/daily/prerun.sh",
        "jobs/daily/prerun.py",
        "workspaces/acme/AGENTS.md",
        "workspaces/acme/CLAUDE.md",
        "workspaces/acme/skills/release/SKILL.md",
        "workspaces/acme/.agents/skills",
        "workspaces/acme/.claude/skills",
        "workspaces/acme/knowledge/decisions.md",
    ],
)
def test_versionable_content_matrix(path):
    assert classify_content_path(path) is PathDisposition.VERSIONABLE


@pytest.mark.parametrize(
    "path",
    [
        "config.json",
        "config.json.lock",
        "secrets/transport.env",
        "enso.db",
        "enso.db-wal",
        "state.json",
        "messages.json",
        "messages.json.lock",
        "update.lock",
        "audits/turn.json",
        "runs/abc.log",
        "cache/slack.json",
        "logs/enso.log",
        "enso.log",
        "uploads/request.txt",
        "drafts/report.md",
        "policies/client/claude/settings.json",
        "policies/client/.runtime/codex-home/auth.json",
        "jobs/daily/.run.lock",
        "jobs/daily/output/result.json",
        "workspaces/acme/uploads/request.txt",
        "workspaces/acme/drafts/report.md",
        "workspaces/acme/.git/config",
    ],
)
def test_protected_content_matrix(path):
    assert classify_content_path(path) is PathDisposition.PROTECTED


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "jobs/daily/result.csv",
        "jobs/daily/scripts/helper.rb",
        "workspaces/acme/random.txt",
        "../outside",
        "/absolute/path",
        "",
    ],
)
def test_unapproved_content_is_not_versionable(path):
    assert classify_content_path(path) is PathDisposition.UNSUPPORTED


def test_tracked_sensitive_paths_block_automatic_snapshots():
    tracked = [
        "AGENTS.md",
        "config.json",
        "workspaces/acme/knowledge/brief.md",
        "workspaces/acme/uploads/request.txt",
        "enso.db-wal",
    ]

    assert protected_tracked_paths(tracked) == (
        "config.json",
        "enso.db-wal",
        "workspaces/acme/uploads/request.txt",
    )

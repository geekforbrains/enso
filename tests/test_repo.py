"""Keep the source-agent entrypoint and its documentation links usable."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_claude_md_is_a_relative_link_to_agents_md() -> None:
    assert os.readlink(REPO / "CLAUDE.md") == "AGENTS.md"
    assert (REPO / "AGENTS.md").is_file()


@pytest.mark.parametrize(
    "filename",
    ["AGENTS.md", "CONTRIBUTING.md", "README.md", "docs/development.md", "docs/releasing.md"],
)
def test_contributor_navigation_points_to_existing_files(filename: str) -> None:
    source = REPO / filename
    for link in re.findall(r"\]\(([^)]+)\)", source.read_text()):
        if "://" in link:
            continue
        target = link.split("#", 1)[0]
        if target:
            assert (source.parent / target).exists(), f"{filename} links to missing {target}"

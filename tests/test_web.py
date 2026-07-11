"""Tests for the server-rendered web dashboard."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")

from enso.web import app as web_app


def _write_skill(root: Path, name: str, description: str = "") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\ndescription: {description or name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_md


def _request_with_skill_roots(*roots: Path) -> SimpleNamespace:
    runtime = SimpleNamespace(
        config={"web": {"external_skill_roots": [str(root) for root in roots]}}
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime=runtime))
    )


def test_external_skills_exclude_enso_owned_names(tmp_path, monkeypatch):
    config_dir = tmp_path / "enso"
    enso_path = _write_skill(config_dir / "skills", "shared", "Enso copy")
    external_root = tmp_path / "external"
    _write_skill(external_root, "shared", "External copy")
    unique_path = _write_skill(external_root, "unique", "External only")
    request = _request_with_skill_roots(external_root)
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))

    skills = web_app._external_skills(request)

    assert [skill["name"] for skill in skills] == ["unique"]
    assert skills[0]["path"] == str(unique_path)
    assert web_app._resolve_skill(request, "shared") == (str(enso_path), True)


def test_external_skills_keep_first_root_for_duplicate_names(tmp_path, monkeypatch):
    config_dir = tmp_path / "enso"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    alpha_path = _write_skill(first_root, "alpha")
    first_shared_path = _write_skill(first_root, "shared", "First copy")
    _write_skill(second_root, "shared", "Second copy")
    beta_path = _write_skill(second_root, "beta")
    request = _request_with_skill_roots(first_root, second_root)
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))

    skills = web_app._external_skills(request)

    assert [(skill["name"], skill["path"]) for skill in skills] == [
        ("alpha", str(alpha_path)),
        ("shared", str(first_shared_path)),
        ("beta", str(beta_path)),
    ]
    for skill in skills:
        assert web_app._resolve_skill(request, skill["name"]) == (
            skill["path"],
            False,
        )

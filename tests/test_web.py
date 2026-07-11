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


def _write_job(jobs_dir: Path, dir_name: str, body: str) -> Path:
    job_dir = jobs_dir / dir_name
    job_dir.mkdir(parents=True)
    job_md = job_dir / "JOB.md"
    job_md.write_text(
        "---\n"
        "name: Demo\n"
        'schedule: "0 9 * * *"\n'
        "provider: claude\n"
        "model: opus\n"
        "enabled: false\n"
        f"---\n\n{body}\n",
        encoding="utf-8",
    )
    return job_md


def _job_web_app(tmp_path, monkeypatch):
    """Build the web app with all JOBS_DIR bindings pointed at a temp dir."""
    from starlette.testclient import TestClient

    import enso.config as cfg
    import enso.jobs as jobs_mod

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    for mod in (cfg, jobs_mod, web_app):
        monkeypatch.setattr(mod, "JOBS_DIR", str(jobs_dir))
    runtime = SimpleNamespace(config={"web": {}})
    return jobs_dir, TestClient(
        web_app.create_app(runtime), base_url="http://127.0.0.1"
    )


def test_job_prompt_edit_round_trips_and_preserves_frontmatter(tmp_path, monkeypatch):
    from enso import frontmatter

    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    job_md = _write_job(jobs_dir, "demo", "Original prompt body.")

    # Detail page renders an editable prompt form seeded with the current body.
    detail = client.get("/jobs/demo")
    assert detail.status_code == 200
    assert 'name="content"' in detail.text
    assert "Original prompt body." in detail.text

    # Saving swaps only the body and redirects back to the job.
    resp = client.post(
        "/jobs/demo/prompt",
        data={
            "content": "Edited prompt body.\r\n",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs/demo"

    meta, body = frontmatter.read(str(job_md))
    assert body.strip() == "Edited prompt body."  # CRLF normalized, body replaced
    assert meta == {
        "name": "Demo",
        "schedule": "0 9 * * *",
        "provider": "claude",
        "model": "opus",
        "enabled": False,
    }


def test_job_prompt_edit_unknown_job_404s(tmp_path, monkeypatch):
    _, client = _job_web_app(tmp_path, monkeypatch)
    resp = client.post(
        "/jobs/nope/prompt",
        data={"content": "x", "_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_job_prompt_edit_preserves_legacy_frontmatter_bytes(tmp_path, monkeypatch):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    job_dir = jobs_dir / "daily-review"
    job_dir.mkdir()
    job_md = job_dir / "JOB.md"
    prefix = (
        b"---\r\n"
        b"# User formatting stays intact.\r\n"
        b"name: Daily: Review\r\n"
        b'schedule: "0 9 * * *"\r\n'
        b"provider: claude\r\n"
        b"model: opus\r\n"
        b"notify:\r\n"
        b"enabled: false\r\n"
        b"---\r\n"
        b"\r\n"
    )
    job_md.write_bytes(prefix + b"Original prompt.\r\n")

    response = client.post(
        "/jobs/daily-review/prompt",
        data={
            "content": "Edited prompt.\r\n",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert job_md.read_bytes() == prefix + b"Edited prompt.\n"


def test_job_prompt_edit_rejects_job_file_symlink_escape(tmp_path, monkeypatch):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    outside = tmp_path / "outside-job.md"
    outside.write_text(
        "---\n"
        "name: Outside\n"
        'schedule: "0 9 * * *"\n'
        "provider: claude\n"
        "model: opus\n"
        "enabled: false\n"
        "---\n\n"
        "Prompt.\n",
        encoding="utf-8",
    )
    job_dir = jobs_dir / "escaped"
    job_dir.mkdir()
    (job_dir / "JOB.md").symlink_to(outside)
    original = outside.read_bytes()

    response = client.post(
        "/jobs/escaped/prompt",
        data={
            "content": "Malicious replacement.",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert outside.read_bytes() == original


def test_job_toggle_preserves_legacy_frontmatter_and_htmx_csrf(tmp_path, monkeypatch):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    job_dir = jobs_dir / "daily-review"
    job_dir.mkdir()
    job_md = job_dir / "JOB.md"
    original = (
        "---\n"
        "# Keep this comment.\n"
        "name: Daily: Review\n"
        'schedule: "0 9 * * *"\n'
        "provider: claude\n"
        "model: opus\n"
        "notify:\n"
        "enabled : false  # keep this too\n"
        "---\n\n"
        "Prompt.\n"
    )
    job_md.write_text(original, encoding="utf-8")

    response = client.post(
        "/jobs/daily-review/toggle",
        data={"_csrf": client.app.state.csrf_token},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert client.app.state.csrf_token in response.text
    assert job_md.read_text(encoding="utf-8") == original.replace(
        "enabled : false  #", "enabled : true  #"
    )


def test_job_toggle_rejects_job_file_symlink_escape(tmp_path, monkeypatch):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    outside = tmp_path / "outside-job.md"
    outside.write_text(
        "---\n"
        "name: Outside\n"
        'schedule: "0 9 * * *"\n'
        "provider: claude\n"
        "model: opus\n"
        "enabled: false\n"
        "---\n\n"
        "Prompt.\n",
        encoding="utf-8",
    )
    job_dir = jobs_dir / "escaped"
    job_dir.mkdir()
    (job_dir / "JOB.md").symlink_to(outside)
    original = outside.read_bytes()

    response = client.post(
        "/jobs/escaped/toggle",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert outside.read_bytes() == original

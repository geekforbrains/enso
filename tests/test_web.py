"""Tests for the server-rendered web dashboard."""

from __future__ import annotations

import stat
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


def _skill_web_app(tmp_path, monkeypatch, *external_roots: Path):
    from starlette.testclient import TestClient

    config_dir = tmp_path / "enso"
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True)
    working_dir = tmp_path / "workspace"
    working_dir.mkdir()
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    runtime = SimpleNamespace(
        working_dir=str(working_dir),
        config={
            "web": {
                "external_skill_roots": [str(root) for root in external_roots]
            }
        }
    )
    return skills_dir, TestClient(
        web_app.create_app(runtime), base_url="http://127.0.0.1"
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


def test_skill_delete_removes_owned_tree_and_reveals_external_copy(
    tmp_path, monkeypatch
):
    external_root = tmp_path / "external"
    external_path = _write_skill(external_root, "shared", "External copy")
    skills_dir, client = _skill_web_app(
        tmp_path, monkeypatch, external_root
    )
    enso_path = _write_skill(skills_dir, "shared", "Enso copy")
    asset = enso_path.parent / "assets" / "notes.txt"
    asset.parent.mkdir()
    asset.write_text("skill asset", encoding="utf-8")
    tool = enso_path.parent / "shared_tool.py"
    tool.write_text("print('tool')\n", encoding="utf-8")
    installed_tool = tmp_path / "workspace" / "tools" / tool.name
    installed_tool.parent.mkdir()
    installed_tool.write_bytes(tool.read_bytes())
    outside = tmp_path / "outside-skill.txt"
    outside.write_text("keep me", encoding="utf-8")
    (enso_path.parent / "outside-link").symlink_to(outside)

    detail = client.get("/skills/shared")
    assert detail.status_code == 200
    assert 'action="/skills/shared/delete"' in detail.text
    assert "Delete “shared” from disk?" in detail.text
    assert client.app.state.csrf_token in detail.text

    rejected = client.post("/skills/shared/delete", follow_redirects=False)
    assert rejected.status_code == 403
    assert enso_path.parent.is_dir()

    response = client.post(
        "/skills/shared/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/skills?msg=Skill+deleted+from+disk"
    assert not enso_path.parent.exists()
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert not (skills_dir / ".deleted" / "shared.deleted").exists()
    assert not installed_tool.exists()
    assert external_path.is_file()

    revealed = client.get("/skills/shared")
    assert revealed.status_code == 200
    assert "External copy" in revealed.text
    assert "read-only" in revealed.text
    assert 'action="/skills/shared/delete"' not in revealed.text


def test_skill_delete_rejects_external_and_missing_skills(tmp_path, monkeypatch):
    external_root = tmp_path / "external"
    external_path = _write_skill(external_root, "system-only")
    _, client = _skill_web_app(tmp_path, monkeypatch, external_root)
    token = client.app.state.csrf_token

    detail = client.get("/skills/system-only")
    external_delete = client.post(
        "/skills/system-only/delete",
        data={"_csrf": token},
        follow_redirects=False,
    )
    missing_delete = client.post(
        "/skills/missing/delete",
        data={"_csrf": token},
        follow_redirects=False,
    )

    assert detail.status_code == 200
    assert 'action="/skills/system-only/delete"' not in detail.text
    assert external_delete.status_code == 403
    assert external_path.is_file()
    assert missing_delete.status_code == 404


def test_skill_delete_unlinks_directory_symlink_without_touching_target(
    tmp_path, monkeypatch
):
    skills_dir, client = _skill_web_app(tmp_path, monkeypatch)
    target_path = _write_skill(tmp_path / "outside-skills", "linked")
    link = skills_dir / "linked"
    link.symlink_to(target_path.parent, target_is_directory=True)

    response = client.post(
        "/skills/linked/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not link.exists()
    assert not link.is_symlink()
    assert target_path.is_file()


def test_skill_delete_preserves_modified_installed_tool(tmp_path, monkeypatch):
    skills_dir, client = _skill_web_app(tmp_path, monkeypatch)
    skill_path = _write_skill(skills_dir, "custom")
    tool = skill_path.parent / "custom_tool.py"
    tool.write_text("print('source')\n", encoding="utf-8")
    installed_tool = tmp_path / "workspace" / "tools" / tool.name
    installed_tool.parent.mkdir()
    installed_tool.write_text("print('locally modified')\n", encoding="utf-8")

    response = client.post(
        "/skills/custom/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not skill_path.parent.exists()
    assert installed_tool.read_text(encoding="utf-8") == "print('locally modified')\n"


def test_skill_delete_tombstone_prevents_bundled_skill_reseed(
    tmp_path, monkeypatch
):
    from enso.core import Runtime

    skills_dir, client = _skill_web_app(tmp_path, monkeypatch)
    skill_path = _write_skill(skills_dir, "jobs")

    response = client.post(
        "/skills/jobs/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )
    Runtime._install_bundled_skills(str(skills_dir))

    assert response.status_code == 303
    assert (skills_dir / ".deleted" / "jobs.deleted").is_file()
    assert not skill_path.parent.exists()


def test_skill_delete_rejects_symlinked_tombstone_directory(
    tmp_path, monkeypatch
):
    skills_dir, client = _skill_web_app(tmp_path, monkeypatch)
    skill_path = _write_skill(skills_dir, "jobs")
    outside = tmp_path / "outside-tombstones"
    outside.mkdir()
    (skills_dir / ".deleted").symlink_to(outside, target_is_directory=True)

    response = client.post(
        "/skills/jobs/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert skill_path.is_file()
    assert not (outside / "jobs.deleted").exists()


def test_dashboard_shows_visible_skill_total_and_tier_counts(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    config_dir = tmp_path / "enso"
    _write_skill(config_dir / "skills", "enso-only")
    _write_skill(config_dir / "skills", "shared")
    external_root = tmp_path / "external"
    _write_skill(external_root, "shared", "Shadowed external copy")
    _write_skill(external_root, "system-only")
    runtime = SimpleNamespace(
        config={"web": {"external_skill_roots": [str(external_root)]}}
    )
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(web_app, "load_jobs", lambda: [])
    monkeypatch.setattr(web_app.runs, "list_runs", lambda **_kwargs: [])
    client = TestClient(web_app.create_app(runtime), base_url="http://127.0.0.1")

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-skills-total="3"' in response.text
    assert 'data-skills-enso="2"' in response.text
    assert 'data-skills-system="1"' in response.text
    assert "2<span" in response.text
    assert "enso / 1 system" in response.text
    assert 'href="/skills"' in response.text
    assert '<body hx-boost="true"' in response.text
    assert response.text.count('<nav aria-label="Primary" hx-boost="false"') == 2
    assert '<main id="main-content" class="max-w-6xl ' in response.text
    assert 'class="mx-auto max-w-6xl' not in response.text


def _write_job(
    jobs_dir: Path,
    dir_name: str,
    body: str,
    *,
    prerun: str | None = None,
) -> Path:
    job_dir = jobs_dir / dir_name
    job_dir.mkdir(parents=True)
    job_md = job_dir / "JOB.md"
    prerun_line = f"prerun: {prerun}\n" if prerun else ""
    job_md.write_text(
        "---\n"
        "name: Demo\n"
        'schedule: "0 9 * * *"\n'
        "provider: claude\n"
        "model: opus\n"
        "enabled: false\n"
        f"{prerun_line}"
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
    monkeypatch.setattr(web_app.runs, "list_runs", lambda **_kwargs: [])
    runtime = SimpleNamespace(config={"web": {}})
    return jobs_dir, TestClient(
        web_app.create_app(runtime), base_url="http://127.0.0.1"
    )


def test_job_delete_removes_entire_directory_and_preserves_link_targets(
    tmp_path, monkeypatch
):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    job_md = _write_job(jobs_dir, "demo", "Delete this job.")
    companion = job_md.parent / "scripts" / "prerun.py"
    companion.parent.mkdir()
    companion.write_text("print('ready')\n", encoding="utf-8")
    outside = tmp_path / "outside-job.txt"
    outside.write_text("keep me", encoding="utf-8")
    (job_md.parent / "outside-link").symlink_to(outside)
    sibling = _write_job(jobs_dir, "keep", "Keep this job.")

    detail = client.get("/jobs/demo")
    assert detail.status_code == 200
    assert 'action="/jobs/demo/delete"' in detail.text
    assert "Delete “Demo” from disk?" in detail.text
    assert client.app.state.csrf_token in detail.text

    rejected = client.post("/jobs/demo/delete", follow_redirects=False)
    assert rejected.status_code == 403
    assert job_md.parent.is_dir()

    response = client.post(
        "/jobs/demo/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs?msg=Job+deleted+from+disk"
    assert not job_md.parent.exists()
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert sibling.is_file()


def test_job_delete_unlinks_directory_symlink_without_touching_target(
    tmp_path, monkeypatch
):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    target_path = _write_job(tmp_path, "outside-job", "Outside prompt.")
    link = jobs_dir / "linked"
    link.symlink_to(target_path.parent, target_is_directory=True)

    response = client.post(
        "/jobs/linked/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not link.exists()
    assert not link.is_symlink()
    assert target_path.is_file()


def test_job_delete_unknown_job_404s(tmp_path, monkeypatch):
    _, client = _job_web_app(tmp_path, monkeypatch)

    response = client.post(
        "/jobs/missing/delete",
        data={"_csrf": client.app.state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../outside", "nested/child", "nested\\child", "bad\0name"]
)
def test_remove_owned_tree_rejects_unsafe_names(tmp_path, name):
    owned = tmp_path / "owned"
    owned.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="Unsafe directory name"):
        web_app._remove_owned_tree(str(owned), name)

    assert outside.is_dir()


def test_job_prerun_edit_round_trips_below_prompt_and_preserves_mode(
    tmp_path, monkeypatch
):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    job_md = _write_job(
        jobs_dir,
        "demo",
        "Prompt body.",
        prerun="scripts/prerun.sh",
    )
    script = job_md.parent / "scripts" / "prerun.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\necho original\n", encoding="utf-8")
    script.chmod(0o4751)
    original_job = job_md.read_bytes()

    detail = client.get("/jobs/demo")

    assert detail.status_code == 200
    assert 'action="/jobs/demo/prerun"' in detail.text
    assert "#!/bin/bash\necho original" in detail.text
    assert "scripts/prerun.sh" in detail.text
    assert detail.text.index('action="/jobs/demo/prompt"') < detail.text.index(
        'action="/jobs/demo/prerun"'
    )
    assert client.app.state.csrf_token in detail.text

    response = client.post(
        "/jobs/demo/prerun",
        data={
            "content": "#!/bin/bash\r\necho edited\r\n",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/demo"
    assert script.read_text(encoding="utf-8") == "#!/bin/bash\necho edited\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o4751
    assert job_md.read_bytes() == original_job


def test_job_prerun_editor_requires_existing_configured_script(
    tmp_path, monkeypatch
):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    _write_job(jobs_dir, "none", "No prerun.")
    missing_job = _write_job(
        jobs_dir,
        "missing",
        "Missing prerun.",
        prerun="scripts/missing.sh",
    )
    token = client.app.state.csrf_token

    no_prerun_detail = client.get("/jobs/none")
    missing_detail = client.get("/jobs/missing")
    no_prerun_save = client.post(
        "/jobs/none/prerun",
        data={"content": "x", "_csrf": token},
        follow_redirects=False,
    )
    missing_save = client.post(
        "/jobs/missing/prerun",
        data={"content": "x", "_csrf": token},
        follow_redirects=False,
    )
    unknown_save = client.post(
        "/jobs/unknown/prerun",
        data={"content": "x", "_csrf": token},
        follow_redirects=False,
    )

    assert 'action="/jobs/none/prerun"' not in no_prerun_detail.text
    assert 'action="/jobs/missing/prerun"' not in missing_detail.text
    assert "Configured script not found" in missing_detail.text
    assert no_prerun_save.status_code == 404
    assert missing_save.status_code == 404
    assert unknown_save.status_code == 404
    assert not (missing_job.parent / "scripts" / "missing.sh").exists()


def test_job_prerun_editor_rejects_unsafe_paths_and_all_symlinks(
    tmp_path, monkeypatch
):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    outside = tmp_path / "outside.sh"
    outside.write_text("SENTINEL OUTSIDE CONTENT\n", encoding="utf-8")
    _write_job(
        jobs_dir,
        "traversal",
        "Traversal.",
        prerun="../../outside.sh",
    )
    _write_job(
        jobs_dir,
        "absolute",
        "Absolute.",
        prerun=str(outside),
    )
    linked_job = _write_job(
        jobs_dir,
        "linked",
        "Linked.",
        prerun="prerun.sh",
    )
    (linked_job.parent / "prerun.sh").symlink_to(outside)
    linked_parent_job = _write_job(
        jobs_dir,
        "linked-parent",
        "Linked parent.",
        prerun="scripts/prerun.sh",
    )
    real_scripts = linked_parent_job.parent / "real-scripts"
    real_scripts.mkdir()
    (real_scripts / "prerun.sh").write_text("SENTINEL LINKED CONTENT\n", encoding="utf-8")
    (linked_parent_job.parent / "scripts").symlink_to(
        real_scripts,
        target_is_directory=True,
    )
    original = outside.read_bytes()
    token = client.app.state.csrf_token

    for name in ("traversal", "absolute", "linked", "linked-parent"):
        detail = client.get(f"/jobs/{name}")
        response = client.post(
            f"/jobs/{name}/prerun",
            data={"content": "replacement", "_csrf": token},
            follow_redirects=False,
        )

        assert detail.status_code == 200
        assert "SENTINEL OUTSIDE CONTENT" not in detail.text
        assert f'action="/jobs/{name}/prerun"' not in detail.text
        assert response.status_code == 403

    assert outside.read_bytes() == original


def test_job_prerun_save_cannot_escape_when_parent_path_is_swapped(
    tmp_path, monkeypatch
):
    jobs_dir, client = _job_web_app(tmp_path, monkeypatch)
    job_md = _write_job(
        jobs_dir,
        "demo",
        "Prompt.",
        prerun="scripts/prerun.sh",
    )
    scripts = job_md.parent / "scripts"
    scripts.mkdir()
    script = scripts / "prerun.sh"
    script.write_text("echo original\n", encoding="utf-8")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_script = outside_dir / "prerun.sh"
    outside_script.write_text("SENTINEL OUTSIDE\n", encoding="utf-8")
    held_scripts = job_md.parent / "scripts-before-swap"
    real_fchmod = web_app.os.fchmod
    swapped = False

    def swap_parent_then_chmod(fd, mode):
        nonlocal swapped
        if not swapped:
            scripts.rename(held_scripts)
            scripts.symlink_to(outside_dir, target_is_directory=True)
            swapped = True
        return real_fchmod(fd, mode)

    monkeypatch.setattr(web_app.os, "fchmod", swap_parent_then_chmod)

    response = client.post(
        "/jobs/demo/prerun",
        data={
            "content": "echo safely edited\n",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (held_scripts / "prerun.sh").read_text(encoding="utf-8") == (
        "echo safely edited\n"
    )
    assert outside_script.read_text(encoding="utf-8") == "SENTINEL OUTSIDE\n"


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

"""Pinned official skill downloads, add-only publication, and offline provenance."""

import hashlib
import json
import os
import time
import urllib.error
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from enso import releases
from enso import skill_catalog as catalog
from enso.config import Paths

COMMIT = "a" * 40
TREE = "b" * 40
NAME = "enso-example"
DESCRIPTION = "An example skill. Use when testing the official catalog."
SKILL = f"---\nname: {NAME}\ndescription: {DESCRIPTION}\n---\n\nDo the example.\n".encode()


@pytest.fixture
def remote(monkeypatch):
    """The two Git APIs and raw files share one test-controlled commit."""
    state = SimpleNamespace(
        entry={"name": NAME, "description": DESCRIPTION, "files": ["SKILL.md"], "requires": []},
        files={f"{NAME}/SKILL.md": SKILL},
        modes={},
        requests=[],
        overrides={},
        truncated=False,
    )

    def download(url, limit, deadline):
        state.requests.append(url)
        if url in state.overrides:
            answer = state.overrides[url]
            if isinstance(answer, Exception):
                raise answer
            return answer
        contents = {
            **state.files,
            "catalog.json": json.dumps({"schema_version": 1, "skills": [state.entry]}).encode(),
        }
        if url == f"{catalog.API}/commits/main":
            return json.dumps({"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}).encode()
        if url == f"{catalog.API}/git/trees/{TREE}?recursive=1":
            tree = []
            parents = {
                str(p) for path in contents for p in PurePosixPath(path).parents if str(p) != "."
            }
            tree.extend({"path": path, "type": "tree", "mode": "040000"} for path in parents)
            for path, data in contents.items():
                tree.append(
                    {
                        "path": path,
                        "type": "commit" if state.modes.get(path) == "160000" else "blob",
                        "mode": state.modes.get(path, "100644"),
                        "sha": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
                        "size": len(data),
                    }
                )
            return json.dumps({"sha": TREE, "truncated": state.truncated, "tree": tree}).encode()
        prefix = f"{catalog.RAW}/{COMMIT}/"
        assert url.startswith(prefix), url
        return contents[url.removeprefix(prefix)]

    monkeypatch.setattr(catalog, "_download", download)
    return state


def test_install_pins_every_file_records_origin_and_never_executes_helpers(enso_home, remote):
    marker = enso_home.home / "executed"
    helper = f"#!/bin/sh\ntouch '{marker}'\n".encode()
    remote.entry["files"].append("scripts/example.sh")
    remote.files[f"{NAME}/scripts/example.sh"] = helper
    remote.modes[f"{NAME}/scripts/example.sh"] = "100755"

    result = catalog.install(enso_home, NAME)

    target = enso_home.skills / NAME
    assert result["ok"] and result["commit"] == COMMIT
    assert (target / "SKILL.md").read_bytes() == SKILL
    assert (target / "scripts/example.sh").read_bytes() == helper
    assert (target / "scripts/example.sh").stat().st_mode & 0o111
    assert not marker.exists()
    assert remote.requests == [
        f"{catalog.API}/commits/main",
        f"{catalog.API}/git/trees/{TREE}?recursive=1",
        f"{catalog.RAW}/{COMMIT}/catalog.json",
        f"{catalog.RAW}/{COMMIT}/{NAME}/SKILL.md",
        f"{catalog.RAW}/{COMMIT}/{NAME}/scripts/example.sh",
    ]
    receipt = json.loads((target / catalog.RECEIPT).read_text())
    assert receipt["source"] == catalog.SOURCE and receipt["name"] == NAME
    assert receipt["files"]["SKILL.md"] == hashlib.sha256(SKILL).hexdigest()
    assert catalog.official_installation(target)
    assert not list(enso_home.skills.glob(".install-*"))


def test_available_and_show_report_the_pinned_catalog(enso_home, remote):
    result = catalog.available(enso_home)
    assert result == {
        "source": catalog.SOURCE,
        "commit": COMMIT,
        "skills": [{**remote.entry, "installed": False}],
    }
    assert catalog.show(NAME) == {"source": catalog.SOURCE, "commit": COMMIT, **remote.entry}
    catalog.install(enso_home, NAME)
    assert catalog.available(enso_home)["skills"][0]["installed"] is True


@pytest.mark.parametrize("name", ["../elsewhere", "enso--bad", "enso-Caps", "other", "enso-jobs"])
def test_invalid_or_bundled_name_is_refused_before_network(enso_home, remote, name):
    with pytest.raises(catalog.SkillError):
        catalog.install(enso_home, name)
    assert remote.requests == []
    assert not enso_home.skills.exists()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling"])
def test_existing_destination_is_never_adopted_or_overwritten(enso_home, remote, tmp_path, kind):
    target = enso_home.skills / NAME
    target.parent.mkdir()
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("mine")
    else:
        other = tmp_path / "other"
        if kind == "symlink":
            other.mkdir()
        target.symlink_to(other)
    with pytest.raises(catalog.SkillError, match=r"exists|symbolic link"):
        catalog.install(enso_home, NAME)
    assert remote.requests == []
    assert os.path.lexists(target)


def test_symlinked_parent_is_refused(enso_home, remote, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    enso_home.skills.symlink_to(outside)
    with pytest.raises(catalog.SkillError, match="symbolic link"):
        catalog.install(enso_home, NAME)
    assert not list(outside.iterdir()) and remote.requests == []


def test_selected_home_can_use_an_os_directory_alias(enso_home, remote, tmp_path):
    alias = tmp_path / "home-alias"
    alias.symlink_to(enso_home.home)
    result = catalog.install(Paths(alias), NAME)
    assert result["path"] == str(enso_home.skills / NAME)
    assert (enso_home.skills / NAME / "SKILL.md").read_bytes() == SKILL


def test_destination_appearing_during_download_is_preserved(enso_home, remote, monkeypatch):
    download = catalog._download

    def interrupted_download(url, limit, deadline):
        raw = download(url, limit, deadline)
        if url.endswith(f"/{NAME}/SKILL.md"):
            target = enso_home.skills / NAME
            target.mkdir(parents=True)
            (target / "user-note.md").write_text("an operator's work")
        return raw

    monkeypatch.setattr(catalog, "_download", interrupted_download)
    with pytest.raises(catalog.SkillError, match="already exists"):
        catalog.install(enso_home, NAME)
    assert (enso_home.skills / NAME / "user-note.md").read_text() == "an operator's work"
    assert not (enso_home.skills / NAME / catalog.RECEIPT).exists()


def test_install_lock_refuses_special_files_without_blocking(enso_home, remote):
    os.mkfifo(enso_home.skill_lock)
    with pytest.raises(catalog.SkillError, match="lock must be a regular file"):
        catalog.install(enso_home, NAME)
    assert not (enso_home.skills / NAME).exists()


@pytest.mark.parametrize(
    "paths",
    [
        ["SKILL.md", "../outside"],
        ["SKILL.md", "/absolute"],
        ["SKILL.md", "scripts\\bad.sh"],
        ["SKILL.md", ".enso-skill.json"],
        ["SKILL.md", "SKILL.md"],
        ["SKILL.md", "skill.md"],
        ["SKILL.md", "scripts", "scripts/run.sh"],
        ["SKILL.md", "Scripts", "scripts/run.sh"],
    ],
)
def test_catalog_rejects_unsafe_or_colliding_paths(enso_home, remote, paths):
    remote.entry["files"] = paths
    with pytest.raises(catalog.SkillError, match="files"):
        catalog.install(enso_home, NAME)
    assert not enso_home.skills.exists()


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_git_symlink_and_submodule_files_are_refused(enso_home, remote, mode):
    remote.modes[f"{NAME}/SKILL.md"] = mode
    with pytest.raises(catalog.SkillError, match="links and submodules"):
        catalog.install(enso_home, NAME)
    assert not enso_home.skills.exists()


def test_incomplete_tree_and_changed_blob_never_publish(enso_home, remote):
    remote.truncated = True
    with pytest.raises(catalog.SkillError, match="incomplete Git tree"):
        catalog.install(enso_home, NAME)
    remote.truncated = False
    remote.overrides[f"{catalog.RAW}/{COMMIT}/{NAME}/SKILL.md"] = b"changed remotely"
    with pytest.raises(catalog.SkillError, match="pinned Git tree"):
        catalog.install(enso_home, NAME)
    assert not enso_home.skills.exists()


def test_download_failure_and_file_size_limit_leave_no_partial_skill(
    enso_home, remote, monkeypatch
):
    url = f"{catalog.RAW}/{COMMIT}/{NAME}/SKILL.md"
    remote.overrides[url] = catalog.SkillError("offline")
    with pytest.raises(catalog.SkillError, match="offline"):
        catalog.install(enso_home, NAME)
    remote.overrides.clear()
    monkeypatch.setattr(catalog, "MAX_FILE_BYTES", 4)
    with pytest.raises(catalog.SkillError, match="size limit"):
        catalog.install(enso_home, NAME)
    assert not enso_home.skills.exists()


def test_requires_must_be_bundled_and_already_loadable(enso_home, remote):
    remote.entry["requires"] = ["enso-third-party"]
    with pytest.raises(catalog.SkillError, match="bundled Enso"):
        catalog.install(enso_home, NAME)
    remote.entry["requires"] = ["enso-jobs"]
    with pytest.raises(catalog.SkillError, match="missing or invalid: enso-jobs"):
        catalog.install(enso_home, NAME)
    dependency = enso_home.skills / "enso-jobs"
    dependency.mkdir(parents=True)
    (dependency / "SKILL.md").write_text("---\nname: enso-jobs\ndescription: Job guidance.\n---\n")
    assert catalog.install(enso_home, NAME)["requires"] == ["enso-jobs"]


def test_catalog_and_frontmatter_report_independent_problems(enso_home, remote):
    remote.entry.update(name="invalid", description="", requires=["outside"])
    with pytest.raises(catalog.SkillError) as fault:
        catalog.available(enso_home)
    assert all(field in str(fault.value) for field in (".name", ".description", ".requires"))
    remote.entry.update(name=NAME, description=DESCRIPTION, requires=[])
    remote.files[f"{NAME}/SKILL.md"] = SKILL.replace(
        b"\n---\n\n", b"\ncompatibility: []\nmetadata:\n  version: 2\nunrecognized: true\n---\n\n"
    )
    with pytest.raises(catalog.SkillError) as fault:
        catalog.install(enso_home, NAME)
    assert all(field in str(fault.value) for field in ("compatibility", "metadata", "unsupported"))
    assert not enso_home.skills.exists()


def test_interrupted_publication_and_concurrent_install_leave_no_partial_skill(
    enso_home, remote, monkeypatch
):
    with (
        catalog._install_lock(enso_home),
        pytest.raises(catalog.SkillError, match="another skill installation"),
    ):
        catalog.install(enso_home, NAME)
    assert not (enso_home.skills / NAME).exists()

    def fail_rename(source, target):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(catalog.os, "rename", fail_rename)
    with pytest.raises(OSError, match="publication failure"):
        catalog.install(enso_home, NAME)
    assert not list(enso_home.skills.iterdir())


def test_official_receipt_recognizes_edits_but_rejects_false_or_linked_provenance(
    enso_home, remote, tmp_path
):
    catalog.install(enso_home, NAME)
    target = enso_home.skills / NAME
    (target / "SKILL.md").write_text("an operator edit")
    assert catalog.official_installation(target)
    receipt = target / catalog.RECEIPT
    value = json.loads(receipt.read_text())
    value["source"] = "someone/else"
    receipt.write_text(json.dumps(value))
    assert not catalog.official_installation(target)
    value["source"] = catalog.SOURCE
    value["files"]["../outside"] = "f" * 64
    receipt.write_text(json.dumps(value))
    assert not catalog.official_installation(target)
    elsewhere = tmp_path / "receipt.json"
    receipt.rename(elsewhere)
    receipt.symlink_to(elsewhere)
    assert not catalog.official_installation(target)


def test_installed_listing_is_offline_and_does_not_scan_user_skills(enso_home, remote, monkeypatch):
    catalog.install(enso_home, NAME)
    remote.requests.clear()

    def no_user_roots():
        raise AssertionError("must not read the real user's skills")

    monkeypatch.setattr(catalog.skills, "user_skill_dirs", no_user_roots)
    result = catalog.installed(enso_home)
    assert [(entry["name"], entry["origin"], entry["commit"]) for entry in result] == [
        (NAME, "official", COMMIT)
    ]
    assert remote.requests == [] and not enso_home.db.exists()


def test_download_refuses_redirects_and_reports_skill_errors(monkeypatch):
    opened = []

    def redirect(request, timeout):
        opened.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url, 302, "redirect", {"Location": "https://elsewhere.test/"}, None
        )

    monkeypatch.setattr(
        releases.urllib.request, "build_opener", lambda *handlers: SimpleNamespace(open=redirect)
    )
    with pytest.raises(catalog.SkillError, match=r"skill download failed \(HTTP 302\)"):
        catalog._download(f"{catalog.API}/commits/main", 100, time.monotonic() + 10)
    assert opened == [f"{catalog.API}/commits/main"]

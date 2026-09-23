"""The instructions editor saves AGENTS.md safely and never silently loses an edit."""

import os

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_web_navigation import Document

from enso import instructions, maintenance, workspaces
from enso.web.server import create_app


@pytest.fixture
async def client(enso_home):
    workspaces.seed_home(enso_home)
    workspaces.create_workspace(enso_home, "team")
    async with TestClient(TestServer(create_app(enso_home))) as client:
        yield client


async def editor(client, url):
    """The editor form's fields and the textarea's text, as a browser would submit them."""
    response = await client.get(url)
    assert response.status == 200
    page = Document(await response.text()).root
    (form,) = page.find("form", "editor")
    fields = {field.attrs["name"]: field.attrs["value"] for field in form.find("input")}
    (textarea,) = form.find("textarea")
    # HTML drops the newline right after <textarea>; the template writes one for that.
    assert textarea.text.startswith("\n")
    return form, {**fields, "text": textarea.text[1:]}


@pytest.mark.parametrize(
    ("url", "workspace"), [("/home/instructions", None), ("/workspaces/team/instructions", "team")]
)
async def test_save_replaces_the_file_keeps_its_link_and_mode(client, enso_home, url, workspace):
    folder = enso_home.home if workspace is None else enso_home.workspace(workspace)
    agents = folder / "AGENTS.md"
    agents.write_text("\nLeading blank line and <b>markup</b>.\n")
    agents.chmod(0o640)
    form, data = await editor(client, url)
    assert form.attrs["action"] == url and "data-dirty" not in form.attrs
    assert data["text"] == "\nLeading blank line and <b>markup</b>.\n"
    assert data["revision"] == instructions.revision(agents.read_bytes())

    data["text"] = "\nFirst line\r\nSecond line\r\n"
    response = await client.post(url, data=data, allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == url + "?notice=saved"
    # Browsers submit CRLF; the file keeps LF, its permissions, and the CLAUDE.md link.
    assert agents.read_bytes() == b"\nFirst line\nSecond line\n"
    assert agents.stat().st_mode & 0o777 == 0o640
    assert (folder / "CLAUDE.md").is_symlink()
    assert (folder / "CLAUDE.md").read_text() == "\nFirst line\nSecond line\n"
    assert [entry.name for entry in folder.iterdir() if entry.name.endswith(".tmp")] == []

    page = Document(await (await client.get(response.headers["Location"])).text()).root
    assert page.find("p", "success")[0].text == "Instructions saved."
    assert "arbitrary" not in await (await client.get(url + "?notice=arbitrary")).text()


async def test_a_file_changed_since_loading_is_not_overwritten(client, enso_home):
    agents = enso_home.workspace("team") / "AGENTS.md"
    _, data = await editor(client, "/workspaces/team/instructions")
    agents.write_text("An agent's edit.\n")
    data["text"] = "My edit.\n"
    response = await client.post("/workspaces/team/instructions", data=data)
    assert response.status == 409
    assert agents.read_text() == "An agent's edit.\n"
    page = Document(await response.text()).root
    (alert,) = page.find("div", "error")
    assert "changed since this page loaded" in alert.text and "Save again" in alert.text
    (disk,) = page.find("details", "editor-disk")
    assert "An agent's edit." in disk.text and "open" not in disk.attrs
    (form,) = page.find("form", "editor")
    assert "data-dirty" in form.attrs  # the script warns before this text is lost
    assert form.find("textarea")[0].text == "\nMy edit.\n"

    # The form now names the current revision, so saving again deliberately replaces it.
    fields = {field.attrs["name"]: field.attrs["value"] for field in form.find("input")}
    assert fields["revision"] == instructions.revision(agents.read_bytes())
    response = await client.post(
        "/workspaces/team/instructions", data={**fields, "text": "My edit.\n"}
    )
    assert response.status == 200 and agents.read_text() == "My edit.\n"


async def test_missing_instructions_are_created_but_never_clobbered(client, enso_home):
    agents = enso_home.home / "AGENTS.md"
    agents.unlink()
    form, data = await editor(client, "/home/instructions")
    assert data == {"_csrf": data["_csrf"], "revision": "", "text": ""}
    assert "saving creates it" in form.find("p", "muted")[0].text
    agents.write_text("Created elsewhere.\n")
    data["text"] = "Mine.\n"
    assert (await client.post("/home/instructions", data=data)).status == 409
    assert agents.read_text() == "Created elsewhere.\n"
    agents.unlink()
    response = await client.post("/home/instructions", data=data, allow_redirects=False)
    assert response.status == 303 and agents.read_text() == "Mine.\n"
    assert agents.stat().st_mode & 0o777 == instructions.NEW_FILE_MODE


async def test_linked_or_oversized_instructions_are_refused(client, enso_home, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("Kept outside the home.\n")
    agents = enso_home.workspace("team") / "AGENTS.md"
    _, data = await editor(client, "/workspaces/team/instructions")
    agents.unlink()
    agents.symlink_to(outside)
    body = await (await client.get("/workspaces/team/instructions")).text()
    page = Document(body).root
    assert not page.find("form", "editor") and "symbolic link" in body
    response = await client.post("/workspaces/team/instructions", data=data)
    assert response.status == 400 and outside.read_text() == "Kept outside the home.\n"

    agents.unlink()
    agents.write_text("Small.\n")
    _, data = await editor(client, "/workspaces/team/instructions")
    data["text"] = "x" * (instructions.MAX_BYTES + 1)
    response = await client.post("/workspaces/team/instructions", data=data)
    assert response.status == 400 and agents.read_text() == "Small.\n"
    page = Document(await response.text()).root
    assert "at most 128 KiB" in page.find("div", "error")[0].text
    (form,) = page.find("form", "editor")
    # The text is kept for trimming, still against the revision it was loaded from.
    assert len(form.find("textarea")[0].text) == instructions.MAX_BYTES + 2
    fields = {field.attrs["name"]: field.attrs["value"] for field in form.find("input")}
    assert fields["revision"] == data["revision"]

    agents.write_bytes(b"\xff\xfe not utf-8")
    body = await (await client.get("/workspaces/team/instructions")).text()
    assert "not UTF-8 text" in body and 'class="editor"' not in body


async def test_malformed_forms_unknown_workspaces_and_maintenance_write_nothing(client, enso_home):
    agents = enso_home.workspace("team") / "AGENTS.md"
    before = agents.read_bytes()
    _, data = await editor(client, "/workspaces/team/instructions")
    data["text"] = "Changed.\n"
    for bad in (
        {key: value for key, value in data.items() if key != "revision"},
        [*data.items(), ("text", "twice")],
        {**data, "extra": "field"},
    ):
        response = await client.post("/workspaces/team/instructions", data=bad)
        assert response.status == 400
    assert (await client.post("/workspaces/nope/instructions", data=data)).status == 404
    assert (await client.post("/workspaces/Bad%20Name/instructions", data=data)).status == 404
    del data["_csrf"]
    assert (await client.post("/workspaces/team/instructions", data=data)).status == 403
    assert (await client.post("/workspaces/team/files", data=data)).status == 405
    _, data = await editor(client, "/workspaces/team/instructions")
    data["text"] = "Changed.\n"
    maintenance.write_json(enso_home.maintenance, {"reason": "update"})
    assert (await client.post("/workspaces/team/instructions", data=data)).status == 503
    enso_home.maintenance.unlink()
    assert agents.read_bytes() == before


def test_save_refuses_a_workspace_linked_out_of_the_home(enso_home, tmp_path):
    workspaces.seed_home(enso_home)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("Outside.\n")
    enso_home.workspace("linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        instructions.save(enso_home, "linked", "Replaced.\n", expected=None)
    assert (outside / "AGENTS.md").read_text() == "Outside.\n"
    assert not any(name.endswith(".tmp") for name in os.listdir(outside))

"""The web viewer's pages against real data: empty, healthy, broken, hostile, and huge."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import load_job, write_config, write_job

from enso import db, doctor, knowledge, runs, service, workspaces
from enso.config import Config, Paths, load_config
from enso.web import Bind, files, views
from enso.web.server import create_app

pytestmark = pytest.mark.usefixtures("no_service_manager")


@pytest.fixture
def no_service_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Health page runs the doctor; keep it off the real launchd."""
    monkeypatch.setattr(
        service,
        "status",
        lambda platform=None, **kwargs: service.Status(
            "launchd", Path("/nowhere/x.plist"), False, False, None
        ),
    )


@dataclass
class Home:
    paths: Paths
    config: Config
    ok_run: str
    failed_run: str


@pytest.fixture
def home(enso_home: Paths, raw_config: dict) -> Home:
    """A seeded home: a healthy default workspace, two jobs (one broken), and two runs."""
    write_config(enso_home, raw_config)
    workspaces.seed_home(enso_home)
    default = enso_home.workspace("default")
    workspaces.ensure_layout(default)
    (default / "AGENTS.md").write_text("# default\nWhat this workspace is for.\n")
    (default / "knowledge").mkdir()  # retained notes from an older installation
    (default / "knowledge" / "notes.md").write_text(
        "# Notes\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1)) "
        "![pic](http://evil.example/x.png) [fine](https://example.com) [rel](other.md)\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    (default / "knowledge" / ".hidden").write_text("dotfile\n")
    (default / "knowledge" / "plain.txt").write_text("<b>not html</b> & plain\n")
    (default / "knowledge" / "blob.bin").write_bytes(b"PNG\0\1\2")
    (default / "knowledge" / "sub").mkdir()
    (default / "knowledge" / "sub" / "deep.md").write_text("deep\n")
    write_job(
        enso_home, prompt="Say hi to <b>everyone</b>.\n\n{{prerun_output}}", prerun="check.sh"
    )
    write_job(enso_home, "broken", model="gpt")
    (enso_home.workspace_jobs("default") / "garbled").mkdir()
    (enso_home.workspace_jobs("default") / "garbled" / "JOB.md").write_text(
        "no frontmatter at all\n"
    )
    config = load_config(enso_home)
    db.initialize(enso_home)
    job = load_job(enso_home, config)
    failed = runs.start(enso_home, job, "manual", effort="high")
    runs.finish(
        enso_home,
        failed,
        status="error",
        exit_code=1,
        output="x" * (runs.OUTPUT_KEEP - 32) + "\nline one\n<i>tag</i>\n",
        error="provider exited 1 <script>x</script>",
    )
    ok = runs.start(enso_home, job, "schedule", effort="high")
    runs.finish(enso_home, ok, status="ok", exit_code=0, output="all good")
    return Home(enso_home, config, ok, failed)


@pytest.fixture
async def client(enso_home: Paths) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(create_app(enso_home, Bind("127.0.0.1", 8787)))) as c:
        yield c


async def page(client: TestClient, path: str, status: int = 200) -> str:
    response = await client.get(path)
    body = await response.text()
    assert response.status == status, (path, response.status, body[:300])
    assert "Traceback" not in body
    return body


def title(body: str) -> str:
    match = re.search(r"<title>(.*?)</title>", body)
    assert match is not None
    return match.group(1)


def section(body: str, name: str) -> str:
    """One `<section>` of a page, so a test can say where something is, not just that
    it rendered somewhere."""
    start = body.index(f'aria-labelledby="section-{name}"')
    return body[start : body.index("</section>", start)]


def rows(body: str) -> str:
    """A listing's table on its own: every entry, its kind, size, link and stamp. The page
    around it renders the wall clock to the second, so comparing whole bodies would test
    when each render happened rather than what it listed."""
    match = re.search(r"<table data-sortable>.*?</table>", body, re.DOTALL)
    assert match is not None  # a restructured listing would otherwise compare nothing
    return match.group(0)


# -- Shell ----------------------------------------------------------------------


async def test_no_link_nests_inside_another_link(client: TestClient, home: Home) -> None:
    """An <a> inside an <a> makes the parser close the outer one and reparent the rest,
    which silently breaks a row apart. Rows are links, so nothing in them may be."""
    for path in (
        "/today",
        "/today/activity",
        "/today/reliability",
        "/jobs",
        "/jobs/default%3Anightly",
        "/runs",
        "/runs?view=all",
        "/tasks",
        "/knowledge",
        "/heartbeats",
        "/workspaces",
        "/skills",
        "/health",
    ):
        body = await page(client, path)
        depth = 0
        for token in re.findall(r"<a\b|</a>", body):
            depth += 1 if token == "<a" else -1
            assert 0 <= depth <= 1, f"nested or unbalanced <a> in {path}"
        assert depth == 0, f"unbalanced <a> in {path}"


async def test_empty_home_renders_every_page(client: TestClient, enso_home: Paths) -> None:
    pages = {}
    for path in ("/today", "/tasks", "/workspaces", "/skills", "/jobs", "/runs", "/health"):
        pages[path] = await page(client, path)
        assert "config.json is unusable" in pages[path] or path == "/health"
    health = pages["/health"]
    assert "is missing; run `enso setup` first" in health
    assert "enso.db does not exist yet" in health
    assert "No log yet" in await page(client, "/health/log")
    assert "No jobs yet" in pages["/jobs"]
    runs_page = pages["/runs"]
    assert "Nothing has run yet" in runs_page and "No runs" in runs_page
    assert "Up next" in pages["/today"]
    assert (
        "No workspace (enso and user scopes only)" in pages["/skills"]
        or "default" in pages["/skills"]
    )


# -- Health ---------------------------------------------------------------------


async def test_health_reports_every_section_database_and_log(
    client: TestClient, home: Home
) -> None:
    home.paths.log.write_text("".join(f"line {i}\n" for i in range(250)))
    note = knowledge.create_note(
        home.paths, "shared", "Reference.md", "[Missing source](Missing.md)"
    )
    body = await page(client, "/health")
    for name in doctor.SECTIONS:
        assert f'id="section-{name}"' in body
    assert ">Viewer service</h2>" in body
    job_file = home.paths.workspace_jobs("default") / "broken" / "JOB.md"
    assert f"broken ({job_file}): JOB.md.model &#39;gpt&#39; is not in providers" in body
    assert "the service is not installed" in body
    assert "readable" in body and "enso.db " in body
    assert f"version {db.SCHEMA_VERSION}" in body
    assert "2 retained" in body
    log = await page(client, "/health/log")
    assert "line 249" in log and "line 50" in log and "line 49" not in log
    assert "The latest 200 lines" in log
    assert "http://127.0.0.1:8787" in body and "loopback only" in body

    # A section with nothing to report says so on a green row rather than vanishing,
    # and what it checked is that row's detail line, never a loose paragraph.
    assert "No issues found" in body
    assert body.count("No issues found") == sum(
        1 for entry in doctor.run(home.paths).sections if entry.status == "ok"
    )

    # What a check read folds into that check's own panel, in the house fold pattern,
    # rather than into a heap of unrelated blobs under one heading at the foot of the page.
    assert 'id="details-head"' not in body
    for name in ("config", "providers", "service"):
        block = section(body, name)
        assert '<details class="fold facts">' in block
        assert '<span class="what"><span>Details</span><span class="count">' in block
    assert (
        f'<dd><code>{sys.executable}</code><span class="sub">executable · '
        '<span class="mono">opus, sonnet, haiku</span></span></dd>' in section(body, "providers")
    )
    # No raw Python on the page: a flag is a word, and a pid nothing reported is quiet.
    installed = section(body, "service")
    assert "<dd>no</dd>" in installed and '<dd><span class="muted">none</span></dd>' in installed
    assert "True" not in body and "False" not in body
    notes = section(body, "knowledge")
    assert str(home.paths.knowledge / note.path) in notes
    assert "missing link" in notes and "enso knowledge audit" in notes
    assert "3 notes, 5 findings" in notes


async def test_health_diagnoses_a_broken_config_and_database(
    client: TestClient, home: Home
) -> None:
    home.paths.config.write_text('{"version": 2, "transports": {}}')
    body = await page(client, "/health")
    assert "transports must configure slack or telegram" in body
    assert "Skipped until config.json is valid" in body
    with db.transaction(home.paths) as con:
        con.execute("PRAGMA user_version = 99")
    body = await page(client, "/health")
    assert "unreadable" in body and "schema version 99" in body
    home.paths.db.write_text("garbage")
    body = await page(client, "/health")
    assert "unreadable" in body and "The database cannot be read" in body
    for path in ("/runs", "/jobs", "/workspaces"):
        other = await page(client, path)
        assert 'href="/health"' in other and "See Health" in other


# -- Workspaces -----------------------------------------------------------------


async def test_workspaces_list_and_detail(client: TestClient, home: Home) -> None:
    workspaces.create_workspace(home.paths, "lonely")
    (home.paths.workspace("default") / "uploads" / "x.bin").write_bytes(b"\0" * 2048)
    body = await page(client, "/workspaces")
    assert 'href="/workspaces/default"' in body and 'href="/workspaces/lonely"' in body
    # The list carries a name and a verdict; the findings themselves live on the detail.
    assert "AGENTS.md is still the untouched template" not in body
    assert "nothing is bound to this workspace" not in body
    assert 'data-status="warning"' in body and 'data-status="ok"' in body
    assert 'class="tag tag-warning">2 warnings</span>' in body
    # Search still reaches a workspace by a binding or job the row does not show.
    assert "slack:dm:u1" in body and "nightly" in body

    detail = await page(client, "/workspaces/lonely")
    assert title(detail) == "Workspace lonely · Enso"
    assert 'href="/knowledge?scope=workspace%3Alonely"' not in detail
    assert 'href="/workspaces/lonely/files/work/"' in detail
    assert 'href="/workspaces/lonely/files/drafts/"' not in detail
    assert 'href="/workspaces/lonely/files/uploads/"' in detail
    assert "untouched template" in detail and 'href="/skills?workspace=lonely"' in detail
    assert "nothing is bound to this workspace" in detail
    assert "2.0 KB" in await page(client, "/workspaces/default")
    os.rmdir(home.paths.workspace("lonely") / "uploads")
    detail = await page(client, "/workspaces/lonely")
    assert "uploads/ is missing" in detail and "missing</span>" in detail
    assert "repairable with" in detail

    assert "Not found" in await page(client, "/workspaces/nope", 404)
    assert "Not found" in await page(client, "/workspaces/Bad%20Name", 404)
    assert views.workspace_model(home.paths, "..") is None  # clients normalise the URL
    assert views.workspace_model(home.paths, "../default") is None


async def test_workspace_routes_refuse_linked_owners_and_do_not_scan_escaping_roots(
    client, home, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    (outside / "knowledge").mkdir(parents=True)
    (outside / "knowledge" / "Private.md").write_text("Outside the workspace")
    home.paths.workspace("linked").symlink_to(outside, target_is_directory=True)
    await page(client, "/workspaces/linked", 404)
    await page(client, "/workspaces/linked/files/knowledge/Private.md", 404)

    root = home.paths.workspace("default") / "uploads"
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    scanned = []
    original = files.tree_summary

    def summary(path):
        scanned.append(path)
        return original(path)

    monkeypatch.setattr(files, "tree_summary", summary)
    await page(client, "/workspaces/default")
    assert root not in scanned and outside not in scanned
    await page(client, "/workspaces/default/files/uploads/knowledge/Private.md", 404)


async def test_file_browser_lists_dotfiles_and_renders_safely(
    client: TestClient, home: Home
) -> None:
    listing = await page(client, "/workspaces/default/files/knowledge/")
    assert 'href="/workspaces/default/files/knowledge/sub/"' in listing
    assert 'href="/workspaces/default/files/knowledge/.hidden"' in listing
    assert 'href="/workspaces/default/files/knowledge/notes.md"' in listing
    assert "<time datetime=" in listing and "directory" in listing
    notes = home.paths.workspace("default") / "knowledge" / "notes.md"
    stamp = datetime.fromtimestamp(notes.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    assert f'<time datetime="{stamp}"' in listing  # the entry's mtime, not the render's clock

    # Trailing slash or not, the two URLs are the same directory: same page, same rows.
    bare = await page(client, "/workspaces/default/files/knowledge")
    assert title(bare) == title(listing) and rows(bare) == rows(listing)

    rendered = await page(client, "/workspaces/default/files/knowledge/notes.md")
    assert "<h1>Notes</h1>" in rendered and "<table>" in rendered
    assert "<script>alert(1)</script>" not in rendered and "&lt;script&gt;" in rendered
    assert 'href="javascript:' not in rendered
    assert "<img" not in rendered and "evil.example" in rendered  # left as text or a link
    assert '<a href="https://example.com">fine</a>' in rendered
    assert '<a href="other.md">rel</a>' in rendered
    assert "rendered Markdown" in rendered and "?raw=1" in rendered

    raw = await page(client, "/workspaces/default/files/knowledge/notes.md?raw=1")
    assert "<h1>Notes</h1>" not in raw and "&lt;script&gt;alert(1)&lt;/script&gt;" in raw
    plain = await page(client, "/workspaces/default/files/knowledge/plain.txt")
    assert "&lt;b&gt;not html&lt;/b&gt; &amp; plain" in plain and "<b>not html</b>" not in plain
    binary = await page(client, "/workspaces/default/files/knowledge/blob.bin")
    assert "Binary content is not shown." in binary and "PNG" not in binary
    deep = await page(client, "/workspaces/default/files/knowledge/sub/deep.md")
    assert 'href="/workspaces/default/files/knowledge/sub/"' in deep and "<p>deep</p>" in deep

    big = home.paths.workspace("default") / "work" / "big.txt"
    with open(big, "wb") as handle:
        handle.truncate(files.MAX_FILE_PREVIEW_BYTES + 1)
    large = await page(client, "/workspaces/default/files/work/big.txt")
    assert "Larger than the 2 MiB preview limit." in large and "2.0 MB" in large
    (home.paths.workspace("default") / "work" / "empty.txt").write_text("")
    assert "The file is empty." in await page(client, "/workspaces/default/files/work/empty.txt")
    assert "This directory is empty." in await page(client, "/workspaces/default/files/uploads/")


async def test_file_browser_rejects_every_escape(
    client: TestClient, home: Home, tmp_path: Path
) -> None:
    default = home.paths.workspace("default")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-private-content\n")
    os.symlink(home.paths.config, default / "knowledge" / "leak")
    os.symlink(outside, default / "knowledge" / "hole")
    os.symlink(default / "knowledge" / "plain.txt", default / "knowledge" / "inside")
    os.symlink(outside, default / "drafts")

    for path in (
        "/workspaces/default/files/knowledge/../../../config.json",
        "/workspaces/default/files/knowledge/%2e%2e/%2e%2e/config.json",
        "/workspaces/default/files/knowledge/..%2f..%2fconfig.json",
        "/workspaces/default/files/knowledge/sub/../../AGENTS.md",
        "/workspaces/default/files/knowledge/leak",
        "/workspaces/default/files/knowledge/hole/",
        "/workspaces/default/files/knowledge/hole/secret.txt",
        "/workspaces/default/files/drafts/",
        "/workspaces/default/files/drafts/secret.txt",
        "/workspaces/default/files/skills/",
        "/workspaces/default/files/AGENTS.md",
        "/workspaces/default/files/knowledge/missing.txt",
        "/workspaces/nope/files/knowledge/",
        "/workspaces/default/files/knowledge/%00",
    ):
        body = await page(client, path, 404)
        assert "outside-private-content" not in body and "xoxb" not in body, path
    listing = await page(client, "/workspaces/default/files/knowledge/")
    assert "symlink" in listing and "leak" in listing  # visible, since the agent sees it
    inside = await page(client, "/workspaces/default/files/knowledge/inside")
    assert "&lt;b&gt;not html&lt;/b&gt;" in inside  # a link that stays inside is fine
    detail = await page(client, "/workspaces/default")
    assert "drafts" in detail


# -- Skills ---------------------------------------------------------------------


def write_skill(directory: Path, name: str, description: str | None = None) -> None:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or f'What {name} does.'}\n---\n"
    )


def plain(html: str) -> str:
    """Markup reduced to the one line of text it reads as."""
    return " ".join(re.sub(r"<[^>]+>", "", html).split())


# A workspace name is qualified by a prefix drawn quieter than the name, so the label is
# markup rather than a bare string and the tests compare what the heading reads.
SKILL_HEAD = re.compile(
    r'<div class="hourhead" data-group>\s*<b>(.*?)</b>\s*'
    r"<span data-group-count[^>]*>([^<]+)</span>",
    re.DOTALL,
)


def skill_groups(body: str) -> list[tuple[str, str]]:
    """Every Skills group heading in page order, as the label it reads and its count."""
    return [(plain(label), count) for label, count in SKILL_HEAD.findall(body)]


def skill_group_at(body: str, label: str) -> int:
    """Where the one Skills group heading reading ``label`` starts."""
    found = SKILL_HEAD.finditer(body)
    starts = [match.start() for match in found if plain(match.group(1)) == label]
    assert len(starts) == 1, (label, starts)
    return starts[0]


async def test_skills_make_collisions_obvious(
    client: TestClient, home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enso import skills as skills_module

    user = tmp_path / "user-skills"
    (user / "commit").mkdir(parents=True)
    (user / "commit" / "SKILL.md").write_text("---\nname: commit\ndescription: Commit.\n---\n")
    (user / "enso-tables").mkdir()
    (user / "enso-tables" / "SKILL.md").write_text(
        "---\nname: enso-tables\ndescription: Mine.\n---\n"
    )
    monkeypatch.setattr(skills_module, "user_skill_dirs", lambda home=None: [user])
    research = home.paths.workspace("default") / "skills" / "research"
    research.mkdir()
    (research / "SKILL.md").write_text("---\nname: research\ndescription: Look things up.\n---\n")
    twin = home.paths.skills / "research"
    twin.mkdir()
    (twin / "SKILL.md").write_text("---\nname: wrong\n---\n")
    (home.paths.skills / "empty").mkdir()

    # The list is a name and a description; everything else is on the skill's own page.
    body = await page(client, "/skills")
    assert 'href="/skills/research"' in body and 'href="/skills/commit"' in body
    assert 'data-status="error"' in body
    assert 'data-status="active"' in body  # the status is filterable even when unremarkable
    assert 'role="img" aria-label="error" title="error"' in body
    assert 'role="img" aria-label="active" title="active"' in body
    assert "Look things up." in body  # the description is what the row's detail line holds
    assert "collides with" not in body  # but never the error message; that is on its page
    # The group heading says the scope, so neither the row nor a filter repeats it.
    assert 'aria-label="Scope"' not in body and "data-scope=" not in body
    assert re.search(r'class="tag tag-error">\d+ errors?</span>', body)
    assert 'name="workspace"' in body and '<option value="default"' in body

    # The page behind a row carries what the row dropped, and makes a collision readable.
    detail = await page(client, "/skills/research")
    assert "collides with the enso scope (error)" in detail
    assert "collides with the workspace scope (error)" in detail
    assert "Look things up." in detail
    assert "SKILL.md name is &#39;wrong&#39; but the directory is &#39;research&#39;" in detail
    assert "workspace scope" in detail and "enso scope" in detail
    assert 'class="tag tag-error">error</span>' in detail
    # enso-tables exists at both enso and user scope: a warning, not an error.
    tables = await page(client, "/skills/enso-tables")
    assert "collides with the user scope (warning)" in tables
    assert "not managed by Enso" in tables
    assert "SKILL.md has no description" in detail
    assert "SKILL.md is missing" in await page(client, "/skills/empty")
    assert "Not found" in await page(client, "/skills/nope", 404)

    only = await page(client, "/skills?workspace=default")
    assert "reaching the default workspace" in only and "No workspace (" not in only
    assert ("enso / default", "1 skill") in skill_groups(only)  # research, its own
    assert "Not found" in await page(client, "/skills?workspace=nope", 404)


async def test_skills_are_grouped_by_where_they_come_from(
    client: TestClient, home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace group per workspace that owns skills, alphabetically, then enso, then user."""
    from enso import skills as skills_module

    user = tmp_path / "user-skills"
    write_skill(user / "outside", "outside")
    monkeypatch.setattr(skills_module, "user_skill_dirs", lambda home=None: [user])
    for name in ("zebra", "alpha", "quiet"):
        workspaces.ensure_layout(home.paths.workspace(name))
    write_skill(home.paths.workspace("zebra") / "skills" / "zed", "zed")
    write_skill(home.paths.workspace("alpha") / "skills" / "aye", "aye")
    write_skill(home.paths.workspace("alpha") / "skills" / "bee", "bee")
    write_skill(home.paths.skills / "shared", "shared")
    bundled = len(workspaces.BUNDLED_SKILLS) + 1  # the seeded skills plus `shared`

    body = await page(client, "/skills")

    # `default` and `quiet` own no skills, so neither gets a heading over nothing.
    assert skill_groups(body) == [
        ("enso / alpha", "2 skills"),
        ("enso / zebra", "1 skill"),
        ("enso", f"{bundled} skills"),
        ("user", "1 skill"),
    ]
    assert f"{bundled + 4} skills across every workspace" in body
    # A workspace-scope skill sits under the workspace whose directory holds it.
    alpha = skill_group_at(body, "enso / alpha")
    zebra = skill_group_at(body, "enso / zebra")
    assert alpha < body.index('href="/skills/aye"') < zebra
    assert zebra < body.index('href="/skills/zed"') < skill_group_at(body, "enso")

    # Choosing one workspace leaves one workspace group, named for it.
    alone = await page(client, "/skills?workspace=alpha")
    assert skill_groups(alone) == [
        ("enso / alpha", "2 skills"),
        ("enso", f"{bundled} skills"),
        ("user", "1 skill"),
    ]
    assert f"{bundled + 3} skills reaching the alpha workspace" in alone
    assert 'href="/skills/zed"' not in alone


# -- Jobs -----------------------------------------------------------------------


async def test_jobs_list_and_detail(client: TestClient, home: Home) -> None:
    body = await page(client, "/jobs")
    assert 'href="/jobs/default%3Anightly"' in body and 'href="/jobs/default%3Abroken"' in body
    assert 'href="/jobs/default%3Agarbled"' in body
    assert "<code>0 9 * * *</code>" in body and "<code>claude</code>" in body
    # A row says something is wrong with its dot; the job's page says what.
    assert "JOB.md.model &#39;gpt&#39; is not in providers.claude.models" not in body
    assert 'data-enabled="yes"' in body and 'data-status="error"' in body
    assert 'role="img" aria-label="failing or broken" title="failing or broken"' in body
    assert "<time datetime=" in body and "in " in body  # next run for the healthy job
    assert "9 * * *" in body  # the schedule is the one fact a row keeps

    # The facts are equal columns on a wide list, which holds only while every row spends
    # the same three slots in the same order, so a job whose file will not parse dashes the
    # two it cannot answer instead of dropping them and sliding the column.
    assert body.count('<span class="detail columns">') == body.count('<a class="row entity"')
    garbled_row = re.search(
        r'<a class="row entity" href="/jobs/default%3Agarbled".*?</a>', body, re.DOTALL
    )
    assert garbled_row is not None
    assert '<span class="err">unreadable</span>' in garbled_row.group(0)
    assert garbled_row.group(0).count("<span>-</span>") == 2

    # The sparkline box is a fixed width its marks never scale inside, so it anchors right:
    # the newest run lands on the same axis in every row, beside the value it explains.
    spark = re.search(r'<svg class="spark"[^>]*>', body)
    assert spark is not None and 'preserveAspectRatio="xMaxYMid meet"' in spark.group(0)

    detail = await page(client, "/jobs/default%3Anightly")
    assert title(detail) == "Job default:nightly · Enso"
    assert '<div class="markdown">' in detail  # the prompt is JOB.md's body, rendered
    assert "<p>Say hi to &lt;b&gt;everyone&lt;/b&gt;.</p>" in detail
    assert "{{prerun_output}}" in detail
    assert "<code>check.sh</code> (120s timeout)" in detail and "Postrun" in detail
    assert "900s" in detail and "transport default" in detail
    assert f'href="/runs/{home.ok_run}"' in detail and f'href="/runs/{home.failed_run}"' in detail
    assert (
        'href="/runs?job=default%3Anightly' in detail
        and "provider exited 1 &lt;script&gt;" in detail
    )
    assert "<button" not in detail.split("<main")[1].split("Recent runs")[0]

    broken = await page(client, "/jobs/default%3Abroken")
    assert "problems</span>" in broken and "not in providers.claude.models" in broken
    assert "not while it has problems" in broken
    garbled = await page(client, "/jobs/default%3Agarbled")
    assert (
        "needs a leading --- frontmatter block" in garbled and "This job has never run" in garbled
    )
    assert "Not found" in await page(client, "/jobs/default%3Anope", 404)


async def test_a_bad_schedule_stays_visible(client: TestClient, home: Home) -> None:
    write_job(home.paths, "hourly", schedule="@hourly")

    listed = await page(client, "/jobs")
    assert 'href="/jobs/default%3Ahourly"' in listed and "@hourly" in listed

    detail = await page(client, "/jobs/default%3Ahourly")
    assert "problems</span>" in detail and "must be exactly five fields" in detail
    assert "not while it has problems" in detail

    home.paths.config.write_text("{broken")
    listed = await page(client, "/jobs")
    assert 'href="/jobs/default%3Anightly"' in listed and "See Health" in listed
    assert "Config-dependent checks are skipped" in listed


# -- Today ----------------------------------------------------------------------


@pytest.fixture
def today_clock(monkeypatch):
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        fixed = datetime(2026, 9, 7, 12).astimezone()

        @classmethod
        def now(cls, tz=None):
            return cls.fixed.astimezone(tz) if tz else cls.fixed

    monkeypatch.setattr(views, "datetime", Clock)
    return Clock


@pytest.mark.parametrize(
    ("query", "hours", "tick_every"),
    [
        ("", 6, 1),
        ("?range=12", 12, 2),
        ("?range=24", 24, 3),
        ("?range=bogus", 6, 1),  # an unparsable, negative or out-of-range value reads as 6
    ],
)
async def test_today_chart_ranges_keep_overnight_failures(
    client: TestClient, home: Home, today_clock, query, hours, tick_every
) -> None:
    with db.transaction(home.paths) as con:
        for run_id, age in ((home.ok_run, 1), (home.failed_run, 18)):
            con.execute(
                "UPDATE runs SET started_at = ? WHERE id = ?",
                ((today_clock.fixed - timedelta(hours=age)).isoformat(), run_id),
            )
    body = await page(client, "/today" + query)
    assert "Runs, last 24 hours" in body  # the summary strip names the activity window
    assert "1 failure in the last 24 hours" in body
    assert f"· {hours} hours back</span>" in body
    assert f'<a href="/today?range={hours}" aria-current="page">{hours}h</a>' in body
    assert '<nav class="chart-range wide-only" aria-label="Schedule range">' in body
    ticks = re.search(r'<div class="axis ticks">(.*?)</div>', body, re.DOTALL)
    assert ticks is not None
    start = today_clock.fixed + timedelta(hours=1 - hours)
    labels = re.findall(r"<span>(.*?)</span>", ticks.group(1))
    assert labels == [
        (start + timedelta(hours=index)).strftime("%H")
        if (start + timedelta(hours=index)).hour % tick_every == 0
        else ""
        for index in range(hours + 2)
    ]
    track = re.search(r'<svg class="track".*?</svg>', body, re.DOTALL)
    assert track is not None
    assert track.group(0).count('class="gridline"') == hours + 1
    assert track.group(0).count('vector-effect="non-scaling-stroke"') == hours + 1
    assert track.group(0).count('class="f-error"') == (1 if hours == 24 else 0)
    assert 'height="12"' in track.group(0)

    suffix = f"?range={hours}" if hours != 6 else ""
    for section in ("/today", "/today/activity", "/today/reliability"):
        section_body = body if section == "/today" else await page(client, section + query)
        assert f'<a href="{section}{suffix}" aria-current="page">' in section_body
        for target in ("/today", "/today/activity", "/today/reliability"):
            assert f'href="{target}{suffix}"' in section_body
        # The summary strip and the failure banner under it belong to Schedule; the other
        # two sections are the detail behind those numbers and open straight onto their
        # own list. Activity still carries the overnight failure at every chart range.
        schedule = section == "/today"
        assert ('<div class="kpis">' in section_body) is schedule
        assert ("1 failure in the last 24 hours" in section_body) is schedule
        if section == "/today/activity":
            assert f'href="/runs/{home.failed_run}"' in section_body


def test_today_lanes_leave_out_jobs_that_will_not_run(home: Home, today_clock):
    """Only a job that is enabled, scheduled, and due again gets a lane on the chart."""
    write_job(home.paths, "quarter-hour", schedule="*/15 * * * *")
    write_job(home.paths, "disabled", schedule="* * * * *", enabled=False)
    chart = views.today_model(home.paths, "schedule")["chart"]
    lanes = {lane.job: lane for lane in chart["lanes"]}
    assert "disabled" not in lanes and "default:quarter-hour" in lanes
    rows, _ = views._job_rows(home.paths, home.config, today_clock.fixed)
    scheduled = next(row for row in rows if row.ref == "default:quarter-hour")
    stage = replace(scheduled, job=replace(scheduled.job, schedule=None))
    no_next = replace(scheduled, next_run=None)
    empty = views._chart(
        [stage, no_next],
        {},
        today_clock.fixed,
        today_clock.fixed + timedelta(hours=3),
        today_clock.fixed,
        1,
    )
    assert empty["lanes"] == []


@pytest.mark.parametrize("failed", [0, 3])
async def test_reliability_uses_one_sample_within_the_global_scan(
    client: TestClient, home: Home, failed: int
) -> None:
    write_job(home.paths, "occasional")
    write_job(home.paths, "outside-scan")
    outcomes = ["error", "timeout", "prerun_error"][:failed] + ["ok"] * (30 - failed)
    samples = (
        [("nightly", "error", 90_000)]  # the 31st run must not affect counts or averages
        + [("nightly", status, 2_000) for status in outcomes]
        + [
            ("occasional", "no_work", 0),
            ("occasional", "skipped", 1_000),
            ("occasional", "running", None),
            ("occasional", "ok", 9_000),
        ]
        + [("removed-job", "ok", 1_000)] * 365
    )
    # All jobs, even removed ones, share the 400-run scan; a current job whose
    # history is entirely older than it is absent. These runs also predate Today.
    with db.transaction(home.paths) as con:
        con.execute(
            "UPDATE runs SET job = 'outside-scan', started_at = '2019-01-01T00:00:00+00:00'"
        )
        con.executemany(
            """INSERT INTO runs
               (id, job, workspace, provider, model, effort, trigger,
                started_at, duration_ms, status)
               VALUES (?, ?, 'default', 'claude', 'opus', 'high', 'schedule', ?, ?, ?)""",
            [
                (
                    f"{index:012x}",
                    job,
                    (datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=index)).isoformat(),
                    duration,
                    status,
                )
                for index, (job, status, duration) in enumerate(samples)
            ],
        )

    body = await page(client, "/today/reliability")
    assert "Up to 30 latest runs per job" in body
    assert "Bars, counts, and averages use each job's latest runs" in body
    assert "newest 400 runs across all jobs, including runs older than the schedule window" in body
    assert 'href="/jobs/default%3Aoutside-scan"' not in body
    assert 'href="/jobs/default%3Aremoved-job"' not in body

    nightly = re.search(
        r'<a class="row entity" href="/jobs/default%3Anightly">.*?</a>', body, re.DOTALL
    )
    assert nightly is not None
    row = nightly.group(0)
    assert row.count("<rect ") == 30
    assert 'aria-label="30 recent runs"' in row
    assert f'<b class="err">{failed} failed</b>' in row if failed else "<b>30 runs</b>" in row
    assert row.count('class="f-error"') == (2 if failed else 0)
    assert row.count('class="f-warning"') == (1 if failed else 0)
    assert "avg 2s" in row

    occasional = re.search(
        r'<a class="row entity" href="/jobs/default%3Aoccasional">.*?</a>', body, re.DOTALL
    )
    assert occasional is not None
    row = occasional.group(0)
    assert row.count("<rect ") == 4
    assert 'aria-label="4 recent runs"' in row and "<b>4 runs</b>" in row
    assert "avg 5s" in row  # only recorded, nonzero durations enter the average


# -- Runs -----------------------------------------------------------------------


async def test_run_view_descriptions_match_quiet_outcomes(client: TestClient, home: Home) -> None:
    job = load_job(home.paths, home.config)
    quiet = {}
    for status in ("no_work", "skipped"):
        run_id = runs.start(home.paths, job, "schedule", effort="high")
        runs.finish(home.paths, run_id, status=status)
        quiet[status] = run_id
    with db.transaction(home.paths) as con:
        con.executemany(
            "UPDATE runs SET started_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            [(run_id,) for run_id in quiet.values()],
        )

    signal = await page(client, "/runs")
    assert "No-work polls and skipped runs are left out" in signal
    assert "of 2 runs" in signal
    for run_id in quiet.values():
        assert f'href="/runs/{run_id}"' not in signal

    everything = await page(client, "/runs?view=all")
    assert "Consecutive no-work and skipped runs are folded" in everything
    assert "of 4 runs" in everything
    folded = re.search(r'<details class="fold">.*?</details>', everything, re.DOTALL)
    assert folded is not None
    for run_id in quiet.values():
        assert f'href="/runs/{run_id}"' in folded.group(0)

    for view in ("signal", "all", "failed"):
        for status, run_id in quiet.items():
            filtered = await page(client, f"/runs?view={view}&status={status}")
            assert f"Runs with status {status.replace('_', ' ')};" in filtered
            assert "the status filter overrides the view" in filtered
            assert "of 1 run matching the filter" in filtered
            assert f'href="/runs/{run_id}"' in filtered


async def test_runs_list_filters_and_pages_without_loading_output(
    client: TestClient, home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = load_job(home.paths, home.config)
    for index in range(runs.PAGE_SIZE + 3):
        run_id = runs.start(home.paths, job, "schedule", effort="high")
        runs.finish(
            home.paths, run_id, status="no_work" if index % 2 else "ok", output="x" * 10_000
        )
    total = runs.PAGE_SIZE + 5

    def never(row: object) -> object:
        raise AssertionError("a run list instantiated a full Run")

    monkeypatch.setattr(runs, "_run", never)
    quiet = sum(1 for index in range(runs.PAGE_SIZE + 3) if index % 2)

    # The default view leaves out the polls that found no work.
    signal = await page(client, "/runs")
    assert f"of {total - quiet} runs" in signal
    assert '<details class="fold">' not in signal  # no-work runs are not listed

    # `all` is the whole retained history, paged, with the quiet runs folded.
    first = await page(client, "/runs?view=all")
    assert f"Showing 1\u2013{runs.PAGE_SIZE} of {total} runs" in first
    assert "Page 1 of 2" in first and "view=all&amp;page=2" in first
    assert first.count("data-row") == runs.PAGE_SIZE
    assert '<details class="fold">' in first  # folding needs no JavaScript
    assert "xxxxxxxxxx" not in first  # a list never loads output
    assert "<time datetime=" in first
    assert '<form class="toolbar" method="get" action="/runs"' in first
    assert (
        '<option value="default:nightly" selected' not in first
        and '<option value="default:nightly"' in first
    )
    second = await page(client, "/runs?view=all&page=2")
    assert f"Showing {runs.PAGE_SIZE + 1}\u2013{total} of {total} runs" in second
    assert "Page 2 of 2" in second
    beyond = await page(client, "/runs?view=all&page=99")
    assert f"Showing {runs.PAGE_SIZE + 1}\u2013{total}" in beyond
    assert "Showing 1\u2013" in await page(client, "/runs?view=all&page=abc")

    filtered = await page(client, "/runs?job=default%3Anightly&status=error")
    assert "Showing 1\u20131 of 1 run matching the filter" in filtered
    assert f'href="/runs/{home.failed_run}"' in filtered
    assert 'role="img" aria-label="error" title="error"' in filtered
    assert "provider exited 1 &lt;script&gt;x&lt;/script&gt;" in filtered
    assert (
        '<option value="error" selected' in filtered
        and '<option value="default:nightly" selected' in filtered
    )
    no_work = await page(client, "/runs?status=no_work")
    assert 'role="img" aria-label="no work" title="no work"' in no_work
    assert f"of {(runs.PAGE_SIZE + 3) // 2} runs matching" in no_work
    assert "No run matches the filter" in await page(client, "/runs?job=default%3Aother")
    # An unknown status is ignored rather than a 400, leaving the default view.
    assert f"of {total - quiet} runs" in await page(client, "/runs?status=bogus")
    monkeypatch.undo()

    home.paths.db.write_text("garbage")
    broken = await page(client, "/runs")
    assert "Run history could not be read" in broken and 'href="/health"' in broken


async def test_run_detail_shows_everything_including_a_megabyte(
    client: TestClient, home: Home
) -> None:
    body = await page(client, f"/runs/{home.failed_run}")
    assert title(body) == f"Run {home.failed_run} · Enso"
    assert 'tag-error">error</span>' in body and "<dt>Exit code</dt><dd>1</dd>" in body
    assert 'href="/jobs/default%3Anightly"' in body and 'href="/workspaces/default"' in body
    assert "provider exited 1 &lt;script&gt;x&lt;/script&gt;" in body
    assert '<span class="why">exit code 1 · ' in body
    assert "<script>x</script>" not in body
    assert "&lt;i&gt;tag&lt;/i&gt;" in body and "<i>tag</i>" not in body
    # A megabyte of output is past the render limit, so it keeps the preformatted block.
    assert '<pre class="output bare"' in body and "x" * 1000 in body
    assert '<div class="markdown output-md">' not in body
    stored = runs.get(home.paths, home.failed_run)
    assert stored is not None and stored.output is not None
    assert f"{len(stored.output)} characters" in body and len(stored.output) > runs.OUTPUT_KEEP - 40
    assert len(body) > runs.OUTPUT_KEEP
    assert "<time datetime=" in body and "manual" in body

    assert home.failed_run in await page(client, f"/runs/{home.failed_run[:6]}")
    assert "Not found" in await page(client, "/runs/zzzz", 404)
    assert "Not found" in await page(client, "/runs/", 404)

    job = load_job(home.paths, home.config)
    running = runs.start(home.paths, job, "schedule", effort="high")
    live = await page(client, f"/runs/{running}")
    assert 'tag-running">running</span>' in live and "still running" in live
    assert "<dt>Ended</dt><dd>-</dd>" in live


async def test_run_output_renders_as_markdown_with_its_html_escaped(
    client: TestClient, home: Home
) -> None:
    job = load_job(home.paths, home.config)
    run_id = runs.start(home.paths, job, "manual", effort="high")
    runs.finish(
        home.paths,
        run_id,
        status="ok",
        exit_code=0,
        output=(
            "banner v1\n--------\nworkdir: /tmp\nmodel: fake\n\n"
            "## Result\n\n- one\n- two\n\n"
            "<script>alert(1)</script> and a raw <b>tag</b>\n\n"
            "```sh\nls -la\n```\n"
        ),
    )

    body = await page(client, f"/runs/{run_id}")
    assert '<div class="markdown output-md">' in body
    assert "<h2>Result</h2>" in body and "<li>one</li>" in body
    assert '<code class="language-sh">ls -la' in body
    # Provider output is the least trusted text on the site: it is escaped, never markup.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&lt;b&gt;tag&lt;/b&gt;" in body
    assert "<script>alert(1)" not in body and "<b>tag</b>" not in body
    # A transcript's line breaks are its structure, and its rules are separators, not
    # headings: the banner stays text and the dashes under it become a rule.
    assert "workdir: /tmp<br />" in body
    assert "<h2>banner v1</h2>" not in body and "<hr />" in body


async def test_run_detail_shows_attempts_and_escapes_feedback(
    client: TestClient, enso_home: Paths, config: Config
) -> None:
    db.initialize(enso_home)
    write_job(enso_home)
    run_id = runs.start(enso_home, load_job(enso_home, config), "manual", effort="high")
    runs.record_attempt(
        enso_home,
        run_id,
        number=1,
        status="ok",
        exit_code=0,
        output="First answer",
        error="",
        session_id="same-session",
        duration_ms=123,
        postrun_exit_code=10,
        postrun_output="Commit <script>unsafe()</script> changes.",
    )
    # Completed attempts remain inspectable while the next turn or check is still running.
    ongoing = await client.get(f"/runs/{run_id}")
    ongoing_text = await ongoing.text()
    assert ongoing.status == 200 and "Attempt 1" in ongoing_text
    assert "Follow-up feedback" in ongoing_text and "First answer" in ongoing_text
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in ongoing_text
    assert "<script>unsafe()</script>" not in ongoing_text
    runs.finish(
        enso_home,
        run_id,
        status="error",
        output="Final answer",
        error="Validation failed",
        postrun_error="Validation failed",
        session_id="same-session",
    )
    final = await client.get(f"/runs/{run_id}")
    final_text = await final.text()
    assert final.status == 200 and "Postrun error" in final_text
    assert "Validation failed" in final_text and "same-session" in final_text
    assert "including hooks" in final_text and "Provider time budget" in final_text
    summary_text = await (await client.get("/runs")).text()
    assert "First answer" not in summary_text and "unsafe()" not in summary_text


async def test_nothing_writes(client: TestClient, home: Home) -> None:
    before = {
        path: path.stat().st_mtime_ns
        for path in home.paths.home.rglob("*")
        if path.is_file() and not path.name.startswith("enso.db")
    }
    for path in (
        "/health",
        "/workspaces",
        "/workspaces/default",
        "/workspaces/default/files/knowledge/notes.md",
        "/skills",
        "/jobs",
        "/jobs/default%3Anightly",
        "/runs",
        f"/runs/{home.ok_run}",
    ):
        await page(client, path)
    after = {
        path: path.stat().st_mtime_ns
        for path in home.paths.home.rglob("*")
        if path.is_file() and not path.name.startswith("enso.db")
    }
    assert after == before
    assert runs.count(home.paths) == 2


async def test_same_named_jobs_link_to_their_own_history(client, home):
    write_job(home.paths, "nightly", workspace="team", prompt="Only the team workspace")
    job = load_job(home.paths, home.config, "team:nightly")
    team_run = runs.start(home.paths, job, "manual", effort="high")
    runs.finish(home.paths, team_run, status="ok", output="Team result")
    listing = await page(client, "/jobs")
    assert 'href="/jobs/team%3Anightly"' in listing
    assert 'href="/jobs/default%3Anightly"' in listing
    team = await page(client, "/jobs/team:nightly")
    default = await page(client, "/jobs/default:nightly")
    assert "Only the team workspace" in team and "Only the team workspace" not in default
    assert f'href="/runs/{team_run}"' in team and home.ok_run not in team
    assert f'href="/runs/{home.ok_run}"' in default and team_run not in default
    await page(client, "/jobs/nightly", status=404)

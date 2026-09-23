"""Optional real-browser checks against a disposable home and loopback viewer."""

import asyncio
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestServer
from conftest import load_job, write_config, write_job

from enso import db, knowledge, runs, secrets, workspaces
from enso.config import load_config
from enso.web.server import create_app

playwright = pytest.importorskip("playwright.async_api")
expect = playwright.expect


@pytest.fixture(params=["chromium", "webkit"])
async def browser(enso_home, monkeypatch, request):
    # Hermetic browser downloads live beside Playwright, outside the isolated user home.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    async with playwright.async_playwright() as driver:
        current = await getattr(driver, request.param).launch()
        yield current
        await current.close()


@pytest.fixture
async def viewer(enso_home):
    async with TestServer(create_app(enso_home)) as server:
        yield str(server.make_url("/"))


@pytest.mark.parametrize("width", [1280, 320])
async def test_secret_forms_filter_keyboard_and_native_confirmation(
    browser, viewer, enso_home, width, tmp_path
):
    long_name = "VERY_LONG_SECRET_NAME_" * 8
    secrets.add(enso_home, long_name, "synthetic-only")
    context = await browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme="dark" if width == 320 else "light",
        # Form/keyboard behavior does not depend on cross-document animations. Rapid
        # form navigation can make Chromium abort a native view transition mid-test.
        reduced_motion="reduce",
    )
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(viewer + "secrets")
    if width == 320:
        menu = page.locator("[data-more-menu]")
        await menu.locator("summary").focus()
        await menu.locator("summary").press("Enter")
        await expect(menu.locator("a").first).to_be_focused()
        await page.keyboard.press("Escape")
        await expect(menu.locator("summary")).to_be_focused()
        await expect(menu).not_to_have_attribute("open", "")
    await page.get_by_label("Name", exact=True).fill("QA_TOKEN")
    value = page.get_by_label("Value", exact=True)
    await value.fill("synthetic-only")
    await value.press("End")
    await value.press("/")
    await expect(value).to_have_value("synthetic-only/")  # `/` leaves typing alone
    await page.get_by_role("button", name="Add secret", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Secret added.")
    await expect(value).to_have_value("")
    await expect(page.get_by_label("Name", exact=True)).to_have_value("")

    await page.get_by_label("Name", exact=True).fill("QA_TOKEN")
    await value.fill("duplicate-value-must-not-return")
    await page.get_by_role("button", name="Add secret", exact=True).click()
    await expect(page.get_by_role("alert")).to_contain_text("already exists")
    await expect(page.get_by_label("Name", exact=True)).to_have_value("QA_TOKEN")
    await expect(value).to_have_value("")
    assert "duplicate-value-must-not-return" not in await page.content()

    await page.get_by_role("link", name="Secrets (2)", exact=True).click()
    await expect(page.locator("[data-confirm-trigger]").first).to_be_visible()
    await page.keyboard.press("/")
    search = page.get_by_role("searchbox", name="Search secret names")
    await expect(search).to_be_focused()
    await search.fill("qa_token")
    await expect(page.locator("[data-count]")).to_have_text("1 of 2")
    await expect(page.get_by_role("button", name=f"Delete {long_name}", exact=True)).to_be_hidden()
    await search.fill("nothing-matches")
    await expect(page.locator("[data-empty]")).to_be_visible()
    await expect(page.locator("[data-count]")).to_have_text("0 of 2")
    await search.fill("")
    await expect(page.locator("[data-count]")).to_have_text("2 of 2")

    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.evaluate(
        "Promise.all(document.getAnimations().map(animation => animation.finished))"
    )
    await page.screenshot(path=str(tmp_path / f"secrets-{width}.png"), full_page=True)
    delete = page.get_by_role("button", name="Delete QA_TOKEN", exact=True)
    posts = []
    page.on(
        "request", lambda request: posts.append(request.url) if request.method == "POST" else None
    )
    dialogs = []

    async def cancel(dialog):
        dialogs.append(dialog.message)
        await dialog.dismiss()

    page.once("dialog", cancel)
    await delete.focus()
    await delete.press("Enter")
    await expect(delete).to_be_focused()
    assert dialogs == ["Delete QA_TOKEN? Future commands and jobs requiring it will fail."]
    assert posts == [] and "QA_TOKEN" in secrets.names(enso_home)
    page.once("dialog", lambda dialog: dialog.accept())
    await delete.press("Enter")
    await expect(page.get_by_role("status")).to_have_text("Secret deleted.")
    assert secrets.names(enso_home) == [long_name]
    assert not errors
    await context.close()


@pytest.mark.parametrize("javascript", [True, False])
async def test_knowledge_search_navigation_and_refresh(browser, viewer, enso_home, javascript):
    paths = enso_home.knowledge
    paths.mkdir(parents=True)
    guide = paths / "Deployment guide.md"
    guide.write_text(knowledge.normalize_text("Deployment procedure."))
    (paths / "Journal.md").write_text(knowledge.normalize_text("Deployment is mentioned here."))
    context = await browser.new_context(java_script_enabled=javascript)
    page = await context.new_page()
    await page.goto(viewer + "knowledge")
    search = page.get_by_role("searchbox")
    await search.fill("deploymnt")
    await search.press("Enter")
    rows = page.locator("a.knowledge-row")
    await expect(rows).to_have_count(1)
    await expect(rows.first).to_contain_text("Deployment guide")
    await rows.first.click()
    await expect(page.locator(".markdown")).to_contain_text("Deployment procedure.")
    await page.go_back()
    await expect(search).to_have_value("deploymnt")
    await expect(rows).to_have_count(1)
    guide.rename(paths / "Shipping guide.md")
    await page.reload()
    await expect(rows).to_have_count(0)
    await search.fill("shipping")
    await page.get_by_role("button", name="Search", exact=True).click()
    await expect(rows).to_have_count(1)
    await expect(rows.first).to_contain_text("Shipping guide")
    await context.close()


@pytest.fixture
def table_notes(enso_home, raw_config):
    """Exercise prose, compact figures, and a register without using private notes."""
    write_config(enso_home, raw_config)
    root = enso_home.knowledge
    root.mkdir(parents=True)
    pricing_id = str(uuid4())
    introduction = (
        "Customer pricing combines annual commitments, usage charges, and renewal terms. "
        "Compare each plan with its audience before choosing a contract. "
    ) * 3
    long_url = "https://contracts.example/" + "contract-archive-" * 16
    long_code = "CUSTOMER_CONTRACT_REFERENCE_" * 14
    pricing_rows = (
        "| Legacy API plans (2022\u201323) | Pro: US$149/month including 1M API calls, "
        "US$0.00015 per extra call, or purely metered at US$0.00015. Enterprise: "
        "US$474\u2013499/month minimum including 1M calls, US$0.00025 per extra call. "
        "| About 20 early accounts, including January, Numeric, Packsmith, Fever Labs, "
        "World 50, and Rohde & Schwarz. |\n"
        "| Growth and Enterprise | An annual platform fee plus an annual MAU commitment, "
        "invoiced in advance, with overage billed monthly. Discounts and waivers are common, "
        "and some contracts include yearly ramps or a fixed allowance. | All large accounts |\n"
        "| Self-hosted | Annual licence to run the software in the customer's own environment. "
        "US$200K/year plus US$0.05 per MAU. | One account |"
    )
    register_row = (
        "| Example customer | Growth and Enterprise | Annual platform and usage "
        "| September 2027 | Customer operations | Awaiting renewal "
        f"| [{long_url}]({long_url}) | `{long_code}` |"
    )
    body = f"""{introduction}

See [[Related review]].

## Pricing models

| Model | How it charges | Who uses it |
| --- | --- | --- |
{pricing_rows}

## Compact figures

| Year | Count | ARR |
| --- | ---: | ---: |
| 2025 | 12 | $120K |
| 2026 | 19 | $240K |

## Contract register

| Customer | Plan | Commitment | Renewal | Owner | Status | Contract | Reference |
| --- | --- | --- | --- | --- | --- | --- | --- |
{register_row}
"""
    (root / "Pricing review.md").write_text(
        knowledge.normalize_text(f"---\nschema: enso.note/v1\nid: {pricing_id}\n---\n\n{body}")
    )
    (root / "Related review.md").write_text(
        knowledge.normalize_text("Return to [[Pricing review]].")
    )
    work = enso_home.workspace("default") / "work"
    work.mkdir(parents=True)
    (work / "tables.md").write_text(body)
    return f"knowledge/notes/{pricing_id}"


@pytest.mark.parametrize("width", [1280, 320])
@pytest.mark.parametrize("javascript", [True, False])
async def test_knowledge_tables_and_folder_disclosure(
    browser, viewer, table_notes, width, javascript, tmp_path
):
    context = await browser.new_context(
        viewport={"width": width, "height": 900},
        java_script_enabled=javascript,
        color_scheme="dark" if width == 320 else "light",
        reduced_motion="reduce",
    )
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(viewer + table_notes)
    folder = page.locator("[data-folder-context]")
    summary = folder.locator("summary")
    await expect(summary).to_contain_text("In this folder · 2")
    await expect(folder).not_to_have_attribute("open", "")
    document = page.locator(".knowledge-document")
    closed_width = (await document.bounding_box())["width"]
    tables = page.get_by_role("region", name="Table", exact=True)
    await expect(tables).to_have_count(3)
    await expect(tables.first.get_by_role("table")).to_have_count(1)
    await expect(tables.first.get_by_role("columnheader", name="Model", exact=True)).to_be_visible()
    if width == 1280:
        prose = await page.locator(".markdown > p").first.bounding_box()
        table = await tables.first.bounding_box()
        assert prose["width"] < table["width"] - 40
    await page.screenshot(
        path=str(tmp_path / f"knowledge-closed-{width}-js-{javascript}.png"), full_page=True
    )

    await summary.focus()
    await summary.press("Enter")
    await expect(folder).to_have_attribute("open", "")
    await expect(summary).to_be_focused()
    await expect(folder.get_by_role("link", name="Related review", exact=True)).to_be_visible()
    reading_box = await document.bounding_box()
    folder_box = await folder.bounding_box()
    if width == 1280:
        assert closed_width - reading_box["width"] > 200
        assert folder_box["x"] >= reading_box["x"] + reading_box["width"]
    else:
        assert folder_box["y"] + folder_box["height"] <= reading_box["y"]

    # Assert the rendered words stay intact, rather than checking a particular CSS rule.
    for row, word in ((0, "Legacy"), (1, "Enterprise")):
        cell = tables.first.locator("tbody tr").nth(row).locator("td").first
        rect_count = await cell.evaluate(
            """(cell, word) => {
                const text = cell.firstChild;
                const start = text.textContent.indexOf(word);
                const range = document.createRange();
                range.setStart(text, start);
                range.setEnd(text, start + word.length);
                return range.getClientRects().length;
            }""",
            word,
        )
        assert rect_count == 1, f"{word} should not split across lines"

    compact = tables.nth(1)
    assert await compact.evaluate("element => element.scrollWidth <= element.clientWidth")
    wide = tables.nth(2)
    assert await wide.evaluate("element => element.scrollWidth > element.clientWidth")
    await wide.scroll_into_view_if_needed()
    await wide.focus()
    await expect(wide).to_be_focused()
    # WebKit starts native scrolling after the key has been held briefly.
    await page.keyboard.down("ArrowRight")
    try:
        await expect(wide).not_to_have_js_property("scrollLeft", 0)
    finally:
        await page.keyboard.up("ArrowRight")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(
        path=str(tmp_path / f"knowledge-open-{width}-js-{javascript}.png"), full_page=True
    )
    assert not errors
    await context.close()


async def test_knowledge_folder_choice_survives_navigation_and_reload(browser, viewer, table_notes):
    context = await browser.new_context(reduced_motion="reduce")
    page = await context.new_page()
    await page.goto(viewer + table_notes)
    folder = page.locator("[data-folder-context]")
    await expect(folder).not_to_have_attribute("open", "")
    await folder.locator("summary").press("Enter")
    await expect(folder).to_have_attribute("open", "")
    await page.locator(".markdown").get_by_role("link", name="Related review", exact=True).click()
    await expect(page.get_by_role("heading", name="Related review", exact=True)).to_be_visible()
    await expect(folder).to_have_attribute("open", "")
    await page.reload()
    await expect(folder).to_have_attribute("open", "")
    await folder.locator("summary").press("Space")
    await expect(folder).not_to_have_attribute("open", "")
    await page.locator(".markdown").get_by_role("link", name="Pricing review", exact=True).click()
    await expect(folder).not_to_have_attribute("open", "")
    await page.reload()
    await expect(folder).not_to_have_attribute("open", "")
    await context.close()


@pytest.mark.parametrize("width", [1280, 320])
async def test_workspace_markdown_tables_scroll_without_page_overflow(
    browser, viewer, table_notes, width, tmp_path
):
    context = await browser.new_context(viewport={"width": width, "height": 900})
    page = await context.new_page()
    await page.goto(viewer + "workspaces/default/files/work/tables.md")
    tables = page.get_by_role("region", name="Table", exact=True)
    await expect(tables).to_have_count(3)
    await expect(tables.first.get_by_role("table")).to_have_count(1)
    assert await tables.nth(2).evaluate("element => element.scrollWidth > element.clientWidth")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(tmp_path / f"workspace-tables-{width}.png"), full_page=True)
    await context.close()


@pytest.fixture
def task_list_notes(enso_home, raw_config):
    write_config(enso_home, raw_config)
    root = enso_home.knowledge
    root.mkdir(parents=True)
    identity = str(uuid4())
    body = """## Review checklist

- [ ] Read the [related note](Related.md).
- [x] Verify **completed state**.
- Ordinary bullet with supporting tasks:
  - [ ] A nested task with a longer description that wraps over several lines on a phone.
  - [X] Nested completed task with `inline code`.

1. Ordinary numbered item.
2. [ ] Finish the review.
3. [x] Record the outcome.
"""
    (root / "Checklist.md").write_text(
        knowledge.normalize_text(f"---\nschema: enso.note/v1\nid: {identity}\n---\n\n{body}")
    )
    (root / "Related.md").write_text(knowledge.normalize_text("Related content is reachable."))
    work = enso_home.workspace("default") / "work"
    work.mkdir(parents=True)
    (work / "Checklist.md").write_text(body)
    (work / "Related.md").write_text("Related content is reachable.")
    return {
        "knowledge": f"knowledge/notes/{identity}",
        "workspace": "workspaces/default/files/work/Checklist.md",
    }


@pytest.mark.parametrize("width", [1280, 320])
@pytest.mark.parametrize("javascript", [True, False])
async def test_markdown_task_lists_show_read_only_state(
    browser, viewer, task_list_notes, width, javascript, tmp_path
):
    context = await browser.new_context(
        viewport={"width": width, "height": 900},
        java_script_enabled=javascript,
        color_scheme="dark" if width == 320 else "light",
        reduced_motion="reduce",
    )
    page = await context.new_page()
    for surface, path in task_list_notes.items():
        await page.goto(viewer + path)
        markdown = page.locator(".markdown")
        checkboxes = markdown.get_by_role("checkbox")
        await expect(checkboxes).to_have_count(6)
        await expect(checkboxes.first).to_have_accessible_name("Read the related note.")
        await expect(checkboxes.nth(1)).to_have_accessible_name("Verify completed state.")
        expected = [False, True, False, True, False, True]
        for index, checked in enumerate(expected):
            checkbox = checkboxes.nth(index)
            await expect(checkbox).to_be_visible()
            await expect(checkbox).to_be_disabled()
            await expect(checkbox).to_be_checked(checked=checked)

        # Click the actual disabled controls; locator.click correctly refuses them.
        for index in (0, 1):
            checkbox = checkboxes.nth(index)
            await checkbox.scroll_into_view_if_needed()
            bounds = await checkbox.bounding_box()
            await page.mouse.click(
                bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2
            )
            await expect(checkbox).to_be_checked(checked=expected[index])

        await expect(markdown).to_contain_text("Verify completed state.")
        await expect(markdown).to_contain_text("Ordinary bullet with supporting tasks:")
        await expect(markdown).to_contain_text("Ordinary numbered item.")
        await expect(markdown.locator("code")).to_have_text("inline code")
        top_level = await checkboxes.first.bounding_box()
        nested = await checkboxes.nth(2).bounding_box()
        assert nested["x"] > top_level["x"]
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(
            path=str(tmp_path / f"{surface}-tasks-{width}-js-{javascript}.png"), full_page=True
        )
        await markdown.get_by_role("link", name="related note", exact=True).click()
        await expect(page.locator(".markdown")).to_contain_text("Related content is reachable.")
    await context.close()


async def test_secret_forms_without_javascript(browser, viewer, enso_home):
    context = await browser.new_context(
        java_script_enabled=False, viewport={"width": 320, "height": 800}
    )
    page = await context.new_page()
    await page.goto(viewer + "secrets")
    await page.get_by_label("Name", exact=True).fill("QA_TOKEN")
    await page.get_by_label("Value", exact=True).fill("synthetic-only")
    await page.get_by_role("button", name="Add secret", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Secret added.")
    await page.get_by_role("link", name="Secrets (1)", exact=True).click()
    await expect(page.locator("[data-confirm-trigger]")).to_be_hidden()
    await page.locator("summary[aria-label='Delete QA_TOKEN']").click()
    await expect(page.get_by_role("button", name="Delete secret", exact=True)).to_be_visible()
    await page.get_by_role("link", name="Cancel", exact=True).click()
    assert secrets.names(enso_home) == ["QA_TOKEN"]
    await page.locator("summary[aria-label='Delete QA_TOKEN']").click()
    await page.get_by_role("button", name="Delete secret", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Secret deleted.")
    assert secrets.names(enso_home) == []
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await context.close()


async def test_command_jobs_show_explicit_concurrency_on_mobile(
    browser, viewer, enso_home, raw_config, tmp_path
):
    write_config(enso_home, raw_config)
    write_job(enso_home, "agent")
    for name, policy in (("refresh", "wait"), ("backup", "skip")):
        concurrency = {"group": "reporting", "on_busy": policy}
        if policy == "wait":
            concurrency["max_wait"] = 300
        write_job(
            enso_home,
            name,
            command="bash refresh-reporting.sh",
            prompt="Refresh the cache.",
            concurrency=concurrency,
        )
    config = load_config(enso_home)
    db.initialize(enso_home)
    job = load_job(enso_home, config, "refresh")
    run_id = runs.start(enso_home, job, "manual", effort=None, kind="command")
    runs.finish(enso_home, run_id, status="ok", exit_code=0, output="Refreshed.")
    context = await browser.new_context(viewport={"width": 320, "height": 900})
    page = await context.new_page()
    await page.goto(viewer + "jobs")
    await page.locator("[data-search]").fill("command")
    await expect(page.locator("[data-count]")).to_have_text("2 of 3")
    await expect(page.locator('a[href="/jobs/default%3Aagent"]')).to_be_hidden()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    for name, policy in (("refresh", "wait"), ("backup", "skip")):
        await page.goto(viewer + f"jobs/default%3A{name}")
        await expect(page.locator("main")).to_contain_text("Command · no model")
        await expect(page.locator("main")).to_contain_text(f"reporting · {policy} when busy")
        await expect(page.get_by_role("heading", name="Description", exact=True)).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(tmp_path / f"job-{policy}-320.png"), full_page=True)

    await page.goto(viewer + f"runs/{run_id}")
    await expect(page.locator("main")).to_contain_text("Command · no model")
    await expect(page.locator("main")).not_to_contain_text("None/None")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await context.close()


@pytest.mark.parametrize("width", [1280, 320])
async def test_instructions_editor_saves_warns_and_keeps_conflicting_edits(
    browser, viewer, enso_home, raw_config, width, tmp_path
):
    name = "a-long-workspace-name-that-has-to-wrap-somewhere-on-a-phone"
    write_config(enso_home, raw_config)
    workspaces.seed_home(enso_home)
    workspaces.create_workspace(enso_home, name)
    agents = enso_home.workspace(name) / "AGENTS.md"
    context = await browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme="dark" if width == 320 else "light",
        reduced_motion="reduce",
    )
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(viewer + "workspaces/" + name)
    await page.get_by_role("link", name="Instructions", exact=True).click()
    field = page.get_by_label("AGENTS.md", exact=True)
    await expect(field).to_have_value(agents.read_text())

    # Unsaved text asks before the page goes; dismissing keeps it.
    await field.click()
    await page.keyboard.press("ControlOrMeta+End")
    await page.keyboard.type("Ship small changes.\n")
    asked = asyncio.get_running_loop().create_future()

    async def stay(dialog):
        await dialog.dismiss()
        asked.set_result(dialog.type)

    page.once("dialog", stay)
    await page.close(run_before_unload=True)
    assert await asyncio.wait_for(asked, 5) == "beforeunload"
    assert not page.is_closed()

    await field.press("ControlOrMeta+s")
    await expect(page.get_by_role("status")).to_have_text("Instructions saved.")
    assert agents.read_text().endswith("Ship small changes.\n") and "\r" not in agents.read_text()

    # An agent's edit while the page is open is kept; saving again replaces it deliberately.
    agents.write_text("An agent's note.\n")
    await field.fill("My edit.\n")
    await page.get_by_role("button", name="Save", exact=True).click()
    await expect(page.get_by_role("alert")).to_contain_text("changed since this page loaded")
    await expect(field).to_have_value("My edit.\n")
    assert agents.read_text() == "An agent's note.\n"
    disk = page.locator("details.editor-disk")
    await disk.locator("summary").click()
    await expect(disk.locator("pre")).to_contain_text("An agent's note.")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(tmp_path / f"instructions-conflict-{width}.png"), full_page=True)
    await page.get_by_role("button", name="Save", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Instructions saved.")
    assert agents.read_text() == "My edit.\n"

    await page.goto(viewer + "home/instructions")
    await expect(page.get_by_label("AGENTS.md", exact=True)).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(tmp_path / f"instructions-home-{width}.png"), full_page=True)
    await page.goto(viewer + "workspaces")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(tmp_path / f"workspaces-{width}.png"), full_page=True)
    assert not errors
    await context.close()


async def test_instructions_editor_without_javascript(browser, viewer, enso_home, raw_config):
    write_config(enso_home, raw_config)
    workspaces.seed_home(enso_home)
    agents = enso_home.home / "AGENTS.md"
    context = await browser.new_context(
        java_script_enabled=False, viewport={"width": 320, "height": 800}
    )
    page = await context.new_page()
    await page.goto(viewer + "home/instructions")
    await page.get_by_label("AGENTS.md", exact=True).fill("Shared rules.\n")
    await page.get_by_role("button", name="Save", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Instructions saved.")
    assert agents.read_text() == "Shared rules.\n"
    agents.write_text("Changed elsewhere.\n")
    await page.get_by_label("AGENTS.md", exact=True).fill("Mine.\n")
    await page.get_by_role("button", name="Save", exact=True).click()
    await expect(page.get_by_role("alert")).to_contain_text("changed since this page loaded")
    assert agents.read_text() == "Changed elsewhere.\n"
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await context.close()

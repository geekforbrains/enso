"""Optional real-browser checks against a disposable home and loopback viewer."""

import pytest
from aiohttp.test_utils import TestServer

from enso import knowledge, secrets
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

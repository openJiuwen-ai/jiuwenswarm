"""Browser end-to-end coverage for the co-scribe product surfaces.

These tests drive the served web app (default http://127.0.0.1:5173) with
Playwright against the deployment's own running services -- no mocks, the same
loop a person operates. They skip, stating why, when the stack is not up.

Covered here:
- the app shell loads with the Docs entry visible when a connection exists;
- the Docs panel renders connections and managed documents;
- nothing marketplace-shaped ships under the co-scribe name (checked through
  the same extension-manager resolution the UI uses);
- the session reference chip flow's wiring: the openDocSignal latch, the
  chip's jump, and the panel's auto-opened history (exercised when a session
  with clouddoc turns exists; the write half needs a live agent turn and runs
  in the scripted campaign, not here).
"""

from __future__ import annotations

import os

import pytest

BASE_URL = os.environ.get("CO_SCRIBE_E2E_URL", "http://127.0.0.1:5173")

playwright_api = pytest.importorskip(
    "playwright.async_api", reason="playwright is not installed in this environment"
)


@pytest.fixture()
async def page():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        try:
            resp = await pg.goto(BASE_URL, wait_until="networkidle", timeout=15000)
        except Exception:
            pytest.skip(f"web app not reachable at {BASE_URL}")
        if resp is None or not resp.ok:
            pytest.skip(f"web app not healthy at {BASE_URL}")
        await pg.wait_for_timeout(2000)
        yield pg
        await browser.close()


NAV = '[data-testid="session-sidebar-nav-item"]'


def nav_item(page, key: str):
    """A navigation entry by its key, not by its label.

    These tests used to locate by visible text ("Tasks", "Docs", "Connections").
    The interface is bilingual and this deployment renders Chinese, so every such
    locator found nothing: one test failed and the Docs one *skipped itself* with
    "no cloud-doc connection on this deployment" while three connections were
    configured. A suite that reports "nothing to test" when the label language
    changes provides no coverage and says so in the voice of a pass.
    """
    return page.locator(f'{NAV}[data-variant="{key}"]')


@pytest.mark.asyncio
async def test_shell_loads_without_page_errors(page):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.wait_for_timeout(1000)
    assert await page.locator('[data-testid="session-sidebar-rail"]').count() == 1
    assert await page.locator(NAV).count() >= 1, "导航栏一个入口都没渲染"
    assert not errors, errors


# Co-scribe contributes the Docs page as an application plugin, so the rail entry
# carries the plugin's nav key rather than the "docs" key it had while the shell
# hardcoded it. The plugin is listed only while ``clouddoc.enabled`` is true.
DOCS_NAV_KEY = "app:co-scribe"


@pytest.mark.asyncio
async def test_docs_panel_renders_connections_and_documents(page):
    docs_nav = nav_item(page, DOCS_NAV_KEY)
    if await docs_nav.count() == 0:
        pytest.skip("co-scribe is disabled on this deployment; Docs nav hidden by design")
    await docs_nav.first.click()
    # The connection picker used to be a select above the table. The panel is two
    # columns now: connections down the left, documents on the right. Locating the
    # old id here waited thirty seconds and then failed for a UI that had simply
    # moved -- which is why the assertion names what the column *is*, a list of
    # connections, rather than the control it used to be.
    await page.wait_for_selector('[data-testid="docs-conn-row"]', timeout=30000)
    conns = await page.locator('[data-testid="docs-conn-row"]').count()
    assert conns >= 1, "左栏一个连接都没有"
    rows = await page.locator('[data-testid="docs-table-row"]').count()
    assert rows >= 1, "纳管文档一行都没有"


@pytest.mark.asyncio
async def test_the_panel_offers_only_the_formats_co_scribe_serves(page):
    """Markdown was withdrawn, so it stops being a format you can filter for.

    A picker that still lists a withdrawn format says it is a category you can
    adopt into. The three served formats plus the "file" bucket for what is shared
    but not opened is the whole list.
    """
    docs_nav = nav_item(page, DOCS_NAV_KEY)
    if await docs_nav.count() == 0:
        pytest.skip("co-scribe is disabled on this deployment; Docs nav hidden by design")
    await docs_nav.first.click()
    await page.wait_for_selector('[data-testid="docs-filter-kind"]', timeout=30000)
    values = await page.locator('[data-testid="docs-filter-kind"] option').evaluate_all(
        "els => els.map(e => e.value)"
    )
    assert "markdown" not in values, f"筛选器仍把 markdown 当成一种可选格式：{values}"
    for served in ("document", "spreadsheet", "presentation"):
        assert served in values, f"少了 {served}"


@pytest.mark.asyncio
async def test_no_managed_document_is_in_a_withdrawn_format(page):
    """The other half of the same ruling, read off the rows themselves.

    The picker losing the option would mean nothing if a row in that format were
    still listed -- the retirement is what makes the two agree, and it runs at
    startup against the deployment's own config, so this is the only place it can
    be checked against a real one.
    """
    docs_nav = nav_item(page, DOCS_NAV_KEY)
    if await docs_nav.count() == 0:
        pytest.skip("co-scribe is disabled on this deployment; Docs nav hidden by design")
    await docs_nav.first.click()
    await page.wait_for_selector('[data-testid="docs-table-row"]', timeout=30000)
    kinds = await page.locator('[data-testid="docs-panel-kind-icon"]').evaluate_all(
        "els => els.map(e => e.getAttribute('data-kind'))"
    )
    assert kinds, "一行都没有，这条用例什么也没验到"
    served = {"document", "spreadsheet", "presentation"}
    assert set(kinds) <= served, f"列出了不再服务的格式：{sorted(set(kinds) - served)}"


@pytest.mark.asyncio
async def test_every_watch_filter_option_says_how_many(page):
    """All three options carry a count, and the two halves sum to the whole.

    One option carrying a number while the others do not reads as a label rather
    than a quantity; and counts that do not add up cannot be checked against each
    other, which is the only reason to show them.
    """
    docs_nav = nav_item(page, DOCS_NAV_KEY)
    if await docs_nav.count() == 0:
        pytest.skip("co-scribe is disabled on this deployment; Docs nav hidden by design")
    await docs_nav.first.click()
    await page.wait_for_selector('[data-testid="docs-filter-tier"]', timeout=30000)
    labels = await page.locator('[data-testid="docs-filter-tier"] option').evaluate_all(
        "els => els.map(e => e.textContent)"
    )
    import re as _re

    counts = []
    for label in labels:
        found = _re.findall(r"\d+", label or "")
        assert found, f"这个选项没有数量：{label!r}"
        counts.append(int(found[-1]))
    assert len(counts) == 3, f"值守筛选应当是三个选项：{labels}"
    on, off, whole = counts[0], counts[1], counts[2]
    assert on + off == whole, f"开 {on} + 关 {off} 对不上全部 {whole}"


@pytest.mark.asyncio
async def test_reference_chip_jumps_to_the_documents_history(page):
    """The A5 loop on an existing session: chip -> Docs -> history dialog.

    The chips belong to a session, and a fresh page load starts an empty one, so
    this walks the conversation list for a session whose turns touched a document.
    Without one it skips -- the campaign script creates it.
    """
    await nav_item(page, "chat").first.click()
    await page.wait_for_timeout(2000)
    strip = page.locator('[data-testid="doc-references-strip"]')
    rows = page.locator('[data-testid="multi-session-conversation-list-item"]')
    for i in range(min(await rows.count(), 8)):
        if await strip.count():
            break
        await rows.nth(i).click()
        await page.wait_for_timeout(3000)
    if await strip.count() == 0:
        pytest.skip("no session with clouddoc turns; run the campaign first")
    await page.locator('[data-testid^="doc-ref-chip-"]').first.click()
    # The chip lands in the document workbench (release §14.5a): the document's
    # tab plus the receipts rail, not the old panel history dialog.
    await page.wait_for_selector('[data-testid="doc-workbench"]', timeout=30000)
    assert await page.locator('[data-testid="doc-workbench-tab"]').count() >= 1, (
        "the chip must open the document's workbench tab"
    )
    assert await page.locator('[data-testid="doc-workbench-rail"]').count() == 1, (
        "the receipts rail must be beside the document"
    )


def test_nothing_marketplace_shaped_ships():
    """Co-scribe is the deployment's own harnessed editing, switched by its
    Application plugins card -- not an identity someone installs.

    Three things retired on the way to that: the persona card with plan A, the
    plugin package with the move to the application-plugin mechanism, and the
    doc-crew formation with this release's single-agent scope.

    Retiring the formation is not retiring cluster mode. A team member still
    reaches the whole toolkit through the declarative provider path, and
    ``tests/agents/swarm/test_clouddoc_assembly.py`` is what holds that -- what
    went away is the three-role division of labour, not team support.
    """
    epm = pytest.importorskip("jiuwenswarm.server.runtime.extension_package_manager")
    assert epm.show_agent_group("doc-crew") is None, "编队已退役"
    assert epm.show_agent_template("co-scribe") is None, "人设卡已退役"
    assert epm.show_plugin_package("co-scribe") is None, "the plugin package is retired"

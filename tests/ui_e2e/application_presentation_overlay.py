"""Exercise the real presentation host and sandbox bridge in Chromium with a local transport fixture."""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "jiuwenswarm/channels/web/frontend"


class DemoHandler(BaseHTTPRequestHandler):
    """Serve only the compiled fixture and its package-owned sandbox resource."""

    def do_GET(self):
        routes = {
            "/demo.js": (FRONTEND / "node_modules/.cache/presentation-overlay-demo/demo.js", "text/javascript"),
            "/demo.css": (FRONTEND / "node_modules/.cache/presentation-overlay-demo/demo.css", "text/css"),
            "/extension-assets/overlay.demo/g1/pet.html": (
                FRONTEND / "tests/fixtures/presentationOverlayPet.html", "text/html",
            ),
        }
        if self.path == "/":
            data = b'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/demo.css"><div id="root"></div><script type="module" src="/demo.js"></script>'
            content_type = "text/html"
        elif self.path in routes:
            path, content_type = routes[self.path]
            data = path.read_bytes()
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        """Keep routine HTTP requests out of the behavior report."""


async def test_application_presentation_overlay(url: str, artifacts: Path):
    """Drag beyond the clipped parent and verify shared actions and lifecycle cleanup."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = None
        try:
            page = await browser.new_page(viewport={"width": 1200, "height": 800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(url)
            await page.get_by_test_id("webui-presentation-open").click()
            await page.get_by_role("menuitem", name="悬浮示例").click()
            overlay = page.get_by_test_id("webui-presentation-overlay")
            await expect(overlay).to_be_visible()
            frame = page.frame_locator("iframe")
            await expect(frame.locator("body")).to_have_attribute("data-mode", "overlay")
            await expect(frame.locator("body")).to_have_attribute("data-area-width", "1200")
            assert await page.locator("iframe").evaluate("element => element.contentWindow === window") is False
            initial = await overlay.bounding_box()
            assert initial["x"] > 660, initial
            await page.screenshot(path=str(artifacts / "01-open.png"))

            handle = await frame.get_by_test_id("pet-drag").bounding_box()
            await page.mouse.move(handle["x"] + 70, handle["y"] + 35)
            await page.mouse.down()
            await page.mouse.move(140, 130, steps=30)
            await page.mouse.up()
            await expect(overlay).to_have_css("left", "70px")
            await expect(overlay).to_have_css("top", "95px")
            await page.screenshot(path=str(artifacts / "02-drag-across-app.png"))
            assert await frame.locator("body").get_attribute("data-error") is None

            await page.locator("iframe").evaluate("element => window.originalFrame = element")
            await page.get_by_test_id("demo-settings").click()
            await page.get_by_test_id("demo-session").click()
            await page.get_by_test_id("demo-click").click()
            await expect(page.get_by_test_id("demo-page")).to_have_text("settings")
            await expect(page.get_by_test_id("demo-active-session")).to_have_text("b")
            await expect(page.get_by_test_id("demo-click")).to_have_text("Click 1")
            assert await page.locator("iframe").evaluate("element => element === window.originalFrame")
            await frame.get_by_test_id("pet-send").click()
            unsupported = await frame.locator("body").evaluate(
                "() => testRequest('hitTest', {interactive: false}).catch(error => error.message)",
            )
            assert unsupported == "UNSUPPORTED_ACTION"
            await frame.get_by_test_id("pet-workspace").click()
            await page.wait_for_function("overlayDemo.log.some(item => item[0] === 'workspace')")
            log = await page.evaluate("overlayDemo.log")
            assert ["send", "a", "Hello"] in log, log
            assert ["workspace", "a"] in log, log
            await expect(overlay).to_be_visible()

            await page.evaluate("overlayDemo.language('en')")
            await expect(frame.get_by_test_id("pet-send")).to_have_text("Send")
            await expect(page.get_by_test_id("webui-presentation-overlay-close")).to_have_attribute("aria-label", "Close floating form")
            await page.set_viewport_size({"width": 320, "height": 240})
            await expect(frame.locator("body")).to_have_attribute("data-area-width", "320")
            bounds = await overlay.bounding_box()
            assert bounds["x"] >= 0 and bounds["y"] >= 0
            assert bounds["x"] + bounds["width"] <= 320 and bounds["y"] + bounds["height"] <= 240
            await page.screenshot(path=str(artifacts / "03-resize.png"))
            await page.get_by_test_id("webui-presentation-overlay-close").click()
            await expect(overlay).to_have_count(0)

            await page.set_viewport_size({"width": 1200, "height": 800})
            await page.get_by_test_id("webui-presentation-open").click()
            await expect(page.get_by_role("menuitem", name="Floating example")).to_be_enabled()
            await page.get_by_role("menuitem", name="Floating example").click()
            await expect(overlay).to_be_visible()
            await page.evaluate("overlayDemo.publish('desktop')")
            await expect(overlay).to_have_count(0)
            await expect(page.get_by_test_id("webui-presentation-open")).to_be_disabled()
            await page.evaluate("overlayDemo.publish()")
            await expect(page.get_by_test_id("webui-presentation-open")).to_be_enabled()
            await page.get_by_test_id("webui-presentation-open").click()
            await expect(page.get_by_role("menuitem", name="Floating example")).to_be_enabled()
            await page.get_by_role("menuitem", name="Floating example").click()
            await expect(overlay).to_be_visible()
            await page.evaluate("overlayDemo.clear()")
            await expect(overlay).to_have_count(0)
            await page.evaluate("overlayDemo.publish()")
            await page.get_by_test_id("webui-presentation-open").click()
            await expect(page.get_by_role("menuitem", name="Floating example")).to_be_enabled()
            await page.get_by_role("menuitem", name="Floating example").click()
            await expect(overlay).to_be_visible()
            await page.evaluate("overlayDemo.disconnect()")
            await expect(overlay).to_have_count(0)
            assert not errors, errors
            print("PASS: browser activation, cross-app drag, click-through outside the surface, page/session retention, send/workspace, i18n, resize, close, release and disconnect")
        except Exception:
            if page is not None:
                await page.screenshot(path=str(artifacts / "failure.png"))
                print("Browser errors:", errors)
                print("Page:", await page.locator("body").inner_text())
            raise
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts or Path(tempfile.mkdtemp(prefix="presentation-overlay-"))
    artifacts.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(test_application_presentation_overlay(f"http://127.0.0.1:{server.server_port}", artifacts))
        print(f"Screenshots: {artifacts}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()

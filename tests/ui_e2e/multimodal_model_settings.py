"""Exercise multimodal settings against a running personal Web/Server stack.

Use an isolated data directory seeded with one legacy vision/audio model, vision
on and audio off. No external generation requests or real credentials are used.
"""

import argparse
import asyncio
import json
import re
import uuid
from pathlib import Path

from dotenv import dotenv_values
from playwright.async_api import async_playwright, expect
import yaml
import websockets


async def verify(base_url: str, data_dir: Path, output: Path, browser_executable: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    results = []
    errors = []
    config_path = data_dir / "config/config.yaml"

    def state():
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))["multimodal"]

    def record(name):
        results.append({"case": name, "status": "passed"})
        print(f"PASS {name}", flush=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=browser_executable)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        async def dismiss_setup_guide():
            await page.get_by_test_id("model-setup-guide-skip").click()
        await page.add_locator_handler(page.get_by_test_id("model-setup-guide"), dismiss_setup_guide)

        async def shot(name):
            await page.screenshot(path=str(output / name), full_page=True, animations="disabled")

        async def nav(module):
            await page.locator(f'[data-testid="settings-nav-button"][data-variant="{module}"]').click()

        async def models():
            await nav("models")
            await page.locator('[data-testid="settings-models-tab"][data-variant="multimodal"]').click()
            await expect(page.get_by_test_id("settings-multimodal-add-btn")).to_be_enabled()

        def toggle(tool):
            return page.locator(f'[data-testid="settings-switch"][data-variant="{tool}-enabled"]')

        def metadata(name):
            return page.locator(f'[data-testid="settings-multimodal-metadata-field-input"][data-variant="{name}"]')

        def editor():
            return page.locator('[data-testid="settings-agent-media-config-dialog"], [data-testid="settings-agent-config-dialog"]').first

        async def capabilities(selected):
            await page.get_by_test_id("settings-multimodal-capabilities-select").click()
            panel = page.get_by_role("listbox").last
            await expect(panel).to_be_visible()
            options = panel.get_by_role("option")
            for index in range(await options.count()):
                option = options.nth(index)
                value = await option.get_attribute("data-variant")
                checked = await option.get_attribute("aria-selected") == "true"
                if checked != (value in selected):
                    await option.click()
            await page.keyboard.press("Escape")

        async def fill(name, selected, purpose="understanding", key="verification-placeholder"):
            if purpose != "understanding":
                await metadata("purpose").click()
                await page.locator(f'[data-testid="ui-select-option"][data-variant="{purpose}"]').click()
            await capabilities(selected)
            await page.get_by_test_id("settings-model-provider-select-trigger").click()
            await page.locator('[data-testid="settings-model-provider-select-option"][data-variant="custom"]').click()
            await expect(metadata("display_name")).to_have_value("")
            root = editor()
            await root.locator('[data-variant="api_base"] input').fill("https://example.invalid/v1")
            await root.locator('[data-variant="api_key"] input').fill(key)
            await root.locator('[data-variant="model_name"] input').fill(name)
            await root.locator('[data-variant="model_name"] input').press("Tab")
            await expect(metadata("display_name")).to_have_value(name)
            await metadata("display_name").fill(name)

        async def save():
            await editor().locator('[data-testid$="-save-btn"]').click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_have_count(0)

        async def edit_model(model_id):
            card = page.locator(f'[data-testid="settings-multimodal-model-card"][data-variant="{model_id}"]')
            await card.get_by_test_id("settings-multimodal-edit-btn").click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_be_visible()

        async def delete_model(model_id):
            card = page.locator(f'[data-testid="settings-multimodal-model-card"][data-variant="{model_id}"]')
            await card.get_by_test_id("settings-multimodal-delete-btn").click()

        try:
            migrated_before_page = state()
            assert len(migrated_before_page["models"]) == 1
            initial_yaml = config_path.read_bytes()
            initial_env = config_path.with_name(".env").read_bytes()
            record("startup completes migration before any settings page is opened")
            await page.goto(base_url)
            await page.get_by_text("设置", exact=True).first.click()
            await expect(page.get_by_test_id("settings-page")).to_be_visible()
            await models()
            migrated = state()
            assert migrated == migrated_before_page
            assert config_path.read_bytes() == initial_yaml
            assert config_path.with_name(".env").read_bytes() == initial_env
            assert len(migrated["models"]) == 1
            legacy_id = migrated["models"][0]["model_id"]
            assert migrated["models"][0]["capabilities"] == ["vision", "audio"]
            assert migrated["tools"]["vision"]["enabled"] and not migrated["tools"]["audio"]["enabled"]
            await shot("01-history-migration.png")
            record("historical equal models merge and mixed switches remain independent")

            await nav("agent")
            for tool, label in (("vision", "图片处理"), ("audio", "音频处理"), ("video", "视频理解")):
                await expect(toggle(tool)).to_have_attribute("aria-label", label)
            await expect(toggle("vision")).to_have_attribute("aria-checked", "true")
            await expect(toggle("audio")).to_have_attribute("aria-checked", "false")
            await toggle("vision").click()
            await expect(toggle("vision")).to_have_attribute("aria-checked", "false")
            assert state()["tools"]["vision"]["model_id"] == legacy_id
            await toggle("vision").click()
            await expect(toggle("vision")).to_have_attribute("aria-checked", "true")
            assert await page.get_by_test_id("settings-multimodal-model-dialog").count() == 0
            record("manual off preserves binding and on reuses it without configuration")

            await toggle("video").click()
            await expect(page.get_by_test_id("settings-multimodal-capabilities-select")).to_contain_text("视频理解")
            assert not state()["tools"]["video"]["enabled"]
            await editor().locator('[data-testid$="-cancel-btn"]').click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_have_count(0)
            assert not state()["tools"]["video"]["enabled"]
            record("unbound switch opens a defaulted shortcut and cancel leaves it off")

            await models()
            await page.get_by_test_id("settings-multimodal-add-btn").click()
            await fill("验证理解模型 B", ["vision", "video"])
            await shot("02-understanding-form.png")
            await save()
            current = state()
            b_id = next(m["model_id"] for m in current["models"] if m["display_name"] == "验证理解模型 B")
            assert current["tools"]["vision"]["model_id"] == legacy_id
            assert current["tools"]["audio"] == {"model_id": legacy_id, "enabled": False}
            assert current["tools"]["video"] == {"model_id": b_id, "enabled": True}
            record("create binds/enables only unbound capabilities and preserves existing selections")

            await nav("agent")
            await expect(toggle("video")).to_have_attribute("aria-checked", "true")
            await page.locator('.settings-section').filter(has=toggle("vision")).evaluate('(element) => element.scrollIntoView({block: "start"})')
            await shot("03-tool-selections.png")
            selection = page.locator('[data-testid="settings-select"][data-variant="vision-model"]')
            await selection.click()
            panel = page.get_by_role("listbox").last
            await expect(panel).to_contain_text("验证理解模型 B")
            await panel.get_by_role("option", name="验证理解模型 B", exact=True).click()
            await expect(selection).to_contain_text("验证理解模型 B")
            assert state()["tools"]["vision"]["model_id"] == b_id
            assert dotenv_values(config_path.with_name(".env"))["VISION_MODEL_NAME"] == "验证理解模型 B"
            record("tool dropdown chooses a compatible model and updates legacy execution fields")

            await models()
            await edit_model(b_id)
            await capabilities(["vision", "audio", "video"])
            await save()
            assert state()["tools"]["audio"] == {"model_id": legacy_id, "enabled": False}
            record("editing adds a candidate without overriding an already bound disabled tool")

            await edit_model(b_id)
            await capabilities(["audio", "video"])
            await editor().locator('[data-testid$="-save-btn"]').click()
            confirmation = page.get_by_role("dialog").last
            await expect(confirmation).to_contain_text("确认修改模型能力")
            await expect(confirmation).to_contain_text("图片处理")
            await shot("04-capability-confirmation.png")
            await confirmation.get_by_role("button", name="取消", exact=True).click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_be_visible()
            assert "vision" in next(m for m in state()["models"] if m["model_id"] == b_id)["capabilities"]
            await editor().locator('[data-testid$="-save-btn"]').click()
            await page.get_by_role("dialog").last.get_by_role("button", name="确认修改", exact=True).click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_have_count(0)
            assert state()["tools"]["vision"] == {"model_id": legacy_id, "enabled": True}
            record("capability removal confirms affected references; cancel keeps draft and confirm selects fallback")

            await delete_model(legacy_id)
            confirmation = page.get_by_role("dialog").last
            await expect(confirmation).to_contain_text("图片处理、音频处理")
            await shot("05-delete-confirmation.png")
            await confirmation.get_by_role("button", name="取消", exact=True).click()
            assert any(m["model_id"] == legacy_id for m in state()["models"])
            await delete_model(legacy_id)
            await page.get_by_role("dialog").last.get_by_role("button", name="确认删除", exact=True).click()
            await expect(page.locator(f'[data-testid="settings-multimodal-model-card"][data-variant="{legacy_id}"]')).to_have_count(0)
            current = state()
            assert current["tools"]["vision"] == {"model_id": None, "enabled": False}
            assert current["tools"]["audio"] == {"model_id": b_id, "enabled": False}
            assert dotenv_values(config_path.with_name(".env"))["VISION_API_KEY"] == ""
            record("delete confirms off references, clears unsupported tool and preserves fallback switch state")

            await nav("agent")
            await toggle("visual_gen").click()
            await expect(metadata("purpose")).to_contain_text("多模态生成")
            await expect(page.get_by_test_id("settings-multimodal-capabilities-select")).to_contain_text("图像生成")
            await fill("验证生成模型 G", ["visual_gen", "video_gen"], "generation")
            await shot("06-generation-form.png")
            await save()
            current = state()
            g_id = next(m["model_id"] for m in current["models"] if m["display_name"] == "验证生成模型 G")
            assert current["tools"]["visual_gen"] == {"model_id": g_id, "enabled": True}
            assert current["tools"]["video_gen"] == {"model_id": g_id, "enabled": True}
            record("generation shortcut supports multiple capabilities and binds both tools")

            await models()
            await expect(page.get_by_test_id("settings-multimodal-model-card")).to_have_count(2)
            await shot("07-final-model-list.png")
            await page.reload()
            await page.get_by_text("设置", exact=True).first.click()
            await nav("agent")
            await expect(toggle("vision")).to_have_attribute("aria-checked", "false")
            await expect(toggle("audio")).to_have_attribute("aria-checked", "false")
            await expect(toggle("video")).to_have_attribute("aria-checked", "true")
            await expect(toggle("visual_gen")).to_have_attribute("aria-checked", "true")
            await page.locator('.settings-section').filter(has=toggle("vision")).evaluate('(element) => element.scrollIntoView({block: "start"})')
            await shot("08-persisted-tool-settings.png")
            record("reload preserves models, bindings and independent switches")

            await toggle("vision").click()
            await expect(page.get_by_test_id("settings-multimodal-capabilities-select")).to_contain_text("图片处理")
            await fill("验证快捷改选 G2", ["visual_gen"], "generation")
            await save()
            current = state()
            g2_id = next(m["model_id"] for m in current["models"] if m["display_name"] == "验证快捷改选 G2")
            assert current["tools"]["vision"] == {"model_id": None, "enabled": False}
            assert current["tools"]["visual_gen"]["model_id"] == g_id
            await expect(page.get_by_test_id("settings-multimodal-shortcut-notice")).to_contain_text("图片处理")
            await shot("09-shortcut-purpose-change.png")
            record("shortcut purpose and capabilities are editable; saving another type leaves origin unchanged")

            await toggle("visual_gen").click()
            await expect(toggle("visual_gen")).to_have_attribute("aria-checked", "false")
            await models()
            await delete_model(g_id)
            await expect(page.get_by_role("dialog").last).to_contain_text("图像生成、视频生成")
            await page.get_by_role("dialog").last.get_by_role("button", name="确认删除", exact=True).click()
            await expect(page.locator(f'[data-testid="settings-multimodal-model-card"][data-variant="{g_id}"]')).to_have_count(0)
            current = state()
            assert current["tools"]["visual_gen"] == {"model_id": g2_id, "enabled": False}
            assert current["tools"]["video_gen"] == {"model_id": None, "enabled": False}
            record("generation deletion independently selects fallback or clears and disables each tool")

            await page.get_by_test_id("settings-multimodal-add-btn").click()
            await metadata("purpose").click()
            await page.locator('[data-testid="ui-select-option"][data-variant="generation"]').click()
            await capabilities(["video_gen"])
            await page.get_by_test_id("settings-model-provider-select-trigger").click()
            await page.locator('[data-testid="settings-model-provider-select-option"][data-variant="custom_api:vllm-omni"]').click()
            await editor().locator('[data-variant="api_base"] input').fill("http://127.0.0.1:8000")
            await metadata("display_name").fill("vLLM Omni 本地")
            await shot("10-optional-generation-credentials.png")
            await save()
            current = state()
            omni = next(m for m in current["models"] if m["display_name"] == "vLLM Omni 本地")
            assert omni["connection"]["api_key"] == "" and omni["connection"]["model_name"] == ""
            assert current["tools"]["video_gen"] == {"model_id": omni["model_id"], "enabled": True}
            record("vLLM Omni preserves optional key/model validation and automatically binds an unbound tool")

            await edit_model(b_id)
            await capabilities(["vision", "audio", "video"])
            await editor().locator('[data-variant="api_base"] input').fill("https://updated.example.invalid/v1")
            await save()
            current = state()
            assert current["tools"]["vision"] == {"model_id": b_id, "enabled": True}
            assert not current["tools"]["audio"]["enabled"]
            env = dotenv_values(config_path.with_name(".env"))
            assert all(env[f"{tool}_API_BASE"] == "https://updated.example.invalid/v1" for tool in ("VISION", "AUDIO", "VIDEO"))
            record("edit adds unbound capability and synchronizes all on/off bound execution slots")

            await page.get_by_test_id("settings-multimodal-add-btn").click()
            before_invalid = state()
            await editor().locator('[data-testid$="-save-btn"]').click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_be_visible()
            assert state() == before_invalid
            cap_trigger = page.get_by_test_id("settings-multimodal-capabilities-select")
            await cap_trigger.focus()
            await page.keyboard.press("ArrowDown")
            panel = page.get_by_role("listbox").last
            await expect(panel).to_have_attribute("aria-multiselectable", "true")
            await expect(panel).to_be_focused()
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Space")
            await expect(panel.get_by_role("option").first).to_have_attribute("aria-selected", "true")
            await expect(panel).to_be_visible()
            await page.keyboard.press("Escape")
            await metadata("display_name").fill("仅修改元数据的草稿")
            await editor().locator('[data-testid$="-cancel-btn"]').click()
            await expect(page.get_by_role("dialog").last).to_contain_text("未保存")
            await page.get_by_role("dialog").last.get_by_role("button", name="取消", exact=True).click()
            await expect(metadata("display_name")).to_have_value("仅修改元数据的草稿")
            await editor().locator('[data-testid$="-cancel-btn"]').click()
            await page.get_by_role("dialog").last.get_by_role("button", name="确认", exact=True).click()
            await expect(page.get_by_test_id("settings-multimodal-model-dialog")).to_have_count(0)
            assert state() == before_invalid
            record("required validation, keyboard multiselect and metadata-only dirty cancellation preserve state")

            await nav("general")
            await page.locator('[data-testid="settings-select"][data-variant="preferred-language"]').click()
            await page.get_by_role("option", name="English", exact=True).click()
            await models()
            await expect(page.get_by_test_id("settings-multimodal-add-btn")).to_have_text("Add model")
            await shot("11-english-models.png")
            await page.set_viewport_size({"width": 860, "height": 900})
            await page.get_by_test_id("settings-multimodal-add-btn").click()
            await expect(page.get_by_test_id("settings-multimodal-capabilities-select")).to_be_visible()
            box = await page.get_by_role("dialog").last.bounding_box()
            assert box and box["x"] >= 0 and box["y"] >= 0 and box["y"] + box["height"] <= 901
            await page.locator('[data-testid="settings-multimodal-metadata-field-label"][data-variant="capabilities"]').get_by_test_id("ui-help-tips-trigger").hover()
            await expect(page.get_by_role("tooltip")).to_contain_text("Select only capabilities")
            await page.get_by_test_id("settings-multimodal-capabilities-select").click()
            await expect(page.get_by_role("listbox").last).not_to_contain_text("Select only capabilities")
            await shot("12-english-narrow-form.png")
            await page.keyboard.press("Escape")
            await editor().locator('[data-testid$="-cancel-btn"]').click()
            record("English labels/help and narrow dialog bounds render through the actual page")
            assert not errors, errors
        except Exception as error:
            await shot("failure.png")
            results.append({"case": "execution", "status": "failed", "error": str(error)})
            print((await page.locator("body").inner_text())[-3500:], flush=True)
            raise
        finally:
            (output / "results.json").write_text(json.dumps({
                "browser_version": browser.version, "base_url": base_url,
                "data_dir": str(data_dir), "cases": results, "page_errors": errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            await browser.close()


async def verify_exceptions(base_url: str, data_dir: Path, output: Path, browser_executable: str) -> None:
    from tests.ui_e2e.cron_ui_report import _send_ws_request

    output.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config/config.yaml"
    results = []

    def state():
        return yaml.safe_load(config_path.read_text())["multimodal"]

    def record(name):
        results.append({"case": name, "status": "passed"})
        print(f"PASS {name}", flush=True)

    async def rpc(ws, method, params=None):
        result = await asyncio.wait_for(_send_ws_request(ws, method=method, params=params), 30)
        assert result.get("ok"), result
        return result["payload"]

    ws_url = base_url.split("/chat", 1)[0].replace("http://", "ws://") + "/ws"
    async with websockets.connect(ws_url) as ws:
        await ws.recv()
        snapshot = await rpc(ws, "multimodal.models.list")
        duplicates = []
        duplicate_name = f"Duplicate model {uuid.uuid4().hex[:6]}"
        for _ in range(2):
            model = {"model_id": f"mm_{uuid.uuid4()}", "display_name": duplicate_name, "purpose": "understanding",
                     "capabilities": ["vision"], "connection": {"provider": "OpenAI", "protocol": "openai",
                     "api_base": "http://127.0.0.1:9/v1", "api_key": "verification", "model_name": "duplicate-model",
                     "context_window_tokens": 512}}
            result = await rpc(ws, "multimodal.models.upsert", {"mode": "create", "model": model})
            snapshot = result["snapshot"]
            duplicates.append(model["model_id"])

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, executable_path=browser_executable)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(15000)

            async def open_models(target):
                await target.goto(base_url)
                await target.get_by_text(re.compile(r"^(设置|Settings)$")).first.click()
                await target.locator('[data-testid="settings-nav-button"][data-variant="models"]').click()
                await target.locator('[data-testid="settings-models-tab"][data-variant="multimodal"]').click()
                await expect(target.get_by_test_id("settings-multimodal-add-btn")).to_be_enabled()

            try:
                await open_models(page)
                await page.locator('[data-testid="settings-nav-button"][data-variant="agent"]').click()
                selection = page.locator('[data-testid="settings-select"][data-variant="vision-model"]')
                await selection.click()
                options = page.get_by_role("listbox").last.get_by_role("option").filter(has_text=duplicate_name)
                await expect(options).to_have_count(2)
                assert len(set(await options.all_text_contents())) == 2
                label = await options.first.inner_text()
                await options.first.click()
                await expect(selection).to_contain_text(label)
                assert state()["tools"]["vision"]["model_id"] == duplicates[0]
                record("F08/E12 same-name same-connection candidates remain distinguishable and bind by stable ID")
                await page.locator('[data-testid="settings-nav-button"][data-variant="models"]').click()
                await page.locator('[data-testid="settings-models-tab"][data-variant="multimodal"]').click()
                card = page.locator(f'[data-testid="settings-multimodal-model-card"][data-variant="{duplicates[1]}"]')
                await card.get_by_test_id("settings-multimodal-delete-btn").click()
                await expect(page.get_by_test_id("settings-multimodal-reference-tools")).to_have_count(0)
                await page.screenshot(path=str(output / "13-unbound-delete.png"), full_page=True)
                await page.get_by_role("dialog").last.get_by_role("button").last.click()
                await expect(card).to_have_count(0)
                record("F21 unbound deletion uses ordinary confirmation without a false reference list")

                network_page = await browser.new_page()
                intercepted = asyncio.Event()

                async def intercept(route):
                    server = route.connect_to_server()

                    async def from_client(message):
                        request = json.loads(message)
                        if request.get("method") == "multimodal.models.upsert":
                            intercepted.set()
                            await route.close(code=1011, reason="Controlled test connection failure")
                        else:
                            server.send(message)

                    route.on_message(from_client)

                await network_page.route_web_socket("**/ws", intercept)
                await open_models(network_page)
                card = network_page.locator(f'[data-testid="settings-multimodal-model-card"][data-variant="{duplicates[0]}"]')
                await card.get_by_test_id("settings-multimodal-edit-btn").click()
                name = network_page.locator('[data-testid="settings-multimodal-metadata-field-input"][data-variant="display_name"]')
                await name.fill("Unsaved network draft")
                before = config_path.read_bytes()
                editor = network_page.locator('[data-testid="settings-agent-media-config-dialog"], [data-testid="settings-agent-config-dialog"]').first
                await editor.locator('[data-testid$="-save-btn"]').click()
                await asyncio.wait_for(intercepted.wait(), 15)
                await expect(network_page.get_by_test_id("settings-multimodal-model-dialog")).to_be_visible()
                await expect(name).to_have_value("Unsaved network draft")
                await expect(network_page.get_by_role("alert").first).to_be_visible()
                assert config_path.read_bytes() == before
                await network_page.screenshot(path=str(output / "14-network-draft-preserved.png"), full_page=True)
                await network_page.close()
                record("E04 real WebSocket interruption before save preserves the draft and saved configuration")

                snapshot = await rpc(ws, "multimodal.models.list")
                for model in list(snapshot["models"]):
                    tools = [tool for tool, binding in snapshot["tools"].items() if binding["model_id"] == model["model_id"]]
                    result = await rpc(ws, "multimodal.models.delete", {"model_id": model["model_id"], "confirmed_tools": tools})
                    snapshot = result["snapshot"]
                await open_models(page)
                await expect(page.get_by_test_id("settings-multimodal-empty")).to_be_visible()
                await expect(page.get_by_test_id("settings-multimodal-model-card")).to_have_count(0)
                assert all(binding == {"model_id": None, "enabled": False} for binding in state()["tools"].values())
                await page.screenshot(path=str(output / "15-empty-model-list.png"), full_page=True)
                record("F01/F24/E13 deleting all models shows the empty state and clears/disables all five tools")
            except Exception:
                await page.screenshot(path=str(output / "failure.png"), full_page=True)
                raise
            finally:
                (output / "results.json").write_text(json.dumps({"cases": results}, ensure_ascii=False, indent=2))
                await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser-executable", required=True)
    parser.add_argument("--phase", choices=["full", "exceptions"], default="full")
    args = parser.parse_args()
    run = verify if args.phase == "full" else verify_exceptions
    asyncio.run(run(args.base_url, args.data_dir, args.output, args.browser_executable))

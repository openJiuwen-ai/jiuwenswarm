"""Real Web UI acceptance through an isolated Chromium, Gateway and Worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil
import yaml
from playwright.async_api import async_playwright

import issue4689_native as native


async def until(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Browser acceptance condition did not become true")
        await asyncio.sleep(.05)


async def run(args):
    native.GATEWAY = True
    root = Path(args.code).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = native.ModelFixture
    fixture.calls = fixture.main_calls = 0
    fixture.entered.clear()
    fixture.release.clear()
    fixture.scenario = args.scenario
    model = ThreadingHTTPServer(("127.0.0.1", 0), fixture)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    result = {"scenario": args.scenario, "surface": "actual-Web-UI/Chromium/Gateway/native-Worker"}
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-browser-", ignore_cleanup_errors=True) as directory:
        data = Path(directory) / "data"
        config_dir = data / "config"
        config_dir.mkdir(parents=True)
        config = yaml.safe_load((root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
        config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
            "model_client_config": {"client_provider": "OpenAI", "api_base": f"http://127.0.0.1:{model.server_port}/v1",
                                    "api_key": "local-fixture", "model_name": "fixture-model"},
            "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
        config["progressive_tool_enabled"] = False
        config["permissions"]["enabled"] = False
        config.setdefault("react", {}).update(enable_skill_evolution=False, enable_read_image_multimodal=False)
        (config_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (config_dir / ".env").write_text("# isolated browser fixture\n", encoding="utf-8")
        session = data / "agent/sessions/browser-session"
        session.mkdir(parents=True)
        (session / "metadata.json").write_text(json.dumps({"session_id": "browser-session", "mode": "agent",
            "channel_id": "web", "user_id": "acceptance-owner", "title": "Browser acceptance", "work_mode": "work",
            "project_dir": str(data / "agent/workspace"), "created_at": time.time()}), encoding="utf-8")
        fixture.counter = data / "side-effects.txt"
        front_port, gateway_port, ui_port, control_port = (native.port() for _ in range(4))
        env = {**os.environ, "PYTHONPATH": str(root), "JIUWENSWARM_DATA_DIR": str(data),
               "JIUWENSWARM_HOME": str(Path(directory) / "home"), "JIUWENSWARM_RUNTIME_PROFILE": "desktop",
               "NO_PROXY": "127.0.0.1,localhost", "WEB_PORT": str(gateway_port), "GATEWAY_PORT": str(control_port),
               "FRONTEND_PORT": str(ui_port)}
        env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
        log = output.with_suffix(".log").open("wb")
        preparation = subprocess.run([sys.executable, "-c",
            "from jiuwenswarm.common.utils import prepare_runtime_workspace; "
            "prepare_runtime_workspace(cleanup_stale_descs=False); "
            "from jiuwenswarm.common.config import migrate_model_business_ids; migrate_model_business_ids(); "
            "from jiuwenswarm.common.model_migration import migrate_legacy_model_selections; migrate_legacy_model_selections()"],
            cwd=root, env=env, stdout=log, stderr=log, timeout=90)
        assert preparation.returncode == 0, "desktop-style workspace preparation must finish first"
        env["JIUWENSWARM_RUNTIME_WORKSPACE_READY"] = "1"
        processes = []
        def launch_front():
            p = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(front_port)],
                                 cwd=root, env=env, stdout=log, stderr=log)
            processes.append(p)
            return p
        def launch_gateway():
            p = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.gateway.app_gateway", "--host", "127.0.0.1",
                "--port", str(gateway_port), "--agent-server-url", f"ws://127.0.0.1:{front_port}", "--web-path", "/ws"],
                cwd=root, env=env, stdout=log, stderr=log)
            processes.append(p)
            return p
        async def snapshot(name):
            deadline = time.monotonic() + 60
            while True:
                try:
                    value = await page.evaluate("""async () => {
                        const { webClient } = await import('/src/services/webClient.ts');
                        return await webClient.request('session.get_metadata', {session_id: 'browser-session'});
                    }""")
                    return value.get("runtime", {})
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(.25)
        front, gateway = launch_front(), launch_gateway()
        frontend = subprocess.Popen(["D:/nodejs/node.exe", str(root / "jiuwenswarm/channels/web/frontend/node_modules/vite/bin/vite.js"),
            "--host", "127.0.0.1", "--port", str(ui_port)], cwd=root / "jiuwenswarm/channels/web/frontend", env=env, stdout=log, stderr=log)
        worker = None
        frames, errors = [], []
        browser = page = None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True, executable_path=playwright.chromium.executable_path)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                def websocket(ws):
                    if "/ws" not in ws.url:
                        return  # Vite's HMR socket is not the application's transport.
                    def capture(direction, raw):
                        try:
                            frames.append({"direction": direction, "at_ns": time.perf_counter_ns(), "wire": json.loads(raw)})
                        except (ValueError, TypeError):
                            pass
                    ws.on("framesent", lambda frame: capture("sent", frame))
                    ws.on("framereceived", lambda frame: capture("received", frame))
                page.on("websocket", websocket)
                for _ in range(200):
                    try:
                        await page.goto(f"http://127.0.0.1:{ui_port}/chat/browser-session?user_id=acceptance-owner", timeout=1500)
                        break
                    except Exception:
                        await asyncio.sleep(.1)
                editor = page.get_by_test_id("chat-panel-input")
                await editor.wait_for(timeout=120000)
                try:
                    skip = page.get_by_test_id("model-setup-guide-skip")
                    await skip.wait_for(timeout=10000)
                    await skip.click()
                except Exception:
                    pass
                await page.screenshot(path=str(output.with_suffix(".initial.png")))
                await editor.fill("[4689-original] Browser acceptance, continue once")
                await page.get_by_test_id("chat-panel-input-send").click()
                await until(fixture.entered.is_set)
                if args.scenario == "tool":
                    await until(fixture.counter.exists)
                elif args.scenario == "interaction":
                    await page.get_by_test_id("interaction-slot-ix-prompt").wait_for(timeout=90000)
                result["before"] = await snapshot("browser-before")
                worker = native.module_processes(front, "jiuwenswarm.server.worker.main")[-1]
                identity = (worker.pid, worker.create_time())
                result["worker_pid"] = identity[0]
                result["before_execution_ids"] = [r["execution_id"] for r in result["before"].get("executions", [])
                    if r["work_kind"] == "chat_stream"]
                assert len(result["before_execution_ids"]) == 1
                await page.screenshot(path=str(output.with_suffix(".executing.png")))
                native.stop_front(gateway, "jiuwenswarm.gateway.app_gateway")
                native.stop_front(front)
                await asyncio.sleep(.2)
                front, gateway = launch_front(), launch_gateway()
                if args.reload:
                    await page.reload()
                if not args.reload:
                    await until(lambda: any(r["direction"] == "sent" and r["wire"].get("method") == "session.get_metadata"
                        and r["wire"].get("params", {}).get("runtime_resume") for r in frames), timeout=60)
                result["after"] = await snapshot("browser-after")
                assert worker.is_running() and worker.create_time() == identity[1]
                assert result["before"]["worker_instance"] == result["after"]["worker_instance"]
                assert result["before_execution_ids"] == [r["execution_id"] for r in result["after"].get("executions", [])
                    if r["work_kind"] == "chat_stream"]
                if args.scenario == "interaction":
                    card = page.get_by_test_id("interaction-slot-ix-prompt")
                    await card.wait_for(timeout=60000)
                    await card.get_by_test_id("interaction-slot-ix-custom-input").fill("Continue the original interaction")
                    await card.get_by_test_id("interaction-slot-ix-confirm-button").click()
                    fixture.release.set()
                elif args.scenario == "cancel":
                    await page.locator('[data-testid="chat-panel-input-send"][data-variant="stop"]').click(timeout=60000)
                    fixture.release.set()
                else:
                    fixture.release.set()
                if args.scenario != "cancel":
                    await page.get_by_text("resident-worker-ok", exact=False).first.wait_for(timeout=90000)
                else:
                    for _ in range(200):
                        state = await snapshot("browser-cancelled")
                        if not state.get("is_processing"):
                            break
                        await asyncio.sleep(.05)
                    assert not state.get("is_processing"), "cancel must terminate the original execution"
                await page.screenshot(path=str(output.with_suffix(".completed.png")))
                result["final"] = await snapshot("browser-final")
                sent = [r["wire"] for r in frames if r["direction"] == "sent"]
                chat_inputs = [r for r in sent if r.get("method") == "chat.send" and "[4689-original]" in json.dumps(r)]
                assert len(chat_inputs) == 1, "reconnection must not resend original input"
                result.update(original_chat_submissions=len(chat_inputs), model_calls=fixture.main_calls,
                              same_worker=True, same_instance=True, same_execution=True, browser_errors=errors,
                              replay_subscription_count=sum(r.get("method") == "session.get_metadata" and bool(r.get("params", {}).get("runtime_resume")) for r in sent),
                              side_effects=fixture.counter.read_text().splitlines() if fixture.counter.exists() else [])
                if args.scenario == "tool":
                    assert result["side_effects"] == ["once"]
                result["history_records"] = len((session / "history.jsonl").read_text(encoding="utf-8").splitlines())
                await browser.close()
                browser = None
                result["passed"] = True
        except Exception as exc:
            result.update(passed=False, error=f"{type(exc).__name__}: {exc}", browser_errors=errors)
            if page is not None and not page.is_closed():
                try:
                    result["visible_text"] = (await page.locator("body").inner_text())[-12000:]
                    await page.screenshot(path=str(output.with_suffix(".failed.png")))
                except Exception:
                    pass
            raise
        finally:
            fixture.release.set()
            if browser is not None:
                await browser.close()
            if worker is None and front.poll() is None:
                candidates = native.module_processes(front, "jiuwenswarm.server.worker.main")
                if candidates:
                    worker = candidates[-1]
            for p, module in ((gateway, "jiuwenswarm.gateway.app_gateway"), (front, "jiuwenswarm.server.app_agentserver")):
                if p.poll() is None:
                    native.stop_front(p, module)
            if frontend.poll() is None:
                frontend.terminate()
                frontend.wait(timeout=10)
            shutdown_deadline = time.monotonic() + 30
            helper = subprocess.run([sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker", "--timeout", "29"],
                                    cwd=root, env=env, capture_output=True, timeout=30)
            result["owner_shutdown_exit_code"] = helper.returncode
            if helper.returncode == 0:
                result["owner_shutdown"] = json.loads(helper.stdout)
                if worker is not None:
                    try:
                        await asyncio.to_thread(worker.wait, max(.001, shutdown_deadline - time.monotonic()))
                    except psutil.NoSuchProcess:
                        pass
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output.with_suffix(".frames.json").write_text(json.dumps(frames, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log.close()
    model.shutdown()
    model.server_close()
    print(json.dumps({key: result.get(key) for key in ["passed", "scenario", "worker_pid", "same_execution",
        "original_chat_submissions", "model_calls", "side_effects", "owner_shutdown_exit_code"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=("tool", "interaction", "cancel"), default="tool")
    parser.add_argument("--reload", action="store_true")
    asyncio.run(run(parser.parse_args()))

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

try:
    from .runtime_openjiuwen import (
        build_repo_pythonpath,
        build_workspace_env,
        resolve_browser_executable,
        resolve_npm_executable,
        resolve_openjiuwen_runtime,
        resolve_runtime_python,
        terminate_process_tree,
    )
except ImportError:
    from runtime_openjiuwen import (
        build_repo_pythonpath,
        build_workspace_env,
        resolve_browser_executable,
        resolve_npm_executable,
        resolve_openjiuwen_runtime,
        resolve_runtime_python,
        terminate_process_tree,
    )


UI_E2E_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend"
WEB_DIST_DIR = WEB_DIR / "dist"
APP_WEB = REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "app_web.py"


@dataclass
class CaseResult:
    name: str
    status: str
    details: str
    screenshots: list[str] = field(default_factory=list)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_process(
    cmd: list[str], *, env: dict[str, str], log_path: Path, cwd: Path
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def _wait_for_log(log_path: Path, needle: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists() and needle in log_path.read_text(encoding="utf-8", errors="ignore"):
            return
        await asyncio.sleep(0.5)
    tail = ""
    if log_path.exists():
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:])
    raise RuntimeError(f"Timed out waiting for {needle!r} in {log_path}\n{tail}")


async def _wait_for_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for 127.0.0.1:{port} to accept connections")


async def _launch_browser() -> tuple[Browser, Any]:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        executable_path=resolve_browser_executable(),
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    return browser, playwright


async def _install_fake_microphone(page: Page) -> None:
    await page.add_init_script(
        """
        (() => {
          const track = { stop() {} };
          const stream = { getTracks: () => [track] };
          Object.defineProperty(navigator, 'mediaDevices', {
            configurable: true,
            value: { getUserMedia: async () => stream },
          });
          class FakeMediaRecorder {
            static isTypeSupported() { return true; }
            constructor(_stream, options = {}) {
              this.mimeType = options.mimeType || 'audio/webm';
              this.state = 'inactive';
              this.ondataavailable = null;
              this.onerror = null;
              this.onstop = null;
            }
            start() { this.state = 'recording'; }
            stop() {
              this.state = 'inactive';
              if (this.onstop) this.onstop();
            }
          }
          Object.defineProperty(window, 'MediaRecorder', {
            configurable: true,
            value: FakeMediaRecorder,
          });
        })();
        """
    )


async def _screenshot(page: Page, report_dir: Path, name: str) -> str:
    path = report_dir / name
    await page.screenshot(path=str(path), full_page=True, animations="disabled", caret="hide")
    return path.name


async def _run_flow(page: Page, ui_port: int, report_dir: Path) -> list[CaseResult]:
    results: list[CaseResult] = []
    await _install_fake_microphone(page)
    await page.goto(f"http://127.0.0.1:{ui_port}/", wait_until="domcontentloaded")
    await page.locator('[data-testid="app-shell"]').wait_for(timeout=90_000)
    setup_skip = page.locator('[data-testid="model-setup-guide-skip"][data-variant="welcome"]')
    try:
        await setup_skip.wait_for(state="visible", timeout=10_000)
        await setup_skip.click()
        await setup_skip.wait_for(state="detached", timeout=10_000)
    except Exception:  # noqa: BLE001 - the guide is optional for configured workspaces
        pass

    microphone = page.locator('[data-testid="chat-panel-input-microphone"]')
    await microphone.wait_for(state="visible", timeout=30_000)
    microphone_enabled = await microphone.is_enabled()
    if microphone_enabled:
        await microphone.click()
        await microphone.wait_for(timeout=10_000)
    recording = await microphone.get_attribute("aria-pressed") == "true"
    mic_screenshot = await _screenshot(page, report_dir, "01-task-microphone-recording.png")
    results.append(
        CaseResult(
            name="Task microphone enters recording state",
            status="PASS" if microphone_enabled and recording else "FAIL",
            details=(
                f"button_enabled={microphone_enabled}, aria-pressed={str(recording).lower()}; "
                "browser media APIs were replaced with a deterministic microphone fixture"
            ),
            screenshots=[mic_screenshot],
        )
    )

    await page.locator('[data-testid="session-sidebar-nav-item"][data-variant="settings"]').click()
    await page.locator('[data-testid="settings-page"]').wait_for(timeout=30_000)
    experimental_nav = page.locator(".settings-page__nav-button").filter(
        has_text=re.compile(r"实验功能|Experimental", re.IGNORECASE)
    )
    await experimental_nav.click()
    experimental = page.locator('[data-settings-module="experimental"]')
    await experimental.wait_for(timeout=30_000)

    asr_fields = [
        page.get_by_label(re.compile(r"ASR API (地址|URL)", re.IGNORECASE)),
        page.get_by_label(re.compile(r"ASR API (密钥|key)", re.IGNORECASE)),
        page.get_by_label(re.compile(r"ASR 模型|ASR model", re.IGNORECASE)),
    ]
    asr_visible = all([await field.is_visible() for field in asr_fields])
    settings_screenshot = await _screenshot(page, report_dir, "02-experimental-asr-settings.png")
    results.append(
        CaseResult(
            name="Experimental settings expose independent ASR configuration",
            status="PASS" if asr_visible else "FAIL",
            details=f"visible ASR fields={sum([await field.is_visible() for field in asr_fields])}/3",
            screenshots=[settings_screenshot],
        )
    )

    duplex_switch = page.get_by_role(
        "switch", name=re.compile(r"启用任务对话全双工入口|Enable Full-duplex", re.IGNORECASE)
    )
    await duplex_switch.wait_for(state="visible", timeout=30_000)
    if await duplex_switch.get_attribute("aria-checked") != "true":
        await duplex_switch.click()
        await duplex_switch.wait_for(timeout=10_000)
    switch_enabled = await duplex_switch.get_attribute("aria-checked") == "true"

    await page.locator('[data-testid="session-sidebar-nav-item"][data-variant="chat"]').click()
    duplex_action = page.locator('[data-testid="chat-panel-input-full-duplex"]')
    await duplex_action.wait_for(state="visible", timeout=30_000)
    duplex_visible = await duplex_action.is_visible()
    duplex_screenshot = await _screenshot(page, report_dir, "03-task-full-duplex-entry.png")
    results.append(
        CaseResult(
            name="Enabling Full-duplex exposes the task composer action",
            status="PASS" if switch_enabled and duplex_visible else "FAIL",
            details=(
                f"switch_enabled={str(switch_enabled).lower()}, "
                f"full_duplex_action_visible={str(duplex_visible).lower()}"
            ),
            screenshots=[duplex_screenshot],
        )
    )
    return results


def _write_report(
    report_dir: Path,
    *,
    timestamp: str,
    home: Path,
    backend_port: int,
    ui_port: int,
    backend_log: Path,
    ui_log: Path,
    cases: list[CaseResult],
) -> Path:
    report_path = report_dir / "report.md"
    lines = [
        "# Task Multimodal Web UI E2E Report",
        "",
        f"- Generated at: `{timestamp}`",
        f"- Isolated home: `{home}`",
        f"- Backend WS: `ws://127.0.0.1:{backend_port}/ws`",
        f"- Static UI: `http://127.0.0.1:{ui_port}`",
        f"- Backend log: `{backend_log}`",
        f"- UI log: `{ui_log}`",
        "",
        "## Summary",
        "",
        "| Case | Status | Details |",
        "| --- | --- | --- |",
    ]
    for case in cases:
        lines.append(f"| {case.name} | {case.status} | {case.details} |")
    lines.extend(["", "## Screenshots", ""])
    for case in cases:
        for screenshot in case.screenshots:
            lines.extend([f"![{case.name}]({screenshot})", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    (report_dir / "report.json").write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "home": str(home),
                "backend_port": backend_port,
                "ui_port": ui_port,
                "cases": [asdict(case) for case in cases],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return report_path


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Run task ASR and Full-duplex browser E2E tests.")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--runtime-python", default=resolve_runtime_python(REPO_ROOT))
    parser.add_argument("--report-dir", default="")
    args = parser.parse_args()

    if args.build:
        subprocess.run([resolve_npm_executable(), "run", "build"], cwd=str(WEB_DIR), check=True)
    elif not WEB_DIST_DIR.exists():
        raise SystemExit(f"Missing dist directory: {WEB_DIST_DIR}. Run with --build first.")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = (
        Path(args.report_dir).expanduser().resolve()
        if args.report_dir
        else UI_E2E_ROOT / "artifacts" / "task_multimodal_ui" / timestamp
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    home = Path(args.home).expanduser().resolve()
    agent_port, backend_port, ui_port, gateway_port = (_pick_free_port() for _ in range(4))
    backend_log = report_dir / "backend.log"
    ui_log = report_dir / "ui.log"
    resolve_openjiuwen_runtime(args.runtime_python, require=True)
    env = build_workspace_env(home)
    env.update(
        {
            "AGENT_PORT": str(agent_port),
            "AGENT_SERVER_PORT": str(agent_port),
            "WEB_PORT": str(backend_port),
            "GATEWAY_PORT": str(gateway_port),
            "JIUWENSWARM_CLI_PORTS": "1",
            "PYTHONUTF8": "1",
            "PYTHONPATH": build_repo_pythonpath(REPO_ROOT, env.get("PYTHONPATH")),
        }
    )
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)

    backend_proc = _start_process(
        [args.runtime_python, "-m", "jiuwenswarm.app"],
        env=env,
        log_path=backend_log,
        cwd=REPO_ROOT,
    )
    ui_proc: subprocess.Popen | None = None
    browser: Browser | None = None
    playwright: Any = None
    cases: list[CaseResult] = []
    try:
        await _wait_for_log(backend_log, "WebChannel 已启动")
        ui_proc = _start_process(
            [
                args.runtime_python,
                str(APP_WEB),
                "--host",
                "127.0.0.1",
                "--port",
                str(ui_port),
                "--dist",
                str(WEB_DIST_DIR),
                "--proxy-target",
                f"http://127.0.0.1:{backend_port}",
            ],
            env=env,
            log_path=ui_log,
            cwd=REPO_ROOT,
        )
        await _wait_for_port(ui_port)
        browser, playwright = await _launch_browser()
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        cases = await _run_flow(page, ui_port, report_dir)
    except Exception as exc:  # noqa: BLE001
        cases.append(CaseResult(name="Runner failure", status="FAIL", details=str(exc)))
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()
        for process in (ui_proc, backend_proc):
            if process is not None:
                terminate_process_tree(process)

    report_path = _write_report(
        report_dir,
        timestamp=timestamp,
        home=home,
        backend_port=backend_port,
        ui_port=ui_port,
        backend_log=backend_log,
        ui_log=ui_log,
        cases=cases,
    )
    print(report_path)
    return 1 if any(case.status != "PASS" for case in cases) else 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())

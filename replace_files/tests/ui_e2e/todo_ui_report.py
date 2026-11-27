from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

try:
    from .runtime_openjiuwen import (
        build_repo_pythonpath,
        resolve_openjiuwen_runtime,
        resolve_runtime_python,
    )
except ImportError:
    from runtime_openjiuwen import (
        build_repo_pythonpath,
        resolve_openjiuwen_runtime,
        resolve_runtime_python,
    )


UI_E2E_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "jiuwenclaw" / "web"
WEB_DIST_DIR = WEB_DIR / "dist"
APP_WEB = REPO_ROOT / "jiuwenclaw" / "app_web.py"
DEFAULT_HOME = Path.home()


@dataclass
class CaseResult:
    name: str
    status: str
    details: str
    screenshots: list[str] = field(default_factory=list)


@dataclass
class ReportContext:
    timestamp: str
    workspace_home: str
    agent_port: int
    backend_port: int
    ui_port: int
    session_id: str
    backend_log: str
    ui_log: str
    report_dir: str
    jiuwenclaw_head: str | None = None
    agent_core_head: str | None = None
    agent_core_source: str | None = None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repo_head(path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def _default_runtime_python() -> str:
    return resolve_runtime_python(REPO_ROOT)


def _chrome_path() -> str | None:
    return (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )


def _start_process(cmd: list[str], *, env: dict[str, str], log_path: Path, cwd: Path) -> subprocess.Popen:
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


async def _wait_for_log(log_path: Path, needle: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="ignore")
            if needle in content:
                return
        await asyncio.sleep(0.5)
    tail = ""
    if log_path.exists():
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:])
    raise RuntimeError(f"Timed out waiting for {needle!r} in {log_path}\n{tail}")


async def _launch_browser() -> tuple[Browser, Any]:
    playwright = await async_playwright().start()
    chrome_path = _chrome_path()
    browser = await playwright.chromium.launch(
        headless=True,
        executable_path=chrome_path,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    return browser, playwright


async def _close_browser(browser: Browser, playwright: Any) -> None:
    await browser.close()
    await playwright.stop()


async def _wait_for_session(page: Page) -> str:
    await page.wait_for_selector('[data-testid="app-shell"]')
    deadline = time.time() + 30
    while time.time() < deadline:
        session_id = await page.locator('[data-testid="app-shell"]').get_attribute("data-session-id")
        if session_id and session_id != "new":
            return session_id
        await asyncio.sleep(0.2)
    raise RuntimeError("UI session id was not established in time")


async def _screenshot(page: Page, report_dir: Path, name: str, *, locator: str | None = None) -> str:
    path = report_dir / name
    if locator:
        target = page.locator(locator)
        try:
            if await target.count():
                await target.screenshot(
                    path=str(path),
                    timeout=5000,
                    animations="disabled",
                    caret="hide",
                )
                return path.name
        except Exception:  # noqa: BLE001
            pass

    try:
        await page.screenshot(
            path=str(path),
            full_page=True,
            timeout=5000,
            animations="disabled",
            caret="hide",
            scale="css",
        )
        return path.name
    except Exception:  # noqa: BLE001
        pass

    session = await page.context.new_cdp_session(page)
    try:
        metrics = await session.send("Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or {}
        width = max(1, int(size.get("width") or 1280))
        height = max(1, min(int(size.get("height") or 1600), 4000))
        screenshot = await session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "clip": {
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": height,
                    "scale": 1,
                },
            },
        )
        path.write_bytes(base64.b64decode(screenshot["data"]))
    finally:
        await session.detach()
    return path.name


async def _read_tool_executions(page: Page) -> list[dict[str, str]]:
    return await page.locator('[data-testid^="tool-execution-"]').evaluate_all(
        """
        (els) => els.map((el) => ({
          name: el.getAttribute('data-tool-name') || '',
          status: el.getAttribute('data-tool-status') || '',
        }))
        """
    )


async def _read_todos(page: Page) -> list[dict[str, str]]:
    return await page.locator('[data-testid^="todo-item-"]').evaluate_all(
        """
        (els) => els.map((el) => ({
          status: el.getAttribute('data-todo-status') || '',
          text: (el.textContent || '').trim(),
        }))
        """
    )


def _write_report(report_dir: Path, context: ReportContext, cases: list[CaseResult]) -> Path:
    report_path = report_dir / "report.md"
    lines = [
        "# Todo Web UI Test Report",
        "",
        f"- Generated at: `{context.timestamp}`",
        f"- HOME: `{context.workspace_home}`",
        f"- Backend WS: `ws://127.0.0.1:{context.backend_port}/ws`",
        f"- Static UI: `http://127.0.0.1:{context.ui_port}`",
        f"- Session ID: `{context.session_id}`",
        f"- jiuwenclaw HEAD: `{context.jiuwenclaw_head}`",
        f"- agent-core HEAD: `{context.agent_core_head}`",
        f"- agent-core source: `{context.agent_core_source}`",
        f"- Backend log: `{context.backend_log}`",
        f"- UI log: `{context.ui_log}`",
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
        if not case.screenshots:
            continue
        lines.append(f"### {case.name}")
        lines.append("")
        for screenshot in case.screenshots:
            lines.append(f"![{case.name}]({screenshot})")
            lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")

    json_path = report_dir / "report.json"
    json_path.write_text(
        json.dumps(
            {
                "context": asdict(context),
                "cases": [asdict(case) for case in cases],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return report_path


async def _run_ui_flow_impl(
    page: Page,
    *,
    ui_port: int,
    report_dir: Path,
    case_prefix: str,
) -> tuple[list[CaseResult], str]:
    results: list[CaseResult] = []

    await page.goto(f"http://127.0.0.1:{ui_port}/", wait_until="domcontentloaded")
    session_id = await _wait_for_session(page)

    agent_mode = page.locator('[data-testid="chat-mode-agent"]')
    await agent_mode.click()

    deadline = time.time() + 10
    while time.time() < deadline:
        classes = await agent_mode.get_attribute("class") or ""
        if "chat-mode-btn--active" in classes:
            break
        await asyncio.sleep(0.2)

    alpha = f"{case_prefix}-alpha"
    beta = f"{case_prefix}-beta"
    prompt = "\n".join(
        [
            "这是一个 Todo UI smoke test。",
            "请严格按顺序执行下面 3 步：",
            f'1. 只使用 todo 工具，创建两个待办，任务名必须完全等于 "{alpha}" 和 "{beta}"。',
            f'2. 创建后，必须使用 `todo_modify` 把 "{alpha}" 标记为已完成，保留 "{beta}" 为待处理。',
            "3. 不要调用 bash、code、browser、cron 等其他工具，最后只回复一句 `todo smoke done`。",
        ]
    )
    await page.locator('[data-testid="chat-input"]').fill(prompt)
    await page.locator('[data-testid="chat-send"]').click()

    tool_executions: list[dict[str, str]] = []
    todos: list[dict[str, str]] = []
    flow_deadline = time.time() + 180
    while time.time() < flow_deadline:
        tool_executions = await _read_tool_executions(page)
        todos = await _read_todos(page)
        alpha_completed = any(alpha in item["text"] and item["status"] == "completed" for item in todos)
        beta_pending = any(beta in item["text"] and item["status"] == "pending" for item in todos)
        create_done = any(item["name"] == "todo_create" and item["status"] == "completed" for item in tool_executions)
        modify_done = any(item["name"] == "todo_modify" and item["status"] == "completed" for item in tool_executions)
        if create_done and modify_done and alpha_completed and beta_pending:
            break
        await asyncio.sleep(1)

    screenshot_full = await _screenshot(page, report_dir, "01-todo-chat-and-panel.png")
    screenshot_panel = await _screenshot(
        page,
        report_dir,
        "02-todo-panel-updated.png",
        locator='[data-testid="tool-panel"]',
    )

    create_done = any(item["name"] == "todo_create" and item["status"] == "completed" for item in tool_executions)
    modify_done = any(item["name"] == "todo_modify" and item["status"] == "completed" for item in tool_executions)
    alpha_completed = any(alpha in item["text"] and item["status"] == "completed" for item in todos)
    beta_pending = any(beta in item["text"] and item["status"] == "pending" for item in todos)
    tool_summary = ", ".join(f'{item["name"]}:{item["status"]}' for item in tool_executions) or "no tool execution found"
    todo_summary = ", ".join(f'{item["text"]} [{item["status"]}]' for item in todos) or "no todo items found"

    results.append(
        CaseResult(
            name="Todo created from chat is rendered in Tool Panel",
            status="PASS" if create_done and any(alpha in item["text"] for item in todos) and any(beta in item["text"] for item in todos) else "FAIL",
            details=f"Tool executions: [{tool_summary}]. Tool panel todos: [{todo_summary}].",
            screenshots=[screenshot_full, screenshot_panel],
        )
    )
    results.append(
        CaseResult(
            name="todo_modify completion is reflected in Tool Panel",
            status="PASS" if modify_done and alpha_completed and beta_pending else "FAIL",
            details=f"Expected `{alpha}` completed and `{beta}` pending. Observed todos: [{todo_summary}].",
            screenshots=[screenshot_full, screenshot_panel],
        )
    )

    return results, session_id


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Run todo Web UI smoke tests and write a report with screenshots.")
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="HOME used to run jiuwenclaw. Defaults to the real ~/.jiuwenclaw owner home.")
    parser.add_argument("--build", action="store_true", help="Build the frontend before running the UI report.")
    parser.add_argument("--agent-port", type=int, default=0, help="Agent websocket server port. Default picks a free port.")
    parser.add_argument("--backend-port", type=int, default=0, help="Jiuwenclaw WebChannel websocket port. Default picks a free port.")
    parser.add_argument("--ui-port", type=int, default=0, help="Static UI HTTP port. Default picks a free port.")
    parser.add_argument("--runtime-python", default=_default_runtime_python(), help="Python used to start jiuwenclaw.app and app_web.py.")
    parser.add_argument("--report-dir", default="", help="Optional explicit report output directory.")
    args = parser.parse_args()

    if args.build:
        subprocess.run(["npm", "run", "build"], cwd=str(WEB_DIR), check=True)
    elif not WEB_DIST_DIR.exists():
        raise SystemExit(f"Missing dist directory: {WEB_DIST_DIR}. Run with --build first.")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = (
        Path(args.report_dir).expanduser().resolve()
        if args.report_dir
        else (UI_E2E_ROOT / "artifacts" / "todo_ui" / timestamp)
    )
    report_dir.mkdir(parents=True, exist_ok=True)

    agent_port = args.agent_port or _pick_free_port()
    backend_port = args.backend_port or _pick_free_port()
    ui_port = args.ui_port or _pick_free_port()
    backend_log = report_dir / "backend.log"
    ui_log = report_dir / "ui.log"
    runtime_info = resolve_openjiuwen_runtime(args.runtime_python, require=True)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(Path(args.home).expanduser()),
            "AGENT_PORT": str(agent_port),
            "WEB_PORT": str(backend_port),
            "PYTHONPATH": build_repo_pythonpath(REPO_ROOT, env.get("PYTHONPATH")),
        }
    )
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)

    backend_proc = _start_process(
        [args.runtime_python, "-m", "jiuwenclaw.app"],
        env=env,
        log_path=backend_log,
        cwd=REPO_ROOT,
    )
    ui_proc = None
    browser = None
    playwright = None
    session_id = ""
    cases: list[CaseResult] = []
    exit_code = 0

    try:
        await _wait_for_log(backend_log, "WebChannel 已启动", timeout=90)
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
        await _wait_for_log(ui_log, f"http://127.0.0.1:{ui_port}", timeout=30)

        browser, playwright = await _launch_browser()
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        cases, session_id = await _run_ui_flow_impl(
            page,
            ui_port=ui_port,
            report_dir=report_dir,
            case_prefix=f"todo-ui-{timestamp}",
        )
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        cases.append(
            CaseResult(
                name="Runner failure",
                status="FAIL",
                details=str(exc),
            )
        )
    finally:
        if browser is not None and playwright is not None:
            with contextlib.suppress(Exception):
                await _close_browser(browser, playwright)
        for proc in (ui_proc, backend_proc):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

        if session_id:
            session_dir = Path(args.home).expanduser() / ".jiuwenclaw" / "workspace" / "session" / session_id
            with contextlib.suppress(Exception):
                shutil.rmtree(session_dir)

    context = ReportContext(
        timestamp=timestamp,
        workspace_home=str(Path(args.home).expanduser()),
        agent_port=agent_port,
        backend_port=backend_port,
        ui_port=ui_port,
        session_id=session_id,
        backend_log=str(backend_log),
        ui_log=str(ui_log),
        report_dir=str(report_dir),
        jiuwenclaw_head=_repo_head(REPO_ROOT),
        agent_core_head=runtime_info.resolved_ref,
        agent_core_source=runtime_info.source_location,
    )
    report_path = _write_report(report_dir, context, cases)
    print(report_path)
    return exit_code


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())

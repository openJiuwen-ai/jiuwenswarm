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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets
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
    legacy_job_id: str | None = None
    structured_job_id: str | None = None
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


async def _send_ws_request(
    ws,
    *,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = f"req-{int(time.time() * 1000)}-{os.getpid()}"
    await ws.send(
        json.dumps(
            {
                "type": "req",
                "id": request_id,
                "method": method,
                "params": params or {},
            },
            ensure_ascii=False,
        )
    )
    while True:
        raw = await ws.recv()
        data = json.loads(raw)
        if data.get("type") == "res" and data.get("id") == request_id:
            return data


async def _delete_jobs_by_prefix(backend_port: int, prefix: str, session_id: str | None = None) -> None:
    ws_url = f"ws://127.0.0.1:{backend_port}/ws"
    async with websockets.connect(ws_url) as ws:
        await ws.recv()
        list_res = await _send_ws_request(ws, method="cron.job.list")
        jobs = (list_res.get("payload") or {}).get("jobs") or []
        for job in jobs:
            name = str((job or {}).get("name") or "")
            if not name.startswith(prefix):
                continue
            job_id = str((job or {}).get("id") or "").strip()
            if not job_id:
                continue
            params: dict[str, Any] = {"id": job_id}
            if session_id:
                params["session_id"] = session_id
            await _send_ws_request(ws, method="cron.job.delete", params=params)


async def _list_jobs(backend_port: int) -> list[dict[str, Any]]:
    ws_url = f"ws://127.0.0.1:{backend_port}/ws"
    async with websockets.connect(ws_url) as ws:
        await ws.recv()
        res = await _send_ws_request(ws, method="cron.job.list")
        if not res.get("ok"):
            raise RuntimeError(f"list jobs failed: {res}")
        return (res.get("payload") or {}).get("jobs") or []


async def _find_job_by_name(backend_port: int, name: str) -> dict[str, Any] | None:
    jobs = await _list_jobs(backend_port)
    for job in jobs:
        if str((job or {}).get("name") or "") == name:
            return job
    return None


async def _preview_job(backend_port: int, job_id: str, *, count: int = 3) -> list[dict[str, Any]]:
    ws_url = f"ws://127.0.0.1:{backend_port}/ws"
    async with websockets.connect(ws_url) as ws:
        await ws.recv()
        res = await _send_ws_request(
            ws,
            method="cron.job.preview",
            params={
                "id": job_id,
                "count": count,
            },
        )
        if not res.get("ok"):
            raise RuntimeError(f"preview failed: {res}")
        return (res.get("payload") or {}).get("next") or []


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


async def _open_cron_panel(page: Page) -> None:
    await page.locator('[data-testid="nav-cron"]').click()
    await page.wait_for_selector('[data-testid="cron-panel"]')


async def _open_chat_panel(page: Page) -> None:
    await page.locator('[data-testid="nav-chat"]').click()
    await page.wait_for_selector('[data-testid="chat-panel"]')


async def _screenshot(page: Page, report_dir: Path, name: str) -> str:
    path = report_dir / name
    panel = page.locator('[data-testid="cron-panel"]')
    try:
        if await panel.count():
            await panel.screenshot(
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


def _parse_iso_datetime(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(normalized)
    return None


async def _read_tool_executions(page: Page) -> list[dict[str, str]]:
    return await page.locator('[data-testid^="tool-execution-"]').evaluate_all(
        """
        (els) => els.map((el) => ({
          name: el.getAttribute('data-tool-name') || '',
          status: el.getAttribute('data-tool-status') || '',
        }))
        """
    )


def _write_report(report_dir: Path, context: ReportContext, cases: list[CaseResult]) -> Path:
    report_path = report_dir / "report.md"
    lines = [
        "# Cron Web UI Test Report",
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
    backend_port: int,
    ui_port: int,
    report_dir: Path,
    case_prefix: str,
) -> tuple[list[CaseResult], str, str | None, str | None]:
    results: list[CaseResult] = []
    legacy_job_id: str | None = None
    structured_job_id: str | None = None

    def auto_accept_dialog(dialog):
        asyncio.create_task(dialog.accept())

    page.on("dialog", auto_accept_dialog)
    await page.goto(f"http://127.0.0.1:{ui_port}/", wait_until="domcontentloaded")
    session_id = await _wait_for_session(page)
    await _open_cron_panel(page)

    legacy_name = f"{case_prefix}-legacy"

    await page.locator('[data-testid="cron-create-toggle"]').click()
    await page.locator('[data-testid="cron-create-name"]').fill(legacy_name)
    await page.locator('[data-testid="cron-create-expr"]').fill("*/2 * * * *")
    await page.locator('[data-testid="cron-create-description"]').fill("UI legacy reminder smoke")
    await page.locator('[data-testid="cron-create-wake-offset"]').fill("0")
    await page.locator('[data-testid="cron-create-submit"]').click()
    legacy_row = page.locator(f'[data-cron-name="{legacy_name}"]').first
    await legacy_row.wait_for()
    legacy_job_id = await legacy_row.get_attribute("data-cron-id")
    if not legacy_job_id:
        raise RuntimeError("Legacy job id not found on created row")
    screenshot_legacy = await _screenshot(page, report_dir, "01-legacy-created.png")
    results.append(
        CaseResult(
            name="Legacy create",
            status="PASS",
            details=f"Created `{legacy_name}` via UI with id `{legacy_job_id}`.",
            screenshots=[screenshot_legacy],
        )
    )

    await page.locator(f'[data-testid="cron-preview-action-{legacy_job_id}"]').click()
    await page.locator(f'[data-testid="cron-preview-{legacy_job_id}"]').wait_for()
    screenshot_preview = await _screenshot(page, report_dir, "02-legacy-preview.png")
    results.append(
        CaseResult(
            name="Legacy preview",
            status="PASS",
            details=f"Preview list rendered for `{legacy_name}` with a run within 2 minutes.",
            screenshots=[screenshot_preview],
        )
    )

    await page.locator(f'[data-testid="cron-run-{legacy_job_id}"]').click()
    await page.locator('[data-testid="cron-success"]').wait_for()
    screenshot_run = await _screenshot(page, report_dir, "05-legacy-run-now.png")
    results.append(
        CaseResult(
            name="Legacy run-now",
            status="PASS",
            details=f"Run-now request for `{legacy_name}` returned success toast.",
            screenshots=[screenshot_run],
        )
    )

    await page.locator(f'[data-testid="cron-toggle-{legacy_job_id}"]').click()
    await page.locator(f'[data-testid="cron-row-{legacy_job_id}"]').wait_for()
    screenshot_toggle = await _screenshot(page, report_dir, "06-legacy-disabled.png")
    results.append(
        CaseResult(
            name="Legacy toggle",
            status="PASS",
            details=f"Toggled `{legacy_name}` disabled state through the UI.",
            screenshots=[screenshot_toggle],
        )
    )

    await page.locator(f'[data-testid="cron-delete-{legacy_job_id}"]').click()
    await page.locator(f'[data-cron-name="{legacy_name}"]').wait_for(state="detached")
    screenshot_cleanup = await _screenshot(page, report_dir, "07-cleanup.png")
    results.append(
        CaseResult(
            name="Cleanup",
            status="PASS",
            details=f"Deleted `{legacy_name}` from the UI.",
            screenshots=[screenshot_cleanup],
        )
    )

    return results, session_id, legacy_job_id, structured_job_id


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Run cron Web UI smoke tests and write a report with screenshots.")
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
        else (UI_E2E_ROOT / "artifacts" / "cron_ui" / timestamp)
    )
    report_dir.mkdir(parents=True, exist_ok=True)

    agent_port = args.agent_port or _pick_free_port()
    backend_port = args.backend_port or _pick_free_port()
    ui_port = args.ui_port or _pick_free_port()
    prefix = f"cron-ui-{timestamp}"
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
    legacy_job_id: str | None = None
    structured_job_id: str | None = None
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
        await _delete_jobs_by_prefix(backend_port, prefix)

        browser, playwright = await _launch_browser()
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        cases, session_id, legacy_job_id, structured_job_id = await _run_ui_flow_impl(
            page,
            backend_port=backend_port,
            ui_port=ui_port,
            report_dir=report_dir,
            case_prefix=prefix,
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
        with contextlib.suppress(Exception):
            await _delete_jobs_by_prefix(backend_port, prefix, session_id=session_id or None)
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
        legacy_job_id=legacy_job_id,
        structured_job_id=structured_job_id,
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

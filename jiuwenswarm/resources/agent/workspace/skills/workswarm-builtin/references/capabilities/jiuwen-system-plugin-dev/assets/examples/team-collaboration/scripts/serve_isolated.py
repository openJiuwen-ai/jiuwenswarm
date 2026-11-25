"""在隔离环境里起一个真实实例（stub Ops + 真实 HTTP 服务），供 HTTP 契约检查使用。

不安装到宿主、不影响用户界面；用于在装机前把 §5 的全部端点打一遍。

用法：
    python scripts/serve_isolated.py --port-file port.txt --seconds 120
    # 另开一个终端：
    python scripts/api_contract_check.py --base http://127.0.0.1:<port> --sid <sid>

端口与 sid 通过 stdout 打印，同时写入 --port-file 便于脚本读取。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
for path in (str(PKG_ROOT), str(PKG_ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import integration_isolated_load as iso  # noqa: E402

from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput  # noqa: E402
from team_collab_viz import extension as ext_module  # noqa: E402


def source_mtime_ms() -> tuple[int, str]:
    """(后端源码最新 mtime 毫秒, 文件名)。

    只统计 `team_collab_viz/**/*.py`：`web/` 是**每次请求现读**的静态文件，
    前端改动不会让本进程导入的 Python 变旧；把它算进来只会造成误判。
    """
    newest, newest_name = 0, ""
    for path in sorted((PKG_ROOT / "team_collab_viz").rglob("*.py")):
        stamp = int(path.stat().st_mtime * 1000)
        if stamp > newest:
            newest, newest_name = stamp, path.relative_to(PKG_ROOT).as_posix()
    return newest, newest_name


def watch_source_changes(start_ms: int) -> None:
    """源码在本进程启动后被改动 → 本实例已 stale，打红字警告（不静默）。"""
    while True:
        time.sleep(1.0)
        newest, newest_name = source_mtime_ms()
        if newest > start_ms:
            print(f"STALE_INSTANCE source_changed_after_start={newest_name} "
                  f"source_mtime_ms={newest} started_at_ms={start_ms}", flush=True)
            return


class _Registrar:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def register_event_handler(self, event_type, handler, match_pattern, level="user"):
        self.calls.append((event_type, handler.__name__, dict(match_pattern), level))
        return f"h{len(self.calls)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-file", default="")
    ap.add_argument("--seconds", type=float, default=180.0)
    args = ap.parse_args()

    started_at_ms = int(time.time() * 1000)
    src_ms, src_name = source_mtime_ms()
    if src_ms > started_at_ms:
        # 启动瞬间就比源码旧，说明系统时钟/文件系统异常，不静默继续
        print(f"FATAL source_mtime_ms={src_ms}({src_name}) > started_at_ms={started_at_ms}", flush=True)
        return 2
    print(f"SOURCE_MTIME_MS={src_ms} SOURCE_FILE={src_name}", flush=True)
    print(f"STARTED_AT_MS={started_at_ms}", flush=True)
    # 两类指纹：py 参与新鲜度闸门；web 只记录（静态文件每请求现读，改前端不需要重启本实例）
    py_fp = {p.relative_to(PKG_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
             for p in sorted((PKG_ROOT / "team_collab_viz").rglob("*.py"))}
    web_fp = {p.relative_to(PKG_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
              for p in sorted((PKG_ROOT / "web").rglob("*")) if p.is_file()}
    print("PY_FINGERPRINT=" + json.dumps(py_fp, ensure_ascii=False), flush=True)
    print("WEB_FINGERPRINT=" + json.dumps(web_fp, ensure_ascii=False), flush=True)
    threading.Thread(target=watch_source_changes, args=(started_at_ms,), daemon=True).start()

    ops = iso.FakeOps()
    ext = ext_module.create_extension()
    ext.register(_Registrar())
    activation = iso.activation_of(ext)
    port = iso.port_of(ext)
    base = f"http://127.0.0.1:{port}"

    event = iso.SimpleNamespace(event_input=UiActionInput(
        event_type="ui.ready",
        target=RenderTarget(iso.SESSION_ID, iso.SLOT, iso.PACKAGE_ID, ""),
    ))
    asyncio.run(activation.on_ui_ready(event, ops, iso._next_handler))
    # 再打一次事件，让快照带上 lifecycle 事实并验证刷新链路
    monitor = iso.SimpleNamespace(event_input=iso.SimpleNamespace(
        team_name=iso.TEAM_NAME,
        monitor_event=iso.SimpleNamespace(event_type="team_created", team_name=iso.TEAM_NAME,
                                          timestamp=int(time.time() * 1000)),
    ))
    asyncio.run(activation.on_team_monitor_event(monitor, ops, iso._next_handler))

    print(f"PORT={port}", flush=True)
    print(f"BASE={base}", flush=True)
    print(f"SID={iso.SESSION_ID}", flush=True)
    if args.port_file:
        Path(args.port_file).write_text(str(port), encoding="utf-8")
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.5)
    finally:
        print("STOP", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

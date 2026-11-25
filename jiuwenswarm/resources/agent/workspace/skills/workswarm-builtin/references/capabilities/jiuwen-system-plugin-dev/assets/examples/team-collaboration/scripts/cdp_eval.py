"""最小 CDP 客户端：对宿主 Electron 渲染进程执行 JS / 读真实 DOM。

用途：验收要求「从真实 DOM 取 iframe 的 src」，本机宿主渲染进程带
--remote-debugging-port，可直接用 Chrome DevTools Protocol 取真实 DOM。

用法：
    python cdp_eval.py --list
    python cdp_eval.py --expr "document.title"
    python cdp_eval.py --expr @file.js      # 从文件读表达式
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from websockets.sync.client import connect

DEFAULT_CDP = "http://127.0.0.1:60028"


def list_targets(base: str = DEFAULT_CDP) -> list[dict]:
    with urllib.request.urlopen(base + "/json/list", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_target(page_title: str = "WorkSwarm", base: str = DEFAULT_CDP) -> dict:
    targets = list_targets(base)
    for t in targets:
        if t.get("type") != "page":
            continue
        if page_title and page_title in (t.get("title") or ""):
            return t
    raise SystemExit(f"未找到 title 含 {page_title!r} 的 page target；实际：{[t.get('title') for t in targets]}")


def evaluate(expr: str, page_title: str = "WorkSwarm", base: str = DEFAULT_CDP,
             timeout: float = 30.0):
    target = pick_target(page_title, base)
    ws_url = target["webSocketDebuggerUrl"]
    with connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=10) as ws:
        msg = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
                "timeout": int(timeout * 1000),
            },
        }
        ws.send(json.dumps(msg))
        while True:
            raw = ws.recv(timeout=timeout + 5)
            data = json.loads(raw)
            if data.get("id") != 1:
                continue
            if "error" in data:
                raise SystemExit("CDP 错误: " + json.dumps(data["error"], ensure_ascii=False))
            result = data["result"]
            if result.get("exceptionDetails"):
                raise SystemExit("JS 异常: " + json.dumps(result["exceptionDetails"], ensure_ascii=False))
            return result.get("result", {}).get("value")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--expr")
    ap.add_argument("--title", default="WorkSwarm")
    args = ap.parse_args()
    if args.list:
        for t in list_targets():
            print(f"{t.get('type'):8s} {t.get('id')}  {t.get('title')!r}  {t.get('url')}")
        return 0
    expr = args.expr
    if expr and expr.startswith("@"):
        with open(expr[1:], "r", encoding="utf-8") as fh:
            expr = fh.read()
    if not expr:
        ap.print_help()
        return 2
    value = evaluate(expr, page_title=args.title)
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())

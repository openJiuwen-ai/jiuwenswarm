"""在宿主页面的 iframe（opaque origin）内部执行 JS —— CDP 版。

为什么需要它：宿主给 WebUI iframe 固定 `sandbox="allow-scripts ..."`（**没有**
allow-same-origin），父页面无法读取 iframe 文档；但 CDP 可以。
两条路径：
  A. iframe 被浏览器提升为独立 target（type=iframe / iframe 出现在 /json/list）→ 直接连它；
  B. 否则用 Runtime.executionContextCreated 的 `auxData.frameId` 对上 Page.getFrameTree 的 frame，
     带 contextId 执行 Runtime.evaluate。

用法：
    python cdp_frame.py --list
    python cdp_frame.py --match task-graph.html --expr "document.title"
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from websockets.sync.client import connect

DEFAULT_CDP = "http://127.0.0.1:60028"


def http_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Session:
    """一个 CDP 连接 + 事件缓冲。"""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws = connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=10)
        self._id = 0
        self.events: list[dict] = []
        self.timeout = timeout

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def send(self, method: str, params: dict | None = None) -> int:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id

    def wait(self, msg_id: int) -> dict:
        while True:
            data = json.loads(self.ws.recv(timeout=self.timeout))
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP 错误 {data['error']}")
                return data.get("result", {})
            if "method" in data:
                self.events.append(data)

    def call(self, method: str, params: dict | None = None) -> dict:
        return self.wait(self.send(method, params))

    def drain(self, seconds: float = 0.4) -> None:
        """把已经排队的事件收进缓冲（不阻塞太久）。"""
        try:
            while True:
                data = json.loads(self.ws.recv(timeout=seconds))
                if "method" in data:
                    self.events.append(data)
        except TimeoutError:
            return
        except Exception:  # noqa: BLE001  websockets 超时异常类型随版本变化
            return

    def evaluate(self, expr: str, context_id: int | None = None,
                 await_promise: bool = True) -> object:
        params = {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        result = self.call("Runtime.evaluate", params)
        if result.get("exceptionDetails"):
            raise RuntimeError("JS 异常: " + json.dumps(result["exceptionDetails"], ensure_ascii=False)[:800])
        return result.get("result", {}).get("value")


def page_target(page_title: str = "WorkSwarm", base: str = DEFAULT_CDP) -> dict:
    for t in http_json(base, "/json/list"):
        if t.get("type") == "page" and page_title in (t.get("title") or ""):
            return t
    raise SystemExit(f"未找到 title 含 {page_title!r} 的 page target")


def frame_targets(match: str = "", base: str = DEFAULT_CDP) -> list[dict]:
    out = []
    for t in http_json(base, "/json/list"):
        if t.get("type") in ("iframe", "page"):
            if not match or match in (t.get("url") or ""):
                out.append(t)
    return out


def frame_contexts(sess: Session, match: str) -> list[dict]:
    """返回 url 含 match 的 frame 及其默认执行上下文 id。"""
    sess.call("Page.enable")
    sess.call("Runtime.enable")
    sess.drain(0.5)
    tree = sess.call("Page.getFrameTree").get("frameTree", {})
    frames: list[dict] = []

    def walk(node):
        fr = node.get("frame", {})
        frames.append({"frameId": fr.get("id"), "url": fr.get("url"),
                       "origin": fr.get("securityOrigin"), "children": len(node.get("childFrames") or [])})
        for child in node.get("childFrames") or []:
            walk(child)

    walk(tree)
    ctx_by_frame: dict[str, int] = {}
    for ev in sess.events:
        if ev.get("method") == "Runtime.executionContextCreated":
            desc = ev["params"].get("context", {})
            aux = desc.get("auxData") or {}
            if aux.get("isDefault"):
                ctx_by_frame[aux.get("frameId")] = desc.get("id")
    out = []
    for fr in frames:
        if match and match not in (fr.get("url") or ""):
            continue
        fr["executionContextId"] = ctx_by_frame.get(fr["frameId"])
        out.append(fr)
    return out


def eval_in_frame(expr: str, match: str, page_title: str = "WorkSwarm",
                  base: str = DEFAULT_CDP) -> object:
    # 路径 A：iframe 已是独立 target
    for t in http_json(base, "/json/list"):
        if t.get("type") == "iframe" and match in (t.get("url") or ""):
            with Session(t["webSocketDebuggerUrl"]) as sess:
                return sess.evaluate(expr)
    # 路径 B：通过 frame 的执行上下文
    target = page_target(page_title, base)
    with Session(target["webSocketDebuggerUrl"]) as sess:
        frames = frame_contexts(sess, match)
        if not frames:
            raise SystemExit(f"未找到 url 含 {match!r} 的 frame；"
                             f"实际 frame 数={len(sess.events)} 事件")
        frame = frames[0]
        if frame.get("executionContextId") is None:
            raise SystemExit(f"frame {frame.get('url')} 没有默认执行上下文（可能未加载完成）")
        return sess.evaluate(expr, context_id=frame["executionContextId"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--frames", action="store_true", help="列出页面内所有 frame 与上下文 id")
    ap.add_argument("--match", default="")
    ap.add_argument("--expr", default="")
    ap.add_argument("--title", default="WorkSwarm")
    args = ap.parse_args()

    if args.list:
        for t in http_json(DEFAULT_CDP, "/json/list"):
            print(f"{t.get('type'):8s} {t.get('id')}  {(t.get('title') or '')[:20]!r}  {t.get('url')}")
        return 0
    if args.frames:
        target = page_target(args.title)
        with Session(target["webSocketDebuggerUrl"]) as sess:
            for fr in frame_contexts(sess, args.match):
                print(json.dumps(fr, ensure_ascii=False))
        return 0
    if not args.expr:
        ap.print_help()
        return 2
    value = eval_in_frame(args.expr, args.match, page_title=args.title)
    print(json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else value)
    return 0


if __name__ == "__main__":
    sys.exit(main())

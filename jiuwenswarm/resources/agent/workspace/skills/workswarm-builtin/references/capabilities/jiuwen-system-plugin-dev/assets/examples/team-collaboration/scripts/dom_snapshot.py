"""右侧面板只读 DOM 快照（脱敏）。

契约 §5.5 验收口径 2 要求「从真实 DOM 取出两个 iframe 的 src」。本脚本通过宿主
Electron 渲染进程的 CDP 端口读取真实 DOM，输出：
  - 全部 iframe 的 src / title / 位置 / sandbox
  - 页签候选元素文本与选中态
  - 右侧面板容器候选
  - 主题事实（html class / color-scheme / 背景色）

脱敏：会话标识不落原文，只输出 sha256[:8]；不采集聊天正文（只统计长度）。

用法：
    python dom_snapshot.py --json-out dom.json
    python dom_snapshot.py --json-out dom.json --full-text   # 额外导出面板文本（仍脱敏 sid）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp_eval import evaluate, list_targets  # noqa: E402

SID_KEYS = ("sid", "session_id", "sessionId")


def sid_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


JS_DUMP = r"""
(() => {
  const rect = (el) => { try { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; }
    catch (e) { return null; } };
  const short = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n || 60);
  const iframes = Array.from(document.querySelectorAll('iframe')).map((f) => {
    let panel = f.parentElement, depth = 0;
    const chain = [];
    while (panel && depth < 8) {
      chain.push({tag: panel.tagName, cls: short(panel.className, 80)});
      panel = panel.parentElement; depth += 1;
    }
    return {
      src: f.getAttribute('src'), absSrc: f.src, title: f.getAttribute('title'),
      sandbox: f.getAttribute('sandbox'), rect: rect(f), ancestors: chain,
    };
  });
  const tabSel = '[role="tab"], [data-testid*="tab" i], [class*="tab" i]';
  const tabs = Array.from(document.querySelectorAll(tabSel)).slice(0, 60).map((e) => ({
    tag: e.tagName, cls: short(e.className, 80), text: short(e.innerText, 40),
    role: e.getAttribute('role'), selected: e.getAttribute('aria-selected'),
    rect: rect(e),
  }));
  const theme = {
    htmlClass: document.documentElement.className,
    bodyClass: document.body.className,
    colorScheme: getComputedStyle(document.documentElement).colorScheme,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    bodyColor: getComputedStyle(document.body).color,
  };
  return {
    href: location.href, docTitle: document.title,
    viewport: {w: window.innerWidth, h: window.innerHeight},
    iframeCount: iframes.length, iframes, tabCandidates: tabs, theme,
    bodyTextLength: (document.body.innerText || '').length,
  };
})()
"""

JS_PANEL_TEXT = r"""
(() => {
  // 只取包含 iframe 的最近容器链上的文本，避免把聊天正文带出来
  const frames = Array.from(document.querySelectorAll('iframe'));
  if (!frames.length) return null;
  let node = frames[0];
  for (let i = 0; i < 6 && node && node.parentElement; i += 1) {
    node = node.parentElement;
    if (node.querySelectorAll('iframe').length >= frames.length) break;
  }
  return {tag: node && node.tagName, cls: node && String(node.className).slice(0, 120),
          text: node ? (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 800) : null};
})()
"""


def redact(obj):
    """递归脱敏：任何形如 sid=xxx 的串、以及长十六进制/下划线会话 id 都替换为 sha256 前缀。"""
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        def _sub_sid(match: re.Match) -> str:
            key, value = match.group(1), match.group(2)
            return f"{key}=<sid:{sid_digest(value)}>"

        out = re.sub(r"(?i)\b(sid|session_id|sessionId)=([A-Za-z0-9_\-%.]+)", _sub_sid, obj)
        # 裸会话 id：web_<...> 形态（含连字符/下划线后缀，整段替换）
        out = re.sub(r"\bweb_[0-9A-Za-z_\-]{6,}", lambda m: f"<sid:{sid_digest(m.group(0))}>", out)
        # /chat/<会话 id> 路径（界面里唯一的会话标识泄漏点）
        out = re.sub(r"/chat/(?!new\b)([0-9A-Za-z_\-]{6,})",
                     lambda m: f"/chat/<sid:{sid_digest(m.group(1))}>", out)
        return out
    return obj


def extract_sid(src: str) -> str | None:
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(src).query)
    except Exception:  # noqa: BLE001
        return None
    for key in SID_KEYS:
        if key in qs and qs[key]:
            return qs[key][0]
    return None


def collect(full_text: bool = False, page_title: str = "WorkSwarm") -> dict:
    data = evaluate(JS_DUMP, page_title=page_title)
    if full_text:
        data["panelText"] = evaluate(JS_PANEL_TEXT, page_title=page_title)
    # 结构化提取端口与 sid（sid 只留摘要）
    for frame in data.get("iframes") or []:
        src = frame.get("src") or ""
        try:
            parts = urllib.parse.urlsplit(src)
            frame["port"] = parts.port
            frame["path"] = parts.path
            frame["host"] = parts.hostname
            frame["scheme"] = parts.scheme
        except Exception:  # noqa: BLE001
            frame["port"] = None
        sid = extract_sid(src)
        frame["sid_digest"] = sid_digest(sid) if sid else None
        frame.pop("absSrc", None)
    return redact(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default="")
    ap.add_argument("--full-text", action="store_true")
    ap.add_argument("--title", default="WorkSwarm")
    args = ap.parse_args()
    targets = list_targets()
    data = collect(full_text=args.full_text, page_title=args.title)
    payload = {"cdp_targets": [
        {"type": t.get("type"), "title": t.get("title"), "url": redact(t.get("url") or "")}
        for t in targets
    ], "dom": data}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text)
    summary = {
        "href": data.get("href"),
        "iframeCount": data.get("iframeCount"),
        "iframes": [{"src": f.get("src"), "title": f.get("title"), "port": f.get("port"),
                     "path": f.get("path"), "sid_digest": f.get("sid_digest")}
                    for f in (data.get("iframes") or [])],
        "tabTexts": sorted({(t.get("text") or "") for t in (data.get("tabCandidates") or []) if t.get("text")})[:20],
        "theme": data.get("theme"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

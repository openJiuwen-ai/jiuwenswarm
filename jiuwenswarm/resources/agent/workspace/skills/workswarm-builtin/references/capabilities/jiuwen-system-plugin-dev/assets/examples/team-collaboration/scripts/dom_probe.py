"""一次性探查：导出宿主可点击控件与右栏结构到 JSON（供人工核对选择器）。

只读：不点击、不修改 DOM。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cdp_frame as cdf  # noqa: E402

EXPR = r"""
(function(){
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const all = Array.from(document.querySelectorAll('button,[role=button],[role=tab],[aria-expanded],[aria-label]'));
  const items = all.filter(visible).slice(0, 120).map(el => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      role: el.getAttribute('role'),
      text: (el.textContent || '').trim().slice(0, 20),
      label: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      expanded: el.getAttribute('aria-expanded'),
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    };
  });
  return JSON.stringify({
    path: location.pathname + location.search,
    viewport: {w: innerWidth, h: innerHeight},
    iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.getAttribute('src') || f.src),
    tablists: document.querySelectorAll('[role=tablist]').length,
    tabs: document.querySelectorAll('[role=tab]').length,
    items: items
  });
})()
"""


def main() -> int:
    """Dump the clickable controls of the WorkSwarm page to ``dom_probe.json``."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    target = cdf.page_target("WorkSwarm")
    with cdf.Session(target["webSocketDebuggerUrl"]) as sess:
        raw = sess.evaluate(EXPR)
    data = json.loads(raw)
    out = Path(__file__).resolve().parents[3] / "qa-verifier" / "dom_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} items={len(data['items'])} iframes={len(data['iframes'])} path={data['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

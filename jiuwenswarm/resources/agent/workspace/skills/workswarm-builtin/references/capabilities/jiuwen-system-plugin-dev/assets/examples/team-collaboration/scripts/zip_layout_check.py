"""最终 ZIP 的包布局与清单校验（不执行包代码）。

校验项（契约 §7 + recon §2.1 + skill 打包要求）：
  1. ZIP 根目录直接是 manifest.yaml，不多包一层目录；
  2. manifest 字段：schemaVersion==1、package.id/version 合法、extensions[].module 在包内真实存在；
  3. ui.apiVersion==1、两条 web.slot.contribution（id/slot/order/renderer 固定）；
  4. 契约 §7 要求的文件全部存在（后端模块 + web 两页 + README）；
  5. 无缓存/开发残留（__pycache__、*.pyc、.git、.venv、node_modules、*.log）；
  6. 无绝对路径、无 .. 条目、无符号链接；
  7. tests/integration_*.py 与 scripts/** 已随包（本案由 QA 提供，缺失即告警）。

用法：
    python zip_layout_check.py <zip路径> --json-out report.json
退出码：0 全部通过；1 有失败项。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import PurePosixPath

import yaml

PKG_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

PACKAGE_ID = "webui.team-collab-viz"
SLOT = "conversation.panel.right"
REQUIRED_CONTRIBUTIONS = {
    "tcv.team-info": 10,
    "tcv.task-graph": 11,
}
REQUIRED_BACKEND_MODULES = (
    "team_collab_viz/__init__.py",
    "team_collab_viz/extension.py",
    "team_collab_viz/snapshot.py",
    "team_collab_viz/sessionmap.py",
    "team_collab_viz/ui_state.py",
    "team_collab_viz/http_server.py",
    "team_collab_viz/lifecycle.py",
)
REQUIRED_WEB = (
    "web/team-info.html",
    "web/task-graph.html",
)
FORBIDDEN_PARTS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".mypy_cache", "logs"}
FORBIDDEN_SUFFIX = (".pyc", ".pyo", ".log", ".zip", ".bak", ".orig")

RESULTS: list[dict] = []


def check(case: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"case": case, "ok": bool(ok), "detail": detail})
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    if not check("zip:文件存在", __import__("pathlib").Path(args.zip_path).is_file(), args.zip_path):
        print(json.dumps({"total": len(RESULTS), "failed": 1, "failures": RESULTS}, ensure_ascii=False))
        return 1
    if not check("zip:是合法 ZIP", zipfile.is_zipfile(args.zip_path), args.zip_path):
        return 1

    with zipfile.ZipFile(args.zip_path) as zf:
        names = zf.namelist()
        infos = zf.infolist()
        manifest_raw = zf.read("manifest.yaml").decode("utf-8") if "manifest.yaml" in names else ""

    # 1) 根布局
    check("layout:ZIP 根含 manifest.yaml", "manifest.yaml" in names,
          f"前 10 项={names[:10]}")
    top_dirs = sorted({n.split("/")[0] for n in names if "/" in n})
    check("layout:不存在多包一层目录（manifest 不在子目录）",
          not any(n.endswith("/manifest.yaml") for n in names if n != "manifest.yaml"),
          f"顶层目录={top_dirs}")

    # 6) 路径安全
    unsafe = [n for n in names if n.startswith("/") or ".." in PurePosixPath(n).parts or "\\" in n]
    check("layout:无绝对路径/.. /反斜杠条目", not unsafe, f"{unsafe[:5]}")

    # 5) 残留文件
    bad_parts = [n for n in names if FORBIDDEN_PARTS & set(PurePosixPath(n).parts)]
    check("layout:无缓存/开发残留目录", not bad_parts, f"{bad_parts[:5]}")
    bad_suffix = [n for n in names if n.lower().endswith(FORBIDDEN_SUFFIX)]
    check("layout:无 .pyc/.log 等残留文件", not bad_suffix, f"{bad_suffix[:5]}")

    # 2) manifest 字段
    try:
        manifest = yaml.safe_load(manifest_raw) if manifest_raw else None
        parsed = isinstance(manifest, dict)
    except Exception as exc:  # noqa: BLE001
        manifest, parsed = None, False
        check("manifest:YAML 可解析", False, str(exc))
    if parsed:
        check("manifest:YAML 可解析", True, "")
        check("manifest:schemaVersion==1", manifest.get("schemaVersion") == 1,
              f"schemaVersion={manifest.get('schemaVersion')!r}")
        pkg = manifest.get("package") or {}
        pkg_id = pkg.get("id")
        check(f"manifest:package.id=={PACKAGE_ID}", pkg_id == PACKAGE_ID, f"id={pkg_id!r}")
        check("manifest:package.id 符合 ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
              bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", pkg_id or "")), f"id={pkg_id!r}")
        check("manifest:package.version 非空", bool(str(pkg.get("version") or "").strip()),
              f"version={pkg.get('version')!r}")
        check("manifest:package.displayName 非空", bool(str(pkg.get("displayName") or "").strip()),
              f"displayName={pkg.get('displayName')!r}")
        check("manifest:package.description 非空",
              bool(str(pkg.get("description") or "").strip()),
              f"description={pkg.get('description')!r}")
        check("manifest:domain 为 agent 或缺省（不得是 gateway）",
              manifest.get("domain") in (None, "", "agent"), f"domain={manifest.get('domain')!r}")

        exts = manifest.get("extensions") or []
        check("manifest:extensions 非空且为 list", isinstance(exts, list) and len(exts) > 0, f"{exts}")
        modules: list[str] = []
        for ext in exts if isinstance(exts, list) else []:
            module = (ext or {}).get("module") or ""
            modules.append(module)
            check(f"manifest:extension module 存在且非 __init__.py（{module}）",
                  bool(module) and not module.endswith("__init__.py"), f"module={module!r}")
            check(f"manifest:extension runtime==backend（{module}）",
                  (ext or {}).get("runtime") == "backend", f"runtime={(ext or {}).get('runtime')!r}")
            in_zip = module in names
            check(f"manifest:extension 入口在包内真实存在（{module}）", in_zip,
                  f"存在={in_zip}")
            check(f"manifest:extension 入口路径段不含点（{module}）",
                  not any("." in seg for seg in module.split("/")[:-1]), f"module={module}")

        ui = manifest.get("ui") or {}
        check("manifest:ui.apiVersion==1", ui.get("apiVersion") == 1, f"apiVersion={ui.get('apiVersion')!r}")
        items = ui.get("items") or []
        check("manifest:ui.items 为 list 且至少 2 条", isinstance(items, list) and len(items) >= 2,
              f"items={items}")
        by_id = {}
        for item in items if isinstance(items, list) else []:
            item = item or {}
            item_id = item.get("id")
            by_id[item_id] = item
            check(f"manifest:ui item type 合法（{item_id}）",
                  item.get("type") == "web.slot.contribution", f"type={item.get('type')!r}")
            check(f"manifest:ui item slot=={SLOT}（{item_id}）", item.get("slot") == SLOT,
                  f"slot={item.get('slot')!r}")
            renderer = item.get("renderer") or {}
            check(f"manifest:ui item renderer.kind==webui-tree（{item_id}）",
                  renderer.get("kind") == "webui-tree", f"renderer={renderer}")
        for item_id, order in REQUIRED_CONTRIBUTIONS.items():
            item = by_id.get(item_id)
            check(f"manifest:存在贡献 {item_id}", item is not None, f"实际={sorted(by_id)}")
            if item is not None:
                check(f"manifest:贡献 {item_id} order=={order}", item.get("order") == order,
                      f"order={item.get('order')!r}")

    # 4) 必需文件
    for rel in REQUIRED_BACKEND_MODULES:
        check(f"files:后端模块 {rel}", rel in names, "")
    for rel in REQUIRED_WEB:
        check(f"files:前端页面 {rel}", rel in names, "")
    check("files:README.md 存在", "README.md" in names, "")
    check("files:docs/ 说明存在", any(n.startswith("docs/") for n in names),
          f"docs 项={[n for n in names if n.startswith('docs/')][:5]}")
    check("files:tests/ 目录存在", any(n.startswith("tests/") for n in names), "")
    check("files:scripts/ 目录存在（QA 工具随包）", any(n.startswith("scripts/") for n in names),
          f"scripts 项={[n for n in names if n.startswith('scripts/')][:5]}")
    integration = [n for n in names if re.match(r"tests/integration_.*\.py$", n)]
    check("files:tests/integration_*.py 存在（QA 交付）", bool(integration), f"{integration}")
    check("files:tests/unit_*.py 存在（后端交付）",
          any(re.match(r"tests/unit_.*\.py$", n) for n in names),
          f"{[n for n in names if n.startswith('tests/unit')]}")
    web_assets = [n for n in names if n.startswith("web/assets/")]
    check("files:web/assets/ 有前端静态资源", bool(web_assets), f"{web_assets[:8]}")
    for need in ("app.js", "styles.css"):
        check(f"files:web/assets 含 {need}", any(n.endswith("/" + need) for n in web_assets),
              f"{web_assets[:8]}")

    # 7) 与测过的源码同源：zip sha256 + zip 内 .py / web 指纹 == 工作树指纹
    zip_sha = hashlib.sha256(__import__("pathlib").Path(args.zip_path).read_bytes()).hexdigest()
    check("同源:ZIP sha256 已记录", True, f"sha256={zip_sha}")
    tree_py = {p.relative_to(PKG_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
               for p in sorted((PKG_ROOT / "team_collab_viz").rglob("*.py"))}
    tree_web = {p.relative_to(PKG_ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                for p in sorted((PKG_ROOT / "web").rglob("*")) if p.is_file()}
    with zipfile.ZipFile(args.zip_path) as zf:
        zpy = {n: hashlib.sha256(zf.read(n)).hexdigest()[:16]
               for n in zf.namelist() if re.match(r"team_collab_viz/.*\.py$", n)}
        zweb = {n: hashlib.sha256(zf.read(n)).hexdigest()[:16]
                for n in zf.namelist() if n.startswith("web/") and not n.endswith("/")}
    check(f"同源:ZIP 内后端 .py 数=工作树（{len(tree_py)}）", set(zpy) == set(tree_py),
          f"仅ZIP={sorted(set(zpy) - set(tree_py))} 仅工作树={sorted(set(tree_py) - set(zpy))}")
    diff = {n: (zpy.get(n), tree_py[n]) for n in tree_py if zpy.get(n) != tree_py[n]}
    check("同源:ZIP 内 .py 指纹 逐文件 == 工作树（证明测过的代码就是打进去的代码）",
          not diff, f"指纹不一致={diff}")
    print("[fingerprint] zip_py=" + json.dumps(zpy, ensure_ascii=False), flush=True)
    check(f"同源:ZIP 内前端文件数=工作树（{len(tree_web)}）", set(zweb) == set(tree_web),
          f"仅ZIP={sorted(set(zweb) - set(tree_web))} 仅工作树={sorted(set(tree_web) - set(zweb))}")
    wdiff = {n: (zweb.get(n), tree_web[n]) for n in tree_web if zweb.get(n) != tree_web[n]}
    check("同源:ZIP 内 web/** 指纹 逐文件 == 工作树（UI 版本可追溯）", not wdiff, f"指纹不一致={wdiff}")
    print("[fingerprint] zip_web=" + json.dumps(zweb, ensure_ascii=False), flush=True)

    # 统计
    total_bytes = sum(i.file_size for i in infos)
    report = {
        "zip": args.zip_path,
        "zip_sha256": zip_sha,
        "source_fingerprint_sha256_16": tree_py,
        "web_fingerprint_sha256_16": tree_web,
        "entries": len(names),
        "uncompressed_bytes": total_bytes,
        "total": len(RESULTS),
        "passed": sum(1 for r in RESULTS if r["ok"]),
        "failed": sum(1 for r in RESULTS if not r["ok"]),
        "failures": [r for r in RESULTS if not r["ok"]],
        "cases": RESULTS,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"RESULT entries={report['entries']} bytes={total_bytes} "
          f"total={report['total']} passed={report['passed']} failed={report['failed']}")
    for f in report["failures"]:
        print(f"  [FAIL] {f['case']} | {f['detail']}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())

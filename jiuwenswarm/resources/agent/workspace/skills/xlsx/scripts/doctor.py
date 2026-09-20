#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doctor.py - Environment & dependency self-check for the XLSX skill.

Verifies the tools this skill relies on, with special attention to
Chinese / CJK support (encoding libs + a CJK-capable font for rendering).

Usage:
    python3 doctor.py [--json]

Exit code 0 = all required checks passed; 1 = something required is missing.
"""
import argparse
import importlib
import json
import shutil
import subprocess
import sys

# (module, pip-name, required?)
PY_DEPS = [
    ("openpyxl", "openpyxl", True),
    ("pandas", "pandas", True),
    ("lxml", "lxml", False),
]

# External CLI tools used for recalculation / rendering.
CLI_TOOLS = [
    ("soffice", "libreoffice", True),    # recalc + render to PDF
    ("pdftoppm", "poppler-utils", True), # PDF -> PNG
]

# Fonts that can render CJK glyphs. Any one is enough.
CJK_FONT_HINTS = [
    "Noto Sans CJK", "Noto Serif CJK", "Source Han", "WenQuanYi",
    "Microsoft YaHei", "SimSun", "SimHei", "PingFang", "Heiti", "Songti",
]


def check_python(results):
    ok = sys.version_info >= (3, 8)
    results.append({
        "check": "python>=3.8",
        "ok": ok,
        "detail": sys.version.split()[0],
        "required": True,
    })


def check_py_deps(results):
    for mod, pip_name, required in PY_DEPS:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            results.append({"check": f"py:{mod}", "ok": True, "detail": ver, "required": required})
        except Exception as e:  # noqa: BLE001
            results.append({
                "check": f"py:{mod}", "ok": False,
                "detail": f"missing ({e}); install: pip install {pip_name}",
                "required": required,
            })


def check_cli(results):
    for tool, pkg, required in CLI_TOOLS:
        path = shutil.which(tool)
        results.append({
            "check": f"cli:{tool}", "ok": path is not None,
            "detail": path or f"missing; install: {pkg}",
            "required": required,
        })


def check_cjk_font(results):
    """A CJK font is required for correct rendering of Chinese text."""
    found = []
    fc = shutil.which("fc-list")
    if fc:
        try:
            out = subprocess.run([fc, ":lang=zh", "family"],
                                 capture_output=True, text=True, timeout=20).stdout
            families = {line.strip() for line in out.splitlines() if line.strip()}
            found = sorted(families)[:8]
        except Exception:  # noqa: BLE001
            found = []
    if not found:
        # Fall back to a name-hint scan over all fonts.
        if fc:
            try:
                allf = subprocess.run([fc, "family"], capture_output=True, text=True, timeout=20).stdout
                found = sorted({h for h in CJK_FONT_HINTS if h.lower() in allf.lower()})
            except Exception:  # noqa: BLE001
                pass
    results.append({
        "check": "font:CJK",
        "ok": bool(found),
        "detail": (", ".join(found) if found else
                   "no CJK font found; install e.g. google-noto-sans-cjk-fonts "
                   "(Chinese text will render as boxes in PNG/PDF)"),
        "required": True,
    })


def main():
    ap = argparse.ArgumentParser(description="XLSX skill environment self-check")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    results = []
    check_python(results)
    check_py_deps(results)
    check_cli(results)
    check_cjk_font(results)

    required_failed = [r for r in results if r["required"] and not r["ok"]]
    ok = not required_failed

    if args.json:
        print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False, indent=2))
    else:
        print("XLSX skill - environment self-check")
        print("=" * 44)
        for r in results:
            mark = "PASS" if r["ok"] else ("FAIL" if r["required"] else "warn")
            star = "" if r["required"] else " (optional)"
            print(f"[{mark}] {r['check']}{star}: {r['detail']}")
        print("=" * 44)
        print("RESULT:", "ready" if ok else "missing required components (see FAIL above)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

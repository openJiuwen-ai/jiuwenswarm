# -*- coding: utf-8 -*-
"""sci-paper-fars 依赖自检:确认 openai / dotenv / numpy / matplotlib / pdflatex 可用。"""
import importlib.util
import logging
import shutil
import sys
from pathlib import Path


def _find_pdflatex() -> str | None:
    found = shutil.which("pdflatex")
    if found:
        return found
    for base in (
        Path.home() / "AppData" / "Local" / "Programs" / "MiKTeX",
        Path("C:/Program Files") / "MiKTeX",
    ):
        exe = base / "miktex" / "bin" / "x64" / "pdflatex.exe"
        if exe.exists():
            return str(exe)
    return None


def check() -> bool:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("sci_paper_fars.setup_check")
    log.info("=== sci-paper-fars 依赖检查 ===")
    ok = True
    for mod in ("openai", "dotenv", "numpy", "matplotlib"):
        if importlib.util.find_spec(mod) is not None:
            log.info("  [OK] %s", mod)
        else:
            log.info("  [缺] %s 未安装", mod)
            ok = False
    pdflatex = _find_pdflatex()
    if pdflatex:
        log.info("  [OK] pdflatex: %s", pdflatex)
    else:
        log.info("  [缺] pdflatex 未找到(需 MiKTeX/TeX Live)")
        ok = False
    env_path = Path.home() / ".jiuwenswarm" / "config" / ".env"
    if env_path.exists():
        log.info("  [OK] 模型配置 %s 存在", env_path)
    else:
        log.info("  [缺] 模型配置 %s 不存在", env_path)
        ok = False
    if ok:
        log.info("全部就绪。")
    return ok


if __name__ == "__main__":
    sys.exit(0 if check() else 1)

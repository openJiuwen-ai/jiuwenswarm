#!/usr/bin/env python3
# setup.py — local-doc-ocr 环境自检与依赖自动安装
# --------------------------------------------------------------------------
# 透明原则：仅安装下方 PACKAGES 白名单内的声明依赖，不安装任何未知包；
# 仅在检测到缺失时才联网（优先清华镜像，PyPI 官方兜底）安装一次。
# 装好之后本技能全程离线，不再联网。
# --------------------------------------------------------------------------
import importlib.util
import subprocess
import sys

# import 名 -> pip 包名（与 requirements.txt 一致）
PACKAGES = {
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "fitz": "pymupdf",
    "PIL": "pillow",
    "numpy": "numpy",
}

_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    None,  # PyPI 官方兜底
]


def _install_one(pkg):
    for mirror in _MIRRORS:
        cmd = [sys.executable, "-m", "pip", "install",
               "--retries", "5", "--timeout", "60", pkg]
        if mirror:
            cmd += ["-i", mirror]
        print(f"[setup] 正在安装 {pkg}（源：{mirror or 'PyPI 官方'}）...")
        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError:
            print("[setup] 该镜像安装失败，尝试下一个源。")
    return False


def ensure_deps():
    """检测并自动安装缺失依赖。返回 True 表示环境就绪。"""
    missing = [pkg for mod, pkg in PACKAGES.items()
               if importlib.util.find_spec(mod) is None]
    if not missing:
        print("[setup] 依赖已就绪。")
        return True
    print(f"[setup] 检测到缺失依赖：{', '.join(missing)}")
    print("[setup] 即将从 PyPI 自动安装（仅声明过的白名单依赖，需联网一次）。")
    ok = all(_install_one(pkg) for pkg in missing)
    if ok:
        print("[setup] 依赖安装完成，环境就绪。")
    else:
        print("[setup] 部分依赖安装失败，请手动执行：pip install -r requirements.txt")
    return ok


if __name__ == "__main__":
    sys.exit(0 if ensure_deps() else 1)

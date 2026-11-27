# -*- mode: python ; coding: utf-8 -*-
r"""JiuwenClaw PyInstaller 打包配置。

构建前请先：
1. 安装依赖: uv sync --extra dev
2. 构建前端: cd jiuwenclaw/web && npm run build
3. 执行打包: .\scripts\build-exe.ps1  或  uv run pyinstaller scripts/jiuwenclaw.spec
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None

SPEC_DIR = os.path.abspath(globals().get("SPECPATH", os.getcwd()))
project_root = os.path.abspath(os.path.join(SPEC_DIR, os.pardir))

try:
    webview_datas = collect_data_files("webview")
except Exception as exc:
    raise SystemExit(
        "错误: 当前虚拟环境缺少 pywebview，请先安装后再打包。"
        "例如: pip install pywebview 或 uv sync --extra dev"
    ) from exc

# 只显式打包当前平台会用到的 pywebview 模块，
# 避免 collect_submodules("webview") 把 Android/Kivy 等后端也扫描进来。
webview_hiddenimports = [
    "webview",
    "webview.guilib",
    "webview.http",
    "webview.errors",
    "webview.event",
    "webview.localization",
    "webview.menu",
    "webview.screen",
    "webview.util",
    "webview.window",
]
if sys.platform == "win32":
    webview_hiddenimports.extend([
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ])
elif sys.platform == "darwin":
    webview_hiddenimports.extend([
        "webview.platforms.cocoa",
    ])

# 检查前端是否已构建
web_dist = os.path.join(project_root, "jiuwenclaw", "web", "dist")
if not os.path.isdir(web_dist) or not os.listdir(web_dist):
    raise SystemExit(
        "错误: 请先构建前端。执行: cd jiuwenclaw/web && npm install && npm run build"
    )

# 数据文件：resources（含 agent 模板）、前端构建产物
datas = webview_datas + [
    (os.path.join(project_root, "jiuwenclaw", "resources"), "jiuwenclaw/resources"),
    (os.path.join(project_root, "jiuwenclaw", "web", "dist"), "jiuwenclaw/web/dist"),
]
datas += copy_metadata("fastmcp", recursive=True)
datas += copy_metadata("mcp", recursive=True)
datas += copy_metadata("openjiuwen", recursive=True)

# 部分包需要显式声明隐藏导入
hiddenimports = webview_hiddenimports + [
    "pandas",  # pymilvus 依赖
    "tiktoken_ext",  # tiktoken 编码插件（cl100k_base 等）
    "tiktoken_ext.openai_public",
    "ruamel.yaml",
    "ruamel.yaml.reader",
    "ruamel.yaml.representer",
    "ruamel.yaml.nodes",
    "chromadb",
    "chromadb.config",
    "chromadb.telemetry",
    "openjiuwen",
    "psutil",
    "aiosqlite",
    "croniter",
    "websockets",
    "loguru",
    "dotenv",
    "webview",
    "jiuwenclaw.app_web",  # 静态文件服务
    "jiuwenclaw.desktop_app",  # 桌面入口
]

# 排除不需要的模块以减小体积（pandas 为 pymilvus/openjiuwen 所需，不可排除）
excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "numpy.tests",
]

# 入口脚本位于 scripts 目录
entry_script = os.path.join(project_root, "scripts", "jiuwenclaw_exe_entry.py")

a = Analysis(
    [entry_script],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name="jiuwenclaw",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    exclude_binaries=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="jiuwenclaw",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="JiuwenClaw.app",
        icon=None,
        bundle_identifier="com.jiuwenclaw.desktop",
        info_plist={
            "CFBundleName": "JiuwenClaw",
            "CFBundleDisplayName": "JiuwenClaw",
            "CFBundleShortVersionString": "0.1.7",
            "CFBundleVersion": "0.1.7",
            "NSHighResolutionCapable": "True",
        },
    )

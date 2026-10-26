# -*- mode: python ; coding: utf-8 -*-
r"""JiuwenSwarm PyInstaller 打包配置。

构建前请先：
1. 安装依赖: uv sync --extra dev
2. 构建前端: cd jiuwenswarm/channels/web/frontend && npm run build
3. 执行打包: .\scripts\build-exe.ps1  或  uv run pyinstaller scripts/jiuwenswarm.spec
"""

import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

block_cipher = None

SPEC_DIR = os.path.abspath(globals().get("SPECPATH", os.getcwd()))
project_root = os.path.abspath(os.path.join(SPEC_DIR, os.pardir))
symphony_root = os.path.join(project_root, "jiuwenswarm", "symphony")
if symphony_root not in sys.path:
    sys.path.insert(0, symphony_root)

DATA_FILE_PATTERNS = ["**/*.yaml", "**/*.yml", "**/*.json", "**/*.md"]
EXTENSION_DATA_FILE_PATTERNS = ["**/*.py", *DATA_FILE_PATTERNS]
DISPATCH_PACKAGE_ROOTS = ("indexing", "models", "orchestration", "retrieval", "shared")
EXCLUDED_RESOURCE_DIRS = (
    os.path.join("agent", "workspace", "skills", "project-maintainer"),
)
OPENJIUWEN_DATA_EXCLUDES = [
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/deepagents/tools/browser_move/.browser/**",
    "**/deepagents/tools/browser_move/.browser-profiles/**",
    "**/deepagents/tools/browser_move/.venv/**",
    "**/deepagents/tools/browser_move/logs/**",
    "**/deepagents/tools/browser_move/.env",
]


def collect_tree_data_files(source_dir, target_dir, patterns):
    data_files = []
    for pattern in patterns:
        full_pattern = os.path.join(source_dir, pattern)
        for path in glob.glob(full_pattern, recursive=True):
            if not os.path.isfile(path):
                continue
            rel_dir = os.path.dirname(os.path.relpath(path, source_dir))
            dest_dir = os.path.normpath(os.path.join(target_dir, rel_dir))
            data_files.append((path, dest_dir))
    return data_files


def collect_resources_data_files(source_dir, target_dir):
    data_files = []
    excluded_dirs = {
        os.path.normcase(os.path.abspath(os.path.join(source_dir, rel_path)))
        for rel_path in EXCLUDED_RESOURCE_DIRS
    }

    for root, dirs, files in os.walk(source_dir):
        root_abs = os.path.normcase(os.path.abspath(root))
        dirs[:] = [
            dirname for dirname in dirs
            if os.path.normcase(os.path.abspath(os.path.join(root, dirname))) not in excluded_dirs
        ]
        if any(root_abs == excluded or root_abs.startswith(excluded + os.sep) for excluded in excluded_dirs):
            continue

        # 这里 root 本身就是目录，dirname 会砍掉最后一级，导致
        # skills/<name>/SKILL.md 被错误放到 skills/SKILL.md，无子目录的技能整个消失。
        rel_dir = os.path.relpath(root, source_dir)
        if rel_dir == ".":
            rel_dir = ""
        dest_dir = os.path.normpath(os.path.join(target_dir, rel_dir))
        for filename in files:
            data_files.append((os.path.join(root, filename), dest_dir))

    return data_files


def collect_tree_python_modules(source_dir, package_roots):
    modules = []
    for package_root in package_roots:
        package_dir = os.path.join(source_dir, *package_root.split("."))
        for path in glob.glob(os.path.join(package_dir, "**", "*.py"), recursive=True):
            rel_path = os.path.relpath(path, source_dir)
            module_name = os.path.splitext(rel_path)[0].replace(os.sep, ".")
            if module_name.endswith(".__init__"):
                module_name = module_name[: -len(".__init__")]
            modules.append(module_name)
    return sorted(set(modules))

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
web_dist = os.path.join(project_root, "jiuwenswarm", "channels", "web", "frontend", "dist")
if not os.path.isdir(web_dist) or not os.listdir(web_dist):
    raise SystemExit(
        "错误: 请先构建前端。执行: cd jiuwenswarm/channels/web/frontend && npm install && npm run build"
    )

# 数据文件：resources（含 agent 模板）、前端构建产物
datas = webview_datas + [
    (os.path.join(project_root, "jiuwenswarm", "channels", "web", "frontend", "dist"), "jiuwenswarm/channels/web/frontend/dist"),
]
datas += collect_resources_data_files(
    os.path.join(project_root, "jiuwenswarm", "resources"),
    "jiuwenswarm/resources",
)
datas += copy_metadata("fastmcp", recursive=True)
datas += copy_metadata("mcp", recursive=True)
datas += copy_metadata("openjiuwen", recursive=True)
datas += collect_data_files("openjiuwen", include_py_files=False, excludes=OPENJIUWEN_DATA_EXCLUDES)
datas += collect_data_files(
    "a2ui",
    include_py_files=False,
    includes=["assets/0.8/*.json"],
)
datas += collect_data_files(
    "jiuwenswarm.extensions",
    include_py_files=True,
    includes=EXTENSION_DATA_FILE_PATTERNS,
)
datas += collect_data_files(
    "jiuwenswarm.symphony",
    include_py_files=False,
    includes=DATA_FILE_PATTERNS,
)
for package_root in DISPATCH_PACKAGE_ROOTS:
    datas += collect_tree_data_files(
        os.path.join(symphony_root, package_root),
        package_root,
        DATA_FILE_PATTERNS,
    )

# openjiuwen 使用动态导入，需要收集全部子模块
openjiuwen_submodules = collect_submodules("openjiuwen")
symphony_submodules = collect_submodules("jiuwenswarm.symphony")
dispatch_submodules = collect_tree_python_modules(symphony_root, DISPATCH_PACKAGE_ROOTS)

# 部分包需要显式声明隐藏导入
hiddenimports = webview_hiddenimports + [
    "pandas",  # pymilvus 依赖
    "tiktoken_ext",  # tiktoken 编码插件（cl100k_base 等）
    "tiktoken_ext.openai_public",
    "ruamel.yaml",
    "ruamel.yaml.reader",
    "ruamel.yaml.representer",
    "ruamel.yaml.nodes",
    "openjiuwen",
    "psutil",
    "aiosqlite",
    "croniter",
    "websockets",
    "loguru",
    "dotenv",
    "webview",
    "jiuwenswarm.channels.web.app_web",  # 静态文件服务
    "jiuwenswarm.channels.web.desktop_app",  # 桌面入口
] + openjiuwen_submodules + symphony_submodules + dispatch_submodules

# 排除不需要的模块以减小体积（pandas 为 pymilvus/openjiuwen 所需，不可排除）
excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "numpy.tests",
    # 测试框架辅助包（pytest 本体已 collect 进 PYZ）
    "tox",
    "hypothesis",
    "mock",
    "coverage",
]

# 入口脚本位于 scripts 目录
entry_script = os.path.join(project_root, "scripts", "jiuwenswarm_exe_entry.py")

# 图标路径（Windows 用 .ico，macOS 用 .icns）
icon_path = os.path.join(
    project_root, "jiuwenswarm", "channels", "web", "frontend", "public",
    "logo.ico" if sys.platform == "win32" else "logo.icns",
)

# Bundle the standalone ruff binary so that `python -m ruff` works inside
# the frozen exe. The `ruff` PyPI package is a thin Python wrapper around
# this native binary; the wrapper module is not collected into the PYZ, so
# the exe entry (jiuwenswarm_exe_entry.py) forwards `-m ruff` directly to
# the binary placed here.
import sysconfig as _sysconfig
_bundled_binaries = []
_ruff_suffix = ".exe" if sys.platform == "win32" else ""
_ruff_scripts_dir = _sysconfig.get_path("scripts")
_ruff_candidates = []
if _ruff_scripts_dir:
    _ruff_candidates.append(os.path.join(_ruff_scripts_dir, "ruff" + _ruff_suffix))
_exe_dir = os.path.dirname(sys.executable) if sys.executable else None
if _exe_dir:
    _scripts_subdir = "Scripts" if sys.platform == "win32" else "bin"
    _ruff_candidates.append(os.path.join(_exe_dir, _scripts_subdir, "ruff" + _ruff_suffix))
for _c in _ruff_candidates:
    if _c and os.path.isfile(_c):
        _bundled_binaries.append((_c, "."))
        break
if not _bundled_binaries:
    print("WARNING: ruff binary not found in venv; auto-harness lint will be "
          "unavailable in the frozen exe (install ruff in the build venv)")


# Bundle pytest (pure-Python) so that `python -m pytest` works inside the
# frozen exe. the frozen exe's -m branch uses runpy,
# which resolves pytest from the PYZ once collected here.
_pytest_datas, _pytest_binaries, _pytest_hidden = collect_all("pytest")
_pa_datas, _pa_binaries, _pa_hidden = collect_all("pytest_asyncio")
_py_datas, _py_binaries, _py_hidden = collect_all("py")
datas += _pytest_datas + _pa_datas + _py_datas
hiddenimports += _pytest_hidden + _pa_hidden + _py_hidden
_bundled_binaries = _bundled_binaries + _pytest_binaries + _pa_binaries + _py_binaries

# Bundle mypy so that `python -m mypy` works inside the frozen exe.
# `sys.executable -m mypy`. mypy ships mypyc-compiled .pyd extensions plus
# typeshed .pyi data; collect_all grabs all of them.
# NOTE: mypyc extensions in a frozen env are not guaranteed — verify with
# `jiuwenswarm.exe -m mypy --version` after a build; if it fails, drop this
# block and rely on ci_gate_runner's optional-tool skip path instead.
_mypy_datas, _mypy_binaries, _mypy_hidden = collect_all("mypy")
datas += _mypy_datas
hiddenimports += _mypy_hidden
_bundled_binaries = _bundled_binaries + _mypy_binaries

# mypyc shared extension (.pyd) lives at site-packages root, not inside the
# mypy/ package, so collect_all("mypy") misses it; collect explicitly or
# `python -m mypy` fails with ModuleNotFoundError on the hashed mypyc module.
import glob as _glob
_mypyc_binaries = []
_sp_dir = _sysconfig.get_paths().get("purelib")
if _sp_dir:
    for _pyd in _glob.glob(os.path.join(_sp_dir, "*__mypyc*.pyd")):
        _mypyc_binaries.append((_pyd, "."))
_bundled_binaries = _bundled_binaries + _mypyc_binaries

# Bundle chromadb fully. chromadb 1.x is config-driven: it stores fully-qualified
# class names as strings (chromadb.config.Settings.chroma_api_impl defaults to
# "chromadb.api.rust.RustBindingsAPI", chroma_product_telemetry_impl to
# "chromadb.telemetry.product.posthog.Posthog") and resolves them at runtime via
# importlib.import_module(). PyInstaller's static analysis cannot follow these
# string references, so manually listing "chromadb"/"chromadb.config"/
# "chromadb.telemetry" misses the api.rust and telemetry.product.posthog
# submodules — OpenJiuwenMemoryProvider then crashes on vector-store init with
# ModuleNotFoundError and falls back to no-Provider. collect_all grabs every
# submodule + data file (log_config.yml, migrations/, etc.) in one shot.
_chroma_datas, _chroma_binaries, _chroma_hidden = collect_all("chromadb")
datas += _chroma_datas
hiddenimports += _chroma_hidden
_bundled_binaries = _bundled_binaries + _chroma_binaries

# chromadb.api.rust does `import chromadb_rust_bindings` — a C extension shipping
# a .pyd under a top-level package of the same name. collect_all("chromadb")
# won't touch a separate top-level package, so collect it explicitly or the rust
# API path raises ModuleNotFoundError on the .pyd at runtime.
_rust_datas, _rust_binaries, _rust_hidden = collect_all("chromadb_rust_bindings")
datas += _rust_datas
hiddenimports += _rust_hidden
_bundled_binaries = _bundled_binaries + _rust_binaries

a = Analysis(
    [entry_script],
    pathex=[project_root, symphony_root],
    binaries=_bundled_binaries,
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
    name="jiuwenswarm",
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
    icon=icon_path,
    # 运行时数据全部落在用户目录 ~/.jiuwenswarm（cwd、单实例锁、日志、工作区），
    # 不写 Program Files / HKLM，因此运行时不需要管理员权限。
    # uac_admin=True 会让双击 exe 时弹 UAC，去掉它实现"安装需管理员、运行不需要"。
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="jiuwenswarm",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="JiuwenSwarm.app",
        icon=icon_path,
        bundle_identifier="com.jiuwenswarm.desktop",
        info_plist={
            "CFBundleName": "JiuwenSwarm",
            "CFBundleDisplayName": "JiuwenSwarm",
            "CFBundleShortVersionString": "0.2.3",
            "CFBundleVersion": "0.2.3",
            "NSHighResolutionCapable": "True",
        },
    )

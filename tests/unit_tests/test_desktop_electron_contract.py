# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Python 桌面(desktop_app)与 Electron 桌面(main.cjs/preload.cjs)的契约对齐测试。

两套桌面运行时靠人肉同步是已知痛点: Python 侧新增/修改桌面 API 或启动常量时,
Electron 侧容易静默落后(此前 blob save、文件选择、桌面锁定 token 均发生过)。
本文件全部基于源码静态解析, 不 import 任何业务模块, 因此快且无副作用;
任一侧漂移都会在这里红灯, 迫使改动者同步两侧、或显式更新豁免/分歧清单。

新增契约项的步骤: 在两侧实现后, 把对应常量/方法面补充成本文件的断言。
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_APP_PY = REPO_ROOT / "jiuwenswarm" / "channels" / "desktop" / "desktop_app.py"
MAIN_CJS = REPO_ROOT / "jiuwenswarm" / "channels" / "desktop" / "electron" / "main.cjs"
PRELOAD_CJS = REPO_ROOT / "jiuwenswarm" / "channels" / "desktop" / "electron" / "preload.cjs"
FILE_PICKER_PY = REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "file_picker.py"
APP_WEB_PY = REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "app_web.py"
VITE_ENV_DTS = (
    REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend" / "src" / "vite-env.d.ts"
)
FRONTEND_SRC = REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "frontend" / "src"
INSTANCE_CONFIG_PY = REPO_ROOT / "jiuwenswarm" / "instance_manager" / "config.py"
INSTANCE_LOCK_PY = REPO_ROOT / "jiuwenswarm" / "instance_manager" / "lock.py"
STARTUP_DIAGNOSTICS_PY = REPO_ROOT / "jiuwenswarm" / "common" / "startup_diagnostics.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─── Python 源码解析 ────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=None)
def _desktop_app_module() -> ast.Module:
    return ast.parse(_read(DESKTOP_APP_PY))


@functools.lru_cache(maxsize=None)
def _desktop_app_assignments() -> dict[str, ast.expr]:
    result: dict[str, ast.expr] = {}
    for node in _desktop_app_module().body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                result[node.target.id] = node.value
    return result


def _py_literal(node: ast.expr, env: dict | None = None):
    env = env or {}
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        assert node.id in env, f"unresolved name in literal: {node.id}"
        return env[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
        left = _py_literal(node.left, env)
        right = _py_literal(node.right, env)
        return left + right if isinstance(node.op, ast.Add) else left * right
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_py_literal(item, env) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(items)
        if isinstance(node, ast.Set):
            return frozenset(items)
        return items
    if isinstance(node, ast.Dict):
        return {
            _py_literal(key, env): _py_literal(value, env)
            for key, value in zip(node.keys, node.values)
        }
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set"}
        and len(node.args) <= 1
    ):
        if not node.args:
            return frozenset()
        return frozenset(_py_literal(node.args[0], env))
    raise AssertionError(f"unsupported literal node: {ast.dump(node)[:120]}")


def _py_assignment_value(path: Path, name: str) -> object:
    module = ast.parse(_read(path))
    for node in module.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return _py_literal(node.value)  # type: ignore[arg-type]
    raise AssertionError(f"assignment {name!r} not found in {path.name}")


def _window_api_methods() -> set[str]:
    """pywebview 暴露给前端的 API 面 = _WindowApi 的公开方法。"""
    for node in ast.walk(_desktop_app_module()):
        if isinstance(node, ast.ClassDef) and node.name == "_WindowApi":
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not item.name.startswith("_")
            }
    raise AssertionError("class _WindowApi not found in desktop_app.py")


def _function_def(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _gateway_preflight_wait_seconds() -> float:
    fn = _function_def(_desktop_app_module(), "_preflight_gateway_singleton")
    arg = fn.args.args[-1]
    assert arg.arg == "wait", "preflight 签名变化, 请同步更新本测试"
    default = fn.args.defaults[-1]
    assert isinstance(default, ast.Constant) and isinstance(default.value, (int, float))
    return float(default.value)


def _python_shutdown_budget_seconds() -> float:
    """DesktopRuntime.shutdown 内 SIGTERM→强杀的统一预算(desktop_app.py 字面量)。"""
    fn = _function_def(_desktop_app_module(), "shutdown")
    segment = ast.get_source_segment(_read(DESKTOP_APP_PY), fn)
    assert segment is not None
    match = re.search(r"time\.monotonic\(\) \+ (\d+(?:\.\d+)?)", segment)
    assert match, "shutdown 预算字面量未找到, 请同步更新本测试"
    return float(match.group(1))


def _data_url_export_specs() -> dict[str, dict[str, frozenset]]:
    value = _desktop_app_assignments()["DATA_URL_EXPORT_SPECS"]
    assert isinstance(value, ast.Dict)
    result: dict[str, dict[str, frozenset]] = {}
    for key_node, value_node in zip(value.keys, value.values):
        assert isinstance(key_node, ast.Constant) and isinstance(value_node, ast.Call)
        keywords = {item.arg: item.value for item in value_node.keywords}
        result[str(key_node.value)] = {
            "suffixes": frozenset(_py_literal(keywords["allowed_suffixes"])),
            "parameters": frozenset(_py_literal(keywords["allowed_parameters"])),
        }
    return result


# ─── Electron(main.cjs/preload.cjs)源码解析 ───────────────────────────────


def _js_const(source: str, name: str) -> str:
    match = re.search(rf"const {re.escape(name)} = (.*?);", source, re.DOTALL)
    assert match, f"const {name} not found in main.cjs"
    return match.group(1).strip()


def _js_number(expression: str) -> int | float:
    # 只允许数字字面量(含 JS 下划线分隔/十六进制)、+ * 括号与空白, 然后按 Python 语法求值。
    assert re.fullmatch(r"[0-9a-fA-FxX_+*(). ]+", expression), (
        f"unsafe/unsupported numeric expression: {expression!r}"
    )
    return eval(expression)  # noqa: S307 - 字符集白名单后求值


def _js_number_const(source: str, name: str) -> int | float:
    return _js_number(_js_const(source, name))


def _js_quoted_strings(text: str) -> list[str]:
    return re.findall(r"'([^']*)'", text)


def _electron_base_ports(source: str) -> dict[str, int]:
    text = _js_const(source, "BASE_PORTS")
    return {key: int(value) for key, value in re.findall(r"(\w+): (\d+)", text)}


def _electron_port_scan_range(source: str) -> int:
    match = re.search(r"function findAvailablePorts\(scanRange = (\d+)\)", source)
    assert match, "findAvailablePorts(scanRange = N) not found in main.cjs"
    return int(match.group(1))


def _electron_png_signature(source: str) -> list[int]:
    match = re.search(r"const PNG_SIGNATURE = Buffer\.from\((\[[^]]*])\)", source)
    assert match, "PNG_SIGNATURE not found in main.cjs"
    return [int(item, 0) for item in re.findall(r"0x[0-9a-fA-F]+|\d+", match.group(1))]


def _electron_blob_specs(source: str) -> dict[str, dict[str, frozenset]]:
    text = _js_const(source, "BLOB_EXPORT_SPECS")
    result: dict[str, dict[str, frozenset]] = {}
    for match in re.finditer(
        r"'([^']+)':\s*\{ suffixes: \[([^]]*)], parameters: \[([^]]*)]", text
    ):
        result[match.group(1)] = {
            "suffixes": frozenset(_js_quoted_strings(match.group(2))),
            "parameters": frozenset(_js_quoted_strings(match.group(3))),
        }
    assert result, "BLOB_EXPORT_SPECS not parsed from main.cjs"
    return result


def _balanced_braces_block(source: str, start_marker: str) -> str:
    start = source.index(start_marker)
    open_index = source.index("{", start)
    depth = 0
    for index in range(open_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_index : index + 1]
    raise AssertionError(f"unbalanced braces after {start_marker!r}")


def _preload_pywebview_api() -> tuple[set[str], set[str]]:
    """返回 (pywebview.api 键集合, 其引用的 desktopApi 方法名集合)。"""
    source = _read(PRELOAD_CJS)
    block = _balanced_braces_block(source, "exposeInMainWorld('pywebview'")
    keys = set(re.findall(r"^ {4}(\w+):", block, re.MULTILINE))
    referenced = set(re.findall(r"desktopApi\.(\w+)", block))
    assert keys, "pywebview.api keys not parsed from preload.cjs"
    return keys, referenced


def _preload_desktop_api_keys() -> set[str]:
    source = _read(PRELOAD_CJS)
    block = _balanced_braces_block(source, "const desktopApi = Object.freeze({")
    keys = set(re.findall(r"^ {2}(\w+):", block, re.MULTILINE))
    assert keys, "desktopApi keys not parsed from preload.cjs"
    return keys


# ─── 前端消费面解析 ─────────────────────────────────────────────────────────


def _frontend_declared_api_keys() -> set[str]:
    """vite-env.d.ts 中 window.pywebview.api 的可选方法声明 = 前端契约面。"""
    lines = _read(VITE_ENV_DTS).splitlines()
    keys: set[str] = set()
    inside = False
    for line in lines:
        if "api?: {" in line:
            inside = True
            continue
        if inside:
            match = re.match(r" {6}(\w+)\??:", line)
            if match:
                keys.add(match.group(1))
                continue
            if line.strip() == "};":
                break
    assert keys, "pywebview api declarations not parsed from vite-env.d.ts"
    return keys


def _frontend_direct_usages() -> set[str]:
    """src 下直接形如 pywebview?.api?.<method> 的调用点。"""
    usages: set[str] = set()
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or not path.is_file():
            continue
        usages.update(re.findall(r"pywebview\?\.\s*api\?\.\s*(\w+)", _read(path)))
    return usages


# ─── API 面对齐 ─────────────────────────────────────────────────────────────

# Python 桌面独有的 pywebview API: get_startup_status 由 Python 桌面自渲染的
# loading/诊断 HTML 轮询使用(前端 SPA 不调用, 已核实); Electron 的 loading/
# 失败页由主进程渲染, 不需要该桥接。新增豁免必须在此给出理由。
PYWEBVIEW_API_PYTHON_ONLY = {"get_startup_status"}


def test_pywebview_api_surface_matches_electron_preload() -> None:
    python_api = _window_api_methods()
    electron_api = _preload_pywebview_api()[0]
    assert python_api - electron_api == PYWEBVIEW_API_PYTHON_ONLY, (
        "desktop_app._WindowApi 出现 preload.cjs 未暴露(且未豁免)的方法, "
        "请在 preload.cjs 的 pywebview.api 补齐或更新豁免清单"
    )
    assert electron_api - python_api == set(), (
        "preload.cjs 暴露了 desktop_app._WindowApi 不存在的方法, 两侧契约漂移"
    )


def test_preload_pywebview_values_reference_existing_desktop_api() -> None:
    _, referenced = _preload_pywebview_api()
    desktop_keys = _preload_desktop_api_keys()
    missing = referenced - desktop_keys
    assert not missing, f"pywebview.api 引用了 desktopApi 未定义的成员: {sorted(missing)}"


def test_frontend_used_api_available_on_both_desktops() -> None:
    python_api = _window_api_methods()
    electron_api = _preload_pywebview_api()[0]
    shared = python_api & electron_api
    declared = _frontend_declared_api_keys()
    assert declared <= shared, (
        f"vite-env.d.ts 声明了某侧桌面缺失的 API: {sorted(declared - shared)}"
    )
    for usage in sorted(_frontend_direct_usages()):
        assert usage in shared, f"前端直接调用 pywebview?.api?.{usage}, 但两侧桌面未同时提供"


# ─── 启动/关闭常量对齐 ──────────────────────────────────────────────────────

# Python instance_manager 键名 → Electron main.cjs 键名(值必须一致)。
# 注: Python 的 "web" 端口(19000)是 gateway API 端口(app_web 的代理目标),
# Electron 命名为 gatewayApi; Electron 的 "frontend"(5173)是 app_web 静态端口。
BASE_PORT_NAME_MAP = {
    "agent_server": "agentServer",
    "web": "gatewayApi",
    "gateway": "gatewayInternal",
    "frontend": "frontend",
}


def test_base_ports_match() -> None:
    python_ports = _py_assignment_value(INSTANCE_CONFIG_PY, "BASE_PORTS")
    electron_ports = _electron_base_ports(_read(MAIN_CJS))
    assert set(python_ports) == set(BASE_PORT_NAME_MAP), "BASE_PORTS 键集变化, 请更新映射表"
    for python_name, electron_name in BASE_PORT_NAME_MAP.items():
        assert python_ports[python_name] == electron_ports[electron_name], (
            f"端口 {python_name}/{electron_name} 不一致"
        )


def test_startup_timeout_matches() -> None:
    python_seconds = _py_literal(_desktop_app_assignments()["STARTUP_TIMEOUT_SECONDS"])
    electron_ms = _js_number_const(_read(MAIN_CJS), "STARTUP_TIMEOUT_MS")
    assert electron_ms == python_seconds * 1000


def test_port_scan_range_matches() -> None:
    python_range = _py_literal(_desktop_app_assignments()["DESKTOP_PORT_SCAN_RANGE"])
    assert _electron_port_scan_range(_read(MAIN_CJS)) == python_range


def test_shutdown_budget_is_the_documented_divergence() -> None:
    """已知且接受的分歧(评审结论 E: 强杀=崩溃等价, 系统按崩溃容忍设计)。

    Python 统一 8s 预算; Electron SIGTERM 等待 5s + 强杀等待 1.5s。
    钉住当前值: 任一侧改动都会红灯, 迫使重新评审该分歧。
    """
    assert _python_shutdown_budget_seconds() == 8.0
    assert _js_number_const(_read(MAIN_CJS), "SERVICE_SHUTDOWN_TIMEOUT_MS") == 5_000
    assert _js_number_const(_read(MAIN_CJS), "SERVICE_KILL_TIMEOUT_MS") == 1_500


def test_gateway_preflight_wait_matches() -> None:
    python_wait = _gateway_preflight_wait_seconds()
    electron_ms = _js_number_const(_read(MAIN_CJS), "GATEWAY_PREFLIGHT_WAIT_MS")
    assert electron_ms == python_wait * 1000


def test_gateway_lock_filename_matches() -> None:
    python_name = _py_assignment_value(INSTANCE_LOCK_PY, "GATEWAY_LOCK_FILENAME")
    assert _js_const(_read(MAIN_CJS), "GATEWAY_LOCK_FILENAME").strip("'\"") == python_name


def test_startup_doctor_timeout_matches() -> None:
    doctor_timeout = _py_assignment_value(STARTUP_DIAGNOSTICS_PY, "DOCTOR_TIMEOUT_SECONDS")
    python_seconds = _py_literal(
        _desktop_app_assignments()["STARTUP_DOCTOR_TIMEOUT_SECONDS"],
        env={"DOCTOR_TIMEOUT_SECONDS": doctor_timeout},
    )
    electron_ms = _js_number_const(_read(MAIN_CJS), "STARTUP_DOCTOR_TIMEOUT_MS")
    assert electron_ms == python_seconds * 1000


def test_startup_diagnostics_env_name_matches() -> None:
    python_env = _py_assignment_value(
        STARTUP_DIAGNOSTICS_PY, "STARTUP_DIAGNOSTICS_DIR_ENV"
    )
    assert _js_const(_read(MAIN_CJS), "STARTUP_DIAGNOSTICS_DIR_ENV").strip("'") == python_env


def test_warmup_packages_and_read_bytes_match() -> None:
    python_packages = _py_literal(_desktop_app_assignments()["_WARMUP_PACKAGES"])
    electron_packages = _js_quoted_strings(_js_const(_read(MAIN_CJS), "WARMUP_PACKAGES"))
    assert list(python_packages) == electron_packages
    python_bytes = _py_literal(_desktop_app_assignments()["_WARMUP_READ_BYTES"])
    assert _js_number_const(_read(MAIN_CJS), "WARMUP_READ_BYTES") == python_bytes


def test_wait_for_http_ready_semantics_canary() -> None:
    """就绪判定的语义金丝雀: 两侧都必须以 status < 500 为就绪(4xx 也算服务已起)。"""
    assert "if response.status < 500:" in _read(DESKTOP_APP_PY)
    assert "response.status < 500" in _read(MAIN_CJS)


# ─── blob save / 桌面锁定 / 文件选择契约对齐 ───────────────────────────────


def test_blob_chunk_size_and_png_signature_match() -> None:
    python_chunk = _py_literal(_desktop_app_assignments()["DESKTOP_BLOB_CHUNK_SIZE"])
    assert _js_number_const(_read(MAIN_CJS), "DESKTOP_BLOB_CHUNK_SIZE") == python_chunk
    python_signature = _py_literal(_desktop_app_assignments()["PNG_SIGNATURE"])
    assert _electron_png_signature(_read(MAIN_CJS)) == list(python_signature)
    python_max_int = _py_literal(
        _desktop_app_assignments()["MAX_JAVASCRIPT_SAFE_INTEGER"]
    )
    assert _js_number_const(_read(MAIN_CJS), "MAX_JAVASCRIPT_SAFE_INTEGER") == python_max_int


def test_blob_export_mime_whitelist_matches() -> None:
    assert _electron_blob_specs(_read(MAIN_CJS)) == _data_url_export_specs()


def test_desktop_lock_token_contract_matches() -> None:
    """桌面锁定: env 名与首导航查询参数两侧必须一致(app_web 按名匹配)。"""
    assert "JIUWENSWARM_DESKTOP_TOKEN" in _read(DESKTOP_APP_PY)
    assert "JIUWENSWARM_DESKTOP_TOKEN" in _read(MAIN_CJS)
    query_param = re.search(r'_DESKTOP_TOKEN_QUERY_PARAM = "([^"]+)"', _read(APP_WEB_PY))
    assert query_param, "_DESKTOP_TOKEN_QUERY_PARAM not found in app_web.py"
    assert f"/?{query_param.group(1)}=" in _read(MAIN_CJS)


def test_file_picker_whitelists_match() -> None:
    main_source = _read(MAIN_CJS)
    python_attachments = _py_assignment_value(
        FILE_PICKER_PY, "ATTACHMENT_DIALOG_EXTENSIONS"
    )
    electron_attachments = _js_quoted_strings(
        _js_const(main_source, "ATTACHMENT_DIALOG_EXTENSIONS")
    )
    assert list(python_attachments) == electron_attachments, (
        "附件对话框白名单漂移(顺序也需一致, 影响过滤展示)"
    )
    python_images = _py_literal(_desktop_app_assignments()["IMAGE_EXTENSIONS"])
    electron_images = frozenset(
        _js_quoted_strings(_js_const(main_source, "IMAGE_PICK_EXTENSIONS"))
    )
    assert electron_images == python_images
    python_forbidden = _py_literal(
        _desktop_app_assignments()["FORBIDDEN_DOCUMENT_EXTENSIONS"]
    )
    electron_forbidden = frozenset(
        _js_quoted_strings(_js_const(main_source, "FORBIDDEN_PICK_EXTENSIONS"))
    )
    assert electron_forbidden == python_forbidden
    python_max_image = _py_literal(_desktop_app_assignments()["MAX_IMAGE_BYTES"])
    assert _js_number_const(main_source, "MAX_IMAGE_PICK_BYTES") == python_max_image
    # desktop_app 与 file_picker 的同形常量也互相钉住, 防止 Python 内部漂移。
    for name in (
        "ATTACHMENT_DIALOG_EXTENSIONS",
        "IMAGE_EXTENSIONS",
        "FORBIDDEN_DOCUMENT_EXTENSIONS",
        "MAX_IMAGE_BYTES",
    ):
        if name == "ATTACHMENT_DIALOG_EXTENSIONS":
            assert list(_py_assignment_value(FILE_PICKER_PY, name)) == list(python_attachments)
        elif name == "IMAGE_EXTENSIONS":
            assert _py_assignment_value(FILE_PICKER_PY, name) == python_images
        elif name == "FORBIDDEN_DOCUMENT_EXTENSIONS":
            assert _py_assignment_value(FILE_PICKER_PY, name) == python_forbidden
        else:
            assert _py_assignment_value(FILE_PICKER_PY, name) == python_max_image


def test_desktop_env_flag_matches() -> None:
    python_flag = _py_literal(_desktop_app_assignments()["DESKTOP_ENV_FLAG"])
    assert python_flag in _read(MAIN_CJS)

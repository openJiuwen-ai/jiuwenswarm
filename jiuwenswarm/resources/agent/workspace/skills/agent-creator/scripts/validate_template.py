#!/usr/bin/env python3
"""Agent Template Validator — 分层校验 agent 模板包。

L0 规范与质量：占位符残留、展示字段不全。
L1 静态校验：加载阻塞项，纯 stdlib（json / ast / 文件系统）。
L2 热加载：子进程跑 validate_hot_load_worker.py，主脚本只解析结果。

L0 / L1 都通过才执行 L2 校验。

Usage:
    validate_template.py <path | agent-name>
    validate_template.py <path> --no-hot-load

Exit code: 0 通过，1 失败。热加载被跳过时结论见 stdout 的 RESULT 行。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def write_stdout(text: str) -> None:
    """CLI product output to fd 1 (avoid print/sys.stdout for G.LOG.02)."""
    os.write(1, text.encode("utf-8"))


HOT_LOAD_WORKER = Path(__file__).resolve().parent / "validate_hot_load_worker.py"
HOT_LOAD_TIMEOUT_SEC = 120

LAYER_QUALITY = "L0 规范与质量（不影响加载，影响成品）"
LAYER_STATIC = "L1 静态校验（加载阻塞项）"
LAYER_HOT_LOAD = "L2 热加载（真实 harness 加载链）"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
TODO_MARKER = "[TODO"
TODO_SCAN_SUFFIXES = (".md", ".py", ".json")
TODO_SKIP_DIRS = {"__pycache__", ".git", ".state"}
SKILL_MODES = ("all", "auto_list")
I18N_FIELDS = ("display_name", "display_description", "default_init_input")
FIXED_LIST_FIELDS = (("tags", 3), ("quick_inputs", 3))
OPTIONAL_ARRAY_FIELDS = ("skills", "tools", "rails", "mcps", "subagents")
_LEGACY_CAMEL_KEYS = {
    "packageType": "package_type",
    "displayName": "display_name",
    "displayDescription": "display_description",
    "defaultInitInput": "default_init_input",
    "defaultInitPrompt": "default_init_input",
    "quickInputs": "quick_inputs",
}
FORBIDDEN_ROOT_NAMES = {
    "artifact_root",
    "output_root",
    "workspace_root",
    "base_output_dir",
}
FORBIDDEN_OUTPUT_PARAMS = {
    "artifact_root",
    "output_root",
    "workspace_root",
    "base_output_dir",
    "absolute_path",
}
PATH_RECEIVER_WRITE_METHODS = {
    "mkdir",
    "touch",
    "write_bytes",
    "write_text",
}
PATH_ARGUMENT_WRITE_METHODS = {
    "save",
    "to_csv",
    "to_excel",
    "to_html",
    "to_json",
    "to_markdown",
    "to_parquet",
}


class Layer:
    """一层校验的结果。errors 阻塞，warnings 提示，notes 只是事实。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.errors: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.skip_reason = ""
        self.skip_fix = ""

    def error(self, msg: str, fix: str = "") -> None:
        self.errors.append((msg, fix))

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def skip(self, reason: str, fix: str = "") -> None:
        self.skip_reason = reason
        self.skip_fix = fix

    @property
    def tag(self) -> str:
        return self.name.split()[0]

    @property
    def status(self) -> str:
        if self.skip_reason:
            return "SKIP"
        return "FAIL" if self.errors else "PASS"

    def render(self) -> str:
        lines = [f"---- {self.name} ----"]
        if self.status == "SKIP":
            lines.append(f"  SKIP  {self.skip_reason}")
            if self.skip_fix:
                lines.append(f"        -> {self.skip_fix}")
            return "\n".join(lines)
        lines.append(
            f"  {self.status}  {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        )
        for msg, fix in self.errors:
            lines.append(f"    x {msg}")
            if fix:
                lines.append(f"      -> {fix}")
        for msg in self.warnings:
            lines.append(f"    ! {msg}")
        for msg in self.notes:
            lines.append(f"    . {msg}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def get_jiuwenswarm_data_dir() -> Path:
    """从环境变量读取数据根目录；非默认实例时由宿主注入 JIUWENSWARM_DATA_DIR。"""
    raw = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".jiuwenswarm"


def get_agent_workspace_dir() -> Path:
    return get_jiuwenswarm_data_dir() / "agent" / "workspace"


def get_agent_templates_local_dir() -> Path:
    return get_agent_workspace_dir() / "plugins" / "agent_templates" / "local"


def resolve_pkg(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    local = get_agent_templates_local_dir() / arg
    if local.is_dir():
        return local.resolve()
    raise FileNotFoundError(
        f"找不到包目录: {arg}（也不在 {get_agent_templates_local_dir()} 下）"
    )


def _read_json(path: Path, layer: Layer, *, label: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        layer.error(f"{label}: JSON 解析失败: {exc}")
        return None
    if not isinstance(payload, dict):
        layer.error(f"{label}: 顶层必须是 JSON 对象")
        return None
    return payload


def _resolve_declared_path(
    pkg: Path, raw: str, *, field: str, must_be_dir: bool, layer: Layer
) -> Path | None:
    """按加载器 ``_resolve_new_manifest_path`` 的规则解析 manifest 声明的路径。"""
    if not raw:
        layer.error(f"manifest.json: {field} 为空")
        return None
    if Path(raw).expanduser().is_absolute():
        layer.error(
            f"manifest.json: {field} 必须是包内相对路径，当前是绝对路径 {raw!r}",
            "改成 './xxx' 形式的相对路径",
        )
        return None
    resolved = (pkg / raw).resolve()
    if not resolved.is_relative_to(pkg):
        layer.error(f"manifest.json: {field} {raw!r} 越出了包根目录")
        return None
    if not resolved.exists():
        layer.error(f"manifest.json: {field} 指向的路径不存在: {raw}")
        return None
    kind_ok = resolved.is_dir() if must_be_dir else resolved.is_file()
    if not kind_ok:
        expected = "目录" if must_be_dir else "文件"
        layer.error(f"manifest.json: {field} 必须是{expected}: {raw}")
        return None
    return resolved


# ---------------------------------------------------------------------------
# L0 规范与质量
# ---------------------------------------------------------------------------


def _check_todo_residue(pkg: Path, layer: Layer) -> None:
    for path in sorted(pkg.rglob("*")):
        if not path.is_file() or path.suffix not in TODO_SCAN_SUFFIXES:
            continue
        if any(part in TODO_SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [n for n, line in enumerate(text.splitlines(), 1) if TODO_MARKER in line]
        if not hits:
            continue
        shown = ", ".join(str(n) for n in hits[:5])
        more = f" 等 {len(hits)} 处" if len(hits) > 5 else ""
        rel = path.relative_to(pkg).as_posix()
        layer.error(f"{rel}: 残留 [TODO] 占位符（行 {shown}{more}）")


def _check_i18n(value: Any, field: str, layer: Layer) -> None:
    if not isinstance(value, dict):
        layer.error(f"manifest.json: {field} 必须是含 'en' 和 'zh' 的对象")
        return
    for lang in ("en", "zh"):
        if not str(value.get(lang) or "").strip():
            layer.error(f"manifest.json: {field}.{lang} 不能为空")


def _check_default_init_consistency(manifest: dict[str, Any], layer: Layer) -> None:
    default_input = manifest.get("default_init_input")
    quick = manifest.get("quick_inputs")
    if not isinstance(default_input, dict) or not isinstance(quick, list) or not quick:
        return
    first = quick[0]
    if not isinstance(first, dict):
        return
    if any(default_input.get(lang) != first.get(lang) for lang in ("en", "zh")):
        layer.error(
            "manifest.json: default_init_input 必须与 quick_inputs[0] 完全一致",
            "把 quick_inputs 第一条原样复制到 default_init_input",
        )


def _check_legacy_camel_keys(obj: Any, layer: Layer, prefix: str = "") -> None:
    """Reject leftover camelCase manifest keys; canonical names are snake_case."""
    if not isinstance(obj, dict):
        return
    for old, new in _LEGACY_CAMEL_KEYS.items():
        if old not in obj:
            continue
        field = f"{prefix}.{old}" if prefix else old
        layer.error(
            f"manifest.json: 字段名应为 '{new}'，当前是旧名 '{old}'",
            f"把 {field} 重命名为 {new}",
        )
    for key in ("tools", "rails", "mcps"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            _check_legacy_camel_keys(item, layer, prefix=f"{key}[{idx}]")


def _check_display_fields(manifest: dict[str, Any], layer: Layer) -> None:
    _check_legacy_camel_keys(manifest, layer)
    for field in I18N_FIELDS:
        _check_i18n(manifest.get(field), field, layer)

    for field, expected in FIXED_LIST_FIELDS:
        value = manifest.get(field)
        if not isinstance(value, list):
            layer.error(f"manifest.json: 缺少 {field!r}，应为含 {expected} 项的数组")
            continue
        if len(value) != expected:
            layer.error(
                f"manifest.json: {field} 必须正好 {expected} 个，实际 {len(value)} 个"
            )
        for idx, item in enumerate(value):
            _check_i18n(item, f"{field}[{idx}]", layer)

    _check_default_init_consistency(manifest, layer)

    display = manifest.get("display_description")
    zh_desc = display.get("zh") if isinstance(display, dict) else None
    if isinstance(zh_desc, str) and zh_desc and not 40 <= len(zh_desc) <= 50:
        layer.warn(
            f"manifest.json: display_description.zh 共 {len(zh_desc)} 字（建议 40-50）"
        )

    if not str(manifest.get("description") or "").strip():
        layer.error("manifest.json: description 不能为空")

    category = manifest.get("category")
    if not isinstance(category, str) or not category.strip():
        layer.error(
            "manifest.json: category 必填且为非空字符串",
            "填写 Design / Engineering / Life / IndustryConsultant 等分类标识",
        )

    if "avatar" not in manifest:
        layer.error(
            "manifest.json: 缺少 avatar 字段",
            '本 skill 生成的包固定写 "avatar": ""',
        )
    elif manifest.get("avatar") != "":
        layer.error(
            "manifest.json: avatar 必须为空字符串",
            '改成 "avatar": ""；不声明头像路径、不创建 avatars/ 目录',
        )


def _check_quality(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    _check_todo_residue(pkg, layer)
    _check_display_fields(manifest, layer)
    if not (pkg / "README.md").is_file():
        layer.error("README.md 不存在")
    if not (pkg / "persona" / f"{pkg.name}.md").is_file():
        layer.warn(f"persona/{pkg.name}.md 不存在（约定 persona 文件名与包名一致）")


# ---------------------------------------------------------------------------
# L1 静态校验 —— AST 辅助
# ---------------------------------------------------------------------------


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", "")


def _string_literal(node: ast.expr | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _dict_value(node: ast.Dict, key: str) -> ast.expr | None:
    for raw_key, value in zip(node.keys, node.values):
        if isinstance(raw_key, ast.Constant) and raw_key.value == key:
            return value
    return None


def _has_base(cls: ast.ClassDef, keyword: str) -> bool:
    for base in cls.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if keyword in name:
            return True
    return False


def _has_async_method(cls: ast.ClassDef, name: str) -> bool:
    return any(
        isinstance(node, ast.AsyncFunctionDef) and node.name == name
        for node in cls.body
    )


def _required_init_params(init: ast.FunctionDef) -> int:
    """__init__ 里除 self 外、没有默认值的参数个数。"""
    args = init.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    if positional and positional[0] in ("self", "cls"):
        positional = positional[1:]
    required = max(0, len(positional) - len(args.defaults))
    required += sum(1 for default in args.kw_defaults if default is None)
    return required


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in target.elts:
            names.extend(_target_names(item))
        return names
    return []


def _contains_package_path(node: ast.AST, package_path_vars: set[str]) -> bool:
    return any(
        isinstance(child, ast.Name)
        and (child.id == "__file__" or child.id in package_path_vars)
        for child in ast.walk(node)
    )


def _open_mode_is_write(node: ast.Call) -> bool:
    mode = _string_literal(node.args[1]) if len(node.args) > 1 else ""
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = _string_literal(keyword.value)
            break
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _is_runtime_cwd_bypass(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"cwd", "getcwd"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"Path", "os"}
    )


def _is_package_path_write(node: ast.Call, package_path_vars: set[str]) -> bool:
    attr = _callee_name(node.func)
    if attr in PATH_RECEIVER_WRITE_METHODS:
        return _contains_package_path(node.func, package_path_vars)
    if attr == "open":
        return _contains_package_path(
            node.func, package_path_vars
        ) and _open_mode_is_write(node)
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        return (
            bool(node.args)
            and _contains_package_path(node.args[0], package_path_vars)
            and _open_mode_is_write(node)
        )
    if attr in PATH_ARGUMENT_WRITE_METHODS:
        return any(_contains_package_path(arg, package_path_vars) for arg in node.args)
    return False


def _check_runtime_path_policy(
    tree: ast.Module, rel: str, component: str, layer: Layer
) -> None:
    """拦截明确的运行时路径坏味道，不做复杂静态分析。"""
    package_path_vars: set[str] = set()

    assignments = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ),
        key=lambda node: getattr(node, "lineno", 0),
    )
    for node in assignments:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in _target_names(target):
                if name in FORBIDDEN_ROOT_NAMES:
                    layer.error(
                        f"{rel}: 禁止自定义根目录变量 {name!r}",
                        "根目录由 JiuwenSwarm/OpenJiuwen 运行态决定",
                    )

        value = node.value
        if value is not None and _contains_package_path(value, package_path_vars):
            for target in targets:
                package_path_vars.update(_target_names(target))

    for node in sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call)), key=lambda n: n.lineno
    ):
        if _is_runtime_cwd_bypass(node):
            fix = (
                "Tool 的文件写入路径必须由入参显式传入"
                if component == "Tool"
                else "内部状态路径统一使用 get_workspace()"
            )
            layer.error(
                f"{rel}: 禁止使用 Path.cwd() / os.getcwd()（第 {node.lineno} 行）",
                fix,
            )
        if _is_package_path_write(node, package_path_vars):
            fix = (
                "包内路径只读；Tool 的文件写入路径必须由入参显式传入"
                if component == "Tool"
                else "包内路径只读；内部状态使用 get_workspace()"
            )
            layer.error(
                f"{rel}: {component} 禁止用包内路径写运行时文件（第 {node.lineno} 行）",
                fix,
            )


def _check_no_arg_init(
    cls: ast.ClassDef, rel: str, layer: Layer, *, required: bool, fix: str
) -> None:
    init = next(
        (
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    if init is None:
        if required:
            layer.error(f"{rel}: {cls.name} 必须定义可无参构造的 __init__", fix)
        return
    missing = _required_init_params(init)
    if missing:
        layer.error(
            f"{rel}: {cls.name}.__init__ 必须能无参构造，当前有 {missing} 个必填参数",
            fix,
        )


def _check_tool_card(cls: ast.ClassDef, rel: str, layer: Layer) -> None:
    call = next(
        (
            node
            for node in ast.walk(cls)
            if isinstance(node, ast.Call) and _callee_name(node.func) == "ToolCard"
        ),
        None,
    )
    if call is None:
        layer.warn(
            f"{rel}: {cls.name} 内未找到字面量 ToolCard(...) 调用，"
            "无法静态检查 id / name / input_params"
        )
        return
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    for key in ("id", "name"):
        if key not in kwargs:
            layer.error(f"{rel}: ToolCard 必须显式设置 {key!r}")
    params = kwargs.get("input_params")
    if params is None:
        layer.error(
            f"{rel}: ToolCard 必须设置 input_params",
            '无参数时也要写 input_params={"type": "object", "properties": {}}',
        )
        return
    if not isinstance(params, ast.Dict):
        layer.warn(
            f'{rel}: input_params 不是字面量 dict，无法静态确认是否含 "type": "object"'
        )
        return
    has_object_type = any(
        isinstance(key, ast.Constant)
        and key.value == "type"
        and isinstance(value, ast.Constant)
        and value.value == "object"
        for key, value in zip(params.keys, params.values)
    )
    if not has_object_type:
        layer.error(
            f'{rel}: input_params 必须含 "type": "object"',
            '即使无参数也要写 {"type": "object", "properties": {}}',
        )
    properties = _dict_value(params, "properties")
    if isinstance(properties, ast.Dict):
        for raw_key in properties.keys:
            key = _string_literal(raw_key)
            if key in FORBIDDEN_OUTPUT_PARAMS:
                layer.error(
                    f"{rel}: input_params.properties 禁止暴露 {key!r}",
                    "文件写入目录使用必填的 output_dir；不要暴露其他产物根参数",
                )


def _load_declared_class(
    pkg: Path, item: Any, *, field: str, layer: Layer
) -> tuple[ast.ClassDef | None, str, ast.Module | None]:
    if not isinstance(item, dict) or "file" not in item:
        layer.error(f"manifest.json: {field} 必须同时含 'file' 和 'class'")
        return None, field, None
    raw = str(item["file"])
    path = _resolve_declared_path(
        pkg, raw, field=f"{field}.file", must_be_dir=False, layer=layer
    )
    if path is None:
        return None, raw, None
    rel = path.relative_to(pkg).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        layer.error(f"{rel}: Python 语法错误（第 {exc.lineno} 行）: {exc.msg}")
        return None, rel, None
    except (OSError, UnicodeDecodeError) as exc:
        layer.error(f"{rel}: 读取失败: {exc}")
        return None, rel, None

    class_name = str(item.get("class") or item.get("class_name") or "")
    if not class_name:
        layer.error(f"manifest.json: {field} 缺少 'class'")
        return None, rel, tree
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node, rel, tree
    defined = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    layer.error(
        f"{rel}: 找不到 class {class_name!r}（文件内定义了: {', '.join(defined) or '无'}）"
    )
    return None, rel, tree


# ---------------------------------------------------------------------------
# L1 静态校验 —— 各分项
# ---------------------------------------------------------------------------


def _check_manifest_basics(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    if not NAME_RE.match(pkg.name):
        layer.error(f"包目录名 {pkg.name!r} 必须是 kebab-case")
    package_type = manifest.get("package_type")
    if package_type != "agent_template":
        layer.error(
            f"manifest.json: package_type 必须是 'agent_template'，当前是 {package_type!r}"
        )
    source = manifest.get("source")
    if source != "local":
        layer.error(
            f"manifest.json: source 必须是 'local'，当前是 {source!r}",
            '本 skill 生成的包固定写 "source": "local"',
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        layer.error(
            "manifest.json: version 必填且为非空字符串",
            "init 起就写 1.0.0；update 由 register_template.py --bump 在 installed=true 时递增",
        )
    package_name = manifest.get("name")
    if package_name != pkg.name:
        layer.error(
            f"manifest.json: name ({package_name!r}) 必须等于包目录名 ({pkg.name!r})"
        )
    for removed_field in ("agent_card", "agentCard"):
        if removed_field in manifest:
            layer.error(f"manifest.json: 不再支持 {removed_field!r}，请删除该字段")
    for field in OPTIONAL_ARRAY_FIELDS:
        if manifest.get(field) == []:
            layer.error(
                f"manifest.json: {field} 是空数组",
                f"没有内容时整段省略 {field}，不要留 []",
            )


def _check_unsupported_sections(manifest: dict[str, Any], layer: Layer) -> None:
    forbidden = {
        "subagents": "移除 subagents 字段和 subagents/ 目录",
        "mcps": "移除 mcps 字段和 mcps/ 目录",
        "model": "移除 model 字段和 model.json；本 skill 不生成独立模型配置",
    }
    for key, fix in forbidden.items():
        if manifest.get(key):
            layer.error(
                f"manifest.json: 本 skill 不生成 {key!r}，不应声明该字段",
                fix,
            )


def _check_persona(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    persona = manifest.get("persona")
    if not isinstance(persona, dict) or "dir" not in persona:
        layer.error('manifest.json: persona 必填，形如 {"dir": "./persona"}')
        return
    persona_dir = _resolve_declared_path(
        pkg, str(persona["dir"]), field="persona.dir", must_be_dir=True, layer=layer
    )
    if persona_dir is None:
        return
    if not any(p.is_file() for p in persona_dir.rglob("*.md")):
        layer.error(f"{persona['dir']}: persona 目录下没有任何 .md 文件")


def _check_skill_dir(pkg: Path, skill_dir: Path, layer: Layer) -> None:
    rel = skill_dir.relative_to(pkg).as_posix()
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        layer.error(f"{rel}: 缺少 SKILL.md")
        return
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        layer.error(f"{rel}/SKILL.md: 读取失败: {exc}")
        return
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        layer.error(f"{rel}/SKILL.md: 缺少 YAML frontmatter")
        return
    front = match.group(1)
    name = re.search(r"^name\s*:\s*['\"]?([^'\"\s]+)['\"]?\s*$", front, re.MULTILINE)
    if not name:
        layer.error(f"{rel}/SKILL.md: frontmatter 缺少 'name'")
    elif name.group(1) != skill_dir.name:
        layer.error(
            f"{rel}/SKILL.md: frontmatter name {name.group(1)!r} 必须等于目录名 {skill_dir.name!r}"
        )
    if not re.search(r"^description\s*:\s*\S", front, re.MULTILINE):
        layer.error(f"{rel}/SKILL.md: frontmatter 缺少 'description'")


def _check_skills(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    for idx, raw_item in enumerate(manifest.get("skills") or []):
        field = f"skills[{idx}]"
        item = {"dir": raw_item} if isinstance(raw_item, str) else raw_item
        if not isinstance(item, dict) or "dir" not in item:
            layer.error(f"manifest.json: {field} 必须含 'dir'")
            continue
        skill_dir = _resolve_declared_path(
            pkg, str(item["dir"]), field=f"{field}.dir", must_be_dir=True, layer=layer
        )
        if skill_dir is None:
            continue
        mode = item.get("mode", "all")
        if mode not in SKILL_MODES:
            layer.error(
                f"manifest.json: {field}.mode 只能是 {' / '.join(SKILL_MODES)}，当前是 {mode!r}"
            )
        _check_skill_dir(pkg, skill_dir, layer)


def _check_capability_display_fields(item: Any, *, field: str, layer: Layer) -> None:
    """Require per-entry display_name / display_description on tools[] and rails[]."""
    if not isinstance(item, dict):
        return
    for key in ("display_name", "display_description"):
        _check_i18n(item.get(key), f"{field}.{key}", layer)


def _check_tools(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    for idx, item in enumerate(manifest.get("tools") or []):
        field = f"tools[{idx}]"
        _check_capability_display_fields(item, field=field, layer=layer)
        cls, rel, tree = _load_declared_class(pkg, item, field=field, layer=layer)
        if tree is not None:
            _check_runtime_path_policy(tree, rel, "Tool", layer)
        if cls is None:
            continue
        if not _has_base(cls, "Tool"):
            layer.error(
                f"{rel}: {cls.name} 必须继承 Tool",
                "from openjiuwen.core.foundation.tool import Tool, ToolCard",
            )
        _check_no_arg_init(
            cls,
            rel,
            layer,
            required=True,
            fix="改成 def __init__(self) -> None，并在内部创建 ToolCard",
        )
        if not _has_async_method(cls, "invoke"):
            layer.error(f"{rel}: {cls.name} 缺少 async def invoke(...)")
        _check_tool_card(cls, rel, layer)


def _check_rails(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    for idx, item in enumerate(manifest.get("rails") or []):
        field = f"rails[{idx}]"
        _check_capability_display_fields(item, field=field, layer=layer)
        cls, rel, tree = _load_declared_class(pkg, item, field=field, layer=layer)
        if tree is not None:
            _check_runtime_path_policy(tree, rel, "Rail", layer)
        if cls is None:
            continue
        if not _has_base(cls, "Rail"):
            layer.error(f"{rel}: {cls.name} 必须继承 Rail 基类（如 DeepAgentRail）")
        _check_no_arg_init(
            cls,
            rel,
            layer,
            required=False,
            fix="Rail 由加载器无参构造，把依赖挪到生命周期钩子里获取",
        )


def _check_structure(pkg: Path, manifest: dict[str, Any], layer: Layer) -> None:
    _check_manifest_basics(pkg, manifest, layer)
    _check_unsupported_sections(manifest, layer)
    _check_persona(pkg, manifest, layer)
    _check_skills(pkg, manifest, layer)
    _check_tools(pkg, manifest, layer)
    _check_rails(pkg, manifest, layer)


# ---------------------------------------------------------------------------
# 入口：静态校验
# ---------------------------------------------------------------------------


def validate_static(pkg: Path) -> list[Layer]:
    """L0 + L1：纯 stdlib，一趟跑完，全量报错。"""
    quality = Layer(LAYER_QUALITY)
    static = Layer(LAYER_STATIC)

    manifest_path = pkg / "manifest.json"
    if not manifest_path.is_file():
        static.error("manifest.json 不存在", "先运行 init_template.py 初始化包目录")
        return [quality, static]

    manifest = _read_json(manifest_path, static, label="manifest.json")
    if manifest is None:
        return [quality, static]

    _check_quality(pkg, manifest, quality)
    _check_structure(pkg, manifest, static)
    return [quality, static]


# ---------------------------------------------------------------------------
# 入口：热加载校验（子进程 worker，主脚本不 import openjiuwen）
# ---------------------------------------------------------------------------


def _apply_worker_result(layer: Layer, payload: dict[str, Any]) -> None:
    status = str(payload.get("status") or "")
    if status == "skip":
        layer.skip(
            str(payload.get("skip_reason") or "热加载被跳过"),
            str(payload.get("skip_fix") or ""),
        )
        return
    for item in payload.get("errors") or []:
        if isinstance(item, (list, tuple)) and item:
            msg = str(item[0])
            fix = str(item[1]) if len(item) > 1 else ""
            layer.error(msg, fix)
        elif isinstance(item, str):
            layer.error(item)
    for note in payload.get("notes") or []:
        layer.note(str(note))
    if status not in ("pass", "fail"):
        layer.skip(
            f"worker 返回未知 status: {status!r}",
            "检查 validate_hot_load_worker.py 输出协议",
        )


def _parse_worker_stdout(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def validate_hot_load(pkg: Path) -> Layer:
    """L2：子进程跑 worker，解析最后一行 JSON，灌回 Layer。"""
    layer = Layer(LAYER_HOT_LOAD)
    if not HOT_LOAD_WORKER.is_file():
        layer.skip(
            f"找不到热加载 worker: {HOT_LOAD_WORKER}",
            "确认 scripts/validate_hot_load_worker.py 与本脚本同目录",
        )
        return layer

    cmd = [sys.executable, str(HOT_LOAD_WORKER), str(pkg)]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=HOT_LOAD_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        layer.skip(
            f"热加载 worker 超时（>{HOT_LOAD_TIMEOUT_SEC}s）",
            "检查包内 tool/rail 是否在 import/绑定阶段卡住",
        )
        return layer
    except OSError as exc:
        layer.skip(
            f"无法启动热加载 worker: {exc}",
            "检查当前 python 与 scripts/validate_hot_load_worker.py",
        )
        return layer

    payload = _parse_worker_stdout(completed.stdout or "")
    if payload is None:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > 300:
            detail = detail[-300:]
        hint = f"；stderr/stdout 尾部: {detail}" if detail else ""
        layer.skip(
            f"热加载 worker 未返回可解析 JSON（exit={completed.returncode}）{hint}",
            "这不是包的问题，请检查 openjiuwen 运行环境或 worker 崩溃日志",
        )
        return layer

    _apply_worker_result(layer, payload)
    return layer


# ---------------------------------------------------------------------------
# 汇总输出
# ---------------------------------------------------------------------------


def _print_summary(quality: Layer, static: Layer, hot: Layer) -> None:
    parts = []
    for layer in (quality, static, hot):
        state = "跳过" if layer.status == "SKIP" else f"{len(layer.errors)}错误"
        parts.append(f"{layer.tag}={state}")
    summary = "  ".join(parts)

    if static.errors:
        tail = f"，再修 L0 的 {len(quality.errors)} 项" if quality.errors else ""
        write_stdout(f"RESULT: FAIL   {summary}\n")
        write_stdout(
            f"NEXT:   先修 L1 的 {len(static.errors)} 项（否则包无法加载）{tail}，然后重跑本脚本\n"
        )
        return
    if quality.errors:
        write_stdout(f"RESULT: FAIL   {summary}\n")
        write_stdout(f"NEXT:   修完 L0 的 {len(quality.errors)} 项后重跑本脚本\n")
        return
    if hot.errors:
        write_stdout(f"RESULT: FAIL   {summary}\n")
        write_stdout("NEXT:   按 L2 的报错修复包内容后重跑本脚本\n")
        return
    if hot.status == "SKIP":
        write_stdout(f"RESULT: PARTIAL   {summary}\n")
        write_stdout(
            "NEXT:   L0 / L1 已通过，但热加载未执行（SKIP 不等于通过），处理方式见上方 L2\n"
        )
        return
    write_stdout(f"RESULT: PASS   {summary}\n")
    write_stdout(
        "NEXT:   运行 register_template.py 注册 marketplace，再按 SKILL.md「输出规范」回复用户\n"
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="校验 agent 模板包（L0 规范 / L1 静态 / L2 热加载）"
    )
    parser.add_argument("target", help="包目录绝对路径，或 local/ 下的 agent-name")
    parser.add_argument(
        "--no-hot-load", action="store_true", help="只跑 L0 + L1，跳过热加载"
    )
    args = parser.parse_args()

    try:
        pkg = resolve_pkg(args.target)
    except FileNotFoundError as exc:
        write_stdout(f"Error: {exc}\n")
        return 1

    write_stdout(f"Validating agent template: {pkg.name}\n")
    write_stdout(f"  Path: {pkg}\n\n")

    quality, static = validate_static(pkg)
    if args.no_hot_load:
        hot = Layer(LAYER_HOT_LOAD)
        hot.skip("已通过 --no-hot-load 显式跳过", "去掉该参数即可执行热加载")
    elif quality.errors or static.errors:
        hot = Layer(LAYER_HOT_LOAD)
        hot.skip("静态校验未通过，跳过热加载", "先修完 L0 / L1 的错误再重跑")
    else:
        hot = validate_hot_load(pkg)

    for layer in (quality, static, hot):
        write_stdout(layer.render() + "\n")
        write_stdout("\n")
    _print_summary(quality, static, hot)

    ok = all(layer.status == "PASS" for layer in (quality, static, hot))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

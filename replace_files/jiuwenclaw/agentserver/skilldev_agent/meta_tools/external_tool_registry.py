# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load and normalize user-uploaded external tool definitions from the task workspace."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AVAILABLE_TOOLS_REL = Path("resources") / "available-tools"
TOOL_USAGE_FILENAME = "tool_usage.json"
_TOOL_SPEC_FILENAME_RE = re.compile(r"^(.+)__(.+)\.json$", re.IGNORECASE)
_TOOL_SPEC_TRANSPORT_KEYS = frozenset({"filename", "base64Data", "base64", "name", "url", "mime"})


@dataclass(frozen=True)
class ExternalToolKey:
    plugin_id: str
    tool_name: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.plugin_id, self.tool_name)


@dataclass(frozen=True)
class ExternalToolSpec:
    """Normalized external tool definition."""

    plugin_id: str
    tool_name: str
    description: str
    protocol: str
    plugin_type: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    source_file: str = ""

    @property
    def key(self) -> ExternalToolKey:
        return ExternalToolKey(plugin_id=self.plugin_id, tool_name=self.tool_name)

    def parameter_summary(self) -> list[dict[str, Any]]:
        props = self.parameters.get("properties", {})
        if not isinstance(props, dict):
            return []
        required = set(self.parameters.get("required") or [])
        out: list[dict[str, Any]] = []
        for pname, pinfo in props.items():
            if not isinstance(pinfo, dict):
                continue
            out.append(
                {
                    "name": pname,
                    "type": pinfo.get("type", "unknown"),
                    "description": pinfo.get("description", ""),
                    "required": pname in required,
                }
            )
        return out

    def to_list_entry(self) -> dict[str, Any]:
        return {
            "pluginId": self.plugin_id,
            "toolName": self.tool_name,
            "description": self.description,
            "protocol": self.protocol,
            "arguments": self.parameters,
            "parameters": self.parameter_summary(),
            "source_file": self.source_file,
        }

    def to_definition_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pluginId": self.plugin_id,
            "toolName": self.tool_name,
            "description": self.description,
            "protocol": self.protocol,
            "arguments": self.parameters,
        }
        if self.plugin_type:
            out["pluginType"] = self.plugin_type
        return out

    def to_usage_catalog_entry(self) -> dict[str, Any]:
        """Entry for ``tool_usage.json`` — how to invoke via ``function_call_tool``."""
        arg_hints: dict[str, str] = {}
        for param in self.parameter_summary():
            name = param.get("name", "")
            if not name:
                continue
            ptype = param.get("type", "string")
            arg_hints[name] = f"<{'必填' if param.get('required') else '可选'}{ptype}>"
        arguments_example = arg_hints if arg_hints else {}
        return {
            "pluginId": self.plugin_id,
            "toolName": self.tool_name,
            "pluginType": self.plugin_type,
            "description": self.description,
            "protocol": self.protocol,
            "parameters": self.parameter_summary(),
            "definition_file": tool_spec_filename(self.plugin_id, self.tool_name),
            "invoker": "function_call_tool",
            "usage": (
                "调用 function_call_tool（pluginId、toolName、arguments 均必填）"
                f'示例: {{"pluginId": "{self.plugin_id}", "toolName": "{self.tool_name}", '
                f'"arguments": {json.dumps(arguments_example, ensure_ascii=False)}}}'
            ),
        }


def sanitize_tool_spec_segment(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value.strip())


def resolve_tool_spec_identity(raw: dict[str, Any]) -> tuple[str, str]:
    """Resolve plugin id and tool name from an uploaded tool definition."""
    plugin_id = str(
        raw.get("pluginId")
        or raw.get("bundleName")
        or raw.get("toolId")
        or raw.get("plugin_id")
        or ""
    ).strip()
    tool_name = str(raw.get("toolName") or raw.get("name") or "").strip()
    return plugin_id, tool_name


def tool_spec_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip transport-only fields; keep the uploaded tool definition as-is."""
    return {k: v for k, v in raw.items() if k not in _TOOL_SPEC_TRANSPORT_KEYS}


def tool_spec_filename(plugin_id: str, tool_name: str) -> str:
    """Canonical on-disk name: ``<pluginId>__<toolName>.json``."""
    safe_plugin = plugin_id
    safe_tool = tool_name
    if not safe_plugin or not safe_tool:
        raise ValueError("pluginId 与 toolName 均不能为空")
    return f"{safe_plugin}__{safe_tool}.json"


def normalize_tool_definition(raw: dict[str, Any], *, source_file: str = "") -> ExternalToolSpec | None:
    if not isinstance(raw, dict):
        return None
    plugin_id, tool_name = resolve_tool_spec_identity(raw)
    if not plugin_id or not tool_name:
        return None
    description = str(raw.get("description") or "").strip()
    protocol = str(raw.get("protocol") or "REST").strip().upper() or "REST"
    plugin_type = str(raw.get("pluginType") or raw.get("plugin_type") or "").strip()
    parameters = raw.get("arguments") or raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        parameters = {}
    return ExternalToolSpec(
        plugin_id=plugin_id,
        tool_name=tool_name,
        description=description,
        protocol=protocol,
        plugin_type=plugin_type,
        parameters=parameters,
        source_file=source_file,
    )


def iter_tool_definitions_from_json(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def write_tool_spec_file(dest_dir: Path, tool_def: dict[str, Any]) -> Path:
    """Write one tool definition to ``<pluginId>__<toolName>.json`` (pass-through payload)."""
    plugin_id, tool_name = resolve_tool_spec_identity(tool_def)
    if not plugin_id or not tool_name:
        raise ValueError("工具定义缺少 pluginId/bundleName 与 toolName")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / tool_spec_filename(plugin_id, tool_name)
    tool_def["schemaVersion"] = "1.0"
    out_path.write_text(
        json.dumps(tool_spec_payload(tool_def), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def _register_spec(
    registry: dict[tuple[str, str], ExternalToolSpec],
    spec: ExternalToolSpec,
) -> None:
    registry[spec.key.as_tuple()] = spec


def _load_tools_from_dir(tools_dir: Path) -> dict[tuple[str, str], ExternalToolSpec]:
    registry: dict[tuple[str, str], ExternalToolSpec] = {}
    if not tools_dir.is_dir():
        return registry
    for json_file in sorted(tools_dir.glob("*.json")):
        if json_file.name == TOOL_USAGE_FILENAME:
            continue
        try:
            parsed = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[external_tool_registry] skip %s: %s", json_file.name, exc)
            continue
        for item in iter_tool_definitions_from_json(parsed):
            spec = normalize_tool_definition(item, source_file=json_file.name)
            if spec is not None:
                _register_spec(registry, spec)
    return registry


def load_external_tools(workspace_dir: str | Path) -> dict[tuple[str, str], ExternalToolSpec]:
    """Load external tools keyed by ``(pluginId, toolName)``."""
    tools_dir = Path(workspace_dir) / AVAILABLE_TOOLS_REL
    return _load_tools_from_dir(tools_dir)


def resolve_available_tools_dir(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir) / AVAILABLE_TOOLS_REL


def write_tool_usage_catalog(tools_dir: Path) -> Path | None:
    """Regenerate ``tool_usage.json`` from all ``<pluginId>__<toolName>.json`` in *tools_dir*."""
    tools_dir = Path(tools_dir)
    registry = _load_tools_from_dir(tools_dir)

    if not registry:
        usage_path = tools_dir / TOOL_USAGE_FILENAME
        if usage_path.is_file():
            usage_path.unlink()
        return None

    catalog = [spec.to_usage_catalog_entry() for spec in registry.values()]
    usage_path = tools_dir / TOOL_USAGE_FILENAME
    usage_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "[external_tool_registry] wrote %s (%d tools)",
        usage_path.name,
        len(catalog),
    )
    return usage_path


def format_tool_usage_hint() -> str:
    """Brief resource_hint text: what ``tool_usage.json`` is and how to use it."""
    rel = f"{AVAILABLE_TOOLS_REL.as_posix()}/{TOOL_USAGE_FILENAME}"
    return (
        "## 外部工具索引（tool_usage.json）\n"
        f"- 路径：`{rel}`（本任务工作区内；单工具定义见同目录 `<pluginId>__<toolName>.json`）\n"
        "- 作用：汇总已上传工具的 pluginId、toolName、参数说明及 function_call_tool 调用示例。\n"
        "- 用法：用 file_read 读取该文件后，按条目试调 function_call_tool"
        "（pluginId、toolName、arguments 均必填；无参时 arguments 为 {}）。"
    )

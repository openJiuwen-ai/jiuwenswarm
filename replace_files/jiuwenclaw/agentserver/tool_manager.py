
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ToolManager - tools.add 等 RPC：落盘用户 MCP 工具配置并交给 mcp_toolkits 生成 McpServerConfig。"""

from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner

from jiuwenclaw.utils import get_agent_tools_dir, logger

from jiuwenclaw.agentserver.tools.ephemeral_stdio_mcp_tool import (
    EphemeralStdioMcpTool,
    list_stdio_mcp_tool_defs,
    stdio_params_from_mcp_config,
)
from jiuwenclaw.agentserver.tools.mcp_toolkits import create_mcp_tool

_CAT_CAFE_SERVER_NAME_PREFIX = "cat-cafe"
_REQUEST_SCOPED_CAT_CAFE_SERVER_ID = "cat-cafe-request"

# 请求级 stdio 参数：每个异步上下文独立，避免并发请求间配置串台
_CAT_CAFE_STDIO_PARAMS: ContextVar[dict[str, Any]] = ContextVar("_CAT_CAFE_STDIO_PARAMS", default={})


def _get_cat_cafe_stdio_params() -> dict[str, Any]:
    """回调函数：供 EphemeralStdioMcpTool 在 invoke 时获取当前请求级的 stdio 参数。"""
    return _CAT_CAFE_STDIO_PARAMS.get()


_CAT_CAFE_SERVER_NAME_PREFIX = "cat-cafe"
_REQUEST_SCOPED_CAT_CAFE_SERVER_ID = "cat-cafe-request"


def _mcp_add_result_is_ok(result: Any) -> bool:
    """解析 ``add_mcp_server`` 返回值。"""
    if result is None:
        return True
    is_ok = getattr(result, "is_ok", None)
    if callable(is_ok):
        try:
            return bool(is_ok())
        except Exception:
            return False
    return False


def _mcp_add_result_error_text(result: Any) -> str:
    """与 ``browser_tools._result_error_text`` 一致。"""
    if result is None:
        return ""
    for attr in ("error", "msg"):
        fn = getattr(result, attr, None)
        if callable(fn):
            try:
                value = fn()
                if value is not None:
                    return str(value)
            except Exception:
                logger.debug("[ToolManager] callable error")
                pass
    value = getattr(result, "_error", None)
    if value is not None:
        return str(value)
    return str(result)


async def _add_mcp_server_and_ability(agent: Any, mcp_cfg: Any, *, tag: str) -> None:
    """调用 ``add_mcp_server``，按返回值决定是否 ``ability_manager.add``；失败抛 ``RuntimeError``。"""
    result = await Runner.resource_mgr.add_mcp_server(mcp_cfg, tag=tag)
    if _mcp_add_result_is_ok(result):
        agent.ability_manager.add(mcp_cfg)
        return
    err = _mcp_add_result_error_text(result)
    if "already exist" in err.lower():
        agent.ability_manager.add(mcp_cfg)
        logger.info("[ToolManager] add_mcp_server 已存在，仍加入 ability_manager: %s", err)
        return
    raise RuntimeError(f"add_mcp_server 失败: {err}" if err else "add_mcp_server 失败")


# ---------------------------------------------------------------------------
# 落盘 JSON 模板：列表顺序即写入顺序；每项为 (disk_key, default, kind)。
# kind:
#   tool_name — 使用 mcpServers 的 key，忽略 cfg 里的同名键
#   text      — 字符串；cfg 缺省用 default，非 str 则 str()
#   list      — 列表；cfg 非 list 则用 default 的拷贝
#   any       — cfg.get(source_key, default)；default 为 dict/list 时做浅拷贝
# 扩展字段：在列表末尾追加 ("new_key", default, "text"|"list"|"any") 即可。
# ---------------------------------------------------------------------------
TOOL_DISK_SCHEMA: list[tuple[str, Any, str]] = [
    ("name", "", "tool_name"),
    ("description", "", "text"),
    ("type", "", "text"),
    ("url", "", "text"),
    ("env", {}, "any"),
    ("auth_headers", {}, "any"),
    ("auth_query_params", {}, "any"),
    ("command", "", "text"),
    ("args", [], "list"),
]

# 落盘字段名 -> mcpServers 内对象中的键名（缺省表示与 disk_key 相同）
TOOL_DISK_SOURCE_MAP: dict[str, str] = {}


def _mutable_default_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def _coerce_tool_disk_value(
        disk_key: str,
        default: Any,
        kind: str,
        tool_name: str,
        cfg: dict[str, Any],
) -> Any:
    src = TOOL_DISK_SOURCE_MAP.get(disk_key, disk_key)
    if kind == "tool_name":
        return (tool_name or "").strip()
    if kind == "text":
        raw = cfg.get(src, default)
        if raw is None:
            return ""
        return raw if isinstance(raw, str) else str(raw)
    if kind == "list":
        raw = cfg.get(src, default)
        return list(raw) if isinstance(raw, list) else _mutable_default_copy(default)
    if kind == "any":
        if src not in cfg:
            return _mutable_default_copy(default)
        return cfg[src]
    raise ValueError(f"TOOL_DISK_SCHEMA 未知 kind={kind!r}，字段={disk_key!r}")


def _tool_record_for_disk(tool_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """按 TOOL_DISK_SCHEMA + TOOL_DISK_SOURCE_MAP 生成落盘对象，再合并 cfg 中未消费的键。"""
    record: dict[str, Any] = {}
    sources_used: set[str] = set()

    for disk_key, default, kind in TOOL_DISK_SCHEMA:
        src = TOOL_DISK_SOURCE_MAP.get(disk_key, disk_key)
        sources_used.add(src)
        record[disk_key] = _coerce_tool_disk_value(disk_key, default, kind, tool_name, cfg)

    for key, val in cfg.items():
        if key in record or key in sources_used:
            continue
        record[key] = val

    return record


def _safe_tool_file_stem(tool_name: str) -> str:
    name = (tool_name or "").strip()
    if not name:
        raise ValueError("工具名不能为空")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"非法工具名: {tool_name!r}")
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not safe or not re.search(r"[a-zA-Z0-9]", safe):
        raise ValueError(f"非法工具名: {tool_name!r}")
    return safe


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


class ToolManager:
    """管理 tools 相关 RPC（与 SkillManager 的 handler 命名风格一致）。"""

    def __init__(
            self,
            get_agent: Callable[[], Any] | None = None,
            *,
            get_tools_dir: Callable[[], Path] | None = None,
    ) -> None:
        """get_agent: 返回当前底层 Agent，用于 ``Runner.resource_mgr`` / ``ability_manager``。

        get_tools_dir: 可选；返回 MCP 配置落盘目录。多租户下由 ``JiuWenClaw`` 传入
        ``jiuwenclaw_workspace/tools``；缺省回退 ``utils.get_agent_tools_dir()``。
        """
        self._get_agent = get_agent
        self._get_tools_dir = get_tools_dir
        # (tool_id, tool_name)，请求级 Cat Cafe stdio 走 ephemeral 注册时用于下次替换前卸载
        self._cat_cafe_ephemeral_tools: list[tuple[str, str]] = []

    def _resolve_tools_dir(self) -> Path:
        if self._get_tools_dir is not None:
            return self._get_tools_dir()
        return get_agent_tools_dir()

    @staticmethod
    def find_host_project_mcp_json() -> Path | None:
        """固定从宿主 Clowder AI 根目录查找 ``.mcp.json``。"""
        host_root = (os.getenv("CAT_CAFE_MCP_CWD") or "").strip()
        if not host_root:
            return None
        candidate = Path(host_root).resolve() / ".mcp.json"
        if candidate.is_file():
            return candidate
        return None

    async def load_project_mcp_json(self, mcp_json_path: str | Path) -> dict[str, Any]:
        """从项目根目录的 ``.mcp.json`` 导入工具，并复用 ``tools.add`` 的注册逻辑。"""
        path = Path(mcp_json_path)
        if not path.exists():
            return {
                "source": str(path),
                "saved": [],
                "registered_tools": [],
                "skipped": True,
                "reason": "not_found",
            }

        try:
            mcp_json = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"读取项目 MCP 配置失败: {exc}") from exc

        payload = await self.handle_tools_add({"mcp_json": mcp_json})
        payload["source"] = str(path.resolve())
        payload["skipped"] = False
        return payload

    async def handle_tools_add(self, params: dict) -> dict[str, Any]:
        """按工具名拆分落盘；对每个工具调用 ``create_mcp_tool`` 注册。

        params:
            mcp_json: str，根对象须含 ``mcpServers``，每个 key 为工具名。
        """
        mcp_json = params.get("mcp_json")
        if not isinstance(mcp_json, str) or not mcp_json.strip():
            raise ValueError("缺少参数：请提供 mcp_json（JSON 字符串）")

        try:
            root = json.loads(mcp_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc

        if not isinstance(root, dict):
            raise ValueError("根节点必须是 JSON 对象")
        servers = root.get("mcpServers")
        if not isinstance(servers, dict):
            raise ValueError("缺少有效的 mcpServers 对象")

        for tool_name, cfg in servers.items():
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError(f"非法的工具名: {tool_name!r}")
            if not isinstance(cfg, dict):
                raise ValueError(f"mcpServers[{tool_name!r}] 必须是对象")

        agent = self._get_agent() if self._get_agent else None
        if agent is None:
            raise RuntimeError("JiuWenClaw 未初始化，请先调用 create_instance()")

        saved: list[dict[str, str]] = []
        registered: list[dict[str, str]] = []
        tools_dir = self._resolve_tools_dir()
        for tool_name, cfg in servers.items():
            stem = _safe_tool_file_stem(tool_name)
            out_path = tools_dir / f"{stem}.json"
            record = _tool_record_for_disk(tool_name, cfg)
            _atomic_write_json(out_path, record)
            saved.append({"name": tool_name, "path": str(out_path.resolve())})
            logger.info("[ToolManager] 已写入工具配置 name=%s path=%s", tool_name, out_path)

            single_json = json.dumps(record, ensure_ascii=False)
            mcp_cfg = create_mcp_tool(single_json)
            try:
                await _add_mcp_server_and_ability(agent, mcp_cfg, tag=mcp_cfg.server_name)
            except Exception as exc:
                logger.error("[ToolManager] 注册工具失败 name=%s: %s", tool_name, exc)
                raise
            registered.append({"name": mcp_cfg.server_name, "id": mcp_cfg.server_id})
            logger.info("[ToolManager] 已注册工具 name=%s id=%s", mcp_cfg.server_name, mcp_cfg.server_id)

        return {
            "saved": saved,
            "tools_dir": str(tools_dir.resolve()),
            "registered_tools": registered,
        }

    async def load_tools_from_disk(self, skip_server_names: set[str] | None = None) -> dict[str, Any]:
        """启动时扫描 ``tools/*.json``，按落盘记录注册 MCP 工具（MR !379）。"""
        agent = self._get_agent() if self._get_agent else None
        if agent is None:
            raise RuntimeError("JiuWenClaw 未初始化，请先调用 create_instance()")

        tools_dir = self._resolve_tools_dir()
        tools_dir.mkdir(parents=True, exist_ok=True)
        registered: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        skipped_names = {name for name in (skip_server_names or set()) if isinstance(name, str) and name}

        for path in sorted(tools_dir.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[ToolManager] 跳过无效工具配置 %s: %s", path, exc)
                errors.append({"path": str(path), "error": str(exc)})
                continue
            if not isinstance(record, dict):
                logger.warning("[ToolManager] 跳过非对象 JSON: %s", path)
                errors.append({"path": str(path), "error": "根节点须为 JSON 对象"})
                continue

            name_hint = record.get("name") if isinstance(record.get("name"), str) else path.stem
            if name_hint in skipped_names:
                logger.info("[ToolManager] 跳过已从项目 .mcp.json 同步的工具 name=%s path=%s", name_hint, path)
                continue
            try:
                single_json = json.dumps(record, ensure_ascii=False)
                mcp_cfg = create_mcp_tool(single_json)
                await _add_mcp_server_and_ability(agent, mcp_cfg, tag=mcp_cfg.server_name)
            except Exception as exc:
                logger.error("[ToolManager] 启动加载工具失败 %s (%s): %s", path, name_hint, exc)
                errors.append({"path": str(path), "error": str(exc)})
                continue

            registered.append({"name": mcp_cfg.server_name, "id": mcp_cfg.server_id})
            logger.info(
                "[ToolManager] 启动已加载工具 name=%s id=%s path=%s",
                mcp_cfg.server_name,
                mcp_cfg.server_id,
                path,
            )

        return {
            "tools_dir": str(tools_dir.resolve()),
            "registered_tools": registered,
            "errors": errors,
        }

    async def register_request_scoped_cat_cafe_mcp(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """按请求注册 Cat Cafe MCP；stdio 工具以单次 invoke 模式执行。"""
        if not isinstance(cfg, dict):
            raise ValueError("cat_cafe_mcp 必须是对象")

        agent = self._get_agent() if self._get_agent else None
        if agent is None:
            raise RuntimeError("JiuWenClaw 未初始化，请先调用 create_instance()")

        names_to_remove = [
            name
            for name in getattr(agent.ability_manager, "_mcp_servers", {}).keys()
            if isinstance(name, str) and (name == _CAT_CAFE_SERVER_NAME_PREFIX or name.startswith(f"{_CAT_CAFE_SERVER_NAME_PREFIX}-"))
        ]
        for server_name in names_to_remove:
            get_server_ids = getattr(Runner.resource_mgr, "get_mcp_server_ids", None)
            server_ids = list(get_server_ids(server_name) or []) if callable(get_server_ids) else []
            for server_id in server_ids:
                try:
                    await Runner.resource_mgr.remove_tool_server(server_id, ignore_not_exist=True)
                except Exception as exc:
                    logger.warning("[ToolManager] 移除旧的 Cat Cafe MCP 失败 name=%s id=%s: %s", server_name, server_id, exc)
            agent.ability_manager.remove(server_name)

        record = {
            "name": _CAT_CAFE_SERVER_NAME_PREFIX,
            "server_id": _REQUEST_SCOPED_CAT_CAFE_SERVER_ID,
            **cfg,
        }
        single_json = json.dumps(record, ensure_ascii=False)
        mcp_cfg = create_mcp_tool(single_json)

        # stdio：不经过 add_mcp_server，每工具每次 invoke 单独起停子进程，避免会话间状态串台
        if getattr(mcp_cfg, "client_type", "") == "stdio":
            stdio_sp = stdio_params_from_mcp_config(mcp_cfg.params or {})
            _CAT_CAFE_STDIO_PARAMS.set(stdio_sp)

            if not self._cat_cafe_ephemeral_tools:
                try:
                    tool_defs = await list_stdio_mcp_tool_defs(mcp_cfg.params or {})
                except Exception as exc:
                    raise RuntimeError(f"列举 Cat Cafe stdio MCP 工具失败: {exc}") from exc
                for td in tool_defs:
                    tname = td["name"]
                    tool_id = f"{mcp_cfg.server_id}.{mcp_cfg.server_name}.{tname}"
                    card = ToolCard(
                        id=tool_id,
                        name=tname,
                        description=td.get("description") or "",
                        input_params=td.get("input_params") or {},
                    )
                    ephemeral = EphemeralStdioMcpTool(card, _get_cat_cafe_stdio_params)
                    add_res = Runner.resource_mgr.add_tool(ephemeral, tag=mcp_cfg.server_name)
                    if add_res is not None and hasattr(add_res, "is_ok") and not add_res.is_ok():
                        err = _mcp_add_result_error_text(add_res)
                        if "already exist" not in err.lower():
                            raise RuntimeError(f"注册 ephemeral Cat Cafe 工具失败 {tname}: {err}")
                        logger.info(
                            "[ToolManager] ephemeral Cat Cafe 工具已存在，复用现有资源 tool=%s id=%s err=%s",
                            tname,
                            tool_id,
                            err,
                        )
                    agent.ability_manager.add(card)
                    self._cat_cafe_ephemeral_tools.append((tool_id, tname))
                logger.info(
                    "[ToolManager] 已注册请求级 Cat Cafe MCP（stdio 每调用隔离）name=%s id=%s tools=%s",
                    mcp_cfg.server_name,
                    mcp_cfg.server_id,
                    [t[1] for t in self._cat_cafe_ephemeral_tools],
                )
            else:
                logger.info(
                    "[ToolManager] 已有 ephemeral Cat Cafe 工具，仅更新 stdio 参数 tools=%s",
                    [t[1] for t in self._cat_cafe_ephemeral_tools],
                )
            return {
                "registered": True,
                "name": mcp_cfg.server_name,
                "server_id": mcp_cfg.server_id,
            }

        await _add_mcp_server_and_ability(agent, mcp_cfg, tag=mcp_cfg.server_name)
        logger.info("[ToolManager] 已注册请求级 Cat Cafe MCP name=%s id=%s", mcp_cfg.server_name, mcp_cfg.server_id)
        return {
            "registered": True,
            "name": mcp_cfg.server_name,
            "server_id": mcp_cfg.server_id,
        }

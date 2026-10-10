# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP marketplace registry.

Reads the package cache under ``<workspace>/mcp/mcp_builtins/`` and
exposes MCP records for the ``mcp.*`` RPC handlers. Each package's display
metadata comes from the marketplace index ``index.json`` (same dir, keyed by
``source`` == package name); connection/enabled state is derived from
``<workspace>/mcp/state.json`` (merged with config.yaml's hand-written
``mcp.servers`` by ``get_mcp_servers``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_workspace_dir  # re-export for test patches

from jiuwenswarm.server.runtime.mcp.paths import (
    has_skill_file as _has_skill_file,
    load_json,
)

logger = logging.getLogger(__name__)

# MCP 工作区根目录：所有 MCP 相关数据（内置包缓存/连接状态/凭证/
# 已装 skill）统一收敛到 <workspace>/mcp/ 下（实现在 paths.py）。
# marketplace 源索引：index.json 按 source(== 包名) 列出每个 MCP 的
# name/name_en/description_zh/examples 等展示字段，与包目录同放 mcp_builtins/ 下。
_INDEX_FILE = "index.json"

# Fields masked out of the returned mcp_spec to avoid leaking secrets.
_SENSITIVE_MCP_KEYS = frozenset({"headers", "env"})


_MCP_ROOT = "mcp"
_PACKAGES_SUBDIR = "mcp_builtins"

# Thin re-exports so existing call sites (and tests that patch
# registry._load_json) keep working after load_json moved to paths.py.
_load_json = load_json


def _mcp_root() -> Path:
    """<workspace>/mcp/ — unified MCP data root."""
    return get_workspace_dir() / _MCP_ROOT


def _packages_dir() -> Path:
    """<workspace>/mcp/mcp_builtins/ — marketplace 包目录 + index.json."""
    return _mcp_root() / _PACKAGES_SUBDIR


def _detect_integration_type(pkg_dir: Path) -> str:
    """Classify an MCP package by its integration shape.

    Detection precedence (each checked in order — the first match wins):

      1. cli.json present                       → ``cli``        (form C)
         The CLI binary manages its own runtime + auth (feishu/dingtalk/...).

      2. mcp.json with a usable mcpServers entry:
         - command present                      → ``stdio-mcp``  (form B)
         - url present                          → ``remote-mcp`` (form A)

      3. No cli.json, no usable mcp.json, but a ``skills/`` directory exists
         → ``skill-only``  (form D — ctrip-wendao, netease-mail).
         No MCP server; the MCP IS its bundled skill scripts. Credential
         acquisition is orthogonal (token-schema.json, no creds, or a future
         scheme) so we don't key on any one file — a skills/ dir with no MCP
         host is the defining trait.

      4. otherwise                             → ``remote-mcp`` (fallback)
    """
    if (pkg_dir / "cli.json").is_file():
        return "cli"
    mcp = _load_json(pkg_dir / "mcp.json")
    if mcp and isinstance(mcp.get("mcpServers"), dict):
        first = next(iter(mcp["mcpServers"].values()), {})
        if isinstance(first, dict):
            if first.get("command"):
                return "stdio-mcp"
            if first.get("url"):
                return "remote-mcp"
    # No cli.json and no usable mcp.json. If the package ships a skills/
    # directory, it's a skill-only MCP — the skills ARE the MCP (no MCP
    # server to spawn). token-schema.json is just one possible
    # credential source; a pure-credentialless skill package also lands here.
    if (pkg_dir / "skills").is_dir():
        return "skill-only"
    return "remote-mcp"


def _load_index_entry(name: str) -> dict[str, Any]:
    """从索引 index.json 按 source==name 查一条 MCP 记录。"""
    idx_path = _packages_dir() / _INDEX_FILE
    data = _load_json(idx_path)
    if not data or not isinstance(data.get("mcps"), list):
        return {}
    for c in data["mcps"]:
        if isinstance(c, dict) and str(c.get("source", "")).strip() == name:
            return c
    return {}


def _connector_meta_by_name(name: str) -> dict[str, Any]:
    """Load display metadata for a connector MCP by marketplace name.
    """
    n = str(name or "").strip()
    if not n:
        return {}
    return _load_index_entry(n)


def get_connector_catalog_display(name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(displayName, displayDescription)`` for equipment show cards.

    Values are ``{zh, en}`` dicts aligned with extension catalog cards.
    Unknown connectors fall back to the connector id and empty description.
    """
    n = str(name or "").strip()
    if not n:
        return {"zh": "", "en": ""}, {"zh": "", "en": ""}
    meta = _connector_meta_by_name(n)
    display_name = {
        "zh": str(
            meta.get("display_name")
            or meta.get("name")
            or meta.get("name_zh")
            or n
        ),
        "en": str(
            meta.get("name_en")
            or meta.get("display_name_en")
            or meta.get("name")
            or meta.get("display_name")
            or n
        ),
    }
    display_desc = {
        "zh": str(meta.get("description_zh") or meta.get("description") or ""),
        "en": str(
            meta.get("description")
            or meta.get("description_en")
            or meta.get("description_zh")
            or ""
        ),
    }
    return display_name, display_desc


def _bundled_skill_names(pkg_dir: Path) -> list[str]:
    """Bundled skill runtime names (handles nested + flat marketplace layouts)."""
    skills_dir = pkg_dir / "skills"
    if not skills_dir.is_dir():
        return []
    # flat layout: SKILL.md sits directly under skills/ -> one skill named after the package
    if _has_skill_file(skills_dir):
        return [pkg_dir.name]
    names: list[str] = []
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_dir() and _has_skill_file(entry):
            names.append(entry.name)
    return names


def _mask_mcp_spec(mcp_spec: dict[str, Any]) -> dict[str, Any]:
    masked = dict(mcp_spec)
    for key in _SENSITIVE_MCP_KEYS:
        if key in masked and isinstance(masked[key], dict):
            masked[key] = {k: "***" for k in masked[key]}
    return masked


# Icon 文件名候选：marketplace 包目录下 icon.svg 或 icon.png（固定命名，
# index.json 未声明 icon，靠探测磁盘确定）。
_ICON_FILES = ("icon.svg", "icon.png")


def _load_icon_data_url(pkg_dir: Path) -> str | None:
    """Read a package's icon as a data URL (svg/png inlined), or None.

    Frontend can't reach the workspace filesystem, so inline the icon bytes
    as a data URL the <img src> can render directly — no extra HTTP route
    or fetch round-trip. SVG/PNG icons are tiny (a few KB), so the base64
    overhead in the summary payload is acceptable. Returns None when the
    package ships no icon file (frontend falls back to a default/initial).
    """
    import base64
    for fname in _ICON_FILES:
        p = pkg_dir / fname
        if not p.is_file():
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        ext = p.suffix.lower().lstrip(".")
        mime = "image/svg+xml" if ext == "svg" else "image/png"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return None


def _build_summary(
    name: str,
    pkg_dir: Path,
    meta: dict[str, Any],
    connected_names: set[str],
    connecting_names: set[str] | None = None,
) -> dict[str, Any]:
    integration_type = _detect_integration_type(pkg_dir)
    bundled = _bundled_skill_names(pkg_dir)
    is_connected = name in connected_names
    is_connecting = name in (connecting_names or set())
    # connection_state: connected (apply done) > connecting (connect in
    # progress, apply not yet run / OAuth in flight) > disconnected. The
    # "connecting" badge lets the user reload the tab mid-connect and still
    # see "connecting" — not a phantom "connected" before apply finishes.
    if is_connected:
        connection_state = "connected"
    elif is_connecting:
        connection_state = "connecting"
    else:
        connection_state = "disconnected"
    # Icon: source meta.icon is unreliable (mostly unset); probe disk for
    # icon.svg/icon.png and inline as data URL so the frontend renders it
    # without an extra HTTP route.
    icon = _load_icon_data_url(pkg_dir)
    return {
        "name": name,
        # 中文名优先：display_name > name(中文) > name_zh > 包名
        "display_name": meta.get("display_name") or meta.get("name") or meta.get("name_zh") or name,
        # 中文描述优先（索引里 description_zh 是干净中文，description 是英文）
        "description": meta.get("description_zh") or meta.get("description", ""),
        "category": meta.get("category", ""),
        "integration_type": integration_type,
        "connection_state": connection_state,
        "has_bundled_skills": bool(bundled),
        "icon": icon,
        # source: built_in (marketplace package on disk) vs customize
        # (user-registered, no package). Drives the frontend's "edit"
        # button visibility — only customize MCPs are editable.
        "source": "built_in",
        # 连接状态用 connection_state 单字段表达；会话级启用走 chat.send 的
        # ``mcp`` 字段（非持久 flag），不在此返回 enabled/connected 冗余字段。
    }


def _build_detail(
    name: str,
    pkg_dir: Path,
    meta: dict[str, Any],
    connected_names: set[str],
    connecting_names: set[str] | None = None,
) -> dict[str, Any] | None:
    summary = _build_summary(name, pkg_dir, meta, connected_names, connecting_names)
    mcp_spec_raw = _load_json(pkg_dir / "mcp.json")
    mcp_spec: dict[str, Any] = {}
    if mcp_spec_raw and isinstance(mcp_spec_raw.get("mcpServers"), dict):
        # Flatten the mcpServers dict to the first server entry (one pkg = one MCP).
        first_name, first_cfg = next(iter(mcp_spec_raw["mcpServers"].items()), (None, {}))
        if isinstance(first_cfg, dict):
            mcp_spec = _mask_mcp_spec(first_cfg)
    cli_present = (pkg_dir / "cli.json").exists()
    # skills: full {name, description} list (bundled_skills is just names).
    # tools: tools currently registered for a connected MCP. 
    skills = get_mcp_skills(name)
    detail: dict[str, Any] = {
        **summary,
        # 中文描述优先：description_zh > description(中文兜底) > "" ;
        # 索引里 description_zh 是干净的中文描述，description 是英文。
        "description": meta.get("description_zh") or meta.get("description", ""),
        # 示例同样优先取中文
        "examples": meta.get("examples_zh") or meta.get("examples", []),
        "mcp_spec": mcp_spec or None,
        "cli_spec_present": cli_present,
        "bundled_skills": _bundled_skill_names(pkg_dir),
        "skills": skills,
        "tools": get_mcp_tools(name),
    }
    return detail


def _connected_server_names() -> set[str]:
    """Names of MCPs currently connected (state==connected) in state.json.

    This drives the frontend's "connected" badge, so it must NOT include
    ``connecting`` MCPs — an MCP mid-connect (apply not done / OAuth in
    flight) renders as "connecting", not "connected". get_mcp_servers merges
    both connected and connecting (so apply/init can register a connecting
    MCP), but the badge reads only connected.

    disable keeps the connection (config + credentials stay), only hiding
    tools from the agent; disconnect removes the record.
    """
    names: set[str] = set()
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import (
            list_truly_connected_mcps,
        )
        for rec in list_truly_connected_mcps():
            n = str(rec.get("name", "") or "").strip()
            if n:
                names.add(n)
    except Exception:  # noqa: BLE001
        pass
    return names


def _connecting_server_names() -> set[str]:
    """Names of MCPs in the ``connecting`` state (connect in progress).

    Drives the frontend's "connecting" badge. An MCP lands here between
    ``connect_mcp`` (which writes state=connecting) and the handler's flip to
    connected. On a restart while still connecting, the record stays
    connecting — so the frontend shows "connecting" (not "connected"), while
    get_mcp_servers still merges it (list_connected_mcps includes connecting)
    so init re-reads it.
    """
    names: set[str] = set()
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import (
            list_connecting_mcps,
        )
        for rec in list_connecting_mcps():
            n = str(rec.get("name", "") or "").strip()
            if n:
                names.add(n)
    except Exception:  # noqa: BLE001
        pass
    return names


_MCP_LIST_FILTERS = ("builtin", "local")


def list_marketplace_mcps(mcp_filter: str = "builtin") -> list[dict[str, Any]]:
    """List MCPs as summaries, scoped by ``mcp_filter``.

    - ``builtin``: 全部预置（marketplace 包目录）MCP，含各自连接状态徽标。
      列表页「预置」tab 用它。
    - ``local``: 所有自定义 MCP（registered+connected，自定义默认已安装）+
      已连接的预置 MCP。会话页 MCP 选择器用它——会话级启用只对 connected
      有意义，且自定义 MCP 一旦创建即视为已安装。

    其余值按 ``builtin`` 兜底。
    """
    f = str(mcp_filter or "builtin").strip().lower() or "builtin"
    if f not in _MCP_LIST_FILTERS:
        logger.debug("[mcp.registry] unknown mcp.list filter %r, fallback builtin", mcp_filter)
        f = "builtin"

    root = _packages_dir()
    connected = _connected_server_names()
    connecting = _connecting_server_names()
    out: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    if root.is_dir():
        # 华为系（华为云 / 鸿蒙）置顶，其余按包名字母序——让首屏突出自有生态。
        priority = {"huaweiyun-mcp": 0, "harmonyos-mcp": 0}
        for pkg_dir in sorted(
            root.iterdir(),
            key=lambda p: (priority.get(p.name, 1), p.name),
        ):
            if not pkg_dir.is_dir():
                continue
            name = pkg_dir.name
            # local: 只返回已连接的预置 MCP（会话页只需可启用集合）。
            if f == "local" and name not in connected:
                continue
            meta = _load_index_entry(name)
            out.append(_build_summary(name, pkg_dir, meta, connected, connecting))
            seen_names.add(name)
    else:
        logger.debug("[mcp.registry] marketplace packages dir not found: %s", root)
    # 自定义 MCP（无 marketplace 包）只出现在 local——builtin 是纯预置目录。
    if f == "local":
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                list_registered_mcps,
            )
            for rec in list_registered_mcps():
                name = str(rec.get("name", "") or "").strip()
                if not name or name in seen_names:
                    continue
                itype = str(rec.get("integration_type", "") or "remote-mcp").strip() or "remote-mcp"
                state_val = str(rec.get("state", "") or "").strip()
                if state_val == "connected":
                    connection_state = "connected"
                elif state_val == "connecting":
                    connection_state = "connecting"
                else:
                    connection_state = "disconnected"
                out.append({
                    "name": name,
                    "display_name": name,
                    "description": "User-defined custom MCP server",
                    "category": "custom",
                    "integration_type": itype,
                    "connection_state": connection_state,
                    "has_bundled_skills": False,
                    "icon": None,
                    "source": "customize",
                })
                seen_names.add(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[mcp.registry] custom MCP list append failed: %s", exc)
    return out


def get_mcp(name: str) -> dict[str, Any] | None:
    """Return one MCP's detail, or None if not found.

    Marketplace MCPs are read from their package dir; custom MCPs (no
    package) are synthesized from their state.json record so the frontend can
    load detail + tools for them too.
    """
    target = str(name or "").strip()
    if not target:
        return None
    pkg_dir = _packages_dir() / target
    if pkg_dir.is_dir():
        meta = _load_index_entry(target)
        connected = _connected_server_names()
        connecting = _connecting_server_names()
        return _build_detail(target, pkg_dir, meta, connected, connecting)
    # Custom MCP: no marketplace package — synthesize a detail from state.json
    # so the frontend can show it and render its tools panel.
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import (
            get_mcp_record,
        )
        rec = get_mcp_record(target)
    except Exception:  # noqa: BLE001
        rec = None
    if rec is None:
        return None
    itype = str(rec.get("integration_type", "") or "remote-mcp").strip() or "remote-mcp"
    state_val = str(rec.get("state", "") or "").strip()
    is_conn = state_val == "connected"
    is_conn_ing = state_val == "connecting"
    if is_conn:
        connection_state = "connected"
    elif is_conn_ing:
        connection_state = "connecting"
    else:
        connection_state = "disconnected"
    # Echo the connection fields back so the frontend's edit dialog can
    # pre-fill the form (transport/command/args/env/url/headers/timeout_s).
    # Custom MCPs have no package on disk, so state.json is the only source
    # of these values; without them the edit dialog would show blank fields
    # and the user would have to retype everything.
    return {
        "name": target,
        "display_name": target,
        "category": "custom",
        "integration_type": itype,
        "connection_state": connection_state,
        "has_bundled_skills": False,
        "icon": None,
        "source": "customize",
        "description": "User-defined custom MCP server",
        "examples": [],
        "mcp_spec": None,
        "cli_spec_present": False,
        "bundled_skills": [],
        "skills": [],
        "tools": get_mcp_tools(target),
        # Edit-dialog prefill fields (mirrors record_to_mcp_entry's field set).
        "transport": rec.get("transport") or "streamable-http",
        "command": rec.get("command"),
        "args": rec.get("args") or [],
        "env": rec.get("env") or {},
        "url": rec.get("url"),
        "headers": rec.get("headers") or {},
        "timeout_s": rec.get("timeout_s"),
    }


def _skill_description(skill_dir: Path) -> str:
    """Read the description field from a SKILL.md frontmatter."""
    md_path = skill_dir / "SKILL.md"
    if not md_path.is_file():
        return ""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # frontmatter: ---\n...description: ...\n---
    import re
    m = re.search(r"^description:\s*(.+?)$", text, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip().strip("\"'") if m else ""


def get_mcp_skills(name: str) -> list[dict[str, str]]:
    """List an MCP's bundled skills with name + description."""
    n = str(name or "").strip()
    if not n:
        return []
    pkg_dir = _packages_dir() / n
    skills_dir = pkg_dir / "skills"
    if not skills_dir.is_dir():
        return []
    out: list[dict[str, str]] = []
    # flat layout: SKILL.md directly under skills/ -> one skill named after the package
    if _has_skill_file(skills_dir):
        out.append({"name": n, "description": _skill_description(skills_dir)})
        return out
    # nested layout: each child dir with a skill file is one skill
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_dir() and _has_skill_file(entry):
            out.append({"name": entry.name, "description": _skill_description(entry)})
    return out


def get_mcp_tools(name: str) -> list[dict[str, Any]]:
    """Return the tools currently registered for a connected MCP server.

    Reads from the live ToolMgr's registered servers (no remote reconnect) so
    it is safe to call synchronously from ``mcp.show``. Returns ``[]`` when
    the MCP is not connected, has no MCP server (CLI/skill-only), or the
    ToolMgr is unavailable (cold start). Each tool: ``{name, description}``.
    """
    n = str(name or "").strip()
    if not n:
        return []
    try:
        from openjiuwen.core.runner import Runner
        resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
        if resource_registry is None:
            return []
        tool_mgr = resource_registry.tool()
        server_ids = list(tool_mgr.get_mcp_server_ids(n))
        if not server_ids:
            # Fallback: match by server_name when no server_id resolves.
            for sid, res in getattr(tool_mgr, "_mcp_server_resources", {}).items():
                if getattr(res.config, "server_name", "") == n:
                    server_ids.append(sid)
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sid in server_ids:
            for tid in tool_mgr.get_mcp_tool_ids(sid):
                tool = getattr(tool_mgr, "_tools", {}).get(tid)
                if tool is None or not hasattr(tool, "card"):
                    continue
                card = tool.card
                if card.name in seen:
                    continue
                seen.add(card.name)
                tools.append({"name": card.name, "description": card.description or ""})
        return tools
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mcp.registry] get_mcp_tools '%s' failed: %s", n, exc)
        return []


# --- F-1: connect / disconnect / enable / disable / status ---
def _marketplace_mcp_cfg(name: str) -> dict[str, Any] | None:
    """Read marketplace mcp.json, return first server cfg (marketplace format)."""
    pkg = _packages_dir() / str(name or "").strip()
    raw = _load_json(pkg / "mcp.json")
    if not raw or not isinstance(raw.get("mcpServers"), dict):
        return None
    first = next(iter(raw["mcpServers"].values()), {})
    return first if isinstance(first, dict) else None


def _normalize_transport(raw: str, cfg: dict[str, Any]) -> str:
    """Map a marketplace mcp.json transport value to one openjiuwen accepts.

    openjiuwen's MCP client registry only knows: sse / stdio / streamable-http
    / streamable_http / playwright / openapi. Marketplaces use a mix:
    ``streamableHttp`` (camelCase), ``streamable-http``, ``sse``, or omit
    ``type`` entirely (GitHub/Notion/kdocs have only ``url``). Normalize so
    config.yaml stores a value openjiuwen can register without a runtime
    ``Unsupported MCP client type`` rejection.

    Defaults: command present -> stdio; url present -> streamable-http;
    everything else falls back to streamable-http (the modern MCP HTTP
    transport; plain ``http`` is not an openjiuwen client).
    """
    t = str(raw or "").strip().lower()
    if t in ("sse", "stdio"):
        return t
    if t in ("streamablehttp", "streamable-http", "streamable_http"):
        return "streamable-http"
    # 'http' is not a real openjiuwen client; treat as streamable-http.
    if t == "http":
        return "streamable-http"
    # Unknown/empty: derive from command vs url.
    if cfg.get("command"):
        return "stdio"
    return "streamable-http"


def build_config_entry(name: str) -> dict[str, Any] | None:
    """Convert marketplace mcp.json (marketplace format) -> mcp.servers entry.
    Result fields: name/transport[/command/args/env | url/headers]/enabled/server_id_scope.
    """
    n = str(name or "").strip()
    if not n:
        return None
    cfg = _marketplace_mcp_cfg(n)
    if cfg is None:
        return None
    entry: dict[str, Any] = {"name": n}
    entry["transport"] = _normalize_transport(cfg.get("type", ""), cfg)
    if cfg.get("command"):
        entry["command"] = str(cfg.get("command"))
    if isinstance(cfg.get("args"), list):
        entry["args"] = [str(a) for a in cfg["args"]]
    if isinstance(cfg.get("env"), dict):
        entry["env"] = {str(k): str(v) for k, v in cfg["env"].items()}
    if cfg.get("url"):
        entry["url"] = str(cfg.get("url"))
    if isinstance(cfg.get("headers"), dict):
        entry["headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
    if isinstance(cfg.get("timeout"), (int, float)) and int(cfg["timeout"]) > 0:
        entry["timeout_s"] = int(cfg["timeout"])
    entry["enabled"] = True
    entry["server_id_scope"] = f"mcp:{n}"
    return entry


def connect_mcp(name: str, *, install_only: bool = False) -> dict[str, Any]:
    """Install a marketplace MCP (dispatch by integration_type).

    * remote-mcp / stdio-mcp (form A/B): upsert into state.json.
    * cli (form C): run CliDriver install+version, then walk auth steps.
      An auth step with authUrlDomain+authWaitForExit returns an
      ``auth_required`` sentinel; the handler shows the URL to the user and
      resumes via :func:`complete_cli_auth`. When all steps are done, install
      bundled skills and (if cli.json declares an mcp subcommand) register
      a stdio entry.

    ``install_only`` (CLI only): skip bundled-skill copy/load — run CLI
    install + auth only. The state.json record is written without a ``skills``
    field so ``connected_mcp_skill_dirs`` derives no scan dir and the agent
    never sees the MCP's skills. Used when another feature wants the CLI
    installed + authenticated but manages skills itself. Default False keeps
    the full connect flow unchanged.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    pkg_dir = _packages_dir() / n
    if not pkg_dir.is_dir():
        # No marketplace package — this is a custom MCP. Its definition lives
        # in state.json (register_custom_mcp wrote it with state=registered).
        # Connect means: mark it connected so it is selectable per session
        # (the handler flips connecting → connected). Raise KeyError if there
        # is no state.json record — that's a genuine "not found".
        from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record
        rec = get_mcp_record(n)
        if rec is None:
            raise KeyError(f"mcp '{n}' not found")
        # Persist as connecting (NOT connected): the MCP server is not yet
        # loaded into an agent session. connecting means "connect in
        # progress" — get_mcp_servers merges it, the frontend shows
        # "connecting" (not the phantom "connected"), and a restart re-reads
        # it (like connected) so a crash mid-connect doesn't silently drop
        # the connection. The handler flips state to connected; session-level
        # loading is driven later by chat.send's ``mcp`` field
        # (reconcile_session_mcp), not by connect.
        from jiuwenswarm.server.runtime.mcp.state_store import (
            set_mcp_state,
        )
        set_mcp_state(n, state="connecting")
        # Return the definition so the handler can build McpServerConfig from it.
        itype = str(rec.get("integration_type", "") or "remote-mcp").strip() or "remote-mcp"
        mcp_entry = {
            "name": n,
            "transport": rec.get("transport", "streamable-http"),
            "enabled": True,
        }

        for key in ("url", "headers", "command", "args", "env", "timeout_s", "server_id_scope"):
            if key in rec:
                mcp_entry[key] = rec[key]

        # Return the entry flat (name/integration_type/auth_required merged in)
        # so the handler can treat it identically to the marketplace path's
        # build_config_entry result: build_mcp_server_config(entry) and
        # _mask_sensitive_fields(item) read transport/url/headers at top level.
        mcp_entry["integration_type"] = itype
        mcp_entry["auth_required"] = False
        return mcp_entry
    itype = _detect_integration_type(pkg_dir)
    if itype != "cli":
        entry = build_config_entry(n)
        from jiuwenswarm.server.runtime.mcp.credential import (
            CredentialStore,
            detect_credential_kind,
            required_tokens_from_schema,
        )
        kind = detect_credential_kind(n)
        # Form D — skill-only MCP: no cli.json, no usable mcp.json, but a
        # bundled skills/ directory. No MCP server to register; "connecting"
        # means installing the skills so the agent can invoke them. token-
        # schema.json (when present) declares required tokens the skill script
        # reads from env — surface a prompt if any are still missing.
        if entry is None:
            if not (_packages_dir() / n / "skills").is_dir():
                # Neither an mcp.json host nor bundled skills — nothing to connect.
                raise ValueError(
                    f"mcp '{n}' has no usable mcp.json and no skills directory"
                )
            schema_tokens = required_tokens_from_schema(n)
            if schema_tokens:
                store = CredentialStore()
                stored = set(store.get_all(n).keys()) if n else set()
                missing = [k for k in schema_tokens if k not in stored]
                if missing:
                    from jiuwenswarm.server.runtime.mcp.credential import (
                        build_credentials_prompt,
                    )
                    return {
                        "name": n,
                        "integration_type": itype,
                        **build_credentials_prompt(n, missing),
                    }
            # No missing tokens (or a credentialless skill package with no
            # token-schema at all) — install the bundled skills + mark connected.
            skills = _install_bundled_skills_safe(n)
            # skill-only MCPs have nothing BUT their bundled skills: an install
            # that produces zero skills (copy failed / swallowed) must NOT be
            # persisted as connected, or state.json claims "connected" while
            # the agent sees no skills (and a restart re-reads the same stale
            # record). Fail the connect instead of writing a phantom entry.
            if not skills:
                raise RuntimeError(
                    f"mcp '{n}' skill install produced no skills; "
                    f"refusing to mark connected"
                )
            from jiuwenswarm.server.runtime.mcp.state_store import (
                upsert_mcp_record,
            )
            upsert_mcp_record(
                n, {"name": n, "server_id_scope": f"mcp:{n}"},
                state="connected",
                integration_type=itype, skills=skills,
            )
            return {
                "name": n,
                "integration_type": itype,
                "auth_required": False,
                "installed_skills": skills,
                "mcp_entry": None,
            }
        # Form B (mcp.json with ${VAR}): resolve placeholders from the store; if
        # any token is not yet provisioned, return credentials_required.
        if kind == "token":
            store = CredentialStore()
            placeholders_missing = _entry_missing_tokens(entry, store)
            if placeholders_missing:
                from jiuwenswarm.server.runtime.mcp.credential import (
                    build_credentials_prompt,
                )
                return {
                    "name": n,
                    "integration_type": itype,
                    **build_credentials_prompt(n, sorted(placeholders_missing)),
                }
            # state.json stores ${VAR} placeholders (no plaintext tokens).
            # Resolution happens at McpServerConfig build time
            # (interface_deep._build_mcp_server_config consults CredentialStore
            # + os.environ), so the spawned process gets real credentials while
            # state.json stays secret-free.
        # Shared tail: install bundled skills for ANY form (A/B/C), not just CLI.
        # install_mcp_skills is a no-op for packages without skills/, so form
        # A/B MCPs with skills (e.g. notion's 4 skills) get them too.
        skills = _install_bundled_skills_safe(n)
        # Write to state.json as connecting (NOT connected): the MCP is not
        # loaded into an agent session here. connecting means "connect in
        # progress" — get_mcp_servers merges it, the frontend shows
        # "connecting" (not the phantom "connected" a premature write would
        # leave), and a restart re-reads it (like connected). The handler
        # flips to connected; session-level loading is driven later by
        # chat.send's ``mcp`` field (reconcile_session_mcp), not by connect.
        from jiuwenswarm.server.runtime.mcp.state_store import (
            upsert_mcp_record,
        )
        upsert_mcp_record(
            n, entry, state="connecting",
            integration_type=itype, skills=skills or None,
        )
        result = dict(entry)
        result["installed_skills"] = skills
        return result
    # CLI form: some CLI MCPs (gitcode) authenticate via an environment-
    # variable token read by the CLI's own ``auth status`` (gitcode reads
    # GITCODE_TOKEN/GC_TOKEN from env) — NOT via the OAuth auth steps in
    # cli.json. For those, token-schema.json declares the required env keys;
    # surface a credentials_required prompt BEFORE install/auth so the user
    # provisions the token first. 
    if not install_only:
        from jiuwenswarm.server.runtime.mcp.credential import (
            CredentialStore,
            required_tokens_from_schema,
        )
        schema_tokens = required_tokens_from_schema(n)
        if schema_tokens:
            store = CredentialStore()
            stored = set(store.get_all(n).keys()) if n else set()
            missing = [k for k in schema_tokens if k not in stored]
            if missing:
                from jiuwenswarm.server.runtime.mcp.credential import (
                    build_credentials_prompt,
                )
                return {
                    "name": n,
                    "integration_type": "cli",
                    **build_credentials_prompt(n, missing),
                }
    return _connect_cli(n, 0, install_only=install_only)


def _install_bundled_skills_safe(name: str) -> list[str]:
    """Install bundled skills, swallowing errors (skills are optional)."""
    try:
        from jiuwenswarm.server.runtime.mcp.skill_installer import (
            install_mcp_skills,
        )
        return install_mcp_skills(name).get("installed", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.registry] skill install '%s' failed: %s", name, exc)
        return []


def rollback_failed_connect(name: str) -> None:
    """Roll back a state.json entry after a failed connect/CLI auth.

    Marketplace MCP (has package dir): remove record + uninstall skills.
    Custom MCP: flip state back to ``registered`` so the user can retry/edit.

    Both branches wipe the local CredentialStore token file. A failed connect
    means the provisioned tokens did not authenticate (wrong value / expired /
    service rejected) — keeping them would make the MCP permanently broken:
    the next ``mcp.connect`` would find the stale tokens, skip the
    ``credentials_required`` prompt, and re-probe with the same bad tokens
    forever. Wiping forces ``connect_mcp``'s form-B missing-token check to
    re-trigger, so the user gets the credentials modal again and can correct
    the value. No-op for form-A (no tokens stored) and form-C (CLI manages its
    own OAuth state, CredentialStore is empty).
    """
    n = str(name or "").strip()
    if not n:
        return
    try:
        if (_packages_dir() / n).is_dir():
            from jiuwenswarm.server.runtime.mcp.skill_installer import (
                uninstall_mcp_skills,
            )
            from jiuwenswarm.server.runtime.mcp.state_store import (
                remove_mcp_record,
            )
            uninstall_mcp_skills(n)
            remove_mcp_record(n)
        else:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                set_mcp_state,
            )
            set_mcp_state(n, state="registered")
        _wipe_stored_credentials(n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.registry] rollback '%s' failed: %s", n, exc)


def _entry_missing_tokens(entry: dict[str, Any], store: Any) -> set[str]:
    """Collect ${VAR} placeholders not yet in the CredentialStore."""
    from jiuwenswarm.server.runtime.mcp.credential import extract_placeholders
    found = extract_placeholders(entry)
    if not found:
        return set()
    name = str(entry.get("name", "")).strip()
    stored = set(store.get_all(name).keys()) if name else set()
    return found - stored


def _connect_cli(name: str, step_index: int, *, install_only: bool = False) -> dict[str, Any]:
    """Run install + version + auth steps for a CLI MCP.

    Returns an auth_required sentinel or a final result dict.
    """
    from jiuwenswarm.server.runtime.mcp.cli_driver import CliDriver

    n = str(name or "").strip()
    drv = CliDriver(n)
    inst = drv.install()
    if not inst.version_ok:
        raise ValueError(
            f"mcp '{n}' CLI version check failed: got {inst.version!r}, "
            f"min {inst.min_version!r}; {inst.error}"
        )
    steps_total = drv.auth_steps_count()
    idx = max(0, int(step_index))
    # First-connect fast path (idx == 0): probe status before launching any
    # auth step. If a prior login's token is still valid, skip the browser
    # OAuth entirely and finalize. Without this, every connect re-runs
    # ``dws auth login -y`` (pops a browser) even when the user is already
    # authenticated. Resume calls (idx > 0, from complete_cli_auth) already
    # checked status upstream and must not re-probe here.
    if idx == 0 and drv.status().authenticated:
        return _finalize_cli(n, inst, install_only=install_only)
    if idx < steps_total:
        step = drv.auth_step(idx)
        if step.needs_user_action:
            # If the CLI binary opens the browser itself (authSuppressBrowser),
            # don't surface auth_url to the frontend — the frontend would open a
            # second tab (a duplicate of the one the CLI already opened).
            # CLIs that only print the URL (no suppress) need the frontend to
            # open it. The frontend opens the URL only when it's non-empty.
            auth_url = step.auth_url
            if auth_url and drv.manifest.auth_suppress_browser:
                auth_url = None
            return {
                "name": n,
                "integration_type": "cli",
                "auth_required": True,
                "step_index": idx,
                "steps_total": steps_total,
                "auth_url": auth_url,
                "auth_domain": step.auth_domain,
                "command": step.command,
                "install": {
                    "version": inst.version,
                    "min_version": inst.min_version,
                    "version_ok": inst.version_ok,
                },
            }
        if not step.succeeded:
            raise ValueError(f"mcp '{n}' auth step {idx} failed: {step.error}")
        return _connect_cli(n, idx + 1, install_only=install_only)
    return _finalize_cli(n, inst, install_only=install_only)


def _finalize_cli(name: str, install_result: Any, *, install_only: bool = False) -> dict[str, Any]:
    """Install bundled skills for a CLI MCP (form C).

    Pure CLI MCPs (feishu/dingtalk/zsxq/awesun/lovrabet) are NOT MCP servers —
    their CLI binary (e.g. lark-cli) exposes business-domain subcommands
    (docs/im/calendar/...) that a bundled SKILL.md teaches the agent to invoke
    via the exec/command tool. There is no ``mcp`` subcommand on these CLIs,
    so we do NOT register a stdio MCP entry.

    Hybrid packages that ship a real mcp.json with a ``command`` (e.g.
    cloudbase: ``npx -y @cloudbase/cloudbase-mcp``) DO register a stdio entry
    from that mcp.json — the cli.json is only for the auth prelude there.

    ``install_only``: skip skill copy entirely — ``skills`` stays empty and
    the state.json record carries no ``skills`` field, so
    ``connected_mcp_skill_dirs`` derives no scan dir and the agent never sees
    the MCP's skills. The CLI install + auth still ran above; this only
    suppresses the skill side-effects. Used when another feature manages
    skills itself.
    """
    from jiuwenswarm.server.runtime.mcp.skill_installer import (
        install_mcp_skills,
    )
    n = str(name or "").strip()
    if install_only:
        skills_installed: list[str] = []
    else:
        skills_installed = install_mcp_skills(n).get("installed", [])
    entry: dict[str, Any] | None = None
    mcp_cfg = _marketplace_mcp_cfg(n)
    # Only register an MCP server when the package explicitly declares one
    # via mcp.json's command field (hybrid CLI+MCP packages like cloudbase).
    # Pure CLI MCPs (no mcp.json, or mcp.json without command) expose tools
    # via skills + the CLI binary, not via MCP — registering a bogus
    # ``<bin> mcp`` stdio entry would fail at connect time.
    if mcp_cfg and mcp_cfg.get("command"):
        entry = build_config_entry(n)
        if entry is not None:
            # Hybrid CLI+MCP also writes the stdio entry to state.json. Write
            # as connecting, NOT connected: the MCP server is not loaded into
            # an agent session here. connecting lets get_mcp_servers merge the
            # entry, shows "connecting" on the frontend, and re-reads on
            # restart. _finalize_cli_auth flips to connected; session-level
            # loading is driven later by chat.send's ``mcp`` field
            # (reconcile_session_mcp), not by connect.
            from jiuwenswarm.server.runtime.mcp.state_store import (
                upsert_mcp_record,
            )
            upsert_mcp_record(
                n, entry, state="connecting",
                integration_type="cli", skills=skills_installed or None,
            )
    # Pure CLI (no mcp.json command) still needs a state.json record so
    # disconnect/enable/disable can find it; it carries skills, not an MCP entry.
    if entry is None:
        # A pure CLI has no MCP server — its bundled skills ARE the payload.
        # A zero-skill install (copy failed) must not be persisted: the handler
        # would promote connecting → connected and a restart re-reads a stale
        # "connected" record with no skills on disk. Fail the connect instead.
        if not install_only and not skills_installed:
            pkg_dir = _packages_dir() / n
            if (pkg_dir / "skills").is_dir():
                raise RuntimeError(
                    f"mcp '{n}' skill install produced no skills; "
                    f"refusing to connect"
                )
        from jiuwenswarm.server.runtime.mcp.state_store import (
            upsert_mcp_record,
        )
        upsert_mcp_record(
            n, {"name": n, "server_id_scope": f"mcp:{n}"},
            state="connecting",
            integration_type="cli", skills=skills_installed or None,
        )
    return {
        "name": n,
        "integration_type": "cli",
        "auth_required": False,
        "installed_skills": skills_installed,
        "install": {
            "version": install_result.version,
            "min_version": install_result.min_version,
            "version_ok": install_result.version_ok,
        },
        "mcp_entry": entry,
    }


def complete_cli_auth(name: str, step_index: int, *, install_only: bool = False) -> dict[str, Any]:
    """Resume a CLI connect after the user completed OAuth in browser.

    Polls status; on success advances to the next auth step or finalizes.
    Returns the same shape as :func:`_connect_cli`.
    """
    from jiuwenswarm.server.runtime.mcp.cli_driver import CliDriver

    n = str(name or "").strip()
    drv = CliDriver(n)
    idx = max(0, int(step_index))
    # Check status FIRST: some authWaitForExit CLIs (wecom-cli init, dws auth
    # login) don't exit promptly after the user completes OAuth in the browser
    # — the proc keeps running. Status is the authoritative signal; the proc's
    # liveness is only a fallback hint for CLIs whose status command can't tell.
    status = drv.status()
    if not status.authenticated:
        # Status says not authenticated — fall back to "still waiting" if the
        # auth proc is still running (the user may not have finished in browser).
        proc_done = drv.auth_proc_done()
        if proc_done is False:
            return {
                "name": n,
                "integration_type": "cli",
                "auth_required": True,
                "auth_pending": True,
                "step_index": idx,
                "output": "auth process still running",
            }
        return {
            "name": n,
            "integration_type": "cli",
            "auth_required": True,
            "auth_pending": True,
            "step_index": idx,
            "matched": status.matched,
            "output": status.output,
        }
    return _connect_cli(n, idx + 1, install_only=install_only)


def _wipe_stored_credentials(n: str) -> None:
    """Delete the local CredentialStore token file for MCP ``n``.

    Called from the disconnect path (both custom and marketplace) so a
    disconnect severs the auth — the stored ``${VAR}`` tokens must not linger
    on disk after the user disconnects (reconnect re-prompts). Best-effort:
    a failure here must not strand the disconnect (state/skill cleanup already
    ran). Mirrors ``delete_custom_mcp``'s credential wipe.
    """
    try:
        from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
        CredentialStore().delete_mcp(n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.registry] wipe credentials '%s' failed: %s", n, exc)


def disconnect_mcp(name: str) -> dict[str, Any]:
    """Remove an MCP. For CLI form: unauth + remove skills first."""
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    from jiuwenswarm.server.runtime.mcp.state_store import (
        remove_mcp_record,
    )
    if not (_packages_dir() / n).is_dir():
        # Custom MCP: disconnect flips state connected -> registered (keeps the
        # definition so the user can re-connect without re-registering). The
        # handler calls apply_mcp_change(name, "remove") to tear down the MCP
        # server; here we just persist the softer disconnected state.
        from jiuwenswarm.server.runtime.mcp.state_store import (
            set_mcp_state, get_mcp_record,
        )
        rec = get_mcp_record(n)
        if rec is None:
            return {"name": n, "removed": False}
        set_mcp_state(n, state="registered")
        _wipe_stored_credentials(n)
        return {"name": n, "removed": True, "state": "registered"}
    itype = _detect_integration_type(_packages_dir() / n)
    if itype == "cli":
        try:
            from jiuwenswarm.server.runtime.mcp.cli_driver import CliDriver
            drv = CliDriver(n)
            drv.unauth()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp.registry] cli unauth '%s' failed: %s", n, exc)
    # Uninstall bundled skills for any type that has them. Previously only the
    # ``cli`` branch called this, so skill-only / remote-mcp / stdio-mcp MCPs
    # left orphaned ``mcp/skills/<name>/`` dirs and stale ``skill_configs``
    # entries behind after disconnect — the agent could still read those
    # skills. ``uninstall_mcp_skills`` is a no-op when no skills dir exists,
    # so pure remote MCPs without bundled skills are unaffected.
    try:
        from jiuwenswarm.server.runtime.mcp.skill_installer import (
            uninstall_mcp_skills,
        )
        uninstall_mcp_skills(n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.registry] skill uninstall '%s' failed: %s", n, exc)
    # Disconnect wipes the local CredentialStore token file
    # (mcp/credentials/<name>.json) — a disconnect severs the auth, so the
    # stored token must not linger on disk (reconnect re-prompts for it).
    # CLI unauth above clears the CLI-managed OAuth login state, which is a
    # separate credential store from these static ${VAR} tokens.
    _wipe_stored_credentials(n)
    removed = remove_mcp_record(n)
    return removed if removed is not None else {"name": n, "removed": False}


def register_custom_mcp(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Register a user-defined MCP server into state.json.

    Unlike marketplace MCPs this has no package on disk; the caller supplies
    transport/command/args/env or url/headers directly. The entry is written
    enabled with a ``mcp:<name>`` server_id_scope.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    raw_transport = str(config.get("transport", "")).strip().lower()
    if raw_transport not in {"stdio", "sse", "http", "streamable-http", "streamable_http"}:
        raise ValueError("transport must be one of stdio|sse|http|streamable-http")
    # Normalize to a value openjiuwen's MCP client registry accepts (sse/stdio/
    # streamable-http). 'http' is not a real openjiuwen client — map to
    # streamable-http so a custom remote MCP the user typed as "http" registers
    # instead of failing with "Unsupported MCP client type".
    transport = _normalize_transport(raw_transport, config)
    entry: dict[str, Any] = {"name": n, "transport": transport, "enabled": True, "server_id_scope": f"mcp:{n}"}
    if transport == "stdio":
        command = str(config.get("command", "")).strip()
        if not command:
            raise ValueError("stdio transport requires command")
        args = config.get("args")
        if isinstance(args, list):
            # Explicit args — command stays verbatim, args used as-is.
            entry["command"] = command
            entry["args"] = [str(a) for a in args]
        elif args is None or (isinstance(args, str) and not args.strip()):
            # No explicit args: if the command field holds the whole invocation
            # (e.g. "npx -y bing-cn-mcp"), split it into command + args. MCP's
            # stdio transport requires command to be just the binary and args
            # to be a list. A bare single-token command still gets args=[] so
            # downstream validation passes.
            parts = command.split()
            entry["command"] = parts[0]
            entry["args"] = parts[1:] if len(parts) > 1 else []
        else:
            # args is a non-empty string — wrap as a single-element list.
            entry["command"] = command
            entry["args"] = [str(args)]
        if isinstance(config.get("env"), dict):
            entry["env"] = {str(k): str(v) for k, v in config["env"].items()}
    else:
        url = str(config.get("url", "")).strip()
        if not url:
            raise ValueError("remote transport requires url")
        entry["url"] = url
        if isinstance(config.get("headers"), dict):
            entry["headers"] = {str(k): str(v) for k, v in config["headers"].items()}
        if isinstance(config.get("env"), dict):
            entry["env"] = {str(k): str(v) for k, v in config["env"].items()}
    if isinstance(config.get("timeout_s"), (int, float)) and int(config["timeout_s"]) > 0:
        entry["timeout_s"] = int(config["timeout_s"])
    from jiuwenswarm.server.runtime.mcp.state_store import (
        get_mcp_record,
        upsert_mcp_record,
    )
    prior = get_mcp_record(n)
    was_connected = bool(prior and prior.get("state") == "connected")
    upsert_mcp_record(
        n, entry, state="registered",
        integration_type="remote-mcp" if transport != "stdio" else "stdio-mcp",
    )
    entry["was_connected"] = was_connected
    return entry


def delete_custom_mcp(name: str) -> dict[str, Any]:
    """Permanently delete a user-defined custom MCP — terminal counterpart to
    :func:`disconnect_mcp` (which keeps the record + tokens for reconnect).

    Wipes the state.json record, stored credentials, and bundled skills.
    Only ``source=customize`` MCPs are deletable — marketplace MCPs are driven
    by their package dir and would re-surface after a state-only delete.

    Live-server teardown is the handler's job (``apply_mcp_change(remove)``);
    this owns persistent cleanup only. Raises ``ValueError`` for built-in /
    empty name, ``KeyError`` when no state record exists.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    if (_packages_dir() / n).is_dir():
        raise ValueError(f"MCP '{n}' is a built-in marketplace package and cannot be deleted")
    from jiuwenswarm.server.runtime.mcp.state_store import (
        get_mcp_record,
        remove_mcp_record,
    )
    rec = get_mcp_record(n)
    if rec is None:
        raise KeyError(f"mcp '{n}' not found in state")
    was_connected = str(rec.get("state", "") or "").strip() == "connected"
    remove_mcp_record(n)  # definition's only home (custom MCPs never write config.yaml)
    # Best-effort: a cred/perms error here must not strand the deletion.
    try:
        from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
        CredentialStore().delete_mcp(n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.registry] delete credentials '%s' failed: %s", n, exc)
    try:
        from jiuwenswarm.server.runtime.mcp.skill_installer import uninstall_mcp_skills
        uninstall_mcp_skills(n)  # no-op when no skills dir exists
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.registry] skill uninstall '%s' failed: %s", n, exc)
    return {"name": n, "removed": True, "was_connected": was_connected}


def save_mcp_credentials(name: str, tokens: dict[str, Any]) -> dict[str, Any]:
    """Persist user-supplied tokens for a form-B MCP (tianyancha/gildata/...).

    Tokens are written to the local CredentialStore keyed by MCP name; they
    are never echoed back. :func:`connect_mcp` reads them when resolving
    ``${VAR}`` placeholders at connect time.
    """
    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    if not isinstance(tokens, dict) or not tokens:
        raise ValueError("tokens (non-empty dict) is required")
    store = CredentialStore()
    for key, value in tokens.items():
        if value is None:
            continue
        store.save_token(n, str(key), str(value))
    return {"name": n, "saved_keys": sorted(str(k) for k in tokens.keys() if tokens[k] is not None)}

__all__ = [
    "list_marketplace_mcps",
    "get_mcp",
    "get_connector_catalog_display",
    "get_mcp_skills",
    "build_config_entry",
    "connect_mcp",
    "disconnect_mcp",
    "complete_cli_auth",
    "register_custom_mcp",
    "delete_custom_mcp",
    "save_mcp_credentials",
    "rollback_failed_connect",
]

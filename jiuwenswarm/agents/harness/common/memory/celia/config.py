"""Celia configuration and OpenClaw-compatible child environment mapping."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..config import get_embed_config

logger = logging.getLogger(__name__)

_EXTENSION_BINARY_PATH = (
    Path("extensions")
    / "celia_memory"
    / "package"
    / "openclaw"
    / "bin"
    / "gspd_memory_mcp_server"
)
_LEGACY_BINARY_NAME = "gspd_memory_mcp_server"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _find_extension_binary(data_dir: Path) -> Path | None:
    """仅检查外置 extension package 中约定的 OpenClaw MCP 二进制路径。"""

    binary = data_dir / _EXTENSION_BINARY_PATH
    return binary if binary.is_file() else None


def _default_binary_path(data_dir: Path) -> Path:
    """优先使用外置扩展二进制，缺失时返回历史兼容路径。"""

    return _find_extension_binary(data_dir) or (
        data_dir / "celia" / "bin" / _LEGACY_BINARY_NAME
    )


@dataclass(frozen=True)
class CeliaEndpointConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    uid: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CeliaConfig:
    server_binary_path: str
    db_path: str
    log_path: str
    tenant_id: str
    user_id: str
    scope_id: str
    vector_dim: int | None = None
    embed: CeliaEndpointConfig = field(default_factory=CeliaEndpointConfig)
    chat: CeliaEndpointConfig = field(default_factory=CeliaEndpointConfig)
    rerank: CeliaEndpointConfig = field(default_factory=CeliaEndpointConfig)
    dedup_policy: dict[str, Any] = field(default_factory=dict)
    workspace_dir: str = ""
    procedural_dir: str = ""
    procedural_learn_debug: bool = False
    dreaming_enabled: str = "inherit"
    startup_timeout: float = 20.0
    request_timeout: float = 10.0
    flush_timeout: float = 120.0
    fail_open: bool = True
    runtime_state_path: str = ""
    preflight_enabled: bool = False
    request_scope: dict[str, str] = field(default_factory=dict)
    advanced_tools: tuple[str, ...] = ()

    @property
    def normalized_binary_path(self) -> str:
        path = Path(self.server_binary_path).expanduser()
        if path.parent == Path(".") and shutil.which(str(path)):
            return str(path)
        return str(path.absolute())

    @property
    def normalized_db_path(self) -> str:
        return str(Path(self.db_path).expanduser().absolute())

    @property
    def fingerprint(self) -> tuple[str, ...]:
        def secret(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""

        def headers(value: Mapping[str, str]) -> str:
            encoded = json.dumps(dict(value), sort_keys=True, ensure_ascii=False)
            return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        return (
            self.normalized_binary_path,
            self.normalized_db_path,
            str(Path(self.log_path).expanduser().absolute()),
            str(Path(self.runtime_state_path).expanduser().absolute()),
            self.dreaming_enabled,
            self.tenant_id,
            str(self.vector_dim or ""),
            hashlib.sha256(
                json.dumps(self.dedup_policy, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            self.embed.base_url,
            secret(self.embed.api_key),
            self.embed.model,
            headers(self.embed.headers),
            self.chat.base_url,
            secret(self.chat.api_key),
            self.chat.model,
            headers(self.chat.headers),
            self.rerank.base_url,
            secret(self.rerank.api_key),
            self.rerank.model,
            headers(self.rerank.headers),
        )

    @property
    def db_identity(self) -> str:
        return self.normalized_db_path

    def is_available(self) -> bool:
        """Perform only local static checks; never start a process or call a network."""
        return not self.preflight_issues()

    def preflight_issues(self) -> list[str]:
        issues: list[str] = []
        if platform.system().lower() != "linux":
            return ["Celia requires Linux"]
        if platform.machine().lower() not in {"aarch64", "arm64"}:
            return [f"Celia requires ARM64, current architecture is {platform.machine()}"]
        binary = Path(self.normalized_binary_path)
        if not binary.is_file():
            return [f"Celia binary not found: {binary}"]
        if not os.access(binary, os.X_OK):
            issues.append(f"Celia binary is not executable: {binary}")
        db = Path(self.normalized_db_path)
        parent = db.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.exists() or not os.access(parent, os.W_OK):
            issues.append(f"Celia DB directory is not writable: {db.parent}")
        for target in (Path(self.log_path).expanduser().parent, Path(self.runtime_state_path).expanduser().parent):
            current = target
            while not current.exists() and current != current.parent:
                current = current.parent
            if not current.exists() or not os.access(current, os.W_OK):
                issues.append(f"Celia runtime directory is not writable: {target}")
        if not issues and shutil.which("ldd"):
            try:
                result = subprocess.run(
                    ["ldd", str(binary)], capture_output=True, text=True, timeout=5, check=False
                )
                missing = [line.strip() for line in result.stdout.splitlines() if "not found" in line]
                if missing:
                    issues.append("Celia shared libraries missing: " + "; ".join(missing))
            except (OSError, subprocess.SubprocessError):
                logger.debug("Celia ldd preflight unavailable", exc_info=True)
        return issues

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)

        def put(name: str, value: Any) -> None:
            value = _text(value)
            if value:
                env[name] = value

        def put_endpoint(prefix: str, endpoint: CeliaEndpointConfig) -> None:
            env_names = [
                f"OPENAI_{prefix}_BASE_URL",
                f"OPENAI_{prefix}_API_KEY",
                f"OPENAI_{prefix}_MODEL",
                f"OPENAI_{prefix}_HEADERS_JSON",
            ]
            if endpoint.uid and (not endpoint.base_url or not endpoint.api_key):
                for name in env_names:
                    env.pop(name, None)
                logger.warning(
                    "Celia %s sandbox endpoint disabled: missing %s",
                    prefix.lower(),
                    ", ".join(
                        name for name, value in (
                            (f"OPENAI_{prefix}_BASE_URL", endpoint.base_url),
                            (f"OPENAI_{prefix}_API_KEY", endpoint.api_key),
                        ) if not value
                    ),
                )
                return
            put(f"OPENAI_{prefix}_BASE_URL", endpoint.base_url)
            put(f"OPENAI_{prefix}_API_KEY", endpoint.api_key)
            put(f"OPENAI_{prefix}_MODEL", endpoint.model)
            headers = {
                str(k): str(v)
                for k, v in endpoint.headers.items()
                if str(k).lower() not in {"x-api-key", "x-uid"}
            }
            if headers:
                env[f"OPENAI_{prefix}_HEADERS_JSON"] = json.dumps(
                    headers, ensure_ascii=False, separators=(",", ":")
                )

        put_endpoint("EMBED", self.embed)
        put_endpoint("CHAT", self.chat)
        put_endpoint("RERANK", self.rerank)
        if not self.embed.uid or (self.embed.base_url and self.embed.api_key):
            put("CELIA_EMBED_UID", self.embed.uid)
        else:
            env.pop("CELIA_EMBED_UID", None)
        if not self.chat.uid or (self.chat.base_url and self.chat.api_key):
            put("CELIA_CHAT_UID", self.chat.uid)
        else:
            env.pop("CELIA_CHAT_UID", None)
        put("CELIA_TENANT_ID", self.tenant_id)
        put("CELIA_VECTOR_DIM", self.vector_dim)
        put("CELIA_PROCEDURAL_DIR", self.procedural_dir)
        put("CELIA_PROCEDURAL_LEARN_DEBUG", str(self.procedural_learn_debug).lower())
        put("CELIA_XIAOYI_RUNTIME_PATH", self.runtime_state_path)
        if self.dreaming_enabled in {"on", "off"}:
            put("CELIA_DREAMING_ENABLED", "true" if self.dreaming_enabled == "on" else "false")
        else:
            env.pop("CELIA_DREAMING_ENABLED", None)
        return env


def _model_defaults(config: Mapping[str, Any]) -> dict[str, Any]:
    models = config.get("models") if isinstance(config, Mapping) else None
    defaults = models.get("defaults") if isinstance(models, Mapping) else None
    if not isinstance(defaults, list):
        return {}
    for item in defaults:
        if isinstance(item, Mapping) and item.get("is_default"):
            return _mapping(item.get("model_client_config"))
    first = defaults[0] if defaults else {}
    return _mapping(first.get("model_client_config")) if isinstance(first, Mapping) else {}


def _endpoint(
    section: Mapping[str, Any],
    *,
    fallback: Mapping[str, Any] | None = None,
    env_prefix: str,
    uid_env: str | None = None,
    uid_fallback: Any = None,
) -> CeliaEndpointConfig:
    fallback = fallback or {}
    explicit_headers = _mapping(section.get("headers") or section.get("custom_headers"))
    fallback_headers = _mapping(fallback.get("headers") or fallback.get("custom_headers"))
    env_headers: dict[str, Any] = {}
    try:
        parsed_headers = json.loads(os.getenv(f"OPENAI_{env_prefix}_HEADERS_JSON", "") or "{}")
        if isinstance(parsed_headers, dict):
            env_headers = parsed_headers
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid OPENAI_%s_HEADERS_JSON", env_prefix)

    env_base = _text(os.getenv(f"OPENAI_{env_prefix}_BASE_URL"))
    env_api_key = _text(os.getenv(f"OPENAI_{env_prefix}_API_KEY"))
    env_model = _text(os.getenv(f"OPENAI_{env_prefix}_MODEL"))
    env_uid = _text(os.getenv(uid_env or ""))
    if env_prefix == "CHAT":
        if not env_base and os.getenv("SERVICE_URL"):
            env_base = _text(os.getenv("SERVICE_URL")).rstrip("/") + "/celia-claw/v1/sse-api"
        env_api_key = env_api_key or _text(os.getenv("PERSONAL_API_KEY"))
        env_uid = env_uid or _text(os.getenv("PERSONAL_UID"))

    candidates = [
        {
            "base": _first(section.get("base_url"), section.get("api_base")),
            "key": _text(section.get("api_key")),
            "model": _first(section.get("model"), section.get("model_name")),
            "uid": _text(section.get("uid")),
            "headers": explicit_headers,
        },
        {
            "base": _first(fallback.get("base_url"), fallback.get("api_base")),
            "key": _text(fallback.get("api_key")),
            "model": _first(fallback.get("model"), fallback.get("model_name")),
            "uid": _text(uid_fallback),
            "headers": fallback_headers,
        },
        {
            "base": env_base,
            "key": env_api_key,
            "model": env_model,
            "uid": env_uid,
            "headers": env_headers,
        },
    ]
    selected = next((item for item in candidates if item["base"] and item["key"]), None)
    if selected is None:
        if _first(section.get("uid"), env_uid, uid_fallback):
            present = next((item for item in candidates if item["base"] or item["key"]), candidates[0])
            missing = []
            if not present["base"]:
                missing.append(f"OPENAI_{env_prefix}_BASE_URL")
            if not present["key"]:
                missing.append(f"OPENAI_{env_prefix}_API_KEY")
            logger.warning(
                "Celia %s sandbox endpoint disabled: missing %s",
                env_prefix.lower(),
                ", ".join(missing) or "complete endpoint candidate",
            )
        return CeliaEndpointConfig()

    normalized_headers = {str(k): str(v) for k, v in selected["headers"].items()}
    if not any(key.lower() == "x-hag-trace-id" for key in normalized_headers):
        normalized_headers["x-hag-trace-id"] = "celia-memo"
    resolved_uid = _first(selected["uid"], env_uid, uid_fallback)
    resolved_base = str(selected["base"])
    if resolved_uid and ("/celia-claw/" in resolved_base or "/sse-api" in resolved_base):
        if not any(key.lower() == "x-request-from" for key in normalized_headers):
            normalized_headers["x-request-from"] = "openclaw"
        if not any(key.lower() == "accept" for key in normalized_headers):
            normalized_headers["Accept"] = "application/json"
    return CeliaEndpointConfig(
        base_url=resolved_base,
        api_key=str(selected["key"]),
        model=str(selected["model"]),
        uid=resolved_uid,
        headers=normalized_headers,
    )


def build_celia_config(
    config: Mapping[str, Any],
    ext_cfg: Mapping[str, Any],
    *,
    workspace_dir: str = "",
) -> CeliaConfig:
    section = _mapping(ext_cfg.get("celia"))
    embed_section = _mapping(section.get("embed"))
    chat_section = _mapping(section.get("chat"))
    rerank_section = _mapping(section.get("rerank"))

    embed = get_embed_config() or {}
    top_embed = _mapping(config.get("embed"))
    embed_fallback = {
        "api_key": _first(embed.get("api_key"), top_embed.get("embed_api_key")),
        "base_url": _first(embed.get("base_url"), top_embed.get("embed_base_url")),
        "model": _first(embed.get("model"), top_embed.get("embed_model")),
    }
    chat_fallback = _model_defaults(config)

    from jiuwenswarm.common.utils import get_agent_workspace_dir, get_user_workspace_dir

    resolved_workspace = Path(workspace_dir).expanduser() if workspace_dir else get_agent_workspace_dir()
    data_dir = get_user_workspace_dir()
    default_binary = _default_binary_path(data_dir)
    default_db = resolved_workspace / "memory" / "celia_memory" / "celia_memory.db"
    default_log = Path.home() / ".openclaw" / "logs" / "Celia_memory.log"
    default_runtime = Path.home() / ".openclaw" / ".xiaoyiruntime"
    dream_value = _text(section.get("dreaming_enabled"), "inherit").lower()
    if dream_value in {"true", "1", "on"}:
        dream_value = "on"
    elif dream_value in {"false", "0", "off"}:
        dream_value = "off"
    elif dream_value != "inherit":
        dream_value = "inherit"

    resolved_embed = _endpoint(
        embed_section,
        fallback=embed_fallback,
        env_prefix="EMBED",
        uid_env="CELIA_EMBED_UID",
    )
    resolved_chat = _endpoint(
        chat_section,
        fallback=chat_fallback,
        env_prefix="CHAT",
        uid_env="CELIA_CHAT_UID",
    )
    resolved_rerank = _endpoint(rerank_section, env_prefix="RERANK")
    missing_model_fields: list[str] = []
    for prefix, endpoint in (("CHAT", resolved_chat), ("EMBED", resolved_embed)):
        if not endpoint.base_url:
            missing_model_fields.append(f"OPENAI_{prefix}_BASE_URL")
        if not endpoint.api_key:
            missing_model_fields.append(f"OPENAI_{prefix}_API_KEY")
        if not endpoint.model:
            missing_model_fields.append(f"OPENAI_{prefix}_MODEL")
    if missing_model_fields:
        logger.warning(
            "[CeliaMemoryConfig] chat/embed endpoint incomplete; missing=%s; "
            "direct memory_store extraction or vector retrieval may fail",
            ", ".join(missing_model_fields),
        )

    return CeliaConfig(
        server_binary_path=str(default_binary),
        db_path=_first(section.get("db_path"), os.getenv("CELIA_MEMORY_DB_PATH"), default_db),
        log_path=_first(section.get("log_path"), os.getenv("CELIA_MEMORY_LOG_PATH"), default_log),
        tenant_id=_first(
            section.get("tenant_id"),
            os.getenv("CELIA_TENANT_ID"),
            os.getenv("CELIA_MEMORY_TENANT_ID"),
            "default",
        ),
        user_id=_first(ext_cfg.get("user_id"), os.getenv("MEMORY_USER_ID"), "openclaw-user"),
        scope_id=_first(ext_cfg.get("scope_id"), "user"),
        vector_dim=_integer(_first(section.get("vector_dim"), os.getenv("CELIA_VECTOR_DIM"))),
        embed=resolved_embed,
        chat=resolved_chat,
        rerank=resolved_rerank,
        dedup_policy=_mapping(section.get("dedup_policy")),
        workspace_dir=str(resolved_workspace.absolute()),
        procedural_dir=_first(section.get("procedural_dir"), os.getenv("CELIA_PROCEDURAL_DIR")),
        procedural_learn_debug=_bool(section.get("procedural_learn_debug"), False),
        dreaming_enabled=dream_value,
        startup_timeout=_number(section.get("startup_timeout"), 20.0),
        request_timeout=_number(section.get("request_timeout"), 10.0),
        flush_timeout=_number(section.get("flush_timeout"), 120.0),
        fail_open=_bool(section.get("fail_open"), True),
        preflight_enabled=_bool(section.get("preflight_enabled"), False),
        request_scope=_mapping(section.get("request_scope")),
        advanced_tools=tuple(section.get("advanced_tools") or ())
        if isinstance(section.get("advanced_tools"), (list, tuple)) else (),
        runtime_state_path=_first(
            section.get("runtime_state_path"),
            os.getenv("CELIA_XIAOYI_RUNTIME_PATH"),
            default_runtime,
        ),
    )

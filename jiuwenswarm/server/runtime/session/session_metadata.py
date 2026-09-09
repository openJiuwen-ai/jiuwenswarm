"""会话元数据管理模块"""
from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import shutil
import threading
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from jiuwenswarm.common.utils import get_agent_sessions_dir, get_agent_workspace_dir
from jiuwenswarm.server.runtime.session.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    SUPPORTED_WORK_MODES,
    default_work_mode_for_channel,
    normalize_work_mode,
)

logger = logging.getLogger(__name__)

_TEAM_TEMPLATE_SNAPSHOT_FILE = ".team-template-snapshot.json"

# ---------- 异步写入队列(与 session_history 保持一致的模式) ----------
# (session_id, metadata, sessions_root|None, preserve_pin_fields)
_METADATA_QUEUE: queue.Queue[tuple[str, dict[str, Any], str | None, bool]] = queue.Queue(
    maxsize=5000
)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_FILE_LOCK = threading.Lock()

# 内存缓存: key = f"{sessions_root}\n{session_id}"，避免多租户同 session_id 串缓存
_METADATA_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# 会话标题自动生成的截取长度
_TITLE_MAX_LEN = 50
# 心跳任务会话目录前缀，不参与 session.list 等列表展示
_HEARTBEAT_SESSION_PREFIX = "heartbeat_"
_DELIVERY_KIND_SERVER_PUSH = "server_push"


def resolve_session_runtime_team_name(metadata: dict[str, Any] | None) -> str:
    """Return the Agent Teams runtime identity, with legacy metadata fallback."""
    values = metadata if isinstance(metadata, dict) else {}
    return str(values.get("runtime_team_name") or values.get("team_name") or "").strip()

# 匹配所有小写 XML 块:
# 如 <system-reminder>、<file-content>、<command-name> 等系统/工具注入内容
_INJECTED_TAG_RE = re.compile(
    r"<([a-z][\w-]*)(?:\s[^>]*)?>.*?</\1>\n?", re.DOTALL
)
# 匹配截断的 XML 开始标签（标题被 _TITLE_MAX_LEN 截断时可能只剩开始标签）
_INJECTED_TAG_START_RE = re.compile(
    r"<[a-z][\w-]*(?:\s[^>]*)?>?"
)


def _has_valid_work_mode(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in SUPPORTED_WORK_MODES


# ── 惰性迁移:读取时推断缺失字段并回写磁盘 ──────────────────────────────────
# 替代启动迁移 ``migrate_legacy_session_metadata_at_startup``:
#   - 单条读取 ``get_session_metadata`` 与批量读取 ``get_all_sessions_metadata`` /
#     ``collect_all_sessions_metadata`` 在读到老会话缺字段时按需推断并落盘。
#   - 批量入口构建一次 project 映射复用,避免 N+1 扫描 project_store。
#   - 仅写盘"确定性推断"结果;无法消歧的会话仍由运行期兜底返回稳定 schema,
#     不写盘(避免错误推断被持久化)。


def _build_project_lookup() -> tuple[
    dict[str, list[tuple[str, str]]], dict[str, str]
]:
    """构建 project 查找映射供 work_mode / project_id 推断使用。

    Returns:
        ``(dir_to_projects, id_to_work_mode)``:
        - ``dir_to_projects``: 规范化 ``project_dir`` →
          ``[(project_id, work_mode), ...]``,仅含 project_dir 非空的可见项目。
        - ``id_to_work_mode``: ``project_id`` → ``work_mode``,
          含所有项目(含隐藏/无 project_dir),用于 metadata 已有 project_id 时直接命中。

    任何异常降级为空映射,读取兜底仍能保证前端拿到稳定 schema。
    """
    try:
        from jiuwenswarm.server.runtime.session.project_store import (
            list_projects,
            _normalize_path_for_match,
        )
        dir_to_projects: dict[str, list[tuple[str, str]]] = {}
        id_to_work_mode: dict[str, str] = {}
        for p in list_projects(include_hidden=True, cache_bust=True):
            if p.project_id:
                id_to_work_mode[p.project_id] = p.work_mode
            if not p.project_dir or p.hidden:
                continue
            dir_to_projects.setdefault(
                _normalize_path_for_match(p.project_dir), []
            ).append((p.project_id, p.work_mode))
        return dir_to_projects, id_to_work_mode
    except Exception:
        return {}, {}


def _apply_metadata_defaults_with_inference(
    session_id: str,
    metadata: dict[str, Any],
    session_dir: Path | None = None,
    *,
    dir_to_projects: dict[str, list[tuple[str, str]]] | None = None,
    id_to_work_mode: dict[str, str] | None = None,
    enable_writeback: bool = True,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """统一兜底 + 推断缺失字段,并在确定性推断时异步写盘。

    三处读取入口(``get_session_metadata`` / ``get_all_sessions_metadata`` /
    ``collect_all_sessions_metadata``)共用本函数,消除兜底不一致。

    推断与写盘策略:
      - 常量默认字段(``project_dir``/``model``/``cron_id``/``pinned`` 等):
        ``setdefault`` 补默认值,不触发写盘(这些字段前端读到默认值即可,
        无需持久化)。
      - ``work_mode``: 缺失/非法时尝试双模式消歧推断(§5.3.4.1):
        - 确定性推断成功(同路径唯一 Project / 同路径双模式按 channel_id 消歧 /
          metadata 已有 project_id 命中真实 Project)→ 写盘持久化,避免后续重复推断。
        - 无法消歧(无 project_dir / 无 Project 命中 / 双模式且 channel_id 为空)
          → 按通道推断默认值兜底,不写盘。
      - ``project_id``: 缺失且能按 work_mode 反查到唯一真实 Project → 回填并写盘。
      - ``last_user_message_at``: 缺失时多级回退(``last_message_at`` →
        ``created_at`` → 目录 mtime),写盘持久化(否则老会话排序错乱)。

    Args:
        session_id: 会话 ID。
        metadata: 读取到的 metadata dict(可能缺字段)。原地修改并返回。
        session_dir: 会话目录 Path,用于 ``last_user_message_at`` 回退到 mtime。
        dir_to_projects: 批量入口预构建的 project_dir → projects 映射;
            ``None`` 时本函数自行构建(单条读取场景)。
        id_to_work_mode: 批量入口预构建的 project_id → work_mode 映射;
            ``None`` 时与 ``dir_to_projects`` 一起自行构建。
        enable_writeback: 是否允许写盘。批量入口在循环中调用时传 ``True``,
            但写盘走异步队列,不会阻塞读路径。

    Returns:
        原地修改后的 ``metadata`` dict(保证所有字段齐全且 ``work_mode`` 合法)。
    """
    if not isinstance(metadata, dict) or not metadata:
        return metadata

    # 标题清理(原三处入口都有,统一到此处)
    if metadata.get("title"):
        sanitized = _sanitize_title(metadata["title"])
        if sanitized != metadata["title"]:
            metadata["title"] = sanitized

    # 常量默认字段:不触发写盘
    metadata.setdefault("project_dir", "")
    metadata.setdefault("project_id", "")
    metadata.setdefault("model", "")
    metadata.setdefault("cron_id", "")
    metadata.setdefault("pinned", False)
    metadata.setdefault("pin_order", 0)
    metadata.setdefault("status", "idle")

    changed = False  # 是否有需要写盘的确定性推断

    # 惰性迁移:历史 .../agent/workspace 前缀重映射到 jiuwenclaw_workspace。
    # 与 project_dir 的"首次锁定不可改"语义不冲突:重映射前后指向同一逻辑目录,
    # 不重写会导致旧前缀命中 dir_to_projects 失败、且 mkdir 复活僵尸目录。
    remapped_dir = _remap_legacy_workspace_prefix(
        str(metadata.get("project_dir") or "")
    )
    if remapped_dir != metadata["project_dir"]:
        metadata["project_dir"] = remapped_dir
        changed = True
    if isinstance(metadata.get("channel_metadata"), dict):
        for key in ("cwd", "project_dir"):
            legacy_val = str(metadata["channel_metadata"].get(key) or "")
            remapped_val = _remap_legacy_workspace_prefix(legacy_val)
            if remapped_val != legacy_val:
                metadata["channel_metadata"][key] = remapped_val
                changed = True

    # last_user_message_at: 多级回退
    # 优先用已有时间字段;不能用 ``or`` 短路——合法的 0.0 时间戳是 falsy。
    if "last_user_message_at" not in metadata:
        fallback = metadata.get("last_message_at")
        if fallback is None:
            fallback = metadata.get("created_at")
        if fallback is None and session_dir is not None:
            try:
                fallback = session_dir.stat().st_mtime
            except OSError:
                fallback = None
        try:
            metadata["last_user_message_at"] = (
                float(fallback) if fallback is not None else 0.0
            )
        except (TypeError, ValueError):
            if session_dir is not None:
                try:
                    metadata["last_user_message_at"] = session_dir.stat().st_mtime
                except OSError:
                    metadata["last_user_message_at"] = 0.0
            else:
                metadata["last_user_message_at"] = 0.0
        changed = True

    # work_mode: 先按通道推断兜底(保证返回值稳定),再尝试确定性推断写盘
    existing_wm = metadata.get("work_mode")
    if _has_valid_work_mode(existing_wm):
        metadata["work_mode"] = existing_wm.strip().lower()  # type: ignore[union-attr]
    else:
        # 兜底:按 channel_id 推断默认值,确保前端拿到稳定 schema
        metadata["work_mode"] = default_work_mode_for_channel(metadata.get("channel_id"))
        # 尝试确定性推断,成功则写盘持久化
        if dir_to_projects is None or id_to_work_mode is None:
            dir_to_projects, id_to_work_mode = _build_project_lookup()
        resolved_wm = _resolve_legacy_work_mode(
            metadata, dir_to_projects, id_to_work_mode
        )
        if resolved_wm is not None:
            metadata["work_mode"] = resolved_wm
            changed = True

    # project_id: 缺失时尝试按 work_mode 反查唯一真实 Project
    if not str(metadata.get("project_id") or "").strip():
        if dir_to_projects is None or id_to_work_mode is None:
            dir_to_projects, id_to_work_mode = _build_project_lookup()
        pp = _normalize_path_for_match_safe(str(metadata.get("project_dir") or ""))
        if pp and pp in dir_to_projects:
            candidates = dir_to_projects[pp]
            if len(candidates) == 1:
                metadata["project_id"] = candidates[0][0]
                changed = True
            else:
                # 同路径双模式:按已确定的 work_mode 选对应 project_id
                known_wm = metadata["work_mode"]
                for pid, pwm in candidates:
                    if pwm == known_wm:
                        metadata["project_id"] = pid
                        changed = True
                        break

    # 确定性推断成功时异步写盘(不阻塞读路径)
    if changed and enable_writeback:
        try:
            _enqueue_write(
                session_id,
                metadata,
                preserve_pin_fields=True,
                sessions_root=sessions_root,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("惰性迁移写回会话 %s 失败: %s", session_id, exc)

    return metadata


def _normalize_path_for_match_safe(path: str) -> str:
    """规范化路径用于跨平台匹配(容忍尾部分隔符/大小写差异)。

    与 :func:`project_store._normalize_path_for_match` 保持一致;
    导入失败时退化为原始字符串(仅影响 project_id 反查准确性,不影响读路径)。
    """
    try:
        from jiuwenswarm.server.runtime.session.project_store import (
            _normalize_path_for_match,
        )
        return _normalize_path_for_match(path)
    except Exception:
        return str(path or "")


def _sanitize_title(title: str) -> str:
    """清理标题中的系统注入 XML 标签。

    匹配所有小写 XML 标签（如 <system-reminder>、<file-content>、<command-name>），
    不匹配用户提及的大写 HTML/JSX（如 <Button>、<Component>）。

    处理两种情况：
    1. 完整的 <tag>...</tag> 块（正则移除）
    2. 被 _TITLE_MAX_LEN 截断的 <tag ... 开头（无闭合标签，整段丢弃）
    """
    if not title:
        return title
    cleaned = _INJECTED_TAG_RE.sub("", title).strip()
    if _INJECTED_TAG_START_RE.match(cleaned):
        return ""
    return cleaned


def _current_timestamp() -> float:
    """返回显式使用 UTC 时区的当前时间戳"""
    return datetime.now(timezone.utc).timestamp()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_session_subdir(
    session_id: str, sessions_root: str | Path | None = None
) -> Path | None:
    """解析 sessions 根下安全子目录；非法 session_id 返回 None（不 mkdir）。"""
    stripped = (session_id or "").strip()
    if not stripped or "\x00" in stripped:
        return None
    from jiuwenswarm.server.runtime.prompt_attachment_loader import sanitize_session_id

    if sanitize_session_id(stripped) != stripped:
        return None
    root = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
    try:
        base = root.resolve()
        candidate = base / stripped
        if candidate.is_symlink():
            return None
        try:
            if candidate.lstat().st_file_attributes & 0x400:
                return None
        except (AttributeError, FileNotFoundError):
            pass
        target = candidate.resolve()
    except OSError:
        return None
    if target == base or not _is_relative_to(target, base):
        return None
    return target


def resolve_session_subdir(
    session_id: str,
    *,
    sessions_root: str | Path | None = None,
) -> Path | None:
    """Resolve a session directory without escaping the selected sessions root."""
    return _safe_session_subdir(session_id, sessions_root)


def get_session_team_template_snapshot(
    session_id: str,
    *,
    sessions_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the private Team template snapshot pinned to one session."""
    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        return None
    path = session_dir / _TEAM_TEMPLATE_SNAPSHOT_FILE
    if not path.is_file():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        logger.warning("failed to read session Team snapshot session=%s: %s", session_id, exc)
        return None
    return snapshot if isinstance(snapshot, dict) and snapshot else None


def _write_session_team_template_snapshot(
    session_id: str,
    snapshot: dict[str, Any],
    *,
    sessions_root: str | Path | None = None,
) -> None:
    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        raise ValueError("invalid session_id")
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / _TEAM_TEMPLATE_SNAPSHOT_FILE
    tmp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with _FILE_LOCK:
        try:
            tmp_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise


def write_session_team_template_snapshot(
    session_id: str,
    snapshot: dict[str, Any],
    *,
    sessions_root: str | Path | None = None,
) -> None:
    """Persist a session's team template snapshot atomically (public wrapper).

    Delegates to the existing atomic tmp+replace writer under ``_FILE_LOCK``.
    Use this from refresh paths that must write synchronously without the
    ``update_session_metadata`` side effects (async queueing,
    ``touch_last_message_at``).
    """
    _write_session_team_template_snapshot(
        session_id, snapshot, sessions_root=sessions_root
    )


def capture_session_team_binding_artifacts(
    session_id: str,
    *,
    sessions_root: str | Path | None = None,
) -> tuple[bytes | None, bytes | None]:
    """Capture metadata and Team snapshot bytes for bind rollback."""
    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        raise ValueError("invalid session_id")
    metadata_path = session_dir / "metadata.json"
    snapshot_path = session_dir / _TEAM_TEMPLATE_SNAPSHOT_FILE
    with _FILE_LOCK:
        metadata = metadata_path.read_bytes() if metadata_path.is_file() else None
        snapshot = snapshot_path.read_bytes() if snapshot_path.is_file() else None
    return metadata, snapshot


def restore_session_team_binding_artifacts(
    session_id: str,
    artifacts: tuple[bytes | None, bytes | None],
    *,
    sessions_root: str | Path | None = None,
) -> None:
    """Restore metadata and Team snapshot after a failed bind transaction."""
    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        raise ValueError("invalid session_id")
    root_s = _normalize_sessions_root_s(sessions_root)
    paths = (
        session_dir / "metadata.json",
        session_dir / _TEAM_TEMPLATE_SNAPSHOT_FILE,
    )
    with _FILE_LOCK:
        for path, content in zip(paths, artifacts, strict=True):
            if content is None:
                path.unlink(missing_ok=True)
                continue
            tmp_path = path.with_name(
                f"{path.name}.{os.getpid()}.{threading.get_ident()}.rollback.tmp"
            )
            try:
                tmp_path.write_bytes(content)
                tmp_path.replace(path)
            except OSError:
                tmp_path.unlink(missing_ok=True)
                raise
    with _CACHE_LOCK:
        _METADATA_CACHE.pop(_metadata_cache_key(session_id, root_s), None)


def _remap_legacy_workspace_prefix(path: str) -> str:
    """将历史的 ``.../agent/workspace`` 前缀重映射到 ``.../agent/jiuwenclaw_workspace``。

    目录改名迁移（utils._migrate_workspace_to_jiuwenclaw_workspace）会重写
    projects.json / metadata.json，但仍可能有遗漏记录（如迁移后手动恢复的
    备份文件）。此处兜底重映射，避免在旧路径上 mkdir 重建"僵尸目录"，
    导致下次重启再触发一轮合并迁移、两次重启间写入的文件对会话不可见。

    仅当旧路径本身已不存在而新路径存在时才重映射（迁移未完成的场景保持原样）。
    """
    raw = (path or "").strip()
    if not raw:
        return raw
    # 逐级向上寻找 .../agent/workspace 锚点（basename/dirname 屏蔽分隔符差异，
    # 兼容 ``\\`` 与 ``/`` 两种风格；取最深层命中，与迁移重写的前缀语义一致）
    normalized = os.path.normpath(raw)
    current = normalized
    while current and current != os.path.dirname(current):
        if (
            os.path.basename(current).lower() == "workspace"
            and os.path.basename(os.path.dirname(current)).lower() == "agent"
        ):
            old_dir = Path(current)
            if old_dir.exists():
                # 旧目录还在（迁移未执行/未完成），不干预
                return raw
            new_dir = old_dir.parent / "jiuwenclaw_workspace"
            if not new_dir.is_dir():
                return raw
            tail = os.path.relpath(normalized, current)
            if tail == ".":
                return str(new_dir)
            return os.path.join(str(new_dir), tail)
        current = os.path.dirname(current)
    return raw


def validate_project_dir(path: str, *, default: Path | None = None) -> Path:
    """Normalize ``project_dir``; create missing dirs; fall back on failure."""
    fallback = default if default is not None else get_agent_workspace_dir().resolve()
    raw = _remap_legacy_workspace_prefix((path or "").strip())
    if not raw:
        return fallback
    try:
        candidate = Path(raw).expanduser().resolve()
        if not candidate.exists():
            logger.info("[session_metadata] 目录不存在，正在创建: %s", candidate)
            candidate.mkdir(parents=True, exist_ok=True)
        if not candidate.is_dir():
            raise NotADirectoryError(f"[session_metadata] 路径不是目录: {candidate}")
        return candidate
    except (OSError, ValueError) as exc:
        logger.warning(
            "[session_metadata] project_dir 处理失败 path=%r: %s，自动使用默认路径 %s",
            path,
            exc,
            fallback,
        )
        return fallback


def _normalize_sessions_root_s(sessions_root: str | Path | None) -> str | None:
    if sessions_root is None:
        return None
    return str(sessions_root)


def _metadata_cache_key(session_id: str, sessions_root: str | None) -> str:
    root = sessions_root if sessions_root is not None else str(get_agent_sessions_dir())
    return f"{root}\n{session_id}"


def _peek_project_dir_from_root(session_id: str, sessions_root: Path) -> str | None:
    """Read ``project_dir`` from ``{sessions_root}/{session_id}/metadata.json`` (no write)."""
    root_s = str(sessions_root)
    cache_key = _metadata_cache_key(session_id, root_s)
    with _CACHE_LOCK:
        cached = _METADATA_CACHE.get(cache_key)
    if isinstance(cached, dict):
        pd = cached.get("project_dir")
        if isinstance(pd, str) and pd.strip():
            return pd.strip()

    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        return None
    fpath = session_dir / "metadata.json"
    if not fpath.is_file():
        return None
    try:
        data = json.loads(fpath.read_text(encoding="utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 metadata.json 失败 session=%s: %s", session_id, exc)
        return None
    if not isinstance(data, dict):
        return None
    pd = data.get("project_dir")
    if isinstance(pd, str) and pd.strip():
        return pd.strip()
    return None


def get_resolved_project_dir(
    session_id: str,
    sessions_root: str | Path | None = None,
    *,
    default: str | Path | None = None,
) -> str:
    """Resolve per-session ``project_dir`` for IM detect / tools.

    Lookup order:
      1. ``{sessions_root}/{session_id}/metadata.json`` when ``sessions_root`` given
      2. Global ``get_agent_sessions_dir()`` (where dig-stable chat sync writes today)
      3. ``default`` (typically tenant ``…/agent/jiuwenclaw_workspace``), else ``get_agent_workspace_dir()``

    Does not write defaults back into metadata (writers stay on chat sync path).
    """
    if default is not None:
        default_path = Path(default).expanduser().resolve()
    else:
        default_path = get_agent_workspace_dir().resolve()
    default_s = str(default_path)

    sess = (session_id or "").strip() or "default"
    probe_root = Path(sessions_root) if sessions_root is not None else get_agent_sessions_dir()
    if _safe_session_subdir(sess, probe_root) is None:
        return default_s

    roots: list[Path] = []
    if sessions_root is not None:
        roots.append(Path(sessions_root))
    global_root = get_agent_sessions_dir()
    try:
        global_key = global_root.resolve()
    except OSError:
        global_key = global_root
    if not roots:
        roots.append(global_root)
    else:
        try:
            if roots[0].resolve() != global_key:
                roots.append(global_root)
        except OSError:
            roots.append(global_root)

    for root in roots:
        pd = _peek_project_dir_from_root(sess, root)
        if pd:
            return str(validate_project_dir(pd, default=default_path))
    return default_s


def _metadata_file(session_id: str, sessions_root: str | None = None) -> Path:
    """获取会话元数据文件路径"""
    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        raise ValueError("invalid session_id")
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "metadata.json"


def _read_metadata(
    session_id: str,
    cache_bust: bool = False,
    *,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """读取会话元数据(优先从内存缓存读取,避免异步写入未落盘时读到陈旧数据)

    读路径不应产生副作用：即便 session 目录不存在，也不触发 mkdir，
    否则会导致仅查询(session.rename 无 title 参数时)隐式创建空 session 目录，
    污染 session.list 结果。
    """
    root_s = _normalize_sessions_root_s(sessions_root)
    cache_key = _metadata_cache_key(session_id, root_s)
    if not cache_bust:
        with _CACHE_LOCK:
            cached = _METADATA_CACHE.get(cache_key)
            if cached is not None:
                return cached.copy()
    session_dir = resolve_session_subdir(session_id, sessions_root=root_s)
    if session_dir is None:
        return {}
    fpath = session_dir / "metadata.json"
    if not fpath.exists():
        return {}
    try:
        data = json.loads(fpath.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("读取 metadata.json 失败: %s", exc)
    return {}


def _read_metadata_prefer_root(
    session_id: str,
    cache_bust: bool = False,
    *,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read an explicit tenant root, or the legacy global root when omitted."""
    if sessions_root is not None:
        return _read_metadata(
            session_id, cache_bust=cache_bust, sessions_root=sessions_root
        )
    return _read_metadata(session_id, cache_bust=cache_bust, sessions_root=None)


def _write_metadata_sync(
    session_id: str,
    metadata: dict[str, Any],
    preserve_pin_fields: bool = False,
    *,
    sessions_root: str | None = None,
) -> dict[str, Any]:
    """同步写入会话元数据(由后台 worker 或 fallback 调用)

    注意: 不更新 _METADATA_CACHE。缓存仅由 _enqueue_write 维护,
    避免 gateway 进程的 init_session_metadata 污染缓存导致后续
    读取不到 agentserver 进程写入的最新数据。
    """
    fpath = _metadata_file(session_id, sessions_root)
    to_write = metadata
    with _FILE_LOCK:
        if preserve_pin_fields and fpath.exists():
            try:
                current = json.loads(fpath.read_text(encoding="utf-8") or "{}")
                if isinstance(current, dict):
                    to_write = _merge_pin_fields(current, metadata)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 metadata.json 置顶字段失败: %s", exc)
        tmp_path = fpath.with_name(
            f"{fpath.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp_path.write_text(
                json.dumps(to_write, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(fpath)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
    return to_write


def _merge_pin_fields(current: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    merged = metadata.copy()
    if "pinned" in current:
        merged["pinned"] = bool(current.get("pinned"))
    if "pin_order" in current:
        merged["pin_order"] = int(current.get("pin_order") or 0)
    return merged


def _merge_pin_fields_from_disk(
    session_id: str,
    metadata: dict[str, Any],
    *,
    sessions_root: str | None = None,
) -> dict[str, Any]:
    """Preserve latest disk pin state for async writes that do not own pin fields."""
    session_dir = resolve_session_subdir(session_id, sessions_root=sessions_root)
    if session_dir is None:
        return metadata
    fpath = session_dir / "metadata.json"
    if not fpath.exists():
        return metadata
    try:
        with _FILE_LOCK:
            current = json.loads(fpath.read_text(encoding="utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 metadata.json 置顶字段失败: %s", exc)
        return metadata
    if not isinstance(current, dict):
        return metadata

    return _merge_pin_fields(current, metadata)


def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return

        def _worker() -> None:
            while True:
                item = _METADATA_QUEUE.get()
                try:
                    # Backward-compatible unpack during rolling reload.
                    if len(item) == 4:
                        sid, metadata, sessions_root, preserve_pin_fields = item
                    else:
                        sid, metadata, preserve_pin_fields = item  # type: ignore[misc]
                        sessions_root = None
                    written = _write_metadata_sync(
                        sid,
                        metadata,
                        preserve_pin_fields=preserve_pin_fields,
                        sessions_root=sessions_root,
                    )
                    if preserve_pin_fields:
                        cache_key = _metadata_cache_key(sid, sessions_root)
                        with _CACHE_LOCK:
                            _METADATA_CACHE[cache_key] = written.copy()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("metadata 异步写入失败: %s", exc)
                finally:
                    _METADATA_QUEUE.task_done()

        t = threading.Thread(target=_worker, name="session-metadata-writer", daemon=True)
        t.start()
        _WORKER_STARTED = True


def _enqueue_write(
    session_id: str,
    metadata: dict[str, Any],
    sync_write: bool = False,
    preserve_pin_fields: bool = False,
    *,
    sessions_root: str | Path | None = None,
) -> None:
    """将写入操作放入异步队列,队列满时退化为同步写。

    ``sync_write=True`` 时跳过异步队列,在更新缓存后直接同步落盘。
    用于跨进程敏感写入(如 ``set_session_pinned``):返回前必须落盘,
    否则只读磁盘的另一进程(AgentServer)会读到陈旧数据。

    注意: ``_write_metadata_sync`` 本身不更新缓存,缓存更新统一在此函数
    顶部完成,与异步路径行为一致,避免 ``init_session_metadata`` 污染缓存。
    """
    root_s = _normalize_sessions_root_s(sessions_root)
    cache_key = _metadata_cache_key(session_id, root_s)
    # 立即更新缓存,确保后续读取能看到最新状态
    if preserve_pin_fields:
        metadata = _merge_pin_fields_from_disk(
            session_id, metadata, sessions_root=root_s
        )
    with _CACHE_LOCK:
        _METADATA_CACHE[cache_key] = metadata.copy()
    if sync_write:
        written = _write_metadata_sync(
            session_id,
            metadata,
            preserve_pin_fields=preserve_pin_fields,
            sessions_root=root_s,
        )
        if preserve_pin_fields:
            with _CACHE_LOCK:
                _METADATA_CACHE[cache_key] = written.copy()
        return
    _ensure_worker_started()
    try:
        _METADATA_QUEUE.put_nowait(
            (session_id, metadata, root_s, preserve_pin_fields)
        )
    except queue.Full:
        if preserve_pin_fields:
            metadata = _merge_pin_fields_from_disk(
                session_id, metadata, sessions_root=root_s
            )
            with _CACHE_LOCK:
                _METADATA_CACHE[cache_key] = metadata.copy()
        written = _write_metadata_sync(
            session_id,
            metadata,
            preserve_pin_fields=preserve_pin_fields,
            sessions_root=root_s,
        )
        if preserve_pin_fields:
            with _CACHE_LOCK:
                _METADATA_CACHE[cache_key] = written.copy()


def _auto_title(content: str) -> str:
    """从首条用户消息自动生成会话标题"""
    # 先剥离所有小写 XML 注入标签，
    # 避免将系统提示/文件注入/工具标签误识别为会话标题
    cleaned = _INJECTED_TAG_RE.sub("", content).strip()
    if not cleaned:
        return ""
    title = cleaned.replace("\n", " ")
    if len(title) > _TITLE_MAX_LEN:
        title = title[:_TITLE_MAX_LEN] + "..."
    return title


def init_session_metadata(
    *,
    session_id: str,
    channel_id: str = "",
    user_id: str = "",
    title: str = "",
    mode: str = "unknown",
    team_name: str = "",
    team_template_id: str = "",
    project_dir: str = "",
    project_id: str = "",
    model: str = "",
    cron_id: str = "",
    work_mode: str = "",
    channel_metadata: dict[str, Any] | None = None,
    sessions_root: str | Path | None = None,
) -> None:
    """初始化会话元数据(同步写,确保创建后立即可读)

    ``channel_metadata`` 可选：TUI ``session.create`` 需在首条聊天前写入
    ``cwd``/``project_dir``，供 ``/resume`` current-dir 过滤（见 Issue #2503）。
    """
    # work_mode：未传时按 channel_id 推断默认值（tui→code，其他→work）
    resolved_work_mode = (
        normalize_work_mode(work_mode, default=default_work_mode_for_channel(channel_id))
        if not (isinstance(work_mode, str) and work_mode.strip())
        else normalize_work_mode(work_mode)
    )
    metadata = {
        "session_id": session_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "created_at": _current_timestamp(),
        "last_message_at": _current_timestamp(),
        "title": title,
        "message_count": 0,
        "mode": mode,
        "team_name": team_name,
        "team_template_id": team_template_id,
        "round_id": 0,
        "project_dir": project_dir,
        "project_id": project_id,
        "model": model,
        "cron_id": cron_id,
        "last_user_message_at": _current_timestamp(),
        "pinned": False,
        "pin_order": 0,
        "status": "idle",
        "work_mode": resolved_work_mode,
    }
    if isinstance(channel_metadata, dict) and channel_metadata:
        metadata["channel_metadata"] = channel_metadata
    root_s = _normalize_sessions_root_s(sessions_root)
    _write_metadata_sync(session_id, metadata, sessions_root=root_s)


def update_session_metadata(
    *,
    session_id: str,
    channel_id: str | None = None,
    user_id: str | None = None,
    title: str | None = None,
    clear_title: bool = False,
    increment_message_count: bool = False,
    set_message_count: int | None = None,
    user_content: str | None = None,
    channel_metadata: dict[str, Any] | None = None,
    mode: str | None = None,
    team_name: str | None = None,
    runtime_team_name: str | None = None,
    team_template_id: str | None = None,
    team_template_snapshot: dict[str, Any] | None = None,
    accent_color: str | None = None,
    project_dir: str | None = None,
    project_id: str | None = None,
    model: str | None = None,
    last_user_message_at: float | None = None,
    pinned: bool | None = None,
    pin_order: int | None = None,
    touch_last_message_at: bool = True,
    cache_bust: bool = False,
    sync: bool = False,
    sync_write: bool = False,
    work_mode: str | None = None,
    sessions_root: str | Path | None = None,
) -> None:
    """更新会话元数据(异步写入,不阻塞调用方)

    title 语义(保持历史防御契约)：
      - title=None  → 不修改（默认）
      - title="x"   → 设置为 "x"
      - title=""    → 忽略（防御意外空值覆盖已有标题）
      - 若需显式清除标题，请设置 clear_title=True

    pinned / pin_order 语义：覆盖式，由 session.pin handler 传入；
    未传(None)时不修改。紧凑重编号由 handler 层统一完成。

    touch_last_message_at：是否刷新 ``last_message_at`` 为当前时刻。默认 ``True``
    (消息追加等场景)。纯状态更新(如 ``set_session_pinned`` 的置顶/重编号)应传
    ``False``,避免腐蚀最后消息时间,破坏 session.list 排序与前端展示。

    cache_bust：是否强制读盘(跳过内存缓存)。默认 ``False``。
    跨进程同步场景(如 ``set_session_pinned`` 的重编号)应传 ``True``,
    避免 Gateway 缓存中的陈旧数据覆盖 AgentServer 的并发更新。

    sync_write：是否在返回前同步落盘。默认 ``False``(走异步队列)。
    跨进程敏感写入(如 ``set_session_pinned`` 的置顶/取消置顶/重编号)应传
    ``True``:返回成功前落盘,否则只读磁盘的另一进程(AgentServer)在窗口期
    内会读到陈旧数据,后续整份 metadata 回写会覆盖刚写入的 ``pinned`` 状态。
    """
    root_s = _normalize_sessions_root_s(sessions_root)
    if isinstance(team_template_snapshot, dict) and team_template_snapshot:
        _write_session_team_template_snapshot(
            session_id,
            team_template_snapshot,
            sessions_root=root_s,
        )
    metadata = _read_metadata_prefer_root(
        session_id, cache_bust=cache_bust, sessions_root=root_s
    )

    if not metadata:
        # 如果元数据不存在,创建新的(外部渠道隐式创建 session 的兜底)
        # 自动生成标题: 当 title 为空且提供了用户消息内容时
        auto_title = ""
        if not title and user_content:
            auto_title = _auto_title(user_content)
        # work_mode：未传时按 channel_id 推断默认值
        #
        # 注意：本分支是"metadata 不存在时新建"，每次都用当次请求的 work_mode
        # 重新构建整个 metadata dict，没有"首次锁定"概念。理论上若两次
        # update_session_metadata 调用之间 metadata 文件尚未落盘（_METADATA_QUEUE
        # 异步写入），第二次调用会覆盖第一次的 work_mode。考虑到：
        #   1. 实际场景下同一 session 的首次 init_session_metadata 已写入磁盘，
        #      极少走到这个兜底新建分支；
        #   2. 队列写入延迟在毫秒级，并发覆盖概率极低；
        #   3. 即便覆盖，work_mode 也来自同一通道的请求推断，结果一致；
        # 因此当前不做额外加锁保护。若未来出现跨通道并发创建同一 session 的
        # 场景，需在此处加文件锁或前置 init_session_metadata 强制写盘。
        resolved_work_mode = (
            normalize_work_mode(work_mode, default=default_work_mode_for_channel(channel_id))
            if not (isinstance(work_mode, str) and work_mode.strip())
            else normalize_work_mode(work_mode)
        )
        metadata = {
            "session_id": session_id,
            "channel_id": channel_id or "",
            "user_id": user_id or "",
            "created_at": _current_timestamp(),
            "last_message_at": _current_timestamp(),
            "title": title or auto_title,
            "message_count": 1 if increment_message_count else 0,
            "mode": mode if mode is not None else "unknown",
            "team_name": team_name or "",
            "runtime_team_name": runtime_team_name or "",
            "team_template_id": team_template_id or "",
            "round_id": 0,
            "project_dir": project_dir or "",
            "project_id": project_id or "",
            "model": model or "",
            "cron_id": "",
            "last_user_message_at": last_user_message_at if last_user_message_at is not None else _current_timestamp(),
            "pinned": bool(pinned),
            "pin_order": pin_order if pin_order is not None else 0,
            "status": "idle",
            "work_mode": resolved_work_mode,
        }
        # 首次创建时写入 channel_metadata
        if channel_metadata:
            metadata["channel_metadata"] = channel_metadata
    else:
        # 更新现有元数据
        if channel_id is not None:
            metadata["channel_id"] = channel_id
        if user_id is not None:
            metadata["user_id"] = user_id
        if mode is not None and mode != "unknown":
            metadata["mode"] = mode
        if team_name is not None:
            metadata["team_name"] = team_name
        if runtime_team_name is not None:
            metadata["runtime_team_name"] = runtime_team_name
        if team_template_id is not None:
            metadata["team_template_id"] = team_template_id
        if accent_color is not None:
            metadata["accent_color"] = accent_color
        # model：覆盖式——每次请求更新为本次模型
        if model is not None:
            metadata["model"] = model
        # last_user_message_at：覆盖式——仅在用户消息时由调用方传入
        if last_user_message_at is not None:
            metadata["last_user_message_at"] = last_user_message_at
        # pinned / pin_order：覆盖式——由 session.pin handler 传入
        if pinned is not None:
            metadata["pinned"] = bool(pinned)
        if pin_order is not None:
            metadata["pin_order"] = int(pin_order)
        # project_dir：首次锁定——仅当当前值为空时写入，后续不覆盖
        if project_dir and not metadata.get("project_dir"):
            metadata["project_dir"] = project_dir
        # project_id：首次锁定——仅当当前值为空时写入，后续不覆盖
        if project_id and not metadata.get("project_id"):
            metadata["project_id"] = project_id
        # work_mode：首次锁定——仅当当前值为空或非法时写入，后续不覆盖
        # （与 project_dir/project_id 一致语义，避免会话跨 work_mode 切换导致归属混乱）
        if work_mode:
            normalized_wm = normalize_work_mode(work_mode)
            existing_wm = metadata.get("work_mode")
            if not _has_valid_work_mode(existing_wm):
                metadata["work_mode"] = normalized_wm
        # 显式清除优先级高于 title 入参
        if clear_title:
            metadata["title"] = ""
        elif title:
            metadata["title"] = title
        if increment_message_count:
            metadata["message_count"] = metadata.get("message_count", 0) + 1
        if set_message_count is not None:
            metadata["message_count"] = set_message_count

        # 自动生成标题: 当 title 为空且提供了用户消息内容时
        if not metadata.get("title") and user_content:
            metadata["title"] = _auto_title(user_content)

        # channel_metadata 仅在首次为空时补充写入（不覆盖）
        if channel_metadata and not metadata.get("channel_metadata"):
            metadata["channel_metadata"] = channel_metadata

        # 更新最后消息时间(可由 touch_last_message_at=False 关闭,供置顶重编号等
        # 非消息操作复用本函数而不腐蚀 last_message_at 语义)
        if touch_last_message_at:
            metadata["last_message_at"] = _current_timestamp()

    _enqueue_write(
        session_id,
        metadata,
        sync_write=sync_write or sync,
        preserve_pin_fields=pinned is None and pin_order is None,
        sessions_root=root_s,
    )


def sync_session_request_metadata(
    *,
    session_id: str,
    channel_id: str | None = None,
    mode: str | None = None,
    model: str | None = None,
    project_dir: str | None = None,
    project_id: str | None = None,
    cron_id: str | None = None,
    last_user_message_at: float | None = None,
    is_chat_turn: bool = True,
    explicit_mode_provided: bool = False,
    explicit_model_provided: bool = False,
    work_mode: str | None = None,
    sessions_root: str | Path | None = None,
) -> str | None:
    """校验请求带来的参数与磁盘 metadata.json 是否需要更新，并按字段语义写入。

    本接口是「请求级参数 → 会话级元数据」的统一校验/同步入口，职责是：
    对比本次请求携带的参数与磁盘已持久化的 metadata，按各字段语义决定写不写。
    不负责参数来源解析（那由渠道层 ``resolve_request_project_dir`` 等纯解析函数完成）。

    字段语义：
      - project_dir：**首次锁定，不可改**。磁盘为空则写入请求值（首次锁定）；
        磁盘已有值且与请求值不一致 → 记 warning（说明会话被换项目目录了，有问题），**不覆盖**。
      - model：**显式覆盖式**——仅当请求方显式携带 model（explicit_model_provided=True）
        时才覆盖磁盘值；未显式携带（如只读 RPC）则保持磁盘原值，不把进程 MODEL_NAME
        默认值回写覆盖用户在该会话用 /model 切换过的模型。
      - last_user_message_at：**仅 chat 轮次刷新**——``last_user_message_at=None`` 时
        不覆盖磁盘值（调用方对只读 RPC 应传 None）。
      - last_message_at：**仅 chat 轮次刷新**（``is_chat_turn=True``）。其语义为
        「agent 最后输出时间」，只读 RPC 无 agent 输出，不应刷新；否则点击技能按钮等
        只读查询会把历史会话的 ``last_message_at``/排序时间刷新成「现在」，导致旧会话
        被置顶。
      - mode：**显式覆盖式**——仅当请求方显式携带 mode（explicit_mode_provided=True）
        时才覆盖磁盘值；未显式携带（如只读 RPC 用默认推断值）则保持磁盘原值，不腐蚀
        已锁定的会话 mode（如 team 会话被只读 RPC 默认推断成 agent）。调用方应传入
        canonical mode（"agent.plan"/"team"）。与 append_history_record 联动一致。

    Args:
        session_id: 会话 ID（空则直接返回 None，不做任何操作）
        channel_id / mode / model / last_user_message_at: 请求级参数，按上述语义写入
        project_dir: 请求携带的项目目录候选值，用于首次锁定
        explicit_mode_provided: 请求是否「显式」携带了 mode；False 时 mode 字段不写盘
        explicit_model_provided: 请求是否「显式」携带了 model；False 时 model 字段不写盘
        is_chat_turn: 本次请求是否为真实 chat 轮次（CHAT_SEND/CHAT_RESUME/CHAT_ANSWER）；
            False 时 ``last_message_at`` 不刷新。默认 True（向后兼容存量调用方）。

    Returns:
        本会话**生效**的 project_dir：磁盘已锁定则返回锁定值，否则返回请求候选值
        （首次锁定后即为该值）；无 session_id 或无候选值时返回 None。

    读盘策略：始终 ``cache_bust=True`` 强制读磁盘。本接口由 AgentServer 进程
    调用,而 ``pinned``/``pin_order`` 由 Gateway 进程写入;AgentServer 的内存
    缓存可能陈旧(上一轮聊天留下的 ``pinned=False``)。若用缓存值整份回写,
    会覆盖 Gateway 刚落盘的置顶状态。强制读盘确保本进程只保留磁盘最新值,
    不主动改 ``pinned``/``pin_order``(仅写请求级字段)。
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None

    root_s = _normalize_sessions_root_s(sessions_root)
    metadata = _read_metadata_prefer_root(
        session_id, cache_bust=True, sessions_root=root_s
    )
    effective_project_dir: str | None = None

    if not metadata:
        # 会话元数据不存在：兜底新建（外部渠道隐式创建 session 的场景）
        now = _current_timestamp()
        # work_mode：未传时按 channel_id 推断默认值
        resolved_work_mode = (
            normalize_work_mode(work_mode, default=default_work_mode_for_channel(channel_id))
            if not (isinstance(work_mode, str) and work_mode.strip())
            else normalize_work_mode(work_mode)
        )
        metadata = {
            "session_id": session_id,
            "channel_id": channel_id or "",
            "user_id": "",
            "created_at": now,
            "last_message_at": now,
            "title": "",
            "message_count": 0,
            "mode": mode if (mode is not None and explicit_mode_provided) else "unknown",
            "team_name": "",
            "team_template_id": "",
            "round_id": 0,
            "project_dir": project_dir or "",
            "project_id": project_id or "",
            "model": model if (model is not None and explicit_model_provided) else "",
            "cron_id": cron_id or "",
            "last_user_message_at": last_user_message_at if last_user_message_at is not None else now,
            "pinned": False,
            "pin_order": 0,
            "status": "idle",
            "work_mode": resolved_work_mode,
        }
        effective_project_dir = project_dir or None
    else:
        # 校验 project_dir：首次锁定 / 不一致告警不覆盖
        locked_project = metadata.get("project_dir")
        if isinstance(locked_project, str) and locked_project.strip():
            # 已锁定：以磁盘值为准
            effective_project_dir = locked_project.strip()
            # 请求带了不同值 → 告警（会话被换项目目录，有问题），但不覆盖
            if project_dir and project_dir.strip() and project_dir.strip() != effective_project_dir:
                logger.warning(
                    "会话 %s 的 project_dir 已锁定为 %s，忽略请求带来的不一致值 %s（锁定不可改）",
                    session_id, effective_project_dir, project_dir.strip(),
                )
        elif project_dir and project_dir.strip():
            # 未锁定且请求带了值 → 首次锁定写入
            metadata["project_dir"] = project_dir.strip()
            effective_project_dir = project_dir.strip()

        # project_id：首次锁定，已锁定则忽略请求值（与 project_dir 一致，不可改）
        if project_id and not (isinstance(metadata.get("project_id"), str) and metadata.get("project_id", "").strip()):
            metadata["project_id"] = project_id
        # cron_id：首次锁定，已锁定则忽略请求值（会话来源标记，与 project_id 一致不可改）
        if cron_id and not (isinstance(metadata.get("cron_id"), str) and metadata.get("cron_id", "").strip()):
            metadata["cron_id"] = cron_id

        # work_mode：首次锁定——仅当磁盘值为空或非法时写入，后续不覆盖
        # （与 project_dir/project_id 一致语义，避免会话跨 work_mode 切换导致归属混乱）
        if work_mode:
            normalized_wm = normalize_work_mode(work_mode)
            existing_wm = metadata.get("work_mode")
            if not _has_valid_work_mode(existing_wm):
                metadata["work_mode"] = normalized_wm

        # model：显式覆盖式——仅当请求方显式携带 model 才覆盖；
        # 未显式携带（如只读 RPC 回退到进程 MODEL_NAME）则保持磁盘原值，
        # 不覆盖用户在该会话用 /model 切换过的模型
        if model is not None and explicit_model_provided:
            metadata["model"] = model
        # last_user_message_at：覆盖式
        if last_user_message_at is not None:
            metadata["last_user_message_at"] = last_user_message_at
        # mode：显式覆盖式——仅当请求方显式携带 mode 才覆盖；
        # 未显式携带（如只读 RPC 默认推断）则保持磁盘原值，不腐蚀已锁定的会话 mode
        if mode is not None and explicit_mode_provided:
            metadata["mode"] = mode
        if channel_id is not None:
            metadata["channel_id"] = channel_id
        # last_message_at：仅 chat 轮次刷新。语义为「agent 最后输出时间」，
        # 只读 RPC 无 agent 输出，不应刷新——否则只读查询会把历史会话的排序时间
        # 刷新成「现在」，导致旧会话被置顶。
        if is_chat_turn:
            metadata["last_message_at"] = _current_timestamp()

    _enqueue_write(
        session_id,
        metadata,
        preserve_pin_fields=True,
        sessions_root=root_s,
    )
    return effective_project_dir


def get_session_metadata(
    session_id: str,
    cache_bust: bool = False,
    *,
    enable_writeback: bool = True,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """获取会话元数据

    Args:
        session_id: 会话 ID
        cache_bust: 强制跳过缓存，直接从磁盘读取（用于跨进程同步场景）
        enable_writeback: 是否允许推断后异步写盘持久化。默认 ``True`` 保持
            原行为;只读校验场景(如 ``discard_turn_changes`` 的绑定校验)应传
            ``False``,避免读路径触发写盘副作用,同时仍享受推断能力(存量会话
            缺 ``project_id`` 时可从 ``project_dir`` 反查补全,避免误拒)。
        sessions_root: 租户 sessions 根；缺省为全局 ``get_agent_sessions_dir()``。
            显式传入时只读取该根，不回退全局树。
    """
    root_s = _normalize_sessions_root_s(sessions_root)
    metadata = _read_metadata_prefer_root(
        session_id, cache_bust, sessions_root=root_s
    )
    if isinstance(metadata, dict) and metadata:
        session_dir = resolve_session_subdir(session_id, sessions_root=root_s)
        if session_dir is None:
            return {}
        metadata = _apply_metadata_defaults_with_inference(
            session_id,
            metadata,
            session_dir=session_dir,
            enable_writeback=enable_writeback,
            sessions_root=root_s,
        )
        metadata.setdefault("team_name", "")
        metadata.setdefault("runtime_team_name", "")
        metadata.setdefault("team_template_id", "")
    return metadata


# 会话级 pin 重编号全局序列化锁:保障「设置目标 → 收集所有置顶 → 重编号 → 写回」全过程原子性。
# Gateway 为会话级 pin 的唯一写入方(仅经 Web 本地 handler 处理,不转发 AgentServer)。
_SESSION_PIN_LOCK = threading.Lock()


def set_session_pinned(session_id: str, pinned: bool) -> tuple[bool, int] | None:
    """置顶/取消置顶会话,并对所有置顶会话紧凑重编号为 1..N。幂等。

    整个操作在进程内全局锁内完成:
      1. 设置目标会话 ``pinned``(取消时同步清零 ``pin_order``);
      2. 扫描全部会话,收集 ``pinned=True`` 的会话;
      3. 按 ``pin_order`` 升序稳定排序,重新分配 1..N(消除间隙);
      4. 逐个写回。

    新置顶的会话 ``pin_order`` 默认为 0,排序后置于最前(即新置顶会显示在置顶区顶部)。
    非置顶会话 ``pin_order`` 置 0。幂等:对已处于目标状态的会话再次操作视为成功。

    所有写入均以 ``touch_last_message_at=False`` 调用 ``update_session_metadata``:
    置顶不是消息,不应刷新 ``last_message_at``(否则会腐蚀 ``session.list`` 排序与
    ``SessionInfo`` 展示的「最后消息时间」语义)。

    Args:
        session_id: 目标会话 ID
        pinned: ``True``=置顶,``False``=取消置顶

    Returns:
        ``(操作后的 pinned, 操作后的 pin_order)``;会话不存在(metadata 缺失)时返回 ``None``。
        取消置顶时 ``pin_order`` 恒为 0。
    """
    with _SESSION_PIN_LOCK:
        meta = _read_metadata(session_id, cache_bust=True)
        if not meta:
            return None
        # 1. 设置目标会话 pinned 状态(保留原 pin_order 供重编号排序,取消时清零)
        #    全部 sync_write=True:跨进程敏感写入,返回前必须落盘,否则只读磁盘的
        #    AgentServer 在窗口期内读到旧值,后续整份 metadata 回写会覆盖 pinned 状态。
        if pinned:
            update_session_metadata(
                session_id=session_id, pinned=True,
                touch_last_message_at=False, cache_bust=True, sync_write=True,
            )
        else:
            update_session_metadata(
                session_id=session_id, pinned=False, pin_order=0,
                touch_last_message_at=False, cache_bust=True, sync_write=True,
            )

        # 2. 收集所有置顶会话(读缓存:步骤 1 刚把新状态写入缓存,cache_bust=False
        #    能立即看到;且 pinned/pin_order 仅由 Gateway 进程写入,缓存即权威源。
        #    若用 cache_bust=True 读盘,异步写入未落盘时会读到步骤 1 之前的旧状态,
        #    导致取消置顶的会话被重新纳入重编号而又写回 pinned=True。)
        sessions_dir = get_agent_sessions_dir()
        pinned_list: list[tuple[str, int]] = []
        if sessions_dir.is_dir():
            for session_dir in sessions_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                sid = session_dir.name
                if sid.startswith(_HEARTBEAT_SESSION_PREFIX):
                    continue
                m = _read_metadata(sid)
                if not m:
                    continue
                if m.get("pinned"):
                    pinned_list.append((sid, int(m.get("pin_order", 0))))

        # 3. 升序排序 + 4. 紧凑重编号写回(force disk read 避免回滚覆盖)
        pinned_list.sort(key=lambda x: x[1])
        new_orders: dict[str, int] = {}
        for idx, (sid, _old) in enumerate(pinned_list, start=1):
            update_session_metadata(
                session_id=sid, pinned=True, pin_order=idx,
                touch_last_message_at=False, cache_bust=True, sync_write=True,
            )
            new_orders[sid] = idx

        return pinned, new_orders.get(session_id, 0)


def increment_session_round_count(session_id: str) -> int:
    """递增并持久化 session 的 round_id，返回递增后的值。

    - 首次调用时从 metadata 中读取 round_id（默认 0），先 ++ 再返回。
    - 持久化到 session metadata，确保重启后 round_id 不丢失。
    """
    metadata = _read_metadata(session_id)
    current_round = int(metadata.get("round_id", 0))
    new_round = current_round + 1
    metadata["round_id"] = new_round
    metadata["last_message_at"] = _current_timestamp()
    _enqueue_write(session_id, metadata, preserve_pin_fields=True)
    return new_round


def remove_session_metadata_cache(
    session_id: str,
    *,
    sessions_root: str | Path | None = None,
) -> None:
    """Remove cached session metadata after the session directory is deleted."""
    root_s = _normalize_sessions_root_s(sessions_root)
    cache_key = _metadata_cache_key(session_id, root_s)
    with _CACHE_LOCK:
        _METADATA_CACHE.pop(cache_key, None)
        _METADATA_CACHE.pop(session_id, None)


def set_session_delivery_context(
    *,
    session_id: str,
    channel_id: str | None,
    source_request_id: str | None,
    route_metadata: dict[str, Any] | None,
    delivery_kind: str = _DELIVERY_KIND_SERVER_PUSH,
) -> dict[str, Any]:
    """刷新 session 级 delivery context，供异步 server_push 恢复路由上下文。"""
    metadata = _read_metadata(session_id)
    current_context_raw = metadata.get("delivery_context")
    current_context = (
        copy.deepcopy(current_context_raw)
        if isinstance(current_context_raw, dict)
        else {}
    )

    normalized_channel_id = str(
        channel_id
        or current_context.get("channel_id")
        or metadata.get("channel_id")
        or ""
    ).strip()
    normalized_request_id = str(
        source_request_id or current_context.get("source_request_id") or ""
    ).strip()

    previous_route_metadata = current_context.get("route_metadata")
    if not isinstance(previous_route_metadata, dict):
        previous_route_metadata = None

    normalized_route_metadata = (
        copy.deepcopy(route_metadata)
        if isinstance(route_metadata, dict) and route_metadata
        else previous_route_metadata
    )

    if not metadata:
        metadata = {
            "session_id": session_id,
            "channel_id": normalized_channel_id,
            "user_id": "",
            "created_at": _current_timestamp(),
            "last_message_at": _current_timestamp(),
            "title": "",
            "message_count": 0,
            "mode": "unknown",
            "round_id": 0,
            "project_dir": "",
            "project_id": "",
            "model": "",
            "last_user_message_at": _current_timestamp(),
            "pinned": False,
            "pin_order": 0,
            "status": "idle",
        }
    else:
        if normalized_channel_id:
            metadata["channel_id"] = normalized_channel_id
        metadata["last_message_at"] = _current_timestamp()

    delivery_context: dict[str, Any] = {
        "delivery_kind": str(delivery_kind or _DELIVERY_KIND_SERVER_PUSH).strip()
        or _DELIVERY_KIND_SERVER_PUSH,
        "session_id": session_id,
        "channel_id": normalized_channel_id,
        "source_request_id": normalized_request_id,
        "updated_at": _current_timestamp(),
    }
    if normalized_route_metadata:
        delivery_context["route_metadata"] = normalized_route_metadata

    metadata["delivery_context"] = delivery_context
    _enqueue_write(session_id, metadata, preserve_pin_fields=True)
    return copy.deepcopy(delivery_context)


def get_session_delivery_context(session_id: str) -> dict[str, Any] | None:
    """读取 session 级 delivery context。"""
    metadata = _read_metadata(session_id)
    context = metadata.get("delivery_context")
    if not isinstance(context, dict):
        return None
    return copy.deepcopy(context)


def build_server_push_message(
    *,
    session_id: str,
    request_id: str,
    payload: dict[str, Any],
    fallback_channel_id: str | None = None,
) -> dict[str, Any]:
    """基于 session delivery context 构造 evolution watcher 的 server_push 消息。"""
    delivery_context = get_session_delivery_context(session_id) or {}
    route_metadata = delivery_context.get("route_metadata")
    channel_id = str(
        delivery_context.get("channel_id") or fallback_channel_id or "default"
    ).strip() or "default"

    message: dict[str, Any] = {
        "request_id": request_id,
        "channel_id": channel_id,
        "session_id": session_id,
        "payload": dict(payload),
    }
    if isinstance(route_metadata, dict) and route_metadata:
        message["metadata"] = copy.deepcopy(route_metadata)
    return message


def remove_team_mode_session_dirs_at_startup() -> None:
    """Remove only explicitly temporary team sessions at startup.

    Stable team sessions are recoverable and must survive restarts. Older code
    removed every ``mode=team`` session here, which prevents team session
    restore once team entities are shared across sessions.
    """
    sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.is_dir():
        return

    removed = 0
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        meta_path = session_dir / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8") or '{}')
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动清理跳过会话 %s: 读取 metadata.json 失败: %s", session_dir.name, exc)
            continue
        if not isinstance(raw, dict):
            continue
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in {"team", "team.plan", "code.team"}:
            continue
        if not bool(raw.get("temporary_team_session") or raw.get("team_temporary")):
            continue

        session_id = session_dir.name
        try:
            shutil.rmtree(session_dir)
            with _CACHE_LOCK:
                _METADATA_CACHE.pop(session_id, None)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动清理删除 team 会话目录失败 %s: %s", session_id, exc)

    if removed:
        logger.info("启动清理: 已删除 %d 个 team 模式会话目录", removed)


def _resolve_legacy_work_mode(
    raw: dict[str, Any],
    dir_to_projects: dict[str, list[tuple[str, str]]],
    id_to_work_mode: dict[str, str],
) -> str | None:
    """启动迁移时为老会话推断 work_mode（§5.3.4.1 同路径双模式消歧）。

    Args:
        raw: 老会话的 metadata dict。
        dir_to_projects: project_dir → [(project_id, work_mode), ...] 映射
            （仅包含 project_dir 非空的项目，用于路径消歧）。
        id_to_work_mode: project_id → work_mode 映射
            （包含**所有**项目，含 project_dir 为空的项目，用于 rule 2 直接命中）。

    Returns:
        推断出的 ``"code"`` / ``"work"``，或 ``None``（无法消歧时保守跳过）。
    """
    # 规则 2：metadata 已有 project_id 命中真实可见 Project → 继承该 Project 的 work_mode
    # 优先用 id_to_work_mode 直接查找（覆盖 project_dir 为空的项目），
    # 再回退到 dir_to_projects 遍历（覆盖旧迁移路径中仅按路径关联的场景）。
    existing_pid = str(raw.get("project_id") or "").strip()
    if existing_pid:
        if existing_pid in id_to_work_mode:
            return id_to_work_mode[existing_pid]
        for _candidates in dir_to_projects.values():
            for pid, pwm in _candidates:
                if pid == existing_pid:
                    return pwm

    # dir_to_projects 的 key 已按 _normalize_path_for_match 规范化,查询侧同口径
    try:
        from jiuwenswarm.server.runtime.session.project_store import (
            _normalize_path_for_match,
        )
        pp = _normalize_path_for_match(str(raw.get("project_dir") or ""))
    except Exception:
        pp = str(raw.get("project_dir") or "")
    if not pp or pp not in dir_to_projects:
        # 无 project_dir 或无 Project 命中：无法消歧，交由运行期兜底
        return None

    candidates = dir_to_projects[pp]
    # 规则 5：同路径只有一个 Project → 直接使用
    if len(candidates) == 1:
        return candidates[0][1]

    # 规则 3/4：同路径双模式，按 channel_id 消歧
    # 注意：channel_id 为空时无法消歧（既非明确 tui 也非明确 web），保守跳过。
    channel_id = str(raw.get("channel_id") or "").strip().lower()
    if channel_id == "tui":
        preferred = "code"
    elif channel_id:
        preferred = "work"
    else:
        # channel_id 为空：无法消歧，交由运行期兜底
        return None
    for _pid, pwm in candidates:
        if pwm == preferred:
            return pwm

    # 候选中无目标 work_mode：保守跳过
    return None


def get_all_sessions_metadata(
    limit: int = 20,
    offset: int = 0,
    sessions_root: str | Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    获取所有会话的元数据。

    Args:
        limit: 分页大小。
        offset: 分页偏移。
        sessions_root: 显式会话目录；缺省时按当前绑定租户（个人版 / 默认）解析。

    Returns:
        (sessions, total): 当前页的会话列表 和 会话总数
    """
    sessions_dir = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return [], 0

    sessions = []
    # 批量入口构建一次 project 映射,所有会话共用,避免 N+1 扫描 project_store。
    dir_to_projects, id_to_work_mode = _build_project_lookup()
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue

        session_id = session_dir.name
        if session_id.startswith(_HEARTBEAT_SESSION_PREFIX):
            continue
        metadata = _read_metadata(session_id, sessions_root=sessions_root)

        if not metadata:
            # 没有 metadata.json 的旧会话: 只构造最小信息,不读取 history.json
            # (避免大量旧会话导致接口变慢);字段补全由 _apply_metadata_defaults_with_inference 负责。
            # 这里不写盘(无 metadata.json 的会话通常是异常残留,写回可能掩盖上游问题)。
            metadata = {
                "session_id": session_id,
                "channel_id": "",
                "user_id": "",
                "created_at": session_dir.stat().st_ctime,
                "last_message_at": session_dir.stat().st_mtime,
                "title": "",
                "message_count": 0,
                "mode": "unknown",
                "project_id": "",
                "project_dir": "",
                "cron_id": "",
                "work_mode": DEFAULT_WEB_WORK_MODE,
            }
            # 无 metadata.json 的会话不做推断写盘,仅补默认值
            metadata = _apply_metadata_defaults_with_inference(
                session_id,
                metadata,
                session_dir=session_dir,
                dir_to_projects=dir_to_projects,
                id_to_work_mode=id_to_work_mode,
                enable_writeback=False,
            )
        else:
            # 批量入口不写盘:避免首次 session.list 触发大量异步写入导致队列满退化为同步写。
            # 缺失字段会在后续单条 get_session_metadata 读取时按需写回(真正的惰性迁移)。
            metadata = _apply_metadata_defaults_with_inference(
                session_id,
                metadata,
                session_dir=session_dir,
                dir_to_projects=dir_to_projects,
                id_to_work_mode=id_to_work_mode,
                enable_writeback=False,
            )

        sessions.append(metadata)

    # 按最后消息时间倒序排序
    sessions.sort(key=lambda x: x.get("last_message_at", 0), reverse=True)

    total = len(sessions)
    return sessions[offset: offset + limit], total


def collect_all_sessions_metadata() -> list[dict[str, Any]]:
    """收集全部会话元数据(不分页、不排序),供项目统计与置顶会话聚合使用。

    跳过 heartbeat 会话;强制读盘(``cache_bust=True``)以跨进程拿最新数据。
    无 ``metadata.json`` 的旧会话以目录时间戳构造最小兜底信息
    (``project_id=""``、``project_dir=""``、``pinned=False``),归入默认项目统计。
    返回的每个 dict 已对新增字段应用默认值兜底。
    """
    sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.is_dir():
        return []
    result: list[dict[str, Any]] = []
    # 批量入口构建一次 project 映射,所有会话共用,避免 N+1 扫描 project_store。
    dir_to_projects, id_to_work_mode = _build_project_lookup()
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        sid = session_dir.name
        if sid.startswith(_HEARTBEAT_SESSION_PREFIX):
            continue
        meta = _read_metadata(sid, cache_bust=True)
        if not meta:
            # 旧会话无 metadata.json: 构造最小兜底,归入默认项目。
            # 不做推断写盘(无 metadata.json 通常是异常残留)。
            try:
                st = session_dir.stat()
            except OSError:
                continue
            meta = {
                "session_id": sid,
                "project_id": "",
                "project_dir": "",
                "cron_id": "",
                "pinned": False,
                "pin_order": 0,
                "last_message_at": st.st_mtime,
                # 与 get_session_metadata / 同函数 else 分支一致: 无用户消息时
                # 回退到 created_at(保证排序稳定性,避免空会话全部沉底)
                "last_user_message_at": st.st_ctime,
                "created_at": st.st_ctime,
                "work_mode": DEFAULT_WEB_WORK_MODE,
            }
            meta = _apply_metadata_defaults_with_inference(
                sid,
                meta,
                session_dir=session_dir,
                dir_to_projects=dir_to_projects,
                id_to_work_mode=id_to_work_mode,
                enable_writeback=False,
            )
        else:
            # 批量入口不写盘:避免首次 collect 触发大量异步写入导致队列满退化为同步写。
            # 缺失字段会在后续单条 get_session_metadata 读取时按需写回(真正的惰性迁移)。
            meta = _apply_metadata_defaults_with_inference(
                sid,
                meta,
                session_dir=session_dir,
                dir_to_projects=dir_to_projects,
                id_to_work_mode=id_to_work_mode,
                enable_writeback=False,
            )
        result.append(meta)
    return result

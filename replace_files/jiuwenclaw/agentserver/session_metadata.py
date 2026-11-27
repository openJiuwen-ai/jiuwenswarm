"""会话元数据管理模块"""
from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from jiuwenclaw.utils import get_agent_sessions_dir, get_agent_workspace_dir, get_user_workspace_dir

logger = logging.getLogger(__name__)

# ---------- 异步写入队列(与 session_history 保持一致的模式) ----------
_METADATA_QUEUE: queue.Queue[tuple[str, dict[str, Any], str | None]] = queue.Queue(maxsize=5000)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_FILE_LOCK = threading.Lock()

# 内存缓存: 解决异步写入时读取到陈旧磁盘数据的竞态条件
_METADATA_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# 会话标题自动生成的截取长度
_TITLE_MAX_LEN = 50


def _current_timestamp() -> float:
    """返回显式使用 UTC 时区的当前时间戳"""
    return datetime.now(timezone.utc).timestamp()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_session_subdir(session_id: str, sessions_root: str | Path | None = None) -> Path | None:
    """解析 sessions 根下安全子目录；非法 session_id 返回 None（不 mkdir）。"""
    stripped = (session_id or "").strip()
    if not stripped or stripped in {".", ".."}:
        return None
    if ".." in stripped or "/" in stripped or "\\" in stripped:
        return None
    root = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
    base = root.resolve()
    target = (base / stripped).resolve()
    if target == base or not _is_relative_to(target, base):
        return None
    return target


def validate_project_dir(path: str, *, default: Path | None = None) -> Path:
    """
    路径处理逻辑：
    1. 解析为绝对路径，展开 ~
    2. 检查是否存在，不存在则自动创建
    3. 创建失败/路径非法 → 安全使用默认路径
    4. 无任何路径限制
    """
    # 回退默认路径
    fallback = default if default is not None else get_agent_workspace_dir().resolve()
    raw = (path or "").strip()
    if not raw:
        return fallback
    try:
        # 1. 解析为绝对路径
        candidate = Path(raw).expanduser().resolve()

        # 2. 路径不存在 → 自动创建（多级目录也支持）
        if not candidate.exists():
            logger.info(f"[session_metadata] 目录不存在，正在创建: {candidate}")
            candidate.mkdir(parents=True, exist_ok=True)

        # 3. 必须是目录（不是文件）
        if not candidate.is_dir():
            raise NotADirectoryError(f"[session_metadata] 路径不是目录: {candidate}")

        return candidate
    # 所有异常 → 都用默认路径
    except (OSError, ValueError) as exc:
        logger.warning(
            "[session_metadata] project_dir 处理失败 path=%r: %s，自动使用默认路径 %s",
            path,
            exc,
            fallback,
        )
        return fallback


def get_resolved_project_dir(
    session_id: str,
    sessions_root: str | Path | None = None,
) -> str:
    """从 metadata 读取 project_dir，无效或未设置时返回全局 agent workspace。"""
    default_s = str(get_agent_workspace_dir().resolve())
    if _safe_session_subdir(session_id, sessions_root) is None:
        return default_s
    sessions_root_s = str(sessions_root) if sessions_root else None
    meta = _read_metadata(session_id, sessions_root_s)
    pd = meta.get("project_dir")
    if not isinstance(pd, str) or not pd.strip():
        meta["project_dir"] = default_s
        _enqueue_write(session_id, meta, sessions_root_s)
        return default_s
    return str(validate_project_dir(pd.strip(), default=Path(default_s)))


def _metadata_file(session_id: str, sessions_root: str | None = None) -> Path:
    """获取会话元数据文件路径"""
    root = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "metadata.json"


def _read_metadata(session_id: str, sessions_root: str | None = None) -> dict[str, Any]:
    """读取会话元数据(优先从内存缓存读取,避免异步写入未落盘时读到陈旧数据)

    读路径不应产生副作用：即便 session 目录不存在，也不触发 mkdir，
    否则会导致仅查询(session.rename 无 title 参数时)隐式创建空 session 目录，
    污染 session.list 结果。
    """
    with _CACHE_LOCK:
        cached = _METADATA_CACHE.get(session_id)
        if cached is not None:
            return cached.copy()
    root = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
    fpath = root / session_id / "metadata.json"
    if not fpath.exists():
        return {}
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("读取 metadata.json 失败: %s", exc)
    return {}


def _write_metadata_sync(session_id: str, metadata: dict[str, Any], sessions_root: str | None = None) -> None:
    """同步写入会话元数据(由后台 worker 或 fallback 调用)

    注意: 不更新 _METADATA_CACHE。缓存仅由 _enqueue_write 维护,
    避免 gateway 进程的 init_session_metadata 污染缓存导致后续
    读取不到 agentserver 进程写入的最新数据。
    """
    fpath = _metadata_file(session_id, sessions_root)
    with _FILE_LOCK:
        fpath.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return

        def _worker() -> None:
            while True:
                sid, metadata, sessions_root = _METADATA_QUEUE.get()
                try:
                    _write_metadata_sync(sid, metadata, sessions_root)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("metadata 异步写入失败: %s", exc)
                finally:
                    _METADATA_QUEUE.task_done()

        t = threading.Thread(target=_worker, name="session-metadata-writer", daemon=True)
        t.start()
        _WORKER_STARTED = True


def _enqueue_write(session_id: str, metadata: dict[str, Any], sessions_root: str | None = None) -> None:
    """将写入操作放入异步队列,队列满时退化为同步写"""
    # 立即更新缓存,确保后续读取能看到最新状态
    with _CACHE_LOCK:
        _METADATA_CACHE[session_id] = metadata.copy()
    _ensure_worker_started()
    try:
        _METADATA_QUEUE.put_nowait((session_id, metadata, sessions_root))
    except queue.Full:
        _write_metadata_sync(session_id, metadata, sessions_root)


def _auto_title(content: str) -> str:
    """从首条用户消息自动生成会话标题"""
    title = content.strip().replace("\n", " ")
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
    sessions_root: str | Path | None = None,
    project_dir: str | None = None,
) -> None:
    """初始化会话元数据(同步写,确保创建后立即可读)"""
    metadata = {
        "session_id": session_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "created_at": _current_timestamp(),
        "last_message_at": _current_timestamp(),
        "title": title,
        "message_count": 0,
        "mode": mode,
    }
    if isinstance(project_dir, str) and project_dir.strip():
        metadata["project_dir"] = str(validate_project_dir(project_dir.strip()))
    sessions_root_s = str(sessions_root) if sessions_root else None
    _write_metadata_sync(session_id, metadata, sessions_root_s)


def update_session_metadata(
    *,
    session_id: str,
    channel_id: str | None = None,
    user_id: str | None = None,
    title: str | None = None,
    clear_title: bool = False,
    increment_message_count: bool = False,
    user_content: str | None = None,
    channel_metadata: dict[str, Any] | None = None,
    mode: str | None = None,
    sessions_root: str | Path | None = None,
    project_dir: str | None = None,
) -> None:
    """更新会话元数据(异步写入,不阻塞调用方)

    title 语义(保持历史防御契约)：
      - title=None  → 不修改（默认）
      - title="x"   → 设置为 "x"
      - title=""    → 忽略（防御意外空值覆盖已有标题）
      - 若需显式清除标题，请设置 clear_title=True
    """
    sessions_root_s = str(sessions_root) if sessions_root else None
    metadata = _read_metadata(session_id, sessions_root_s)

    if not metadata:
        # 如果元数据不存在,创建新的(外部渠道隐式创建 session 的兜底)
        # 自动生成标题: 当 title 为空且提供了用户消息内容时
        auto_title = ""
        if not title and user_content:
            auto_title = _auto_title(user_content)
        metadata = {
            "session_id": session_id,
            "channel_id": channel_id or "",
            "user_id": user_id or "",
            "created_at": _current_timestamp(),
            "last_message_at": _current_timestamp(),
            "title": title or auto_title,
            "message_count": 1 if increment_message_count else 0,
            "mode": mode if mode is not None else "unknown",
        }
        # 首次创建时写入 channel_metadata
        if channel_metadata:
            metadata["channel_metadata"] = channel_metadata
        if isinstance(project_dir, str) and project_dir.strip():
            metadata["project_dir"] = str(validate_project_dir(project_dir.strip()))
    else:
        # 更新现有元数据
        if channel_id is not None:
            metadata["channel_id"] = channel_id
        if user_id is not None:
            metadata["user_id"] = user_id
        if mode is not None:
            metadata["mode"] = mode
        # 显式清除优先级高于 title 入参
        if clear_title:
            metadata["title"] = ""
        elif title:
            metadata["title"] = title
        if increment_message_count:
            metadata["message_count"] = metadata.get("message_count", 0) + 1

        # 自动生成标题: 当 title 为空且提供了用户消息内容时
        if not metadata.get("title") and user_content:
            metadata["title"] = _auto_title(user_content)

        # channel_metadata 仅在首次为空时补充写入（不覆盖）
        if channel_metadata and not metadata.get("channel_metadata"):
            metadata["channel_metadata"] = channel_metadata

        # 总是更新最后消息时间
        metadata["last_message_at"] = _current_timestamp()

        if isinstance(project_dir, str) and project_dir.strip():
            metadata["project_dir"] = str(validate_project_dir(project_dir.strip()))

    _enqueue_write(session_id, metadata, sessions_root_s)


def get_session_metadata(session_id: str) -> dict[str, Any]:
    """获取会话元数据"""
    return _read_metadata(session_id)


def remove_team_mode_session_dirs_at_startup() -> None:
    """agentserver 启动时删除 metadata.json 中 mode 为 team 的会话目录。"""
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
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动清理跳过会话 %s: 读取 metadata.json 失败: %s", session_dir.name, exc)
            continue
        if not isinstance(raw, dict) or raw.get("mode") != "team":
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


def get_all_sessions_metadata(
    limit: int = 20,
    offset: int = 0,
    sessions_root: str | Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    获取所有会话的元数据。

    Args:
        limit: 返回数量上限
        offset: 偏移量
        sessions_root: 会话存储根目录，默认使用单租户路径

    Returns:
        (sessions, total): 当前页的会话列表 和 会话总数
    """
    sessions_dir = Path(sessions_root) if sessions_root else get_agent_sessions_dir()
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return [], 0

    sessions = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue

        session_id = session_dir.name
        metadata = _read_metadata(session_id, sessions_root=str(sessions_dir) if sessions_root else None)

        if not metadata:
            # 没有 metadata.json 的旧会话: 只构造最小信息,不读取 history.json
            # (避免大量旧会话导致接口变慢,完整推断由启动迁移负责)
            metadata = {
                "session_id": session_id,
                "channel_id": "",
                "user_id": "",
                "created_at": session_dir.stat().st_ctime,
                "last_message_at": session_dir.stat().st_mtime,
                "title": "",
                "message_count": 0,
                "mode": "unknown",
            }

        sessions.append(metadata)

    # 按最后消息时间倒序排序
    sessions.sort(key=lambda x: x.get("last_message_at", 0), reverse=True)

    total = len(sessions)
    return sessions[offset: offset + limit], total

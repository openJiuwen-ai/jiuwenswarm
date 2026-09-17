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

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.server.runtime.session.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    SUPPORTED_WORK_MODES,
    default_work_mode_for_channel,
    normalize_work_mode,
)

logger = logging.getLogger(__name__)

# ---------- 异步写入队列(与 session_history 保持一致的模式) ----------
# 队列项: (session_id, metadata, preserve_pin_fields, rebind_gen_at_enqueue)
# rebind_gen_at_enqueue 用于检测"入队后发生过 rebind"的陈旧快照:
# worker 处理时若发现该值 < 当前 rebind_gen, 说明此快照早于一次 project 重绑,
# 需从磁盘保留 rebind 写入的 project 字段, 防止陈旧快照覆盖重绑结果。
_METADATA_QUEUE: queue.Queue[tuple[str, dict[str, Any], bool, int]] = queue.Queue(maxsize=5000)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
# Reentrant: the read path now holds this lock too (see _read_metadata).
# RLock guards against a same-thread nesting (read->write or write->read)
# deadlocking in the future.
_FILE_LOCK = threading.RLock()

# 内存缓存: 解决异步写入时读取到陈旧磁盘数据的竞态条件
_METADATA_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# 会话级 project 重绑版本号: 每次 rebind_session_project 自增。
# _enqueue_write 入队时捕获当前值; worker 处理时若发现入队值 < 当前值,
# 说明该快照早于一次重绑, 需从磁盘保留 rebind 写入的 project 字段
# (project_id/project_dir/work_mode/channel_metadata), 防止陈旧异步快照
# 在 worker 写回时覆盖刚完成的 rebind。
_SESSION_REBIND_GEN: dict[str, int] = {}
_REBIND_GEN_LOCK = threading.Lock()


def _get_rebind_gen(session_id: str) -> int:
    """读取会话当前的重绑版本号（无重绑时为 0）。"""
    with _REBIND_GEN_LOCK:
        return _SESSION_REBIND_GEN.get(session_id, 0)


def _bump_rebind_gen(session_id: str) -> None:
    """自增会话的重绑版本号, 使此前已入队但尚未处理的陈旧快照失效。"""
    with _REBIND_GEN_LOCK:
        _SESSION_REBIND_GEN[session_id] = _SESSION_REBIND_GEN.get(session_id, 0) + 1


# 会话标题自动生成的截取长度
_TITLE_MAX_LEN = 50
# 心跳任务会话目录前缀，不参与 session.list 等列表展示
_HEARTBEAT_SESSION_PREFIX = "heartbeat_"
_DELIVERY_KIND_SERVER_PUSH = "server_push"
# user_id 白名单: 仅允许字母数字及 _-, 拒绝路径遍历字符
_SAFE_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

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
            _enqueue_write(session_id, metadata, preserve_pin_fields=True)
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


def _metadata_file(session_id: str) -> Path:
    """获取会话元数据文件路径"""
    session_dir = get_agent_sessions_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "metadata.json"


def _read_metadata(session_id: str, cache_bust: bool = False) -> dict[str, Any]:
    """读取会话元数据(优先从内存缓存读取,避免异步写入未落盘时读到陈旧数据)

    读路径不应产生副作用：即便 session 目录不存在，也不触发 mkdir，
    否则会导致仅查询(session.rename 无 title 参数时)隐式创建空 session 目录，
    污染 session.list 结果。

    Args:
        session_id: 会话 ID
        cache_bust: 强制跳过缓存，直接从磁盘读取（用于跨进程同步场景，如 session.list）
    """
    if not cache_bust:
        with _CACHE_LOCK:
            cached = _METADATA_CACHE.get(session_id)
            if cached is not None:
                return cached.copy()
    # cache_bust=True 或缓存没有数据时，强制读磁盘
    fpath = get_agent_sessions_dir() / session_id / "metadata.json"
    if not fpath.exists():
        return {}
    # Reading must be mutually exclusive with _write_metadata_sync: the writer
    # replaces the whole file, so an unlocked read can land inside the
    # replacement window and observe empty content.
    try:
        with _FILE_LOCK:
            raw = fpath.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to read metadata.json: %s (session_id=%s)", exc, session_id,
        )
        return {}
    if not raw.strip():
        # An empty file is a FAILURE, not "empty metadata". It must never be
        # returned as a valid {}: callers do read-modify-write and would write
        # the empty dict back, erasing session_id/title and other identity
        # fields, leaving the session without an ID and impossible to open.
        logger.warning(
            "metadata.json is empty, treating as read failure (session_id=%s)",
            session_id,
        )
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to parse metadata.json: %s (session_id=%s, size=%d)",
            exc, session_id, len(raw),
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "metadata.json content is not a dict: %s (session_id=%s)",
            type(data).__name__, session_id,
        )
        return {}
    return data


def _read_metadata_file_in(session_dir: Path) -> dict[str, Any]:
    """直接读指定会话目录下的 metadata.json,不走全局 sessions 目录单例。

    供 ``collect_all_sessions_metadata(user_id=...)`` 按用户家目录读时使用:
    避免触碰进程级 ``get_agent_sessions_dir`` 全局态,多 web 连接并发各读各目录无串扰。
    与 ``_read_metadata`` cache_bust 语义一致(直接读盘,不读内存缓存)。
    """
    fpath = session_dir / "metadata.json"
    if not fpath.exists():
        return {}
    try:
        data = json.loads(fpath.read_text(encoding="utf-8") or '{}')
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 失败: %s", fpath, exc)
    return {}


def _write_metadata_sync(
    session_id: str,
    metadata: dict[str, Any],
    preserve_pin_fields: bool = False,
    preserve_rebound_fields: bool = False,
    rebind_gen_at_enqueue: int | None = None,
) -> dict[str, Any]:
    """同步写入会话元数据(由后台 worker 或 fallback 调用)

    注意: 不更新 _METADATA_CACHE。缓存仅由 _enqueue_write 维护,
    避免 gateway 进程的 init_session_metadata 污染缓存导致后续
    读取不到 agentserver 进程写入的最新数据。

    rebind 版本检查: 调用方可传入快照入队/捕获时记录的 ``rebind_gen_at_enqueue``。
    本函数持 ``_FILE_LOCK`` 后重比 ``_get_rebind_gen(session_id)``, 使"gen 比较"与
    "文件读写"在同一临界区内完成, 消除 worker 队列路径(P3)的 TOCTOU 窗口:
    即使调用方在持锁前比 gen 为"非陈旧", 持锁后又发生了 rebind, 此处也能发现
    并从磁盘当前值保留 rebind 写入的 project 字段。``rebind_session_project``
    自身传入的 gen 已被 bump, 等于当前 gen, 不触发合并。``preserve_rebound_fields``
    作为无 gen 追踪路径(外部直写/测试)的回退开关保留。
    """
    fpath = _metadata_file(session_id)
    to_write = metadata
    with _FILE_LOCK:
        current: dict[str, Any] | None = None
        if fpath.exists():
            try:
                raw = fpath.read_text(encoding="utf-8")
                parsed = json.loads(raw) if raw.strip() else None
                if isinstance(parsed, dict):
                    current = parsed
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to read metadata.json: %s", exc)

        # Identity guard: a caller that received an empty/partial dict and
        # writes it back would permanently erase session_id, title, created_at
        # and friends (writes replace the whole file, they do not merge). When
        # disk has an identity and the incoming payload does not, treat it as a
        # race-induced dirty write and restore the missing fields.
        if current and current.get("session_id") and not metadata.get("session_id"):
            recovered = {k: v for k, v in current.items() if k not in metadata}
            to_write = {**current, **metadata}
            logger.warning(
                "blocked overwrite of session identity (session_id=%s): payload "
                "has no session_id, restored %d field(s) from disk: %s",
                session_id, len(recovered), sorted(recovered),
            )

        if preserve_pin_fields and current is not None:
            to_write = _merge_pin_fields(current, to_write)
        # 权威 rebind 版本检查: 持锁后重比 gen, 判定调用方传入的快照是否已陈旧。
        # 比调用方持锁前的 pre-check 更可靠 —— 消除"gen 比较与文件写入"之间的
        # TOCTOU 窗口(P3)。rebind 自身传入的 gen 已 bump, 等于当前 gen, 不触发合并。
        effective_preserve_rebound = preserve_rebound_fields
        if rebind_gen_at_enqueue is not None and current is not None:
            if rebind_gen_at_enqueue < _get_rebind_gen(session_id):
                effective_preserve_rebound = True
        if effective_preserve_rebound and current is not None:
            to_write = _merge_rebound_fields(current, to_write)

        # Atomic write: write a temp file then rename, so that truncation by
        # write_text can never expose empty content to a concurrent reader
        # (precisely how session identity was being lost).
        payload = json.dumps(to_write, ensure_ascii=False, indent=2)
        tmp = fpath.with_name(f"{fpath.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, fpath)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return to_write


def _merge_pin_fields(current: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    merged = metadata.copy()
    if "pinned" in current:
        merged["pinned"] = bool(current.get("pinned"))
    if "pin_order" in current:
        merged["pin_order"] = int(current.get("pin_order") or 0)
    return merged


def _merge_rebound_fields(current: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """从磁盘当前值保留 rebind 写入的 project 字段, 防止陈旧快照覆盖重绑结果。

    用于 worker 处理"入队早于 rebind"的陈旧快照: 该快照的 project_id/
    project_dir/work_mode/channel_metadata 是重绑前的旧值, 直接写回会覆盖
    rebind。这里以磁盘当前值为准覆盖这几个字段, 其余字段仍用快照值。
    """
    merged = metadata.copy()
    for field in ("project_id", "project_dir", "work_mode", "channel_metadata"):
        if field in current:
            merged[field] = current[field]
    return merged


def _merge_pin_fields_from_disk(session_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Preserve latest disk pin state for async writes that do not own pin fields."""
    fpath = get_agent_sessions_dir() / session_id / "metadata.json"
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
                sid, metadata, preserve_pin_fields, rebind_gen_at_enqueue = _METADATA_QUEUE.get()
                try:
                    # rebind 版本检查已下沉到 _write_metadata_sync 内部, 持
                    # _FILE_LOCK 后重比 gen, 消除"gen 比较与文件写入"的 TOCTOU 窗口(P3)。
                    written = _write_metadata_sync(
                        sid,
                        metadata,
                        preserve_pin_fields=preserve_pin_fields,
                        rebind_gen_at_enqueue=rebind_gen_at_enqueue,
                    )
                    # gen 追踪启用时, _write_metadata_sync 可能在持锁后才发现陈旧
                    # 并合并 rebound 字段, 故只要启用了 gen 追踪就刷新缓存为落盘结果。
                    if preserve_pin_fields or rebind_gen_at_enqueue is not None:
                        with _CACHE_LOCK:
                            _METADATA_CACHE[sid] = written.copy()
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
) -> None:
    """将写入操作放入异步队列,队列满时退化为同步写。

    ``sync_write=True`` 时跳过异步队列,在更新缓存后直接同步落盘。
    用于跨进程敏感写入(如 ``set_session_pinned``):返回前必须落盘,
    否则只读磁盘的另一进程(AgentServer)会读到陈旧数据。

    注意: ``_write_metadata_sync`` 本身不更新缓存,缓存更新统一在此函数
    顶部完成,与异步路径行为一致,避免 ``init_session_metadata`` 污染缓存。
    """
    # 立即更新缓存,确保后续读取能看到最新状态
    # 入队前捕获 rebind 版本号: worker 处理时据此判断本快照是否早于一次重绑
    rebind_gen_at_enqueue = _get_rebind_gen(session_id)
    if preserve_pin_fields:
        metadata = _merge_pin_fields_from_disk(session_id, metadata)
    with _CACHE_LOCK:
        _METADATA_CACHE[session_id] = metadata.copy()
    if sync_write:
        # P2: sync_write 路径同样需要 rebind 版本检查 —— set_session_pinned 等
        # sync_write=True 调用方读取磁盘早于一次 rebind 时, 其快照含旧 project
        # 字段, 回写会覆盖 rebind。版本检查下沉到 _write_metadata_sync 持锁后执行,
        # 与 queue.Full 退化路径、worker 异步路径保持同一保护级别。
        written = _write_metadata_sync(
            session_id,
            metadata,
            preserve_pin_fields=preserve_pin_fields,
            rebind_gen_at_enqueue=rebind_gen_at_enqueue,
        )
        if preserve_pin_fields or rebind_gen_at_enqueue is not None:
            with _CACHE_LOCK:
                _METADATA_CACHE[session_id] = written.copy()
        return
    _ensure_worker_started()
    try:
        _METADATA_QUEUE.put_nowait(
            (session_id, metadata, preserve_pin_fields, rebind_gen_at_enqueue)
        )
    except queue.Full:
        if preserve_pin_fields:
            metadata = _merge_pin_fields_from_disk(session_id, metadata)
            with _CACHE_LOCK:
                _METADATA_CACHE[session_id] = metadata.copy()
        # 队列满退化为同步写: 版本检查同样下沉到 _write_metadata_sync 持锁后执行
        written = _write_metadata_sync(
            session_id,
            metadata,
            preserve_pin_fields=preserve_pin_fields,
            rebind_gen_at_enqueue=rebind_gen_at_enqueue,
        )
        if preserve_pin_fields or rebind_gen_at_enqueue is not None:
            with _CACHE_LOCK:
                _METADATA_CACHE[session_id] = written.copy()


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
    _write_metadata_sync(session_id, metadata)


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
    team_template_id: str | None = None,
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
    metadata = _read_metadata(session_id, cache_bust=cache_bust)

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
        if mode is not None:
            metadata["mode"] = mode
        if team_name is not None:
            metadata["team_name"] = team_name
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
    user_id: str | None = None,
    last_user_message_at: float | None = None,
    is_chat_turn: bool = True,
    explicit_mode_provided: bool = False,
    explicit_model_provided: bool = False,
    work_mode: str | None = None,
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

    metadata = _read_metadata(session_id, cache_bust=True)
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
            "user_id": user_id or "",
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
        # user_id：首次锁定，已锁定则忽略请求值（与 project_id/cron_id 一致不可改）。
        # 由 envelope.user_id 透传，供 gateway 列表接口按用户隔离会话历史。
        if user_id and not (isinstance(metadata.get("user_id"), str) and metadata.get("user_id", "").strip()):
            metadata["user_id"] = user_id
        # last_message_at：仅 chat 轮次刷新。语义为「agent 最后输出时间」，
        # 只读 RPC 无 agent 输出，不应刷新——否则只读查询会把历史会话的排序时间
        # 刷新成「现在」，导致旧会话被置顶。
        if is_chat_turn:
            metadata["last_message_at"] = _current_timestamp()

    _enqueue_write(session_id, metadata, preserve_pin_fields=True)
    return effective_project_dir


def get_session_metadata(
    session_id: str,
    cache_bust: bool = False,
    *,
    enable_writeback: bool = True,
) -> dict[str, Any]:
    """获取会话元数据

    Args:
        session_id: 会话 ID
        cache_bust: 强制跳过缓存，直接从磁盘读取（用于跨进程同步场景）
        enable_writeback: 是否允许推断后异步写盘持久化。默认 ``True`` 保持
            原行为;只读校验场景(如 ``discard_turn_changes`` 的绑定校验)应传
            ``False``,避免读路径触发写盘副作用,同时仍享受推断能力(存量会话
            缺 ``project_id`` 时可从 ``project_dir`` 反查补全,避免误拒)。
    """
    metadata = _read_metadata(session_id, cache_bust)
    if isinstance(metadata, dict) and metadata:
        metadata = _apply_metadata_defaults_with_inference(
            session_id,
            metadata,
            session_dir=get_agent_sessions_dir() / session_id,
            enable_writeback=enable_writeback,
        )
        metadata.setdefault("team_name", "")
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


def rebind_session_project(
    *,
    session_id: str,
    project_id: str,
    project_dir: str,
    work_mode: str,
) -> dict[str, Any] | None:
    """强制重绑 session 的 ``project_id`` / ``project_dir`` / ``work_mode``。

    打破 ``sync_session_request_metadata`` / ``update_session_metadata`` 的
    "首次锁定不可改"约束，专供 TUI ``/workspace set`` 切换工作目录时同步迁移
    当前会话的 project 归属使用。其他场景不应调用本函数。

    语义：
      - 三字段全部**覆盖式**写入（非首次锁定）；
      - ``pinned`` / ``pin_order`` 等 Gateway 拥有的字段通过
        ``preserve_pin_fields=True`` 合并保留，不被整份回写覆盖；
      - ``last_message_at`` 不刷新（重绑不是消息，避免腐蚀排序时间）。

    Args:
        session_id: 目标会话 ID。
        project_id: 新绑定的项目 ID（须为真实 ``proj_<hex>``）。
        project_dir: 新绑定的项目目录绝对路径。
        work_mode: 新工作模式（``"code"`` / ``"work"``）。

    Returns:
        更新后的完整 metadata；会话不存在（metadata 缺失）时返回 ``None``。
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    metadata = _read_metadata(session_id, cache_bust=True)
    if not metadata:
        return None
    clean_dir = (project_dir or "").strip()
    metadata["project_id"] = (project_id or "").strip()
    metadata["project_dir"] = clean_dir
    metadata["work_mode"] = normalize_work_mode(work_mode)
    # 同步 channel_metadata.project_dir / cwd：/resume 的 current-dir 过滤
    # 优先读 channel_metadata（见 resolve_tui_session_project_path），只改顶层
    # project_dir 会导致会话仍被归到旧目录。
    if clean_dir:
        ch_meta = metadata.get("channel_metadata")
        if not isinstance(ch_meta, dict):
            ch_meta = {}
        ch_meta["project_dir"] = clean_dir
        ch_meta["cwd"] = clean_dir
        try:
            from jiuwenswarm.common.utils import resolve_git_branch

            ch_meta["git_branch"] = resolve_git_branch(clean_dir)
        except Exception:  # noqa: BLE001
            logger.debug(
                "rebind_session_project: resolve_git_branch failed for %s",
                clean_dir, exc_info=True,
            )
        metadata["channel_metadata"] = ch_meta
    # 自增 rebind 版本号, 使此前已入队但尚未处理的陈旧异步快照失效:
    # worker 处理它们时会发现 gen_at_enqueue < 当前 gen, 从而从磁盘保留
    # rebind 写入的 project 字段, 避免陈旧快照写回覆盖刚完成的重绑。
    # 必须在 sync_write 之前 bump, 否则在 sync 写盘窗口内被 worker 处理的
    # 陈旧快照无法被识别。
    _bump_rebind_gen(session_id)
    _enqueue_write(
        session_id,
        metadata,
        sync_write=True,
        preserve_pin_fields=True,
    )
    return metadata


def remove_session_metadata_cache(session_id: str) -> None:
    """Remove cached session metadata after the session directory is deleted."""
    with _CACHE_LOCK:
        _METADATA_CACHE.pop(session_id, None)
    with _REBIND_GEN_LOCK:
        _SESSION_REBIND_GEN.pop(session_id, None)


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
        if not is_team_mode(mode):
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
) -> tuple[list[dict[str, Any]], int]:
    """
    获取所有会话的元数据。

    Returns:
        (sessions, total): 当前页的会话列表 和 会话总数
    """
    sessions_dir = get_agent_sessions_dir()
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
        metadata = _read_metadata(session_id)

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


def collect_all_sessions_metadata(
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """收集全部会话元数据(不分页、不排序),供项目统计与置顶会话聚合使用。

    跳过 heartbeat 会话;强制读盘(``cache_bust=True``)以跨进程拿最新数据。
    无 ``metadata.json`` 的旧会话以目录时间戳构造最小兜底信息
    (``project_id=""``、``project_dir=""``、``pinned=False``),归入默认项目统计。
    返回的每个 dict 已对新增字段应用默认值兜底。

    user_id: 非空时,扫描该用户家目录 ``/home/<user_id>/.jiuwenswarm/agent/sessions``
    (与 faas 沙箱按 OS 用户写入的目录一致;前提:业务 user_id == OS 用户名)。
    该路径下直接读盘,不走进程级全局 ``get_agent_sessions_dir`` 单例,避免多连接
    并发时全局态串扰。为空(None)时维持原行为(扫 gateway 进程默认目录)。
    """
    if user_id:
        if not _SAFE_USER_ID_RE.match(user_id):
            logger.warning("[session_metadata] invalid user_id rejected: %r", user_id)
            return []
        sessions_dir = Path("/home") / user_id / ".jiuwenswarm" / "agent" / "sessions"
    else:
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
        if user_id:
            # 按用户家目录读时,绕过走全局单例的 _read_metadata,直接读该目录下文件,
            # 避免改全局态(并发安全)。仅 cache_bust 语义:直接读盘。
            meta = _read_metadata_file_in(session_dir)
        else:
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

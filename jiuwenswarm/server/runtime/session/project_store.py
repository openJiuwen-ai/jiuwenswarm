"""项目存储模块 — projects.json 的持久化与 CRUD。

存储位置: ``get_agent_root_dir() / "projects.json"``(与 ``sessions/`` 目录同级)。

并发安全:
  - 文件级锁(跨进程): 使用 ``<file>.lock`` 伴生锁文件,Windows 用 ``msvcrt.locking``,
    Unix 用 ``fcntl.flock``。锁文件不被 ``os.replace`` 覆盖,保证跨进程互斥。
  - 原子写: 先写 ``.tmp`` 再 ``os.replace``(配合 ``fsync``),避免断电留下半文件。
  - 内存缓存(进程内): 读走缓存(快路径),``cache_bust=True`` 强制读盘用于跨进程同步;
    写在文件锁内重读磁盘 → 变更 → 原子写回 → 刷新缓存,保证多进程一致。

project_id 格式: ``proj_`` + 8 位 hex(由 ``secrets.token_hex`` 生成)。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    is_default_project_id,
    normalize_work_mode,
)
from jiuwenswarm.server.runtime.session.work_mode import (
    infer_legacy_project_work_mode,
)

logger = logging.getLogger(__name__)

_VERSION = 1
_PROJECT_ID_PREFIX = "proj_"
_PROJECT_ID_HEX_LEN = 8  # proj_ 后跟 8 位 hex

# 进程内缓存 + 锁
_CACHE: list[dict[str, Any]] | None = None
_CACHE_LOCK = threading.Lock()

# 跨平台文件锁(与 a2x ownership 一致的实现,自包含以避免跨模块耦合)
_LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT_SEC = 10.0

if sys.platform == "win32":
    import msvcrt

    def _acquire(fd: int) -> None:
        # LK_LOCK 自带 ~10s 重试,超时抛 OSError
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _release(fd: int) -> None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl
    import time

    def _acquire(fd: int) -> None:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    # 保留原始 BlockingIOError 的调用栈,便于排查锁竞争来源
                    raise OSError("timeout acquiring projects.json lock") from exc
                time.sleep(0.05)

    def _release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def _file_lock(data_path: Path) -> Iterator[None]:
    """跨进程文件锁。锁文件为 ``<data_path>.lock``,与数据文件分离,
    因此数据文件的原子替换不会破坏锁。
    """
    data_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = data_path.with_suffix(data_path.suffix + _LOCK_SUFFIX)
    with open(lock_path, "a+b") as f:
        # Windows 需要至少 1 字节才能锁定
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b"\x00")
            f.flush()
        f.seek(0)
        _acquire(f.fileno())
        try:
            yield
        finally:
            _release(f.fileno())


@dataclass
class Project:
    """项目实体(对应 projects.json 中单个项目记录)。"""

    project_id: str
    name: str
    project_dir: str
    pinned: bool = False
    pin_order: int = 0
    hidden: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    # 工作模式："code" 或 "work"；旧数据兜底为 "work"
    work_mode: str = DEFAULT_WEB_WORK_MODE
    # Git 状态快照，由 ProjectGitService 写入，ProjectStore 不主动维护其内容
    # 子字段：enabled/repo_root/initialized_by_jiuwenswarm/detected_at/branch/status/error/is_dirty
    git: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        # work_mode：旧数据缺失时通过 infer_legacy_project_work_mode 兜底推断
        work_mode = infer_legacy_project_work_mode(d) if isinstance(d, dict) else DEFAULT_WEB_WORK_MODE
        # git：缺失时初始化为空 dict
        git_raw = d.get("git", {}) if isinstance(d, dict) else {}
        git = dict(git_raw) if isinstance(git_raw, dict) else {}
        return cls(
            project_id=str(d.get("project_id", "")),
            name=str(d.get("name", "")),
            project_dir=str(d.get("project_dir", "")),
            pinned=bool(d.get("pinned", False)),
            pin_order=int(d.get("pin_order", 0)),
            hidden=bool(d.get("hidden", False)),
            created_at=float(d.get("created_at", 0.0)),
            updated_at=float(d.get("updated_at", 0.0)),
            work_mode=work_mode,
            git=git,
        )


@dataclass(frozen=True)
class CronProjectBinding:
    """Resolved project ownership for cron jobs."""

    project_id: str
    work_mode: str
    error: str | None = None
    code: str | None = None
    hidden: bool = False


# ── 内部读写(均在已持有文件锁时调用) ─────────────────────────────────────────


def _projects_file() -> Path:
    return get_agent_root_dir() / "projects.json"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _gen_project_id() -> str:
    # token_hex(n) 返回 2n 位 hex 字符
    return f"{_PROJECT_ID_PREFIX}{secrets.token_hex(_PROJECT_ID_HEX_LEN // 2)}"


def _read_disk_locked(path: Path) -> list[dict[str, Any]]:
    """在文件锁内读取磁盘(调用方须已加锁)。文件缺失/损坏时返回空列表。"""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    projects = raw.get("projects")
    if not isinstance(projects, list):
        return []
    return [p for p in projects if isinstance(p, dict)]


def _fsync_dir(directory: Path) -> None:
    """fsync 父目录,确保 ``os.replace`` 的目录项落盘(断电耐久性)。

    Windows 无法对目录 fsync(``os.open`` 目录语义不同),跳过;
    Unix 下打开目录 fd 并 fsync。
    """
    if sys.platform == "win32":
        return
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_disk_locked(path: Path, projects: list[dict[str, Any]]) -> None:
    """在文件锁内原子写入(调用方须已加锁)。"""
    data = {"version": _VERSION, "projects": projects}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    # fsync 父目录,确保 replace 的目录项持久化(断电后不丢文件)
    _fsync_dir(path.parent)


_T = TypeVar("_T")


def _mutate(fn: Callable[[list[dict[str, Any]]], _T]) -> _T:
    """在文件锁保护下: 重读磁盘 → 应用变更 → 原子写回 → 刷新缓存。

    重读磁盘确保拿到其他进程的最新写入,避免基于陈旧缓存做变更而丢失更新。
    """
    global _CACHE
    path = _projects_file()
    with _file_lock(path):
        projects = _read_disk_locked(path)
        result = fn(projects)
        _write_disk_locked(path, projects)
        with _CACHE_LOCK:
            _CACHE = [dict(p) for p in projects]
        return result


def _load_cache(cache_bust: bool = False) -> list[dict[str, Any]]:
    """读取缓存;``cache_bust=True`` 强制读盘(跨进程同步场景)。"""
    global _CACHE
    if not cache_bust:
        with _CACHE_LOCK:
            if _CACHE is not None:
                return [dict(p) for p in _CACHE]
    path = _projects_file()
    with _file_lock(path):
        raw = _read_disk_locked(path)
        # 惰性迁移:为缺 work_mode 的老项目推断并写回磁盘。
        # from_dict 已能在读取时兜底,但持久化可避免后续每次读都重复推断,
        # 也保证磁盘 schema 统一(运维/跨进程/直接读文件场景看到的 schema 完整)。
        # git 字段缺省为 {} 由 from_dict 处理,无需写回(纯常量默认,非推断)。
        changed = False
        for p in raw:
            existing_wm = p.get("work_mode")
            if (
                isinstance(existing_wm, str)
                and existing_wm.strip().lower() in {"code", "work"}
            ):
                continue
            # infer_legacy_project_work_mode 总是返回合法值(缺失/非法时回退 "work")
            p["work_mode"] = infer_legacy_project_work_mode(p)
            changed = True
        if changed:
            try:
                _write_disk_locked(path, raw)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "Project 惰性迁移写回 projects.json 失败: %s", exc
                )
    with _CACHE_LOCK:
        _CACHE = [dict(p) for p in raw]
    return [dict(p) for p in raw]


# ── 公共 CRUD 原语 ───────────────────────────────────────────────────────────


def get_project_by_id(
    project_id: str, *, cache_bust: bool = False
) -> Project | None:
    """按 project_id 查找项目(默认项目不入库,不会命中)。"""
    for p in _load_cache(cache_bust):
        if p.get("project_id") == project_id:
            return Project.from_dict(p)
    return None


def get_project_by_dir(
    project_dir: str, *, cache_bust: bool = False
) -> Project | None:
    """按 project_dir 查找项目(不限 hidden 状态,由调用方判断)。

    用于 project.create 的冲突检测与隐藏项目自动恢复。

    .. deprecated::
        本函数按全局路径匹配,不区分 ``work_mode``,在跨模式项目隔离场景下
        会命中其他模式的项目,不应再用于新业务路径的归属判断。
        新代码请用 :func:`get_project_by_dir_and_mode`,或在需要 mode-aware
        查询时显式传入 ``work_mode``。本函数仅保留给 legacy/诊断路径。
    """
    for p in _load_cache(cache_bust):
        if p.get("project_dir") == project_dir:
            return Project.from_dict(p)
    return None


def _normalize_path_for_match(path: str) -> str:
    """规范化路径用于跨平台匹配(容忍尾部分隔符/大小写差异)。

    与 :func:`resolve_session_project_binding` 的路径比较保持一致。
    """
    return os.path.normcase(os.path.normpath(str(path or "")))


def _normalize_work_mode_value(work_mode: str) -> str:
    """规范化 work_mode 参数,非法值兜底为 ``"work"``。

    与 :func:`jiuwenswarm.common.work_mode.normalize_work_mode` 一致,
    仅在 store 内部调用时兜底非法入参;公开入口应使用严格解析。
    """
    return normalize_work_mode(work_mode, default=DEFAULT_WEB_WORK_MODE)


def _wm(raw: dict[str, Any]) -> str:
    """归一化 raw dict 中的 work_mode，旧数据缺失时兜底为 ``"work"``。

    用于所有直接从 ``projects.json`` dict 读取 ``work_mode`` 做比较的场景，
    统一兜底逻辑,避免旧记录因缺 ``work_mode`` 字段而被 mode-aware 查询漏掉。
    """
    return normalize_work_mode(raw.get("work_mode"), default=DEFAULT_WEB_WORK_MODE)


def get_project_by_dir_and_mode(
    project_dir: str,
    work_mode: str,
    *,
    cache_bust: bool = False,
) -> Project | None:
    """按 ``(work_mode, normalized project_dir)`` 查找项目(不限 hidden 状态)。

    用于跨模式隔离场景下的归属判断与冲突检测。同一 ``project_dir`` 在
    ``code`` / ``work`` 两个模式下可分别对应不同 ``project_id``。

    Args:
        project_dir: 项目目录绝对路径。
        work_mode: 工作模式,``"code"`` / ``"work"``(非法值兜底为 ``"work"``)。
        cache_bust: 是否强制读盘。

    Returns:
        命中的 :class:`Project`,未命中返回 ``None``。
    """
    mode = _normalize_work_mode_value(work_mode)
    if not project_dir:
        return None
    norm = _normalize_path_for_match(project_dir)
    for p in _load_cache(cache_bust):
        pdir = p.get("project_dir") or ""
        if not pdir:
            continue
        if (
            _wm(p) == mode
            and _normalize_path_for_match(pdir) == norm
        ):
            return Project.from_dict(p)
    return None


def get_project_by_name_and_mode(
    name: str,
    work_mode: str,
    *,
    cache_bust: bool = False,
) -> Project | None:
    """按 ``(work_mode, name)`` 查找项目(不限 hidden 状态)。

    用于跨模式隔离场景下的名称冲突检测。同一 ``name`` 在 ``code`` / ``work``
    两个模式下可分别对应不同 ``project_id``。

    Args:
        name: 项目展示名。
        work_mode: 工作模式,``"code"`` / ``"work"``(非法值兜底为 ``"work"``)。
        cache_bust: 是否强制读盘。

    Returns:
        命中的 :class:`Project`,未命中返回 ``None``。
    """
    mode = _normalize_work_mode_value(work_mode)
    for p in _load_cache(cache_bust):
        if p.get("name") == name and _wm(p) == mode:
            return Project.from_dict(p)
    return None


def resolve_session_project_binding(
    project_id: str, project_dir: str
) -> tuple[str, str, str | None, str | None]:
    """校验并解析 session.create 的 project_id / project_dir 绑定关系。

    规则:
      1. 两者皆空(或 project_id 为 ``"default"`` 且 path 为空)→ 默认项目,
         兼容旧版行为(无 project_id / path 的存量调用)。
      2. 传 project_id 时:未传 project_dir 则按项目记录自动补齐;传了则校验
         与项目绑定路径一致,不一致报错。
      3. 仅传 project_dir 而无有效 project_id(空或 ``"default"``)→ 拒绝,
         避免 session 有路径却无法归属到任何项目。

    Args:
        project_id: 调用方传入的 project_id(已 strip)。
        project_dir: 调用方传入的 project_dir(已 strip)。

    Returns:
        ``(resolved_project_id, resolved_project_dir, error, code)``:
        成功时 ``error``/``code`` 为 ``None``;失败时前两项无意义,
        ``error`` 为错误描述,``code`` 为错误码(``BAD_REQUEST``/``NOT_FOUND``)。
    """
    # project_dir 非空时必须为绝对路径
    if project_dir and not os.path.isabs(project_dir):
        return "", "", "project_dir must be an absolute path", "BAD_REQUEST"

    # 真实 project_id 判定扩展:空串 / "default" / "default_code" 均视为无项目
    # (旧实现只把 "default" 视为默认项目,引入 default_code 后必须同时识别)
    has_real_project_id = bool(project_id) and not is_default_project_id(project_id)

    # 规则3: 仅传 project_dir 而无有效 project_id → 拒绝
    if project_dir and not has_real_project_id:
        return "", "", "project_dir requires a matching project_id", "BAD_REQUEST"

    # 无有效 project_id → 默认项目(此处 project_dir 必为空,已由规则3保证)
    if not has_real_project_id:
        return project_id or "", "", None, None

    # project_id 非 default:必须对应存在且可见的项目
    proj = get_project_by_id(project_id, cache_bust=True)
    if proj is None or proj.hidden:
        return "", "", "project not found", "NOT_FOUND"

    expected_dir = proj.project_dir or ""
    if not project_dir:
        # 规则2: 仅传 project_id → 自动补齐(可能为空路径项目)
        return project_id, expected_dir, None, None

    # 规则2: 同时传 → 校验一致性(规范化后比较,容忍尾部分隔符/大小写差异)
    if expected_dir:
        same = (
            os.path.normcase(os.path.normpath(project_dir))
            == os.path.normcase(os.path.normpath(expected_dir))
        )
    else:
        same = False
    if not same:
        return "", "", "project_dir does not match the project's bound path", "BAD_REQUEST"
    return project_id, expected_dir, None, None


def list_projects(
    *, include_hidden: bool = False, cache_bust: bool = False
) -> list[Project]:
    """列出项目。``include_hidden=False``(默认)时排除已软删除项目。"""
    result: list[Project] = []
    for p in _load_cache(cache_bust):
        if not include_hidden and p.get("hidden"):
            continue
        result.append(Project.from_dict(p))
    return result


def resolve_cron_project_id(
    project_dir: str, work_mode: str = DEFAULT_WEB_WORK_MODE
) -> str:
    """cron 侧独立实现的 ``(work_mode, project_dir) → project_id`` 解析。

    与 :func:`resolve_session_project_binding` 的路径规范化一致(容忍尾部分隔符 /
    大小写差异),但仅按 ``(work_mode, project_dir)`` 匹配可见项目,不接收
    ``project_id`` 入参。

    规则(设计文档 §6.1 + work_mode 隔离):
      1. ``project_dir`` 为空 → 返回 ``""``(默认项目,由调用方按 work_mode 映射)。
      2. ``project_dir`` 非空且非绝对路径 → 抛 ``ValueError``(调用方转 BAD_REQUEST)。
      3. ``project_dir`` 非空绝对路径 → 规范化后遍历全部项目(含隐藏),
         命中**同 work_mode 的可见项目**返回其 ``project_id``；
         命中隐藏项目 / 无命中返回 ``""``(默认项目兜底)。

    Args:
        project_dir: 项目目录绝对路径。
        work_mode: 工作模式,``"code"`` / ``"work"``(非法值兜底为 ``"work"``)。
            默认 ``"work"`` 保持与旧调用方兼容(旧数据绝大多数为 work 模式)。
    """
    mode = _normalize_work_mode_value(work_mode)
    project_dir = str(project_dir or "").strip()
    if not project_dir:
        return ""
    if not os.path.isabs(project_dir):
        raise ValueError("project_dir must be an absolute path")
    norm = _normalize_path_for_match(project_dir)
    for p in list_projects(include_hidden=True, cache_bust=True):
        pdir = p.project_dir or ""
        if not pdir:
            continue
        if (
            p.work_mode == mode
            and _normalize_path_for_match(pdir) == norm
            and not p.hidden
        ):
            return p.project_id
    return ""


def resolve_cron_project_binding(
    project_id: Any,
    project_dir: Any,
    work_mode: str = DEFAULT_WEB_WORK_MODE,
) -> CronProjectBinding:
    """Resolve cron job project ownership from project_id/project_dir/work_mode.

    Rules are shared by agent-side cron tools and gateway cron controller:
      1. explicit default project ids map directly to their default work_mode;
      2. explicit real project ids must exist and be visible, then inject the
         project's stored work_mode;
      3. without project_id, resolve visible project by (work_mode, project_dir);
      4. empty/unmatched project_dir falls back to the caller's effective
         work_mode and empty project_id.

    ``resolve_cron_project_id`` still owns absolute-path validation and raises
    ``ValueError`` for relative paths, preserving existing caller behavior.
    """
    input_mode = str(work_mode or DEFAULT_WEB_WORK_MODE).strip() or DEFAULT_WEB_WORK_MODE
    match_mode = _normalize_work_mode_value(work_mode)
    raw_project_id = str(project_id or "").strip()
    if raw_project_id in (DEFAULT_PROJECT_ID_WORK, DEFAULT_PROJECT_ID_CODE):
        return CronProjectBinding(
            project_id=raw_project_id,
            work_mode=(
                DEFAULT_TUI_WORK_MODE
                if raw_project_id == DEFAULT_PROJECT_ID_CODE
                else DEFAULT_WEB_WORK_MODE
            ),
        )

    if raw_project_id:
        proj = get_project_by_id(raw_project_id, cache_bust=True)
        if proj is None:
            return CronProjectBinding(
                project_id="",
                work_mode=input_mode,
                error=f"project not found: {raw_project_id!r}",
                code="NOT_FOUND",
            )
        if proj.hidden:
            return CronProjectBinding(
                project_id="",
                work_mode=input_mode,
                error=f"project is hidden: {raw_project_id!r}",
                code="NOT_FOUND",
                hidden=True,
            )
        return CronProjectBinding(
            project_id=raw_project_id,
            work_mode=proj.work_mode or DEFAULT_WEB_WORK_MODE,
        )

    project_dir_val = str(project_dir or "").strip()
    resolved_project_id = resolve_cron_project_id(project_dir_val, match_mode)
    resolved_work_mode = input_mode
    if not is_default_project_id(resolved_project_id):
        proj = get_project_by_id(resolved_project_id, cache_bust=True)
        if proj is not None:
            resolved_work_mode = proj.work_mode or DEFAULT_WEB_WORK_MODE
    elif resolved_project_id == DEFAULT_PROJECT_ID_CODE:
        resolved_work_mode = DEFAULT_TUI_WORK_MODE
    elif resolved_project_id == DEFAULT_PROJECT_ID_WORK:
        resolved_work_mode = DEFAULT_WEB_WORK_MODE
    return CronProjectBinding(
        project_id=resolved_project_id,
        work_mode=resolved_work_mode,
    )


def resolve_cron_project_work_mode(
    project_id: Any,
    work_mode: str = DEFAULT_WEB_WORK_MODE,
) -> CronProjectBinding:
    """Resolve work_mode from a cron job project_id only."""
    return resolve_cron_project_binding(project_id, "", work_mode)


def resolve_cron_job_patch(
    patch: dict[str, Any],
    existing_work_mode: str,
    *,
    resolve_work_mode_fn: Any | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """重解析 cron job patch 中的 work_mode / project_id / project_dir(共享 helper)。

    被 ``CronController.update_job`` 和 ``cron_tools.py update_job`` 共用,
    确保两条链路(Web RPC / AgentTool)的重解析逻辑一致。

    规则(设计文档 §5.3 + §5.4.4):
      1. 拒绝直接 patch work_mode 作为独立字段(剥离后忽略)
      2. work_mode 仅能伴随 project_dir/project_id 重解析生效;单独 patch
         work_mode → ValueError
      3. patch 含 project_dir → 按 (work_mode, project_dir) 重解析 project_id,
         从 patch 删除 project_dir,写入 project_id + work_mode
      4. patch 含 project_id(无 project_dir)→ 从 Project 记录注入 work_mode

    Args:
        patch: 待修改的 patch dict(原地修改并返回)。
        existing_work_mode: 现有 job 的 work_mode(用于 fallback)。
        resolve_work_mode_fn: 校验显式 work_mode 的函数,
            签名 ``(params, channel_id) -> (work_mode, error)``。
            若为 ``None`` 则使用 ``resolve_request_work_mode``。
        channel_id: 传给 ``resolve_work_mode_fn`` 的 channel_id。

    Returns:
        修改后的 patch dict。

    Raises:
        ValueError: work_mode 非法、单独 patch work_mode、或 project 绑定失败。
    """
    from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode

    if resolve_work_mode_fn is None:
        resolve_work_mode_fn = resolve_request_work_mode

    # 剥离直接 patch 的 work_mode(设计文档明确拒绝独立字段)
    explicit_work_mode = patch.pop("work_mode", None)
    if explicit_work_mode is not None and str(explicit_work_mode).strip():
        wm_val, wm_err = resolve_work_mode_fn(
            {"work_mode": explicit_work_mode}, channel_id=channel_id or "web",
        )
        if wm_err is not None:
            raise ValueError(f"invalid work_mode: {explicit_work_mode!r}")
        # work_mode 仅能伴随 project_dir/project_id 重解析生效;单独 patch
        # work_mode 无法确定项目归属,显式拒绝而非静默忽略
        if "project_dir" not in patch and "project_id" not in patch:
            raise ValueError(
                "work_mode cannot be patched alone; "
                "provide project_dir or project_id together"
            )
        effective_work_mode = wm_val
    else:
        effective_work_mode = existing_work_mode or DEFAULT_WEB_WORK_MODE

    if "project_dir" in patch:
        # project_dir 存在时,project_id 必须由解析产生,不允许直接传入
        patch.pop("project_id", None)
        pd_raw = patch.get("project_dir")
        pd_val = (
            str(pd_raw).strip()
            if isinstance(pd_raw, str) and pd_raw.strip()
            else ""
        )
        binding = resolve_cron_project_binding("", pd_val, effective_work_mode)
        if binding.error is not None:
            raise ValueError(binding.error)
        resolved_pid = binding.project_id
        effective_work_mode = binding.work_mode
        patch["project_id"] = resolved_pid
        del patch["project_dir"]
        patch["work_mode"] = effective_work_mode
    elif "project_id" in patch:
        # 允许直接 patch project_id(修改计划 §5.4.4):从 Project 记录注入 work_mode。
        raw_pid = patch.get("project_id")
        pid_val = str(raw_pid).strip() if isinstance(raw_pid, str) else ""
        binding = resolve_cron_project_work_mode(pid_val, effective_work_mode)
        if binding.error is not None:
            raise ValueError(binding.error)
        effective_work_mode = binding.work_mode
        patch["work_mode"] = effective_work_mode

    return patch


def get_project_dir_by_id(project_id: str) -> str:
    """根据 project_id 反查 project_dir(调度器构造执行请求时用)。

    ``project_id`` 为空 → 返回 ``""``(默认项目)；非空 → 从全部项目(含隐藏)查
    ``project_id`` 命中项的 ``project_dir``；无命中返回 ``""``。
    """
    pid = str(project_id or "").strip()
    if not pid:
        return ""
    for p in list_projects(include_hidden=True, cache_bust=True):
        if p.project_id == pid:
            return p.project_dir or ""
    return ""


class ProjectDirConflict(Exception):
    """``project_dir`` 与已有可见项目重复(由 ``create_or_restore_project`` 在锁内抛出)。"""


class ProjectNameConflict(Exception):
    """``name`` 与已有项目(含隐藏)重复(由 ``create_or_restore_project`` /
    ``rename_project`` / ``restore_project`` 在锁内抛出)。
    """


def _gen_unique_project_id(existing_projects: list[dict[str, Any]]) -> str:
    """生成不与现有 ``project_id`` 冲突的 ID(须在文件锁内调用)。

    32 位熵下碰撞概率极低,此处查重+重生成仅为万无一失。
    """
    existing_ids = {p.get("project_id") for p in existing_projects}
    new_id = _gen_project_id()
    while new_id in existing_ids:
        new_id = _gen_project_id()
    return new_id


# 文件系统非法字符(Windows 禁止出现在目录名中;其他平台同样拒绝以保证跨平台一致)
_DIR_ILLEGAL_CHARS = frozenset('<>:"/\\|?*')
# Windows 保留设备名,不能作为目录名
_DIR_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def validate_project_dir_name(name: str) -> str:
    """校验项目名能否作为目录名;含非法字符或为保留名时抛 ``ValueError``。

    不对名称做任何转换(用户选择"拒绝创建"策略),仅校验。
    中文名 / 空格 / Unicode 字母数字均允许(各主流文件系统均支持)。

    Raises:
        ValueError: 名称含文件系统非法字符、为 Windows 保留设备名、
            全为点/空格、或长度超过 200 字符。
    """
    s = str(name or "").strip()
    if not s:
        raise ValueError("project name is required")
    bad = _DIR_ILLEGAL_CHARS.intersection(s)
    if bad:
        raise ValueError(
            f"project name contains illegal characters for directory name: {''.join(sorted(bad))}"
        )
    upper = s.upper().rstrip(".")
    if upper in _DIR_RESERVED_NAMES:
        raise ValueError(f"project name is a reserved device name: {s}")
    if all(c in " ." for c in s):
        raise ValueError("project name must not be all dots or spaces")
    if len(s) > 200:
        raise ValueError("project name is too long for directory name (max 200)")
    return s


def resolve_default_project_dir(
    name: str, work_mode: str = DEFAULT_WEB_WORK_MODE
) -> str:
    """根据项目名 + work_mode 在默认工作区下生成工作目录绝对路径。

    创建项目未指定 ``project_dir`` 时,在
    ``~/.jiuwenswarm/agent/workspace/{code|work}/{name}`` 下按项目名生成工作目录。
    ``code`` 与 ``work`` 模式使用不同子目录,使默认创建路径与跨模式项目隔离
    目标一致,避免同名 code/work 项目默认创建落到同一路径。

    调用方负责 ``mkdir``;本函数仅返回路径并校验名称合法性。

    Args:
        name: 项目展示名(已 strip)。
        work_mode: 工作模式,``"code"`` / ``"work"``(非法值兜底为 ``"work"``)。
            默认 ``"work"`` 保持与旧调用方兼容。

    Returns:
        工作目录绝对路径字符串。

    Raises:
        ValueError: 名称含文件系统非法字符(详见 :func:`validate_project_dir_name`)。
    """
    dir_name = validate_project_dir_name(name)
    mode = _normalize_work_mode_value(work_mode)
    return str(get_agent_root_dir() / "workspace" / mode / dir_name)


def create_project(
    name: str,
    project_dir: str,
    work_mode: str = DEFAULT_WEB_WORK_MODE,
) -> Project:
    """新建项目并持久化(不做 ``project_dir`` 去重,供内部/测试使用)。

    本函数不检测 ``project_dir`` 是否与已有项目重复,调用方需自行保证;
    ``project_id`` 在锁内查重+重生成,避免碰撞。生产路径请用
    :func:`create_or_restore_project`(原子完成查重/恢复/新建,无 TOCTOU 窗口)。

    Args:
        name: 项目展示名(已 strip)。
        project_dir: 项目目录绝对路径。
        work_mode: 工作模式,``"code"`` / ``"work"``(非法值兜底为 ``"work"``)。
            默认 ``"work"`` 保持与旧调用方兼容。
    """
    mode = _normalize_work_mode_value(work_mode)

    def _do(projects: list[dict[str, Any]]) -> Project:
        now = _now()
        proj = Project(
            project_id=_gen_unique_project_id(projects),
            name=name,
            project_dir=project_dir,
            created_at=now,
            updated_at=now,
            work_mode=mode,
        )
        projects.append(proj.to_dict())
        return proj

    return _mutate(_do)


def create_or_restore_project(
    name: str,
    project_dir: str,
    work_mode: str = DEFAULT_WEB_WORK_MODE,
) -> tuple[Project, bool]:
    """原子地新建或恢复项目(在文件锁内完成查重/恢复/新建,关闭 TOCTOU 窗口)。

    冲突检测按 ``work_mode`` 隔离:同一 ``(work_mode, project_dir)`` /
    ``(work_mode, name)`` 在同模式内才视为冲突;不同 ``work_mode`` 的同目录/同名
    项目视为独立项目。

    - ``project_dir`` 为空时:跳过路径匹配/恢复/冲突,直接新建(允许多个空路径
      项目,靠 ``project_id`` + ``name`` + ``work_mode`` 区分,会话按 ``project_id`` 归属);
    - ``project_dir`` 非空且命中**同 work_mode 的已隐藏项目** → 恢复(置 ``hidden=False``,
      更新 ``name``),返回 ``(proj, True)``;
    - ``project_dir`` 非空且命中**同 work_mode 的可见项目** → 抛 :class:`ProjectDirConflict`;
    - ``name`` 与**同 work_mode 的其他项目**(含隐藏项目、非命中的待恢复项)重复 →
      抛 :class:`ProjectNameConflict`;
    - ``name`` 含文件系统非法字符 / 为保留设备名 → 抛 ``ValueError``;
    - 无匹配 → 新建(``project_id`` 锁内查重+重生成),返回 ``(proj, False)``。

    整个操作在单次 ``_mutate`` 内完成,查重与写入同锁,无 check-then-use 窗口。

    Args:
        name: 项目展示名(已 strip)。
        project_dir: 项目目录绝对路径。
        work_mode: 工作模式,``"code"`` / ``"work"``(非法值兜底为 ``"work"``)。
            默认 ``"work"`` 保持与旧调用方兼容(旧数据绝大多数为 work 模式)。
    """
    # 统一校验 name 可作为目录名(所有创建入口的兜底,含非法字符/保留名时抛 ValueError)
    validate_project_dir_name(name)
    mode = _normalize_work_mode_value(work_mode)

    def _do(projects: list[dict[str, Any]]) -> tuple[Project, bool]:
        # 空 project_dir: 不做路径匹配/恢复/冲突,直接走新建分支(允许多个空路径项目)
        path_match = None
        if project_dir:
            norm_pd = _normalize_path_for_match(project_dir)
            for p in projects:
                pdir = p.get("project_dir") or ""
                if not pdir:
                    continue
                # 同 work_mode + 规范化路径匹配
                if (
                    _wm(p) == mode
                    and _normalize_path_for_match(pdir) == norm_pd
                ):
                    path_match = p
                    break

        # 名称唯一性: 与同 work_mode 的其他项目(含隐藏项目、非命中的待恢复项)的
        # name 重复时冲突。不同 work_mode 的同名项目视为独立。
        # 隐藏项目的名称同样保留,防止隐藏期间被新项目复用造成恢复后重名。
        for p in projects:
            if p is path_match:
                continue
            if _wm(p) != mode:
                continue
            if p.get("name") == name:
                raise ProjectNameConflict(name)

        if path_match is not None:
            if path_match.get("hidden"):
                # 命中同 work_mode 的隐藏项目 → 自动恢复
                path_match["hidden"] = False
                path_match["name"] = name
                path_match["updated_at"] = _now()
                return Project.from_dict(path_match), True
            # 命中同 work_mode 的可见项目 → 冲突
            raise ProjectDirConflict(project_dir)
        # 无匹配 → 新建
        now = _now()
        proj = Project(
            project_id=_gen_unique_project_id(projects),
            name=name,
            project_dir=project_dir,
            created_at=now,
            updated_at=now,
            work_mode=mode,
        )
        projects.append(proj.to_dict())
        return proj, False

    return _mutate(_do)


def save_project(project: Project) -> Project:
    """更新已有项目(upsert: 按 project_id 匹配,命中则替换,未命中则追加)。

    刷新 ``updated_at``。调用方通常先 ``get_project_by_id`` 确认存在后再调用。
    """
    def _do(projects: list[dict[str, Any]]) -> Project:
        d = project.to_dict()
        d["updated_at"] = _now()
        for i, p in enumerate(projects):
            if p.get("project_id") == project.project_id:
                projects[i] = d
                return project
        projects.append(d)
        return project

    return _mutate(_do)


def rename_project(project_id: str, name: str) -> Project | None:
    """原子地重命名项目(锁内完成名称冲突检测与写入,关闭 TOCTOU 窗口)。

    冲突检测按 target 的 ``work_mode`` 隔离:仅与**同 work_mode 的其他项目**
    (含隐藏项目、非自身)的 ``name`` 重复时抛 :class:`ProjectNameConflict`。
    不同 ``work_mode`` 的同名项目视为独立,不视为冲突。
    ``name`` 含文件系统非法字符 / 为保留设备名时抛 ``ValueError``。
    隐藏项目的名称同样保留。项目不存在时返回 ``None``(调用方通常已预检存在性)。
    """
    # 统一校验 name 可作为目录名(含非法字符/保留名时抛 ValueError)
    validate_project_dir_name(name)

    def _do(projects: list[dict[str, Any]]) -> Project | None:
        target = None
        for p in projects:
            if p.get("project_id") == project_id:
                target = p
                break
        if target is None:
            return None
        # 名称唯一性: 仅与同 work_mode 的其他项目(含隐藏项目、非自身)的
        # name 重复时冲突。不同 work_mode 的同名项目视为独立。
        target_mode = _wm(target)
        for p in projects:
            if p is target:
                continue
            if _wm(p) != target_mode:
                continue
            if p.get("name") == name:
                raise ProjectNameConflict(name)
        target["name"] = name
        target["updated_at"] = _now()
        return Project.from_dict(target)

    return _mutate(_do)


def restore_project(project_id: str) -> Project | None:
    """原子地恢复已软删除项目(锁内完成名称冲突检测与恢复,关闭 TOCTOU 窗口)。

    冲突检测按 target 的 ``work_mode`` 隔离:仅与**同 work_mode 的其他项目**
    (含隐藏项目、非自身)的 ``name`` 重复时抛 :class:`ProjectNameConflict`。
    不同 ``work_mode`` 的同名项目视为独立,不视为冲突。
    项目不存在或已是可见时返回 ``None``(调用方通常已预检存在性与隐藏状态)。
    """
    def _do(projects: list[dict[str, Any]]) -> Project | None:
        target = None
        for p in projects:
            if p.get("project_id") == project_id:
                target = p
                break
        if target is None:
            return None
        if not target.get("hidden"):
            return None
        # 名称唯一性: 仅与同 work_mode 的其他项目(含隐藏项目、非自身)的
        # name 重复时冲突。不同 work_mode 的同名项目视为独立。
        target_mode = _wm(target)
        target_name = target.get("name")
        for p in projects:
            if p is target:
                continue
            if _wm(p) != target_mode:
                continue
            if p.get("name") == target_name:
                raise ProjectNameConflict(str(target_name or ""))
        target["hidden"] = False
        target["updated_at"] = _now()
        return Project.from_dict(target)

    return _mutate(_do)


def hide_project(project_id: str) -> Project | None:
    """原子地隐藏(软删除)项目(锁内完成 hidden 翻转与置顶取消,关闭 TOCTOU 窗口)。

    项目不存在或已是隐藏时返回 ``None``(调用方通常已预检存在性与可见状态)。
    隐藏时自动取消置顶(``pinned=False``, ``pin_order=0``)。
    """
    def _do(projects: list[dict[str, Any]]) -> Project | None:
        target = None
        for p in projects:
            if p.get("project_id") == project_id:
                target = p
                break
        if target is None:
            return None
        if target.get("hidden"):
            return None
        target["hidden"] = True
        # 隐藏项目自动取消置顶: 隐藏项目不应出现在置顶区
        if target.get("pinned"):
            target["pinned"] = False
            target["pin_order"] = 0
        target["updated_at"] = _now()
        return Project.from_dict(target)

    return _mutate(_do)


def reindex_project_pin_orders() -> None:
    """对所有置顶(pinned=True)项目紧凑重编号为 1..N,消除间隙。

    按 ``pin_order`` 升序稳定排序后重新分配 1..N;非置顶项目置 ``pin_order=0``。
    保证反复置顶/取消后 ``pin_order`` 不会无限增长。
    """
    def _do(projects: list[dict[str, Any]]) -> None:
        pinned = [p for p in projects if p.get("pinned")]
        pinned.sort(key=lambda p: p.get("pin_order", 0))
        for idx, p in enumerate(pinned, start=1):
            p["pin_order"] = idx
            p["updated_at"] = _now()
        for p in projects:
            if not p.get("pinned"):
                p["pin_order"] = 0

    _mutate(_do)


def invalidate_cache() -> None:
    """清空进程内缓存(测试/特殊场景使用;正常流程下写操作会自动刷新缓存)。"""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def find_or_create_code_project_for_dir(project_dir: str) -> Project | None:
    """TUI 前置归属解析:按 ``work_mode="code"`` 查找/创建目录对应的 code 项目。

    TUI 请求只携带 ``project_dir``/cwd,不携带真实 ``project_id``;而
    ``resolve_session_project_binding`` 会拒绝"仅传 project_dir 无真实
    project_id"的请求。因此 TUI 入口必须先固定 ``work_mode="code"`` 解析或
    创建 code 项目,再把真实 ``project_id`` 注入 ``session.create``。

    同名不同目录冲突时(``ProjectNameConflict``),用目录路径哈希后缀重试一次,
    保证 TUI 会话创建不因项目命名冲突而失败。

    Args:
        project_dir: 候选项目目录;必须为非空绝对路径,否则返回 ``None``。

    Returns:
        解析/创建得到的 ``Project``;``project_dir`` 不可用时返回 ``None``。

    Raises:
        创建失败时向上抛出(目录冲突/非法名等),由调用方决定回退策略。
    """
    import hashlib

    pd = str(project_dir or "").strip()
    if not pd or not os.path.isabs(pd):
        return None

    proj = get_project_by_dir_and_mode(
        pd, DEFAULT_TUI_WORK_MODE, cache_bust=True,
    )
    if proj is not None and not proj.hidden:
        return proj

    name = os.path.basename(pd.rstrip("/\\")) or "untitled"
    restored = False
    try:
        proj, restored = create_or_restore_project(
            name=name, project_dir=pd, work_mode=DEFAULT_TUI_WORK_MODE,
        )
    except ProjectNameConflict:
        suffix = hashlib.sha1(pd.encode("utf-8", errors="replace")).hexdigest()[:6]
        proj, restored = create_or_restore_project(
            name=f"{name}-{suffix}", project_dir=pd, work_mode=DEFAULT_TUI_WORK_MODE,
        )
    # 新建(非恢复)的 code 项目需要 auto git init(与 WEB project.create 路径一致)
    # 恢复的隐藏项目也需要重新探测 Git:被隐藏期间 .git 可能被删除或分支被外部
    # 切换,持久化的 git 快照会过期。
    from jiuwenswarm.server.runtime.session.project_git import get_project_git_service
    try:
        if not restored:
            get_project_git_service().ensure_on_project_create(proj)
        else:
            # 恢复项目:做一次轻量 probe 刷新 git 快照,不自动 init
            # (恢复场景下用户可能有意删除 .git,不应自动重建)
            get_project_git_service().probe(proj)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[ProjectStore] git probe/ensure failed for dir=%s restored=%s",
            pd, restored, exc_info=True,
        )
    return proj


def find_or_create_code_project_for_tui_params(params: dict[str, Any]) -> Project | None:
    """Find or create the code project for TUI session params."""
    if not isinstance(params, dict):
        return None
    candidate_dir = str(params.get("project_dir") or params.get("cwd") or "").strip()
    if not candidate_dir:
        return None
    return find_or_create_code_project_for_dir(candidate_dir)

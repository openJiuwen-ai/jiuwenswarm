"""GitDiffWatcherRegistry: diff 实时监控核心逻辑(设计文档 §2.5 / §3.6 / §4.2)。

按 ``project_id`` 聚合底层 diff 计算(多 watcher 共享结果),维护
summary / files / detail 三层 fingerprint,变化时通过
``WebChannel.send_event`` 推送 ``project.git.diff_*`` / ``project.git.error`` 事件。
``mark_dirty`` 唤醒轮询,``cleanup_ws`` / ``cleanup_project`` 释放资源。

性能边界(§2.7):summary 默认 2 秒轮询 + debounce;files/detail 仅在对应 watcher
存在时计算;大型仓库保持低成本。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 轮询间隔(秒):summary 层的默认刷新周期
POLL_INTERVAL_SEC: float = 2.0
# mark_dirty 后的最小 debounce 等待,合并连续 mark_dirty 只重算一次
DEBOUNCE_SEC: float = 0.3
# Git 命令失败时的退避间隔
ERROR_BACKOFF_SEC: float = 5.0

# 结构性错误码:不可重试,watcher 应暂停
_STRUCTURAL_ERROR_CODES: frozenset[str] = frozenset({
    "NOT_GIT_REPOSITORY",
    "PROJECT_DIR_MISSING",
})

# 连续推送失败阈值,达到后自动回收孤儿 watcher
MAX_PUSH_FAILURES: int = 3


@dataclass
class GitDiffWatch:
    """单条 diff 监控订阅。

    同一 ``watch_id`` 下维护 summary / files / detail 三套 fingerprint:
      - summary: 当前工作区 + last_turn 的统计信息指纹
      - files: 文件列表指纹(按 ``files_source`` 区分 current / last_turn)
      - detail: 已订阅文件的 hunk 内容指纹(按 ``detail_source`` + ``detail_files`` 区分)

    切换 ``files_source`` / ``detail_source`` / ``detail_files`` 时仅更新字段 +
    ``mark_dirty``,不取消/重建底层 watcher 任务(设计文档 §5.2.6)。
    """

    watch_id: str
    project_id: str
    session_id: str
    ws: Any
    scope: str = "summary"
    files_source: str | None = None  # "current" | "last_turn"
    detail_source: str | None = None  # "current" | "last_turn"
    detail_files: set[str] = field(default_factory=set)
    include_last_turn: bool = True  # 是否监控 last_turn(设计文档 §4.2.1)
    last_summary_fingerprint: str = ""
    last_files_fingerprint: str = ""
    last_detail_fingerprint: str = ""
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    # 连续推送失败计数:ws 已断开但 cleanup_ws 未覆盖(如断连与 add_watch 竞态
    # 产生的孤儿 watcher)时,达到阈值后自动回收,避免 watcher 泄漏
    push_failures: int = 0


@dataclass(frozen=True, slots=True)
class GitDiffFilesState:
    """Snapshot of the files subscription state for rollback."""

    files_source: str | None
    last_files_fingerprint: str


@dataclass(frozen=True, slots=True)
class GitDiffDetailState:
    """Snapshot of the detail subscription state for rollback."""

    detail_source: str | None
    detail_files: set[str]
    last_detail_fingerprint: str


def _fingerprint(*parts: Any) -> str:
    """对任意可序列化部分计算稳定指纹,用于变化检测。"""
    import json

    def _json_default_serializer(obj: Any) -> Any:
        # set 转 sorted list 以保证稳定序列化;其他不可序列化对象退化为字符串。
        if isinstance(obj, set):
            return sorted(obj)
        return str(obj)

    payload = json.dumps(
        parts, default=_json_default_serializer, sort_keys=True, ensure_ascii=False,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _summary_fingerprint(
    status_dict: dict[str, Any], *, include_last_turn: bool = True,
) -> str:
    """summary 层指纹:覆盖 repo 元信息 + current/last_turn 统计 + dirty 状态。

    不覆盖文件列表或 hunk 内容(设计文档 §3.6 diff_changed 事件)。
    ``include_last_turn=False`` 时跳过 last_turn 统计(设计文档 §4.2.1)。

    纳入 ``current.is_dirty`` 以反映 dirty 状态变化。
    场景:工作区只有 untracked 文件时 ``stats.files_changed=0`` 但
    ``is_dirty=True``;若不纳入 ``is_dirty``,summary fingerprint 不变,
    前端不会收到设计要求的 dirty 状态更新。
    """
    repo = status_dict.get("repo") or {}
    current = status_dict.get("current")
    last_turn = status_dict.get("last_turn")
    cur_stats = (current or {}).get("stats") if current else None
    cur_is_dirty = (current or {}).get("is_dirty") if current else None
    lt_stats = (
        (last_turn or {}).get("stats")
        if (last_turn and include_last_turn)
        else None
    )
    return _fingerprint(
        repo.get("is_git"),
        repo.get("repo_root"),
        repo.get("branch"),
        repo.get("head"),
        repo.get("transient"),
        cur_is_dirty,
        cur_stats,
        lt_stats,
    )


def _files_fingerprint(files_dict: dict[str, Any] | None) -> str:
    """文件列表层指纹:覆盖文件路径、状态、行数统计(不含 hunk)。"""
    if not files_dict:
        return "empty"
    items: list[tuple[str, str, int, int]] = []
    for path, entry in files_dict.items():
        if not isinstance(entry, dict):
            continue
        items.append((
            str(entry.get("file_path", "")),
            str(entry.get("status", "")),
            int(entry.get("lines_added", 0) or 0),
            int(entry.get("lines_removed", 0) or 0),
        ))
    items.sort()
    return _fingerprint(items)


def _detail_fingerprint(files_dict: dict[str, Any] | None, detail_files: set[str]) -> str:
    """详情层指纹:仅覆盖已订阅文件的 hunk 内容。"""
    if not files_dict or not detail_files:
        return "empty"
    items: list[tuple[str, str]] = []
    for path in sorted(detail_files):
        entry = files_dict.get(path)
        if not isinstance(entry, dict):
            items.append((path, "missing"))
            continue
        hunks = entry.get("hunks") or []
        hunk_sigs: list[str] = []
        for h in hunks:
            if isinstance(h, dict):
                hunk_sigs.append(
                    f"{h.get('old_start',0)}:{h.get('old_lines',0)}:"
                    f"{h.get('new_start',0)}:{h.get('new_lines',0)}:"
                    f"{''.join(h.get('lines', []) or [])}"
                )
        items.append((path, "|".join(hunk_sigs)))
    return _fingerprint(items)


def _build_revision(prefix: str, fingerprint: str) -> str:
    """构造 revision 字符串,用于事件 payload 的版本标记。"""
    ts = int(time.time())
    short = fingerprint[:8] if fingerprint else "00000000"
    return f"{prefix}:{ts}:{short}"


class GitDiffWatcherRegistry:
    """diff 实时监控注册中心(设计文档 §2.5 / §4.2)。

    按 ``project_id`` 聚合底层计算:每个 project 维护一个后台轮询任务,
    所有 watcher 共享同一次 diff 结果。``mark_dirty`` 唤醒轮询立即重算。

    事件推送通过 ``channel.send_event(ws, event, payload)`` 完成,
    ``seq``/``stream_id`` 传 ``None``(设计文档 §5.3.11)。
    """

    def __init__(self, channel: Any = None) -> None:
        self._channel = channel
        self._watches: dict[str, GitDiffWatch] = {}
        self._ws_watches: dict[int, set[str]] = {}
        self._project_watches: dict[str, set[str]] = {}
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def set_channel(self, channel: Any) -> None:
        """注入 WebChannel 实例(用于 send_event)。"""
        self._channel = channel

    async def add_watch(
        self,
        ws: Any,
        project_id: str,
        session_id: str,
        scope: str = "summary",
        *,
        include_last_turn: bool = True,
        on_initial: Any = None,
    ) -> GitDiffWatch:
        """新增 diff 监控订阅(设计文档 §4.2.1)。

        创建新 watcher;若提供 ``on_initial`` 回调,则在 watcher 注册后调用,
        回调内完成首次快照计算 + 响应发送;回调成功后由 Registry 内部
        ``commit_initial_summary`` 完成 seed fingerprint + mark_dirty。

        生命周期原子性:回调抛错时自动 ``remove_watch``,避免 watcher 泄漏。
        这取代了此前的 "add_watch + 兜底 mark_dirty 定时器" 方案——
        watcher 创建与首次快照播种是同一个原子事务,中间无异常退出窗口。

        Args:
            on_initial: ``async (watch) -> dict`` 回调,返回首次快照 status_dict;
                抛错则触发自动 ``remove_watch``。返回值用于 seed fingerprint。
                若为 ``None``,保持旧行为(调用方自行 ``commit_initial_summary``)。
        """
        # session_id 为空串时强制关闭 include_last_turn。
        # 原因:``_compute_and_push`` 用 ``if session_id:`` 判定是否计算 last_turn,
        # 空串会被判定为 False 而静默跳过 last_turn 计算。若 ``include_last_turn``
        # 仍为 True,summary fingerprint 会包含 ``last_turn.stats=None``,
        # 且事件 payload 的 ``last_turn`` 字段语义不一致(前端误以为有 last_turn)。
        # 此处显式关闭,让 watcher 语义自洽:无 session 即不监控 last_turn。
        effective_include_last_turn = bool(include_last_turn) and bool(session_id)
        watch_id = f"gitdiff_{project_id}_{session_id or 'noss'}_{uuid.uuid4().hex[:12]}"
        watch = GitDiffWatch(
            watch_id=watch_id,
            project_id=project_id,
            session_id=session_id,
            ws=ws,
            scope=scope,
            include_last_turn=effective_include_last_turn,
        )
        async with self._lock:
            self._watches[watch_id] = watch
            ws_id = id(ws)
            self._ws_watches.setdefault(ws_id, set()).add(watch_id)
            self._project_watches.setdefault(project_id, set()).add(watch_id)

        if on_initial is None:
            # 旧行为:调用方自行 commit。保留用于兼容已有测试与外部调用方。
            return watch

        try:
            status_dict = await on_initial(watch)
        except Exception:
            # 回调失败:原子移除 watcher,避免泄漏。
            # 不吞异常,向上抛让 handler 发送错误响应。
            await self.remove_watch(watch_id, scope="all", expected_ws=ws)
            raise
        # 成功:seed fingerprint + mark_dirty(Registry 内部完成)
        self.commit_initial_summary(watch_id, status_dict)
        return watch

    async def remove_watch(
        self,
        watch_id: str,
        *,
        scope: str = "all",
        expected_ws: Any = None,
    ) -> GitDiffWatch | None:
        """取消监控(设计文档 §4.2.4)。

        ``scope="all"`` 移除整个 watcher;``scope="files"`` 仅取消文件列表;
        ``scope="detail"`` 仅取消文件内容。后两者保留 summary 订阅。
        ``expected_ws`` 非空时校验 watch 归属,不匹配按不存在处理。
        """
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return None
            if expected_ws is not None and watch.ws is not expected_ws:
                return None
            if scope == "all":
                self._remove_watch_internal(watch_id)
            elif scope == "files":
                watch.files_source = None
                watch.last_files_fingerprint = ""
            elif scope == "detail":
                watch.detail_source = None
                watch.detail_files.clear()
                watch.last_detail_fingerprint = ""
            return watch

    async def update_files(
        self,
        watch_id: str,
        source: str,
        *,
        expected_ws: Any = None,
        expected_project_id: str | None = None,
    ) -> GitDiffWatch | None:
        """开启或切换文件列表监控(设计文档 §4.2.2)。

        更新 ``files_source``,清空 ``last_files_fingerprint``。
        handler 在计算首次快照后调 ``seed_files_fingerprint`` + ``mark_dirty``
        (与 ``update_detail`` 一致),避免轮询首轮 "" → 非空 触发冗余
        ``diff_files_changed``。
        ``expected_ws`` 非空时校验 watch 归属,不匹配按不存在处理。
        ``expected_project_id`` 非空时校验 watch 项目归属,避免同一连接内
        交叉项目订阅导致首次快照与后续事件错配。
        """
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return None
            if expected_ws is not None and watch.ws is not expected_ws:
                return None
            if (
                expected_project_id is not None
                and watch.project_id != expected_project_id
            ):
                return None
            watch.files_source = source
            watch.last_files_fingerprint = ""
        # 不在此处 mark_dirty:handler 会在 seed_files_fingerprint + send_response
        # 之后调 mark_dirty,避免轮询首轮 "" → 非空 必然触发冗余 diff_files_changed
        # (与 add_watch 的 seed_summary_fingerprint 模式一致)
        return watch

    async def snapshot_files_state(
        self,
        watch_id: str,
        *,
        expected_ws: Any = None,
        expected_project_id: str | None = None,
    ) -> GitDiffFilesState | None:
        """Capture files subscription state before a tentative update."""
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return None
            if expected_ws is not None and watch.ws is not expected_ws:
                return None
            if (
                expected_project_id is not None
                and watch.project_id != expected_project_id
            ):
                return None
            return GitDiffFilesState(
                files_source=watch.files_source,
                last_files_fingerprint=watch.last_files_fingerprint,
            )

    async def restore_files_state(
        self,
        watch_id: str,
        state: GitDiffFilesState,
        *,
        expected_ws: Any = None,
    ) -> None:
        """Restore files subscription state after a failed first snapshot.

        保留为公开接口以兼容已有调用方与测试;新代码应优先使用
        ``update_files_with_restore``,内部自动管理 snapshot/restore。
        """
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return
            if expected_ws is not None and watch.ws is not expected_ws:
                return
            watch.files_source = state.files_source
            watch.last_files_fingerprint = state.last_files_fingerprint

    async def update_files_with_restore(
        self,
        watch_id: str,
        source: str,
        *,
        expected_ws: Any = None,
        expected_project_id: str | None = None,
        on_snapshot: Any = None,
    ) -> GitDiffWatch | None:
        """原子地更新 files 订阅,并提供失败时自动回滚的钩子。

        背景:此前 handler 在调 ``update_files`` 失败(首次快照计算抛错)时
        需要自己调 ``snapshot_files_state`` + ``restore_files_state``,
        Registry 内部一致性维护被泄漏到 handler。

        本方法封装该流程:
          1. 调用前 snapshot 当前状态
          2. 调用 ``update_files`` 切换 source
          3. 调用 ``on_snapshot`` 回调(handler 在其中计算首次快照并发送响应)
          4. 回调抛错时自动 ``restore_files_state``
          5. 成功时返回更新后的 watch

        Args:
            on_snapshot: ``async (watch) -> None`` 回调,handler 在其中
                完成首次快照计算与响应发送;抛错则触发回滚。
        """
        previous_state = await self.snapshot_files_state(
            watch_id,
            expected_ws=expected_ws,
            expected_project_id=expected_project_id,
        )
        watch = await self.update_files(
            watch_id, source,
            expected_ws=expected_ws,
            expected_project_id=expected_project_id,
        )
        if watch is None:
            return None
        if on_snapshot is None:
            return watch
        try:
            await on_snapshot(watch)
        except Exception:
            if previous_state is not None:
                await self.restore_files_state(
                    watch_id, previous_state, expected_ws=expected_ws,
                )
            raise
        return watch

    async def update_detail(
        self,
        watch_id: str,
        source: str,
        files: list[str],
        *,
        expected_ws: Any = None,
        expected_project_id: str | None = None,
    ) -> GitDiffWatch | None:
        """切换文件内容监控对象(设计文档 §4.2.3)。

        更新 ``detail_source`` / ``detail_files``,清空 ``last_detail_fingerprint``。
        handler 在计算首次快照后调 ``seed_detail_fingerprint`` + ``mark_dirty``。
        ``expected_ws`` 非空时校验 watch 归属,不匹配按不存在处理。
        ``expected_project_id`` 非空时校验 watch 项目归属,避免同一连接内
        交叉项目订阅导致首次快照与后续事件错配。
        """
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return None
            if expected_ws is not None and watch.ws is not expected_ws:
                return None
            if (
                expected_project_id is not None
                and watch.project_id != expected_project_id
            ):
                return None
            watch.detail_source = source
            watch.detail_files = set(files)
            watch.last_detail_fingerprint = ""
        # 不在此处 mark_dirty:handler 会在 seed_detail_fingerprint + send_response
        # 之后调 mark_dirty,避免轮询首轮 "" → 非空 必然触发冗余 diff_detail_changed
        return watch

    async def snapshot_detail_state(
        self,
        watch_id: str,
        *,
        expected_ws: Any = None,
        expected_project_id: str | None = None,
    ) -> GitDiffDetailState | None:
        """Capture detail subscription state before a tentative update."""
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return None
            if expected_ws is not None and watch.ws is not expected_ws:
                return None
            if (
                expected_project_id is not None
                and watch.project_id != expected_project_id
            ):
                return None
            return GitDiffDetailState(
                detail_source=watch.detail_source,
                detail_files=set(watch.detail_files),
                last_detail_fingerprint=watch.last_detail_fingerprint,
            )

    async def restore_detail_state(
        self,
        watch_id: str,
        state: GitDiffDetailState,
        *,
        expected_ws: Any = None,
    ) -> None:
        """Restore detail subscription state after a failed first snapshot.

        保留为公开接口以兼容已有调用方与测试;新代码应优先使用
        ``update_detail_with_restore``,内部自动管理 snapshot/restore。
        """
        async with self._lock:
            watch = self._watches.get(watch_id)
            if watch is None:
                return
            if expected_ws is not None and watch.ws is not expected_ws:
                return
            watch.detail_source = state.detail_source
            watch.detail_files = set(state.detail_files)
            watch.last_detail_fingerprint = state.last_detail_fingerprint

    async def update_detail_with_restore(
        self,
        watch_id: str,
        source: str,
        files: list[str],
        *,
        expected_ws: Any = None,
        expected_project_id: str | None = None,
        on_snapshot: Any = None,
    ) -> GitDiffWatch | None:
        """原子地更新 detail 订阅,并提供失败时自动回滚的钩子。

        与 ``update_files_with_restore`` 对称:
          1. snapshot 当前 detail 状态
          2. ``update_detail`` 切换 source/files
          3. ``on_snapshot`` 回调计算首次快照并发送响应
          4. 回调抛错时自动 ``restore_detail_state``
        """
        previous_state = await self.snapshot_detail_state(
            watch_id,
            expected_ws=expected_ws,
            expected_project_id=expected_project_id,
        )
        watch = await self.update_detail(
            watch_id, source, files,
            expected_ws=expected_ws,
            expected_project_id=expected_project_id,
        )
        if watch is None:
            return None
        if on_snapshot is None:
            return watch
        try:
            await on_snapshot(watch)
        except Exception:
            if previous_state is not None:
                await self.restore_detail_state(
                    watch_id, previous_state, expected_ws=expected_ws,
                )
            raise
        return watch

    def mark_dirty(self, project_id: str, *, watch_id: str | None = None) -> None:
        """标记脏数据,唤醒轮询任务立即重算(设计文档 §2.5)。

        只唤醒仍存在 watcher 的 project;无 watcher 的 project 不启动轮询。
        结构性错误暂停(poll task 已退出)后,本方法会重建轮询任务,
        使 ``project.git.init`` 等写操作能恢复监控。
        """
        if not self._project_watches.get(project_id):
            return
        self._ensure_poll_task(project_id)
        self._wake_project(project_id)

    def seed_summary_fingerprint(
        self, watch_id: str, status_dict: dict[str, Any],
    ) -> None:
        """用首次快照种子 summary fingerprint(内部接口,见 ``commit_initial_summary``)。

        ``diff_watch`` 首次响应由 handler 直接计算并发送;若不回写 fingerprint,
        poll 首轮(fingerprint 从空串变为非空)必然推送一条与首次响应内容相同的
        冗余 ``diff_changed`` 事件。
        """
        watch = self._watches.get(watch_id)
        if watch is None:
            return
        watch.last_summary_fingerprint = _summary_fingerprint(
            status_dict, include_last_turn=watch.include_last_turn,
        )

    def seed_files_fingerprint(
        self, watch_id: str, status_dict: dict[str, Any], source: str,
    ) -> None:
        """用首次快照种子 files fingerprint(内部接口,见 ``commit_initial_files``)。"""
        watch = self._watches.get(watch_id)
        if watch is None:
            return
        files_dict = self._extract_files(status_dict, source)
        watch.last_files_fingerprint = _files_fingerprint(files_dict)

    def seed_detail_fingerprint(
        self, watch_id: str, status_dict: dict[str, Any],
        source: str, detail_files: list[str],
    ) -> None:
        """用首次快照种子 detail fingerprint(内部接口,见 ``commit_initial_detail``)。"""
        watch = self._watches.get(watch_id)
        if watch is None:
            return
        files_dict = self._extract_files(status_dict, source)
        watch.last_detail_fingerprint = _detail_fingerprint(files_dict, set(detail_files))

    # ── 原子提交接口:封装 "seed fingerprint + mark_dirty" 两步操作 ──
    # 背景:此前 handler 必须按序调用 seed_*_fingerprint + mark_dirty 才能避免
    # poll 首轮冗余推送,seed 接口泄漏了 Registry 内部去重机制。
    # 新接口让 handler 只提交 "首次快照",由 Registry 内部决定如何回写 fingerprint
    # 与唤醒轮询,fingerprint 算法变更不再影响 handler。

    def commit_initial_summary(
        self,
        watch_id: str,
        status_dict: dict[str, Any],
    ) -> None:
        """提交 summary 首次快照并唤醒轮询。

        handler 在发送首次响应后调用本方法;Registry 内部完成:
          1. seed summary fingerprint(避免 poll 首轮冗余 ``diff_changed``)
          2. mark_dirty 唤醒轮询任务

        若 watch 已不存在(被 remove_watch 清理),no-op 返回。
        """
        if watch_id not in self._watches:
            return
        self.seed_summary_fingerprint(watch_id, status_dict)
        watch = self._watches.get(watch_id)
        if watch is None:
            return
        self.mark_dirty(watch.project_id, watch_id=watch_id)

    def commit_initial_files(
        self,
        watch_id: str,
        status_dict: dict[str, Any],
        source: str,
    ) -> None:
        """提交 files 首次快照并唤醒轮询。

        handler 在 ``diff_files_watch`` 发送首次响应后调用;Registry 内部完成
        seed files fingerprint + mark_dirty。
        """
        if watch_id not in self._watches:
            return
        self.seed_files_fingerprint(watch_id, status_dict, source)
        watch = self._watches.get(watch_id)
        if watch is None:
            return
        self.mark_dirty(watch.project_id, watch_id=watch_id)

    def commit_initial_detail(
        self,
        watch_id: str,
        status_dict: dict[str, Any],
        source: str,
        detail_files: list[str],
    ) -> None:
        """提交 detail 首次快照并唤醒轮询。

        handler 在 ``diff_detail_watch`` 发送首次响应后调用;Registry 内部完成
        seed detail fingerprint + mark_dirty。
        """
        if watch_id not in self._watches:
            return
        self.seed_detail_fingerprint(watch_id, status_dict, source, detail_files)
        watch = self._watches.get(watch_id)
        if watch is None:
            return
        self.mark_dirty(watch.project_id, watch_id=watch_id)

    def cleanup_ws(self, ws: Any) -> None:
        """清理该连接下所有 watcher(设计文档 §4.2.0 / §5.2.4)。

        断连时调用,避免 watcher 仍继续轮询推送。
        同步方法:在 event loop 内序列化执行,dict 遍历使用 ``list()`` 快照,
        写入操作与持锁的 ``add_watch``/``remove_watch`` 可能交错但不会
        导致迭代异常(dict size 变更已被 ``list()`` 快照规避)。
        """
        ws_id = id(ws)
        watch_ids: set[str] = set()
        for wid in list(self._ws_watches.get(ws_id, set())):
            watch_ids.add(wid)
        self._ws_watches.pop(ws_id, None)
        for watch_id in watch_ids:
            watch = self._watches.pop(watch_id, None)
            if watch is not None:
                pid = watch.project_id
                pw = self._project_watches.get(pid)
                if pw is not None:
                    pw.discard(watch_id)
                    if not pw:
                        self._project_watches.pop(pid, None)
                        self._cancel_poll_task(pid)

    def cleanup_project(self, project_id: str) -> None:
        """清理该项目下所有 watcher(设计文档 §4.1.4 / §4.2.0)。

        隐藏/删除项目时调用。同步方法,与 ``cleanup_ws`` 一样依赖 ``list()``
        快照规避 dict 迭代期间 size 变更,与持锁的 add/remove/update 可能交错
        但不破坏结构。
        """
        watch_ids = list(self._project_watches.get(project_id, set()))
        for watch_id in watch_ids:
            watch = self._watches.pop(watch_id, None)
            if watch is not None:
                ws_id = id(watch.ws)
                ws_set = self._ws_watches.get(ws_id)
                if ws_set is not None:
                    ws_set.discard(watch_id)
                    if not ws_set:
                        self._ws_watches.pop(ws_id, None)
        self._project_watches.pop(project_id, None)
        self._cancel_poll_task(project_id)

    def _remove_watch_internal(self, watch_id: str) -> GitDiffWatch | None:
        """从所有索引中移除 watcher(调用方需持锁)。"""
        watch = self._watches.pop(watch_id, None)
        if watch is None:
            return None
        ws_id = id(watch.ws)
        ws_set = self._ws_watches.get(ws_id)
        if ws_set is not None:
            ws_set.discard(watch_id)
            if not ws_set:
                self._ws_watches.pop(ws_id, None)
        pid = watch.project_id
        pw = self._project_watches.get(pid)
        if pw is not None:
            pw.discard(watch_id)
            if not pw:
                self._project_watches.pop(pid, None)
                self._cancel_poll_task(pid)
        return watch

    def _ensure_poll_task(self, project_id: str) -> None:
        """确保该 project 的轮询任务已启动。"""
        if project_id in self._poll_tasks:
            existing = self._poll_tasks[project_id]
            if not existing.done():
                return
            # 已完成的任务清理后重新创建
            self._poll_tasks.pop(project_id, None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环(测试环境),跳过
            return
        task = loop.create_task(self._poll_loop(project_id))
        self._poll_tasks[project_id] = task

    def _cancel_poll_task(self, project_id: str) -> None:
        """取消并移除该 project 的轮询任务。"""
        task = self._poll_tasks.pop(project_id, None)
        if task is not None and not task.done():
            task.cancel()

    def shutdown(self) -> None:
        """取消所有轮询任务,释放后台资源(供单例重置/进程退出时调用)。"""
        for project_id in list(self._poll_tasks.keys()):
            self._cancel_poll_task(project_id)

    def _wake_project(self, project_id: str) -> None:
        """唤醒该 project 所有 watcher 的 wake_event。"""
        watch_ids = self._project_watches.get(project_id, set())
        if not watch_ids:
            return
        for wid in watch_ids:
            watch = self._watches.get(wid)
            if watch is not None:
                watch.wake_event.set()

    def _get_watches_for_project(self, project_id: str) -> list[GitDiffWatch]:
        """获取该 project 的所有活跃 watcher(快照)。"""
        watch_ids = self._project_watches.get(project_id, set())
        result: list[GitDiffWatch] = []
        for wid in list(watch_ids):
            watch = self._watches.get(wid)
            if watch is not None:
                result.append(watch)
        return result

    async def _poll_loop(self, project_id: str) -> None:
        """每个 project_id 一个轮询任务,共享 diff 计算。

        逻辑:
          1. 收集该 project 的所有 watcher;无 watcher 则退出
          2. 调用 DiffStatusService 计算完整 diff(include_files=True, include_hunks=True)
          3. 对每个 watcher:
             a. summary fingerprint 变化 → 推送 diff_changed
             b. files_source 存在且 files fingerprint 变化 → 推送 diff_files_changed
             c. detail_source 存在且 detail fingerprint 变化 → 推送 diff_detail_changed
          4. 等待 wake_event 或 POLL_INTERVAL_SEC 超时
        """
        logger.debug("[GitDiffWatcher] poll loop started for project=%s", project_id)
        consecutive_errors = 0
        while True:
            try:
                watches = self._get_watches_for_project(project_id)
                if not watches:
                    logger.debug(
                        "[GitDiffWatcher] no watches, exiting poll loop for project=%s",
                        project_id,
                    )
                    return
                wake_events = [w.wake_event for w in watches]
                await self._compute_and_push(project_id, watches)
                consecutive_errors = 0
                # 等待:任一 wake_event set 或超时(取消 pending 避免任务泄漏)
                wake_tasks = [asyncio.ensure_future(e.wait()) for e in wake_events]
                try:
                    done, pending = await asyncio.wait(
                        wake_tasks,
                        timeout=POLL_INTERVAL_SEC,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if pending:
                        # 等待取消完成,避免 "Task was destroyed but it is pending" 警告
                        await asyncio.gather(*pending, return_exceptions=True)
                    # debounce:被 wake_event 唤醒(非超时)时,短暂等待合并连续 mark_dirty。
                    # 连续 switch_branch / 文件保存等场景下,上游可能短时间内多次 mark_dirty,
                    # 直接重算会触发密集 Git 子进程。此处等待 DEBOUNCE_SEC 后 consume 所有
                    # pending wake_event,确保下一轮只计算一次。超时(无唤醒)路径无需 debounce。
                    if done:
                        await asyncio.sleep(DEBOUNCE_SEC)
                except asyncio.CancelledError:
                    for task in wake_tasks:
                        task.cancel()
                    await asyncio.gather(*wake_tasks, return_exceptions=True)
                    raise
                except RuntimeError:
                    for task in wake_tasks:
                        task.cancel()
                    await asyncio.gather(*wake_tasks, return_exceptions=True)
                for w in watches:
                    w.wake_event.clear()
            except asyncio.CancelledError:
                logger.debug(
                    "[GitDiffWatcher] poll loop cancelled for project=%s",
                    project_id,
                )
                return
            except Exception as exc:  # noqa: BLE001
                # 结构性错误(NOT_GIT_REPOSITORY / PROJECT_DIR_MISSING):暂停 watcher,
                # 保留 watches 等待 mark_dirty 唤醒(如 project.git.init 后重试)
                from jiuwenswarm.server.runtime.session.project_git import GitOperationError
                if isinstance(exc, GitOperationError):
                    err_code = getattr(exc.git_error, "code", "")
                    if err_code in _STRUCTURAL_ERROR_CODES:
                        await self._push_error_event(project_id, exc)
                        logger.info(
                            "[GitDiffWatcher] structural error (%s), pausing poll loop "
                            "for project=%s (watches kept, will resume on mark_dirty)",
                            err_code, project_id,
                        )
                        return
                consecutive_errors += 1
                backoff = min(
                    ERROR_BACKOFF_SEC * consecutive_errors,
                    ERROR_BACKOFF_SEC * 3,
                )
                logger.warning(
                    "[GitDiffWatcher] poll loop error (project=%s, count=%d): %s",
                    project_id, consecutive_errors, exc,
                )
                # 推送 error 事件给该 project 的所有 watcher
                await self._push_error_event(project_id, exc)
                await asyncio.sleep(backoff)

    async def _compute_and_push(
        self,
        project_id: str,
        watches: list[GitDiffWatch],
    ) -> None:
        """计算 diff 并对变化的 watcher 推送事件。

        按 session_id 分组 watcher:``current``(工作区 diff)是项目级共享的,
        但 ``last_turn`` 是 session 级的,不能跨 session 共享。
        同一 session 的多个 watcher 共享一次 diff 计算。

        ``current``(工作区 diff)与 session_id 无关,单次调用内只计算一次,
        跨所有 session 复用(设计文档 §2.5:多个 watcher 在同一 project 上共享
        diff 结果,不线性复制昂贵的 Git 命令)。``last_turn`` 是 session 级的,
        每个 session 独立计算。
        """
        # cache_bust=False:轮询周期 2 秒,强制读盘代价高;同进程内 hide/remove
        # 项目会经 store 写路径刷新缓存,且 project.remove 会调 cleanup_project
        from jiuwenswarm.server.runtime.session import project_store
        proj = project_store.get_project_by_id(project_id)
        if proj is None or proj.hidden:
            # 项目已删除/隐藏,清理所有 watcher
            logger.debug(
                "[GitDiffWatcher] project gone, cleaning watches for %s",
                project_id,
            )
            self.cleanup_project(project_id)
            return

        from jiuwenswarm.server.runtime.session.git_diff_status import (
            ProjectGitDiffStatus,
            _convert_turn_diff,
            get_diff_status_service,
            get_session_extra_history_roots,
        )
        from jiuwenswarm.server.utils.diff_service import get_diff_service
        service = get_diff_status_service()
        diff_service = get_diff_service()

        # 项目级 ``current``(工作区 diff)与 session_id 无关,使用所有 watcher 的
        # current 订阅需求并集计算一次,跨 session 复用。summary 只取统计,
        # files 只取元数据,detail 仅为已展开文件计算 hunk。
        current_need_files = any(
            w.files_source == "current"
            or (w.detail_source == "current" and w.detail_files)
            for w in watches
        )
        current_detail_files: set[str] = set()
        for watch in watches:
            if watch.detail_source == "current" and watch.detail_files:
                current_detail_files.update(watch.detail_files)
        current_need_hunks = bool(current_detail_files)

        # 用 asyncio.to_thread 包裹同步 Git 命令,避免阻塞事件循环。
        # 所有错误(结构性 + 临时性)都向上抛出,由 _poll_loop 统一推送
        # error 事件 + 退避/暂停:
        #   - 结构性错误(NOT_GIT_REPOSITORY / PROJECT_DIR_MISSING) → 暂停 watcher
        #   - 临时性错误(GIT_COMMAND_FAILED / GIT_COMMAND_TIMEOUT 等) → 退避重试
        # 同一 project_id 的所有 session 共享同一 Git 仓库,一次失败
        # 必然对所有 session 失败,无需 continue 到下一个 session group
        base_status = await asyncio.to_thread(
            service.get_project_diff_status,
            project=proj,
            session_id=None,
            include_files=current_need_files,
            include_hunks=current_need_hunks,
            hunk_paths=current_detail_files or None,
        )
        revision = _build_revision(
            "gitdiff",
            _fingerprint(project_id, base_status.generated_at),
        )

        repo_root = base_status.repo.repo_root
        project_dir = getattr(proj, "project_dir", "")

        # 按 session_id 分组 watcher:``last_turn`` 是 session 级的,不能跨 session 共享。
        session_groups: dict[str, list[GitDiffWatch]] = {}
        for watch in watches:
            session_groups.setdefault(watch.session_id, []).append(watch)

        for session_id, group_watches in session_groups.items():
            # 该 session 组的 last_turn include_files/include_hunks 需求。
            # current 已在项目级分层计算;last_turn 暂时保持现有 file_ops 路径,
            # 但不把 current detail 需求错误扩大到 last_turn。
            last_turn_need_files = any(
                w.files_source == "last_turn"
                or (w.detail_source == "last_turn" and w.detail_files)
                for w in group_watches
            )
            last_turn_need_hunks = any(
                w.detail_source == "last_turn" and w.detail_files
                for w in group_watches
            )
            serialize_hunks = current_need_hunks or last_turn_need_hunks

            # 计算 session 级 ``last_turn`` diff(不复用跨 session)。
            # 直接调 ``get_turn_diff_summaries`` + ``_convert_turn_diff``,避免再次调
            # ``get_project_diff_status``(会重复计算项目级 ``current``)。
            # 异常处理与 ``get_project_diff_status`` 内部一致:捕获后 last_turn=None。
            group_needs_last_turn = any(
                w.include_last_turn
                or w.files_source == "last_turn"
                or w.detail_source == "last_turn"
                for w in group_watches
            )
            last_turn = None
            if session_id and group_needs_last_turn:
                try:
                    # 与首次快照路径(DiffStatusService.get_project_diff_status)对齐:
                    # 传入 extra_history_roots 以收集 team/member workspace 与
                    # session worktree 下的 file_ops,否则多 member 在 git worktree
                    # 工作时的改动会被漏读,导致实时轮询推送的 last_turn 数据残缺。
                    extra_roots = get_session_extra_history_roots(session_id)
                    turns = await asyncio.to_thread(
                        diff_service.get_turn_diff_summaries,
                        session_id, project_dir,
                        repo_context={
                            "repo_root": repo_root,
                            "branch": base_status.repo.branch,
                            "base_head": base_status.repo.head,
                        },
                        extra_history_roots=extra_roots,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[GitDiffWatcher] get_turn_diff_summaries failed (session=%s): %s",
                        session_id, exc,
                    )
                    turns = []
                if turns:
                    last_turn = _convert_turn_diff(
                        turns[0],
                        repo_root=repo_root,
                        include_files=last_turn_need_files,
                        include_hunks=last_turn_need_hunks,
                    )

            # 合并:复用项目级 ``current`` + session 级 ``last_turn``。
            # ``to_dict`` 会按本组的 include_hunks 序列化 current/last_turn,
            # 与并集计算时多出的 files/hunks 不会影响下游指纹/推送(指纹函数
            # 与事件构造只读取各自所需字段)。
            merged_status = ProjectGitDiffStatus(
                project_id=base_status.project_id,
                session_id=session_id,
                work_mode=base_status.work_mode,
                repo=base_status.repo,
                current=base_status.current,
                last_turn=last_turn,
                generated_at=base_status.generated_at,
            )
            status_dict = merged_status.to_dict(include_hunks=serialize_hunks)

            for watch in group_watches:
                summary_fp = _summary_fingerprint(
                    status_dict, include_last_turn=watch.include_last_turn,
                )
                if summary_fp != watch.last_summary_fingerprint:
                    watch.last_summary_fingerprint = summary_fp
                    await self._push_diff_changed(
                        watch, status_dict, summary_fp, revision,
                    )

                if watch.files_source:
                    files_dict = self._extract_files(status_dict, watch.files_source)
                    files_fp = _files_fingerprint(files_dict)
                    if files_fp != watch.last_files_fingerprint:
                        watch.last_files_fingerprint = files_fp
                        await self._push_files_changed(
                            watch, status_dict, files_dict, files_fp, revision,
                        )

                if watch.detail_source and watch.detail_files:
                    files_dict = self._extract_files(status_dict, watch.detail_source)
                    detail_fp = _detail_fingerprint(files_dict, watch.detail_files)
                    if detail_fp != watch.last_detail_fingerprint:
                        watch.last_detail_fingerprint = detail_fp
                        await self._push_detail_changed(
                            watch, status_dict, files_dict, detail_fp, revision,
                        )

    @staticmethod
    def _extract_files(
        status_dict: dict[str, Any],
        source: str,
    ) -> dict[str, Any] | None:
        """从 status_dict 中提取指定 source 的 files 映射。

        委托给 ``git_diff_status.extract_files_from_status`` 统一实现,
        与 handler 共用同一 schema 访问逻辑。
        """
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            extract_files_from_status,
        )
        return extract_files_from_status(status_dict, source)

    async def _push_diff_changed(
        self,
        watch: GitDiffWatch,
        status_dict: dict[str, Any],
        fingerprint: str,
        revision: str | None = None,
    ) -> None:
        """推送 ``project.git.diff_changed`` 事件(设计文档 §3.6)。

        ``include_last_turn=False`` 时 ``last_turn`` 固定为 ``None``
        (设计文档 §4.2.1)。
        """
        if self._channel is None:
            return
        repo = status_dict.get("repo") or {}
        current = status_dict.get("current")
        last_turn = status_dict.get("last_turn") if watch.include_last_turn else None
        payload = {
            "watch_id": watch.watch_id,
            "project_id": watch.project_id,
            "session_id": watch.session_id,
            "scope": "summary",
            "change_type": "summary",
            "revision": revision or _build_revision("gitdiff", fingerprint),
            "repo": {
                "is_git": repo.get("is_git", False),
                "repo_root": repo.get("repo_root"),
                "branch": repo.get("branch"),
                "head": repo.get("head"),
                "transient": repo.get("transient", False),
            },
            "current": self._summary_entry(current) if current else None,
            "last_turn": self._turn_summary_entry(last_turn) if last_turn else None,
        }
        try:
            await self._channel.send_event(
                watch.ws, "project.git.diff_changed", payload,
            )
            watch.push_failures = 0
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[GitDiffWatcher] push diff_changed failed (watch=%s): %s",
                watch.watch_id, exc,
            )
            await self._on_push_failure(watch)

    @staticmethod
    def _summary_entry(current: dict[str, Any] | None) -> dict[str, Any]:
        """从 current diff 提取 summary 事件所需字段(仅统计,files 固定 ``{}``)。

        委托给 ``git_diff_status.build_summary_entry`` 统一实现。
        调用方已通过 ``if current else None`` 过滤 None,此处 current 必非 None;
        helper 内部也做了 None 兜底以保持健壮性。
        """
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            build_summary_entry,
        )
        return build_summary_entry(current) or {}

    @staticmethod
    def _turn_summary_entry(last_turn: dict[str, Any] | None) -> dict[str, Any] | None:
        """从 last_turn diff 提取 summary 事件所需字段(仅统计,files 固定 ``{}``)。

        委托给 ``git_diff_status.build_turn_summary_entry`` 统一实现。
        调用方已通过 ``if last_turn else None`` 过滤 None,此处 last_turn 必非 None;
        helper 内部也做了 None 兜底以保持健壮性。
        """
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            build_turn_summary_entry,
        )
        return build_turn_summary_entry(last_turn)

    async def _push_files_changed(
        self,
        watch: GitDiffWatch,
        status_dict: dict[str, Any],
        files_dict: dict[str, Any] | None,
        fingerprint: str,
        revision: str | None = None,
    ) -> None:
        """推送 ``project.git.diff_files_changed`` 事件(设计文档 §3.6)。"""
        if self._channel is None:
            return
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            file_map_to_dict_no_hunks,
        )
        files_no_hunks = file_map_to_dict_no_hunks(files_dict)
        payload = {
            "watch_id": watch.watch_id,
            "project_id": watch.project_id,
            "session_id": watch.session_id,
            "source": watch.files_source,
            "change_type": "files",
            "revision": revision or _build_revision("gitdiff", fingerprint),
            "files": files_no_hunks,
        }
        if watch.files_source == "last_turn":
            payload.update(self._last_turn_event_metadata(status_dict))
        try:
            await self._channel.send_event(
                watch.ws, "project.git.diff_files_changed", payload,
            )
            watch.push_failures = 0
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[GitDiffWatcher] push files_changed failed (watch=%s): %s",
                watch.watch_id, exc,
            )
            await self._on_push_failure(watch)

    async def _push_detail_changed(
        self,
        watch: GitDiffWatch,
        status_dict: dict[str, Any],
        files_dict: dict[str, Any] | None,
        fingerprint: str,
        revision: str | None = None,
    ) -> None:
        """推送 ``project.git.diff_detail_changed`` 事件(设计文档 §3.6)。"""
        if self._channel is None:
            return
        detail_files: dict[str, Any] = {}
        if files_dict:
            for path in watch.detail_files:
                entry = files_dict.get(path)
                if isinstance(entry, dict):
                    detail_files[path] = entry
                else:
                    detail_files[path] = None
        payload = {
            "watch_id": watch.watch_id,
            "project_id": watch.project_id,
            "session_id": watch.session_id,
            "source": watch.detail_source,
            "change_type": "detail",
            "revision": revision or _build_revision("gitdiff", fingerprint),
            "files": detail_files,
        }
        if watch.detail_source == "last_turn":
            payload.update(self._last_turn_event_metadata(status_dict))
        try:
            await self._channel.send_event(
                watch.ws, "project.git.diff_detail_changed", payload,
            )
            watch.push_failures = 0
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[GitDiffWatcher] push detail_changed failed (watch=%s): %s",
                watch.watch_id, exc,
            )
            await self._on_push_failure(watch)

    @staticmethod
    def _last_turn_event_metadata(status_dict: dict[str, Any]) -> dict[str, Any]:
        """提取 last_turn 的稳定绑定元数据。"""
        last_turn = status_dict.get("last_turn")
        if not isinstance(last_turn, dict):
            return {}
        return {
            "change_set_id": last_turn.get("change_set_id", ""),
            "turn_index": last_turn.get("turn_index", 0),
            "assistant_message_id": last_turn.get("assistant_message_id", ""),
            "user_message_id": last_turn.get("user_message_id", ""),
            "request_id": last_turn.get("request_id", ""),
            "status": last_turn.get("status", "completed"),
        }

    async def _on_push_failure(self, watch: GitDiffWatch) -> None:
        """推送失败计数;连续失败达到阈值后回收 watcher。

        兜底断连与 ``add_watch`` 竞态产生的孤儿 watcher(ws 已断开但
        ``cleanup_ws`` 未覆盖),避免对已关闭连接无限推送。
        """
        watch.push_failures += 1
        if watch.push_failures < MAX_PUSH_FAILURES:
            return
        logger.info(
            "[GitDiffWatcher] removing watch %s after %d consecutive push failures",
            watch.watch_id, watch.push_failures,
        )
        async with self._lock:
            self._remove_watch_internal(watch.watch_id)

    async def _push_error_event(
        self,
        project_id: str,
        exc: Exception,
    ) -> None:
        """推送 ``project.git.error`` 事件(设计文档 §3.6)。"""
        if self._channel is None:
            return
        watches = self._get_watches_for_project(project_id)
        # 优先从 GitOperationError.git_error 提取结构化错误(GitError dataclass),
        # 兜底 exc 自身属性,最后回落 GIT_COMMAND_FAILED
        git_error = getattr(exc, "git_error", None)

        def _attr(name: str, default: Any) -> Any:
            val = getattr(git_error, name, None) if git_error is not None else None
            if val is None or val == "":
                val = getattr(exc, name, None)
            return val if val not in (None, "") else default

        detail = {
            "code": _attr("code", "GIT_COMMAND_FAILED"),
            "message": str(exc) or "监控过程中发生错误",
            "command": _attr("command", ""),
            "exit_code": _attr("exit_code", None),
            "stdout": _attr("stdout", ""),
            "stderr": _attr("stderr", ""),
            "hint": _attr("hint", "请刷新项目 Git 状态后重试"),
            "retryable": bool(_attr("retryable", True)),
        }
        for watch in watches:
            payload = {
                "watch_id": watch.watch_id,
                "project_id": project_id,
                "detail": detail,
            }
            try:
                await self._channel.send_event(
                    watch.ws, "project.git.error", payload,
                )
                watch.push_failures = 0
            except Exception:  # noqa: BLE001
                await self._on_push_failure(watch)


_registry_instance: GitDiffWatcherRegistry | None = None


def get_git_diff_watcher_registry() -> GitDiffWatcherRegistry:
    """返回全局 ``GitDiffWatcherRegistry`` 单例。"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = GitDiffWatcherRegistry()
    return _registry_instance


def reset_git_diff_watcher_registry() -> None:
    """重置单例(仅供测试)。"""
    global _registry_instance
    if _registry_instance is not None:
        _registry_instance.shutdown()
    _registry_instance = None

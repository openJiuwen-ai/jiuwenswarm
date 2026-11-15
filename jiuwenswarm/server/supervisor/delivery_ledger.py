# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""交付台账与占位卡（P6 静默吞回复兜底）。

supervisor 在派发接受时刻登记台账（deadline 墙钟），任意送达帧即重置
（P7 活性语义：帧流在推进＝worker 未中断；可见白名单帧额外记
``last_visible_output_ts``）；到期未冲刷时按占位帧线缆五要素（构造先例
``server/gateway_push/wire.py`` ``build_server_push_wire``）经网关 socket
直发 ``chat.error`` 占位卡。控制面请求（``__cron__``/``__health_check__``
渠道、``interrupt_`` 前缀）零可见回复属设计使然，登记时刻直接跳过。
台账只存投递身份与游标，不存用户正文。

契约全文见
``.agents/agent-docs/specs/jiuwenswarm/server/supervisor/S_01_delivery-ledger.md``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from e2a.wire_codec import encode_agent_chunk_for_wire
from e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk

logger = logging.getLogger(__name__)

# 占位卡线缆顶层 request_id 后缀（与真实回复天然不同键，网关按该键独立建流）。
PLACEHOLDER_REQUEST_SUFFIX = ":p6"

# 占位状态机：pending → pending_flush → sent（吸收终态）；
# pending/pending_flush → retracted（取消未送达占位，无线缆动作）/ failed。
PLACEHOLDER_PENDING = "pending"
PLACEHOLDER_PENDING_FLUSH = "pending_flush"
PLACEHOLDER_SENT = "sent"
PLACEHOLDER_RETRACTED = "retracted"
PLACEHOLDER_FAILED = "failed"
PLACEHOLDER_TERMINAL_STATES = frozenset({PLACEHOLDER_SENT, PLACEHOLDER_RETRACTED, PLACEHOLDER_FAILED})

# 用户可见白名单：send True 即重置 deadline；流式模式 chat.delta 也计。
VISIBLE_EVENT_WHITELIST = frozenset({"chat.final", "chat.error", "chat.ask_user_question"})
STREAM_VISIBLE_EVENT_WHITELIST = VISIBLE_EVENT_WHITELIST | {"chat.delta"}

# 控制面请求豁免（P7 误报根治）：这些请求的用户可见回复为零帧属设计使然
# （cron 调度/健康探测无用户渠道可回，interrupt 中断控制操作无回复语义），
# 登记台账只会恒发占位卡——登记时刻直接跳过。
CONTROL_CHANNEL_IDS = frozenset({"__cron__", "__health_check__"})
CONTROL_REQUEST_ID_PREFIXES = ("interrupt_", "interrupt-",
                               # cron 派发的中间控制请求（命令回执/内部取数）：
                               # worker 从不回帧，登记台账只会到期误发占位卡
                               # （GMK 实锚 2026-11-11 上午 2 次：cron-command-ack-*/fetch-*
                               #   条目 state=sent seq=0，channel=web 逃过 __cron__ 渠道豁免）。
                               "cron-command-ack-", "fetch-")

# 投递身份键（网关 message_handler._DELIVERY_IDENTITY_METADATA_KEYS 的镜像；
# 单测断言两侧同值防漂移）。台账只允许存这些键的交集，绝不存用户正文。
DELIVERY_IDENTITY_METADATA_KEYS = frozenset({
    "app_id",
    "chat_type",
    "im_chat_type",
    "feishu_chat_id",
    "feishu_open_id",
    "open_id",
    "im_sender_user_id",
    "im_thread_id",
})

# 分级落盘：sequence 游标与 last_visible_output_ts 批量写阈值（50 帧/2s）。
_BATCH_FRAMES = 50
_BATCH_SECONDS = 2.0

# 终态回收：条目进入终态后保留窗（sweep 周期回收内存与磁盘副本，日志留痕）。
_REAP_SECONDS = 3600.0

_PLACEHOLDER_ERROR_TEXT = "长时间未收到回复，本次处理可能已中断"
_PLACEHOLDER_RESEND_HINT = "请重新发送该消息重试；如持续无响应，请稍后再试或联系管理员"


@dataclass
class LedgerEntry:
    scope: list[str] = field(default_factory=list)  # request_scope 五元组（含 session）
    channel_id: str = ""
    user_id: str = ""
    app_id: str = ""
    agent_ref_json: str = ""
    session_id: str = ""
    request_id: str = ""
    dispatch_id: str = ""
    worker_instance: str = ""
    sequence: int = 0
    last_visible_output_ts: float = 0.0
    deadline: float = 0.0
    placeholder_state: str = PLACEHOLDER_PENDING
    dump_ref: str = ""
    registered_at: float = 0.0
    send_failures: int = 0
    terminal_at: float = 0.0
    identity_metadata: dict[str, Any] = field(default_factory=dict)
    dirty_frames: int = 0
    dirty_at: float = 0.0
    dirty_deadline: bool = False


class DeliveryLedger:
    """请求交付台账：墙钟 deadline + 分级落盘 + 占位状态机。"""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        idle_seconds: float = 600.0,
        expire_seconds: float = 24 * 3600.0,
        max_push_failures: int = 3,
        reap_seconds: float = _REAP_SECONDS,
    ) -> None:
        self._directory = Path(directory) if directory is not None else None
        self._idle_seconds = idle_seconds
        self._expire_seconds = expire_seconds
        self._reap_seconds = reap_seconds
        self._max_push_failures = max_push_failures
        self._entries: dict[str, LedgerEntry] = {}

    # ------------------------------------------------------------------ dir
    @property
    def directory(self) -> Path:
        if self._directory is None:
            from jiuwenswarm.common.utils import get_delivery_ledger_dir

            self._directory = get_delivery_ledger_dir()
        return self._directory

    # ------------------------------------------------------------ mutation
    def register(
        self,
        request: AgentRequest,
        *,
        dispatch_id: str,
        worker_instance: str,
        deadline: float,
        dump_ref: str = "",
    ) -> LedgerEntry | None:
        """派发接受时刻登记（同步写）。控制面请求跳过（零可见回复占位卡无意义）。"""
        from jiuwenswarm.common.schema.message import ReqMethod

        if getattr(request, "req_method", None) == ReqMethod.CHAT_CANCEL:
            # 取消/中断请求豁免（AIReview SF-4）：取消零可见回复（chat.interrupt_result
            # 不在可见白名单），process_cli 取消还复用原 rid——登记只会导致取消后
            # 600s 误发占位卡＋游标清零。
            return None
        params = getattr(request, "params", None) or {}
        if params.get("attach_goal") is True or (
            getattr(request, "req_method", None) == ReqMethod.COMMAND_GOAL
            and str(params.get("action", "get")).strip().lower() not in {"set", "resume"}
        ):
            # 页面恢复（attach_goal）与被动 Goal 订阅按设计零用户可见帧——判据与
            # worker 侧 _is_session_work_request 对齐（atomgit id=200347100）：
            # 这类 web 渠道请求登记后必然 600s 到期，向用户误发「处理可能已中断」
            # 占位卡。active Goal（set/resume）有可见回复，照常登记。
            logger.info(
                "[DeliveryLedger] non-session-work request skipped request_id=%s",
                request.request_id,
            )
            return None
        metadata = request.metadata or {}
        if (str(getattr(request, "channel_id", "") or "") in CONTROL_CHANNEL_IDS
                or str(request.request_id or "").startswith(CONTROL_REQUEST_ID_PREFIXES)):
            logger.info(
                "[DeliveryLedger] control-plane request skipped request_id=%s channel_id=%s",
                request.request_id, getattr(request, "channel_id", ""),
            )
            return None
        identity = {
            key: metadata[key]
            for key in DELIVERY_IDENTITY_METADATA_KEYS
            if key in metadata and metadata[key] is not None
        }
        from jiuwenswarm.server.ipc.protocol import request_scope

        scope = list(request_scope(request))
        agent_ref_json = scope[3] if len(scope) > 3 else ""
        entry = LedgerEntry(
            scope=scope,
            channel_id=str(request.channel_id or ""),
            user_id=str(request.user_id or metadata.get("user_id") or ""),
            app_id=str(metadata.get("app_id") or ""),
            agent_ref_json=agent_ref_json,
            session_id=str(request.session_id or ""),
            request_id=str(request.request_id or ""),
            dispatch_id=str(dispatch_id or ""),
            worker_instance=str(worker_instance or ""),
            deadline=float(deadline),
            dump_ref=dump_ref,
            registered_at=time.time(),
            identity_metadata=identity,
        )
        prev_entry = self._entries.get(entry.request_id)
        if prev_entry is not None:
            # 同 rid 重登记：游标取 max——process_cli 取消复用原 rid 覆盖条目时
            # 游标被清零会破坏 after=sequence 重放契约（AIReview SF-4-③）。
            entry.sequence = max(entry.sequence, prev_entry.sequence)
        self._entries[entry.request_id] = entry
        self._flush(entry)
        return entry

    def pop(self, request_id: str) -> None:
        self._entries.pop(request_id, None)
        path = self._entry_path(request_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[DeliveryLedger] remove failed request_id=%s", request_id)

    def on_wire_delivered(
        self, *, request_id: str, payload: dict[str, Any], now: float | None = None
    ) -> None:
        """任意送达帧：重置 deadline（同步，活性语义见模块头注释）；pending/pending_flush 转 retracted。

        sent 为吸收终态：占位卡已送达后真实回复迟到仅更新簿记，不再重置 deadline、
        不改状态（互斥语义=不压制，迟到真实回复照常送达）。
        """
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        now = time.time() if now is None else now
        sequence = _wire_sequence(payload)
        if sequence > entry.sequence:
            entry.sequence = sequence
            entry.dirty_frames += 1
            entry.dirty_at = entry.dirty_at or now
        event = wire_event(payload)
        if event is None or event not in STREAM_VISIBLE_EVENT_WHITELIST:
            # 非白名单帧同样是活性信号（P7）：纯工具执行的长轮次零可见帧属
            # 健康形态，帧流在推进即证明 worker 未中断——deadline 随任意
            # 送达帧重置，占位卡只对「管线整体静默」报警。落盘仍走分级
            # 批量（该链跑在 supervisor 事件循环线程上，禁逐帧 fsync）。
            #
            # 显式取舍（atomgit 199642764）：worker 侧 keepalive 保活帧
            # （agent_ws_server._activity_event）也落入本分支重置 deadline，
            # 因此「可见静默」（last_visible_output_ts 长期无白名单帧）不参与
            # 占位卡判定——本 PR 的报警语义收敛为「管线整体静默（帧流停止）」，
            # 长工具轮零可见输出视为健康形态。last_visible_output_ts 仅作
            # 簿记保留，为后续独立可见静默看门狗留数据面。
            entry.deadline = now + self._idle_seconds
            entry.dirty_deadline = True
            entry.dirty_at = entry.dirty_at or now
            self._flush_batched(entry, now)
            return
        entry.last_visible_output_ts = now
        if entry.placeholder_state in PLACEHOLDER_TERMINAL_STATES:
            # 终态条目（retracted/sent）：迟到帧只更新内存簿记，deadline/游标随
            # 批量落盘（分级阈值），禁止每帧 fsync——该链跑在 supervisor 事件
            # 循环线程上，逐帧 fsync 会阻塞同 loop 的 IPC 收发。
            if entry.placeholder_state == PLACEHOLDER_RETRACTED:
                entry.deadline = now + self._idle_seconds
                entry.dirty_deadline = True
                entry.dirty_at = entry.dirty_at or now
            self._flush_batched(entry, now)
            return
        entry.deadline = now + self._idle_seconds
        entry.dirty_deadline = True
        entry.dirty_at = entry.dirty_at or now
        if entry.placeholder_state in {PLACEHOLDER_PENDING, PLACEHOLDER_PENDING_FLUSH}:
            if event in VISIBLE_EVENT_WHITELIST:
                # 仅终态帧（final/error/question）取消占位卡兜底；chat.delta
                # 只重置 deadline——流式中断（worker 卡死未崩）时条目仍留在
                # pending，sweep 到期照发占位卡，用户有错误兜底。
                entry.placeholder_state = PLACEHOLDER_RETRACTED
                entry.terminal_at = now
                self._flush(entry)
                return
        self._flush_batched(entry, now)

    def transition(self, request_id: str, state: str, *, audit: str = "") -> bool:
        entry = self._entries.get(str(request_id or ""))
        if entry is None or entry.placeholder_state == state:
            return False
        if entry.placeholder_state in PLACEHOLDER_TERMINAL_STATES:
            # 终态吸收：终态不可迁出（sweep 快照竞态下防 SENT 覆盖 RETRACTED）。
            logger.info(
                "[DeliveryLedger] transition rejected from terminal state=%s request_id=%s audit=%s",
                entry.placeholder_state, entry.request_id, audit,
            )
            return False
        entry.placeholder_state = state
        if state in PLACEHOLDER_TERMINAL_STATES:
            entry.terminal_at = time.time()
        self._flush(entry)
        if state == PLACEHOLDER_FAILED:
            logger.warning(
                "[DeliveryLedger] placeholder %s request_id=%s dispatch_id=%s %s",
                state, entry.request_id, entry.dispatch_id, audit,
            )
        return True

    def note_push_failure(self, request_id: str) -> bool:
        """连续发送失败留痕；达到阈值转 failed。返回是否转终态。"""
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return False
        entry.send_failures += 1
        if entry.send_failures >= self._max_push_failures:
            self.transition(request_id, PLACEHOLDER_FAILED, audit="push_failures_exhausted")
            return True
        self._flush(entry)
        return False

    def mark_sequence_only(self, request_id: str, payload: dict[str, Any], now: float | None = None) -> None:
        """非白名单帧传输成功：只推进游标（批量），不重置 deadline。"""
        entry = self._entries.get(str(request_id or ""))
        if entry is None:
            return
        sequence = _wire_sequence(payload)
        if sequence > entry.sequence:
            entry.sequence = sequence
            entry.dirty_frames += 1
            entry.dirty_at = entry.dirty_at or (time.time() if now is None else now)
            self._flush_batched(entry, time.time() if now is None else now)

    # ------------------------------------------------------------- queries
    def entry(self, request_id: str) -> LedgerEntry | None:
        return self._entries.get(str(request_id or ""))

    def entries(self) -> list[LedgerEntry]:
        return list(self._entries.values())

    def due_entries(self, now: float | None = None) -> list[LedgerEntry]:
        now = time.time() if now is None else now
        due: list[LedgerEntry] = []
        for item in self._entries.values():
            if (item.placeholder_state not in PLACEHOLDER_TERMINAL_STATES
                    and item.deadline > 0 and item.deadline <= now):
                due.append(item)
        return due

    def expired_entries(self, now: float | None = None) -> list[LedgerEntry]:
        now = time.time() if now is None else now
        expired: list[LedgerEntry] = []
        for item in self._entries.values():
            if (item.placeholder_state not in PLACEHOLDER_TERMINAL_STATES
                     and item.registered_at > 0 and now - item.registered_at > self._expire_seconds):
                expired.append(item)
        return expired

    def reap_terminal(self, now: float | None = None) -> int:
        """终态条目回收：超过保留窗的 sent/retracted/failed 删除内存与磁盘副本。

        终态时间取 terminal_at（旧盘档缺省回退 registered_at）；回收前日志留痕
        （request_id/终态/终态龄期），保留审计线索于日志面。返回回收条数。
        """
        now = time.time() if now is None else now
        reaped = 0
        for entry in list(self._entries.values()):
            if entry.placeholder_state not in PLACEHOLDER_TERMINAL_STATES:
                continue
            settled_at = entry.terminal_at or entry.registered_at
            if not settled_at or now - settled_at <= self._reap_seconds:
                continue
            self._entries.pop(entry.request_id, None)
            try:
                self._entry_path(entry.request_id).unlink(missing_ok=True)
            except OSError:
                logger.warning("[DeliveryLedger] reap unlink failed request_id=%s",
                               entry.request_id, exc_info=True)
            logger.info(
                "[DeliveryLedger] reaped terminal state=%s age=%.0fs request_id=%s",
                entry.placeholder_state, now - settled_at, entry.request_id,
            )
            reaped += 1
        return reaped

    # ----------------------------------------------------------- persistence
    def load(self) -> int:
        """Front 启动重载：逐条读盘，按墙钟判定（deadline 原样保留，由 sweep 判到期）。"""
        self._entries.clear()
        directory = self.directory
        if not directory.is_dir():
            return 0
        count = 0
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("[DeliveryLedger] unreadable ledger file %s", path.name)
                continue
            if not isinstance(data, dict) or not str(data.get("request_id") or ""):
                continue
            entry = LedgerEntry(**{key: value for key, value in data.items()
                                   if key in LedgerEntry.__dataclass_fields__})
            self._entries[entry.request_id] = entry
            count += 1
        return count

    def _entry_path(self, request_id: str) -> Path:
        return self.directory / f"{_safe_name(request_id)}.json"

    def _flush(self, entry: LedgerEntry) -> None:
        """同步写（注册/deadline/状态转移）。"""
        entry.dirty_frames = 0
        entry.dirty_at = 0.0
        entry.dirty_deadline = False
        data = asdict(entry)
        for key in ("dirty_frames", "dirty_at", "dirty_deadline"):
            data.pop(key, None)
        payload = json.dumps(data, ensure_ascii=False, default=str)
        path = self._entry_path(entry.request_id)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            logger.warning("[DeliveryLedger] flush failed request_id=%s", entry.request_id, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _flush_batched(self, entry: LedgerEntry, now: float) -> None:
        """游标/可见时间戳批量落盘：满 50 帧/2s 阈值，deadline-only 变更同样进窗口。"""
        if entry.dirty_frames == 0 and not entry.dirty_deadline:
            return
        if entry.dirty_frames >= _BATCH_FRAMES or now - (entry.dirty_at or now) >= _BATCH_SECONDS:
            self._flush(entry)


def _safe_name(request_id: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_." else "_" for char in str(request_id))
    return cleaned[:160] or "unknown"


def wire_event(payload: dict[str, Any]) -> str | None:
    body = payload.get("body") or payload.get("payload")
    if isinstance(body, dict):
        nested = body.get("delta") if isinstance(body.get("delta"), dict) else body.get("result")
        if isinstance(nested, dict):
            body = {**body, **nested}
        event = body.get("event_type")
        if isinstance(event, str) and event:
            return event
    return payload.get("response_kind") if isinstance(payload.get("response_kind"), str) else None


def _wire_sequence(payload: dict[str, Any]) -> int:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        runtime_event = metadata.get("runtime_event")
        if isinstance(runtime_event, dict):
            try:
                return int(runtime_event.get("sequence") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def placeholder_request_id(request_id: str) -> str:
    return f"{request_id}{PLACEHOLDER_REQUEST_SUFFIX}"


def build_placeholder_wire(entry: LedgerEntry) -> dict[str, Any]:
    """按占位帧线缆五要素构造 wire（build_server_push_wire chunk 分支先例）。

    1. 顶层 metadata 含 E2A_WIRE_SERVER_PUSH_KEY（网关 push 分支受理面）；
    2. 顶层 request_id=rid+":p6"（线缆顶层字段，网关 Message.id 取该值）；
    3. 顶层 session_id/channel_id；
    4. payload=chat.error（含原始 rid 与重发建议文案）；
    5. metadata 携 request.metadata 投递身份键（DELIVERY_IDENTITY_METADATA_KEYS）与 app_id。
    """
    push_rid = placeholder_request_id(entry.request_id)
    payload = {
        "event_type": "chat.error",
        "error": _PLACEHOLDER_ERROR_TEXT,
        "request_id": entry.request_id,
        "resend_hint": _PLACEHOLDER_RESEND_HINT,
    }
    chunk = AgentResponseChunk(
        request_id=push_rid,
        channel_id=entry.channel_id,
        payload=payload,
        is_complete=True,
        agent_ref=None,
    )
    wire = encode_agent_chunk_for_wire(chunk, response_id=push_rid, sequence=0, is_stream=False)
    metadata = dict(wire.get("metadata") or {})
    for key, value in (entry.identity_metadata or {}).items():
        metadata.setdefault(key, value)
    if entry.app_id:
        metadata.setdefault("app_id", entry.app_id)
    metadata[E2A_WIRE_SERVER_PUSH_KEY] = True
    wire["metadata"] = metadata
    wire["session_id"] = entry.session_id or wire.get("session_id") or ""
    wire["channel_id"] = entry.channel_id
    return wire



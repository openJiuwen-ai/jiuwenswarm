# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small, in-memory bindings for steering an existing output owner.

The runtime owns atomic acceptance/consumption. This adapter only binds the
wire request to that runtime and retains bounded receipts after the stream ends.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.utils import logger

SUPPORTED_MODES = frozenset(
    {"agent", "agent.plan", "agent.fast", "code", "team", "team.plan", "code.team"}
)
TEAM_MODES = frozenset({"team", "team.plan", "code.team"})
_REASONS = {
    "not_active": "RUN_NOT_ACTIVE",
    "waiting_input": "RUN_WAITING_INPUT",
    "unsupported": "STEER_UNSUPPORTED",
    "queue_full": "INPUT_QUEUE_FULL",
    "input_id_conflict": "IDEMPOTENCY_CONFLICT",
    "invalid_input": "CONTENT_UNSUPPORTED",
}


def request_identity(request: AgentRequest) -> tuple[str, ...]:
    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    return (request.channel_id or "default", *TenantAgentPool.extract_ids(request))


def rejected(reason: str, *, input_id: str = "") -> dict[str, Any]:
    return {"input_id": input_id, "status": "not_applied", "reason": reason}


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    if result.get("reason") in _REASONS:
        result["reason"] = _REASONS[result["reason"]]
    return result


_MAX_STEER_IMAGE_FILES = 16
_STEER_CONTENT_LIMIT = 32000


def _extract_steer_image_files(files: Any) -> list[dict[str, Any]]:
    """Normalize the wire steer ``files`` payload into image-file records.

    wire 形态（relay 下发的 list[{type,name,path,uri}]）：``name`` → ``filename``
    映射后复用请求级提取器；非 image 类型 / 空 path / 非 inline mime 会被过滤。
    """
    if not isinstance(files, list) or not files:
        return []
    mapped: list[Any] = []
    for item in files[:_MAX_STEER_IMAGE_FILES]:
        if isinstance(item, dict):
            # G.TYP.07：取值一律走 get（name 的存在性由真值判定保证，行为不变）。
            name = item.get("name")
            if name and not item.get("filename"):
                item = {**item, "filename": str(name)}
        mapped.append(item)
    from jiuwenswarm.agents.harness.common.multimodal import (
        extract_multimodal_image_files,
    )

    return extract_multimodal_image_files({"files": mapped})


def _steer_request_digest(params: dict[str, Any]) -> str:
    """幂等摘要：wire content + 提取后的补充图片 path 集合（排序）。

    只盖 content 不盖 files 时，同 input_id 换图重放会命中首轮回执被静默吞掉
    （新图零投递零落地），客户端得不到 IDEMPOTENCY_CONFLICT 纠错信号。files
    取提取后的 image path 排序集合：非图片条目 / 空 path 等语义等价差异不
    误报冲突，判定口径 =「文字 + 图片集合相同」才是同一笔请求。
    """
    digest = hashlib.sha256()
    digest.update(str(params.get("content") or "").encode("utf-8"))
    for path in sorted(
        str(image.get("path") or "")
        for image in _extract_steer_image_files(params.get("files"))
    ):
        digest.update(b"\x00")
        digest.update(path.encode("utf-8"))
    return digest.hexdigest()


def _live_manifest(agent: Any) -> list[dict[str, Any]]:
    """读 agent 当前的上传清单（request_metadata 通道优先）。

    - team：live harness ``build_context.request_metadata`` 是**权威**通道——
      每轮开轮覆盖写（空列表即清除上一轮残留）。返回 list（含空列表）时
      只信它，**不再并入 agent 镜像**：镜像在 team 模式下仅 steer 写入、
      无跨轮清除路径，并入会把上一轮 steer 的残留复活进本轮清单并随每个
      带图 steer 单调累积（轮内多笔 steer 的累积由 metadata 自身承载）。
    - 单代理：无 build context / metadata 未写入（返回 None）时回退 agent
      实例镜像——deep rail 的实时读取通道，镜像随每次请求覆盖写、无残留。
    """
    from jiuwenswarm.agents.harness.common.multimodal import (
        current_multimodal_image_files_from_agent,
        normalize_image_path,
        team_request_image_files,
    )

    def _dedup(images: Any) -> list[dict[str, Any]]:
        if not isinstance(images, list):
            return []
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for image in images:
            if not isinstance(image, dict):
                continue
            key = normalize_image_path(str(image.get("path") or ""))
            if key and key not in seen:
                seen.add(key)
                merged.append(image)
        return merged

    try:
        harness = getattr(agent, "harness", None)
        build_context = getattr(harness, "build_context", None)
        metadata = getattr(build_context, "request_metadata", None)
        metadata_list = team_request_image_files(metadata)
    except Exception:  # pragma: no cover - 防御性读取
        metadata_list = None
    if metadata_list is not None:
        return _dedup(metadata_list)
    return _dedup(current_multimodal_image_files_from_agent(agent))


def _refresh_live_rail_bound_manifests(
    agent: Any, input_id: str, fresh: list[dict[str, Any]]
) -> None:
    """把补充图片同步进当前活跃轮 rail 的绑定清单（第三通道）。

    绑定清单是 rail 构造期的快照（team 成员/leader 由 swarm provider 从
    build context 复印绑定）；steer 发生在轮进行中，快照不含补充图片，
    inject 的安全门会把引用行拒掉（skip non-uploaded）。经
    ``harness.find_rails`` 找到活跃轮挂载的 ``MultimodalImageRail``，把新图
    追加进快照，leader 本轮下一次模型调用即可物化。成员不经此通道：各自
    下一轮重建 rail 时读合并后的 request_metadata 生效。
    """
    if not fresh:
        return
    try:
        from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
            MultimodalImageRail,
        )

        harness = getattr(agent, "harness", None)
        find_rails = getattr(harness, "find_rails", None)
        if not callable(find_rails):
            return
        for rail in find_rails(MultimodalImageRail) or []:
            append = getattr(rail, "append_round_image_files", None)
            if callable(append):
                append(fresh)
    except Exception:
        # 降级语义不变（不阻断 steer 投递），但必须可观测：引用行已随 content
        # 投递、快照同步失败时本轮 leader 0 像素，无日志则线上无从排查。
        logger.warning(
            "[steer-files] live rail bound-manifest sync failed: input_id=%s "
            "pending=%s",
            input_id,
            [str(image.get("path") or "") for image in fresh],
            exc_info=True,
        )


def _plan_steer_image_files(
    runtime: Any, content: str, image_files: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """计算补充图片的引用行与待落地清单（纯计算，不写任何状态）。

    落地必须等投递确认（见 ``_commit_steer_image_files``）：steer_active 失败
    （异常 / not_applied / unknown）时若清单已写，本轮去重会把同一张图的重试
    吞掉（fresh 恒空 → 引用行不再追加），用户「失败后重试」永远补不上，直到
    下一轮 chat.send 覆盖清单才自愈。content 超出 inbox 的 32k 硬校验预算时
    舍弃引用行、清单仍待落地（fresh 照常返回）。
    """
    from jiuwenswarm.agents.harness.common.multimodal import (
        build_image_reference_line,
        normalize_image_path,
    )

    existing = _live_manifest(runtime)
    seen = {normalize_image_path(str(image.get("path") or "")) for image in existing}
    fresh = [
        image
        for image in image_files
        if normalize_image_path(str(image.get("path") or "")) not in seen
    ]
    if not fresh:
        return content, []
    lines = [
        build_image_reference_line(image, len(existing) + offset)
        for offset, image in enumerate(fresh, start=1)
    ]
    enriched = content.rstrip() + "\n" + "\n".join(lines)
    if len(enriched) > _STEER_CONTENT_LIMIT:
        return content, fresh
    return enriched, fresh


def _commit_steer_image_files(
    runtime: Any, input_id: str, fresh: list[dict[str, Any]]
) -> None:
    """把补充图片落地进三个清单通道（仅在 steer_active 投递确认后调用）。

    三写（与普通消息的非首轮上传同一组通道，见 ``team_helpers``）：
    - live harness ``request_metadata``：后续轮 NativeHarness 重建时 provider 从
      同一 build context 构造 rail，读到合并后的清单（成员同样生效）；
    - agent 实例镜像：未绑定清单的 rail（deep 单代理）实时读取，本轮即可物化；
    - 活跃轮 rail 的绑定清单快照：team leader 的 rail 构造期已绑定快照，仅靠
      前两处它本轮读不到——追加进快照后本轮即可物化（成员仍靠下一轮重建）。
    commit 时按当前清单重新去重（plan 与 commit 之间隔着投递 await），
    重复落地幂等。任一通道写入失败仅告警降级、不阻断投递：引用行已随
    content 发出，落地缺位表现为本轮该图零物化（N12 拒非清单路径），
    必须留日志线索。
    """
    if not fresh:
        return
    from jiuwenswarm.agents.harness.common.multimodal import (
        normalize_image_path,
        store_current_multimodal_image_files_on_agent,
    )
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
        _bind_round_image_files_to_agent,
    )

    existing = _live_manifest(runtime)
    seen = {normalize_image_path(str(image.get("path") or "")) for image in existing}
    pending = [
        image
        for image in fresh
        if normalize_image_path(str(image.get("path") or "")) not in seen
    ]
    if not pending:
        return
    merged = existing + pending
    try:
        store_current_multimodal_image_files_on_agent(runtime, merged)
    except Exception:
        logger.warning(
            "[steer-files] agent mirror write failed: input_id=%s pending=%s",
            input_id,
            [str(image.get("path") or "") for image in pending],
            exc_info=True,
        )
    # _bind_round_image_files_to_agent 从不向调用方抛异常（team_helpers 内部
    # 全路径捕获、仅 DEBUG 级日志），失败以 False 返回；False 同时混同「无
    # harness 的单代理 no-op（正常）」与「team 权威通道写失败」，仅对有
    # harness 的 runtime（team 形态）告警，单专家路径保持静默。
    if not _bind_round_image_files_to_agent(runtime, merged) and (
        getattr(runtime, "harness", None) is not None
    ):
        logger.warning(
            "[steer-files] request_metadata write failed: input_id=%s pending=%s",
            input_id,
            [str(image.get("path") or "") for image in pending],
        )
    _refresh_live_rail_bound_manifests(runtime, input_id, pending)


@dataclass
class _Input:
    digest: str
    client_message_id: str
    runtime: Any
    runtime_request_id: str
    receipt: dict[str, Any] | None = None
    # 投递尚未确认落地（异常路径）的补充图片；回执确认 accepted/consumed 后
    # 由 _compensate_ambiguous_delivery 补落地并清除，见其 docstring。
    pending_files: list[dict[str, Any]] | None = None


@dataclass
class _Binding:
    request_id: str
    invocation_id: str
    identity: tuple[str, ...]
    mode: str
    session_id: str
    active: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inputs: dict[str, _Input] = field(default_factory=dict)
    client_ids: dict[str, str] = field(default_factory=dict)


class SteeringSession:
    """No task creation, output readers, durable replay, or background retries."""

    def __init__(self, resolve_runtime: Callable[[str, str], Awaitable[Any]]) -> None:
        self._resolve_runtime = resolve_runtime
        self._bindings: OrderedDict[str, _Binding] = OrderedDict()
        self._active: _Binding | None = None

    def bind_request(self, request: AgentRequest) -> _Binding:
        """Register the in-memory owner of an existing request stream."""
        params = request.params or {}
        binding = _Binding(
            request_id=request.request_id,
            invocation_id=str(params.get("invocation_id") or ""),
            identity=request_identity(request),
            mode=str(params.get("mode") or "agent"),
            session_id=request.session_id or "default",
        )
        self._bindings[request.request_id] = binding
        self._active = binding
        # Final receipts survive completion, but never retain an unbounded run log.
        for rid, old in list(self._bindings.items()):
            if len(self._bindings) <= 8:
                break
            if not old.active:
                self._bindings.pop(rid)
        return binding

    def owns(self, request: AgentRequest) -> bool:
        binding = self._bindings.get(
            str((request.params or {}).get("active_request_id") or "")
        )
        return (
            binding is not None
            and binding.session_id == request.session_id
            and binding.identity == request_identity(request)
        )

    async def finish_request(self, binding: _Binding) -> None:
        """End acceptance, snapshot receipts and release runtime references."""
        async with binding.lock:
            binding.active = False
            if self._active is binding:
                self._active = None
            for input_id, item in binding.inputs.items():
                item.receipt = await self._receipt(input_id, item)
                # If the owner ended without proof, do not promise consumption.
                if item.receipt.get("status") == "accepted":
                    item.receipt = {"input_id": input_id, "status": "unknown"}
                item.runtime = None

    @staticmethod
    async def _receipt(input_id: str, item: _Input) -> dict[str, Any]:
        if item.receipt is not None:
            return dict(item.receipt)
        try:
            return normalize_result(
                await item.runtime.get_steering_status(
                    active_request_id=item.runtime_request_id,
                    input_id=input_id,
                )
            )
        except Exception:
            return {"input_id": input_id, "status": "unknown"}

    @staticmethod
    def _compensate_ambiguous_delivery(
        input_id: str, item: _Input, receipt: dict[str, Any]
    ) -> None:
        """模糊投递窗口的补偿落地（清单后移落地的第三条腿）。

        steer_active 抛异常时按「未确认不落地」放弃清单写入；但异常 ≠ 未送达
        ——若引用行实际已进模型流（模糊失败），修复前该场景清单已落地、图
        可见，后移落地使其退化为「成功回执 + 该图零物化」且客户端无重发
        信号。本方法在回执路径（重放 / 状态查询，query 与否同分支）确认
        status ∈ {accepted, consumed} 且 runtime 仍在世时补一次
        ``_commit_steer_image_files``：commit 按当前清单重新去重、幂等，
        重复落地无风险；先清 pending 再落地，状态轮询不会重复触发。
        finish_request 快照路径不补：轮已结束（runtime 即将释放），下一轮
        chat.send 覆盖清单，补进死轮无意义。
        """
        pending = item.pending_files
        if not pending or item.runtime is None:
            return
        if receipt.get("status") not in ("accepted", "consumed"):
            return
        item.pending_files = None
        _commit_steer_image_files(item.runtime, input_id, pending)

    async def handle(self, request: AgentRequest, *, query: bool) -> dict[str, Any]:
        params = request.params or {}
        input_id = str(params.get("input_id") or "")
        capability = query and not input_id
        binding = self._bindings.get(str(params.get("active_request_id") or ""))
        if binding is None or not self.owns(request):
            if capability:
                return {"supported": False, "reason": "RUN_NOT_ACTIVE"}
            return (
                {"input_id": input_id, "status": "unknown"}
                if query
                else rejected("RUN_NOT_ACTIVE", input_id=input_id)
            )
        invocation_id = str(params.get("invocation_id") or "")
        if not binding.invocation_id or binding.invocation_id != invocation_id:
            return (
                {"supported": False, "reason": "RUN_NOT_ACTIVE"}
                if capability
                else rejected("RUN_NOT_ACTIVE", input_id=input_id)
            )
        async with binding.lock:
            item = binding.inputs.get(input_id)
            if item is not None:
                if not query:
                    if (
                        _steer_request_digest(params) != item.digest
                        or params["client_message_id"] != item.client_message_id
                    ):
                        return rejected("IDEMPOTENCY_CONFLICT", input_id=input_id)
                receipt = await self._receipt(input_id, item)
                self._compensate_ambiguous_delivery(input_id, item, receipt)
                return receipt
            if query and not capability:
                return {"input_id": input_id, "status": "unknown"}
            if not binding.active or self._active is not binding:
                return (
                    {"supported": False, "reason": "RUN_NOT_ACTIVE"}
                    if capability
                    else rejected("RUN_NOT_ACTIVE", input_id=input_id)
                )
            if binding.mode not in SUPPORTED_MODES:
                return (
                    {"supported": False, "reason": "STEER_UNSUPPORTED"}
                    if capability
                    else rejected("STEER_UNSUPPORTED", input_id=input_id)
                )
            # Hosted subagent approval waits use futures, not the parent Core
            # session's INTERRUPTION_KEY. A supplement cannot answer those waits.
            from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
                SubagentApprovalRegistry,
            )

            approvals = SubagentApprovalRegistry.peek_instance()
            if approvals is not None and any(
                pending.session_id == binding.session_id
                for pending in approvals.pending_requests()
            ):
                return (
                    {"supported": False, "reason": "RUN_WAITING_INPUT"}
                    if capability
                    else rejected("RUN_WAITING_INPUT", input_id=input_id)
                )
            runtime = await self._resolve_runtime(binding.mode, binding.identity[0])
            if runtime is None:
                return (
                    {"supported": False, "reason": "RUN_NOT_ACTIVE"}
                    if capability
                    else rejected("RUN_NOT_ACTIVE", input_id=input_id)
                )
            runtime_id = binding.request_id
            if binding.mode in TEAM_MODES:
                getter = getattr(runtime, "get_active_steering_request_id", None)
                runtime_id = getter() if callable(getter) else None
            supports = getattr(runtime, "get_steering_capability", None)
            if not callable(supports):
                return (
                    {"supported": False, "reason": "STEER_UNSUPPORTED"}
                    if capability
                    else rejected("STEER_UNSUPPORTED", input_id=input_id)
                )
            availability = normalize_result(
                await supports(active_request_id=runtime_id or "")
            )
            if capability:
                return {
                    **availability,
                    "target": "team_leader" if binding.mode in TEAM_MODES else "single",
                }
            if not availability.get("supported"):
                return rejected(
                    availability.get("reason", "STEER_UNSUPPORTED"), input_id=input_id
                )
            if len(binding.inputs) >= 512:
                return rejected("INPUT_QUEUE_FULL", input_id=input_id)
            client_id = params["client_message_id"]
            if client_id in binding.client_ids:
                return rejected("IDEMPOTENCY_CONFLICT", input_id=input_id)
            # 幂等摘要按 wire 原文 content + 补充图片 path 集合计算
            # （_steer_request_digest）；引用行拼接不影响摘要，同 input_id
            # 原样重试不产生二次投递。
            # 引用行先随 content 投递，清单在投递确认（accepted / consumed）后
            # 才落地：steer_active 失败（异常 / not_applied / unknown）时若清单
            # 已写，本轮去重会把同一张图的重试吞掉（fresh 恒空 → 引用行不再
            # 追加），直到下一轮 chat.send 覆盖清单才自愈。异常（模糊失败，
            # 投递可能实际已成功）时 fresh_images 暂存在 item 上，回执路径
            # 确认送达后由 _compensate_ambiguous_delivery 补落地。
            content = params["content"]
            steer_images = _extract_steer_image_files(params.get("files"))
            fresh_images: list[dict[str, Any]] = []
            if steer_images:
                content, fresh_images = _plan_steer_image_files(
                    runtime, content, steer_images
                )
            item = _Input(
                digest=_steer_request_digest(params),
                client_message_id=client_id,
                runtime=runtime,
                runtime_request_id=runtime_id,
                # 投递确认前暂存：异常（模糊失败）路径靠它事后补落地。
                pending_files=fresh_images or None,
            )
            # Register before awaiting: an ambiguous transport failure keeps the
            # same input ID queryable, instead of encouraging a duplicate send.
            binding.inputs[input_id] = item
            binding.client_ids[client_id] = input_id
            try:
                result = normalize_result(
                    await runtime.steer_active(
                        active_request_id=runtime_id,
                        input_id=input_id,
                        content=content,
                    )
                )
            except Exception:
                # 异常 ≠ 未送达（模糊投递窗口）：引用行可能已实际进入模型流。
                # fresh_images 留在 item.pending_files 上，重放 / 状态查询路径
                # 经回执确认 accepted / consumed 后由
                # _compensate_ambiguous_delivery 补落地，闭环 CR-1 后移落地
                # 遗漏的异步确认分支。
                return {"input_id": input_id, "status": "unknown"}
            if result.get("status") == "not_applied":
                item.receipt = result
                return result
            if result.get("status") in ("accepted", "consumed"):
                if fresh_images:
                    _commit_steer_image_files(runtime, input_id, fresh_images)
                # 确认送达即清暂存；unknown（异常或 runtime 返回）保留 pending，
                # 交给回执路径补偿。
                item.pending_files = None
            return result

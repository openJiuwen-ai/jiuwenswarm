# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboFallbackHandler -- 节点级 fallback 的委托接口与 DeepAgent 实现。

设计原则：
- SkillTurboExecutor 不再自建 ReActAgent，而是通过 handler 委托 fallback 执行。
- DeepAdapter 侧提供实现，复用 fork/spawn subagent 的正式执行路径。
- Executor 只关心 handler 的输入/输出契约，不关心内部 agent 如何构建。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.session.agent import Session

logger = logging.getLogger(__name__)

# 节点级 fallback 交付物校验器：契约通过后调用，返回拒绝原因（str）表示
# 未达成节点目标，返回 None 放行。入参为 (inputs, contract_result)。
ResultValidator = Callable[[dict[str, Any], dict[str, Any]], "str | None"]


@dataclass
class FallbackCall:
    """单次节点 fallback 调用参数。"""

    node_name: str
    instruction: str
    inputs: dict[str, Any]
    error: Exception
    parent_session: Session | None = None
    result_validator: ResultValidator | None = None


class FallbackContractError(Exception):
    """fallback subagent 未达成节点契约，需降级到 DeepAgent。"""

    def __init__(
        self,
        *,
        node_name: str,
        reason: str,
        original_error: Exception | None = None,
    ) -> None:
        self.node_name = node_name
        self.reason = reason
        self.original_error = original_error
        super().__init__(
            f"fallback 未达成节点 {node_name} 契约: {reason}"
            + (f" (原错误: {original_error})" if original_error else "")
        )


class SkillTurboFallbackHandler(ABC):
    """节点级 fallback 委托接口。

    SkillTurboExecutor 在节点执行失败时调用此 handler，
    由外部（通常是 DeepAdapter）提供具体实现。
    """

    @abstractmethod
    async def fallback(self, call: FallbackCall) -> dict[str, Any]:
        """非流式 fallback：使用外部 agent 兜底失败节点。"""

    @abstractmethod
    def fallback_stream(self, call: FallbackCall) -> AsyncIterator[dict[str, Any]]:
        """流式 fallback：使用外部 agent 兜底失败节点。"""


class DeepAgentFallbackHandler(SkillTurboFallbackHandler):
    """基于 DeepAgent spawn subagent 的 fallback 实现。

    这里不再自建 ReActAgent。fallback 作为一种 isolated spawn subagent，
    直接复用 DeepAgent fork/spawn 子代理体系：
    - create_deep_agent
    - SubagentContextRail
    - SubagentSkillUseRail
    - ProgressiveToolRail
    - ContextEngineeringRail(minimal)
    - 工具继承与工具事件转发
    """

    def __init__(
        self,
        adapter: Any,
        *,
        request_id: str = "",
        channel_id: str = "",
        session_id: str = "",
    ):
        self._adapter = adapter
        self._request_id = request_id
        self._channel_id = channel_id
        self._session_id = session_id

    @staticmethod
    def _build_fallback_query(
        node_name: str,
        instruction: str,
        inputs: dict[str, Any],
        error: Exception,
    ) -> str:
        """构造 fallback subagent 的任务 prompt。

        要求 subagent 完成节点原任务后，以严格 JSON 收尾，声明是否真正达成节点契约。
        success=false 会触发整个 SkillTurbo 降级到 DeepAgent，因此 subagent 必须如实判定。
        """
        import json

        output_dir = str(inputs.get("output_dir") or "").strip()
        path_guard = (
            "\n## 路径纪律（必须遵守）\n"
            f"- 产物根目录 output_dir：`{output_dir}`\n"
            "- 落盘文件必须使用输入参数中已给出的绝对路径，或基于上述 output_dir "
            "拼接的完整绝对路径；严禁凭记忆重新拼写、缩略或改造目录名。\n"
            if output_dir
            else ""
        )

        return (
            f"你正在替代一个失败的 SkillTurbo 规划节点完成任务。\n"
            f"节点名称: {node_name}\n"
            f"任务说明: {instruction}\n"
            f"原失败原因: {type(error).__name__}: {error}\n"
            f"输入参数: {json.dumps(inputs, ensure_ascii=False, default=str)}\n"
            f"{path_guard}\n"
            f"## 强制输出格式\n"
            f"先输出给用户看的正文内容，然后在末尾另起一行输出契约声明（单行内联代码）：\n"
            f"---\n"
            f'`{{"success": true/false, "result": {{...}}}}`\n'
            f"- success: 是否真正达成了节点任务说明中的全部目标（文件已生成/校验已通过/字段已写入）。\n"
            f"  - 只有在产出可被下游节点直接消费时才填 true。\n"
            f"  - 若未能完成、部分完成、或无法确认，必须填 false，并在 result 中说明原因。\n"
            f"  - 编排类节点（任务说明要求串联多个阶段/子流程，如 PPT 根节点）：success=true\n"
            f"    必须以全部阶段完成、终端产物已生成并交付为准（如最终文件已落盘、已发送）；\n"
            f"    仅修复失败的子阶段不算完成，必须填 false。\n"
            f"- result: 节点产出。成功时填需要回写到 inputs 的字段（如 outline_path、p4_validate_status 等）；"
            f'失败时填 {{"reason": "..."}}。\n'
            f"JSON 必须压成单行，切勿多行展开。切勿伪造 success=true，否则会导致下游节点崩溃。"
        )

    @staticmethod
    def _scan_balanced_json_objects(text: str) -> list[str]:
        """对裸 JSON（无围栏）做括号平衡扫描，返回所有完整 {...} 子串。

        从每个 `{` 起，按字符串/转义感知的括号计数找匹配的 `}`，收集深度归零的子串。
        用于无围栏时定位契约声明。扫描全文（而非仅末尾片段），以兼容契约出现在
        开头/中段等 off-spec 情况，避免漏检导致误判 failed。

        复杂度：匹配成功的各 {...} 区间互不重叠，且至多存在一次扫描到末尾的失败
        匹配，整体为 O(n)。fallback 产出为单次 LLM 响应、长度有界，无需额外截断。
        """
        results: list[str] = []
        n = len(text)
        i = 0
        while i < n:
            if text[i] != "{":
                i += 1
                continue
            depth = 0
            in_str = False
            escape = False
            j = i
            while j < n:
                ch = text[j]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        results.append(text[i:j + 1])
                        break
                j += 1
            i = j + 1 if j < n else n
        return results

    @staticmethod
    def _parse_fallback_output(fallback_output: Any) -> tuple[bool, dict[str, Any]]:
        """解析 subagent 末尾的 JSON 契约声明。

        Returns:
            (success, result): success 表示是否达成节点契约；result 为回写字段。
            无法解析时视为失败。
        """
        import json
        import re

        text = str(fallback_output or "")
        stripped = text.strip()
        if not stripped:
            # 空产出（subagent 未返回任何内容）与"有产出无契约"区分，便于日志诊断。
            logger.warning(
                "[DeepAgentFallbackHandler] fallback output is empty, treat as failed"
            )
            return False, {"reason": "fallback subagent 未产出任何内容"}

        # 收集候选 JSON 片段（按优先级）。解析靠 json.loads，正则只圈定范围，
        # 不用 \{.*?\} 截断（嵌套对象会被首个 } 截断导致解析失败）。
        candidates: list[str] = []
        # 1. 单行内联代码: `{"success": ...}` （贪婪到反引号，保留嵌套）
        candidates.extend(re.findall(r"`(\{.*\})`", text))
        # 2. ```json ... ``` 多行代码块（围栏内全部内容）
        candidates.extend(re.findall(r"```json\s*(\{[\s\S]*?\})\s*```", text))
        # 3. ``` ... ``` 不带 language tag 的代码块
        candidates.extend(re.findall(r"```\s*(\{[\s\S]*?\})\s*```", text))
        # 4. 裸 JSON：从每个 { 起做括号平衡扫描，取最后一个完整且可解析的对象
        candidates.extend(DeepAgentFallbackHandler._scan_balanced_json_objects(stripped))
        # 去重：不同收集方式可能捕获到相同 JSON 片段（如内联代码与裸 JSON 扫描
        # 都会得到 {"success": ...}），用 dict.fromkeys 保持顺序去重，避免重复解析。
        candidates = list(dict.fromkeys(candidates))

        if not candidates:
            logger.warning(
                "[DeepAgentFallbackHandler] fallback output has no JSON contract block, "
                "treat as failed"
            )
            return False, {"reason": "fallback 输出未包含 JSON 契约声明"}

        payload = None
        last_err: json.JSONDecodeError | None = None
        for candidate in reversed(candidates):
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError as e:
                last_err = e
                continue
            if isinstance(obj, dict) and "success" in obj:
                payload = obj
                break
        if payload is None:
            # 所有候选都不可解析或不含 success 字段
            logger.warning(
                "[DeepAgentFallbackHandler] fallback JSON parse failed: %s, treat as failed",
                last_err,
            )
            return False, {"reason": f"fallback JSON 解析失败: {last_err}" if last_err else "fallback 输出未包含 JSON 契约声明"}

        success = bool(payload.get("success", False))
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            result = {"reason": str(result)}
        return success, result

    @staticmethod
    def _build_success_result(
        node_name: str,
        inputs: dict[str, Any],
        contract_result: dict[str, Any],
        error: Exception,
    ) -> dict[str, Any]:
        """构建 fallback 成功后的结果 dict。

        将 subagent 返回的 contract_result 合并回 inputs，
        保留 fallback 标记供下游感知，但状态为 completed 而非 degraded。
        """
        context_result = dict(inputs)
        # 合并 subagent 声明的产出字段（如 outline_path、p4_validate_status 等）
        context_result.update({
            key: value
            for key, value in contract_result.items()
            if key not in {"reason"}
        })
        context_result.update({
            "node": node_name,
            "status": "completed",
            "fallback": True,
            "fallback_reason": f"{type(error).__name__}: {error}",
        })
        return context_result

    @staticmethod
    def _log_degraded_result(
        node_name: str,
        fallback_output: Any,
        error: Exception,
    ) -> None:
        output_text = str(fallback_output or "")
        logger.info(
            "[DeepAgentFallbackHandler] degraded result built node=%s output_len=%d output_preview=%s reason=%s",
            node_name,
            len(output_text),
            output_text[:300],
            f"{type(error).__name__}: {error}",
        )

    @staticmethod
    def _apply_result_validator(
        result_validator: ResultValidator | None,
        node_name: str,
        inputs: dict[str, Any],
        contract_result: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """契约自证通过后，运行节点级交付物校验器。

        校验器拒绝或自身异常均视为契约未达成（fail-closed），返回
        ``(False, {{"reason": ...}})``；放行返回 ``(True, contract_result)``。
        AbortError（HITL 中断信号）不在此列：必须原样上抛给 executor 转
        HITL 三件套，吞掉会把用户中断降级成契约失败继续执行。
        """
        if result_validator is None:
            return True, contract_result
        # AbortError 触点在 plan_node，模块级反向 import 会形成循环
        # （plan_node 顶部导入本模块的 FallbackContractError），故惰性导入。
        from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

        try:
            reason = result_validator(inputs, contract_result)
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            logger.error(
                "[DeepAgentFallbackHandler] result validator crashed node=%s error=%s",
                node_name,
                exc,
            )
            return False, {"reason": f"result validator 异常: {exc}"}
        if reason:
            logger.error(
                "[DeepAgentFallbackHandler] node fallback result rejected by validator "
                "node=%s reason=%s",
                node_name,
                reason,
            )
            return False, {"reason": reason}
        return True, contract_result

    @staticmethod
    def _reconcile_contract_artifacts(
        inputs: dict[str, Any],
        contract_result: dict[str, Any],
    ) -> list[str]:
        """契约通过后，把产物路径归位到 output_dir（原地修正 contract_result）。

        纠正 subagent 自行拼接路径、把产物写到 output_dir 之外导致下游
        按约定路径读取失败的问题。仅当同时满足以下条件才动作：
        - key 以 _path/_file 结尾，且值为已存在的文件；
        - 文件不在 output_dir 之下；
        - 文件位于允许源根之内：优先 inputs["workspace_base"]（会话产物根，
          生产链路由 skill_turbo_tools 构造 inputs 时统一注入，直连调用方
          建议显式传入以收紧允许源根），缺失时退化为 output_dir 父目录；
          源根外的路径一律拒绝归位，防止被注入的 subagent 声明敏感路径
          （如密钥、环境变量文件）借框架复制进交付目录外泄；
        - output_dir 下无同名文件（不覆盖已有产物）。
        动作为复制（非移动）；任何失败仅记 WARNING，不影响契约结果。
        返回本次新建到 output_dir 的产物路径列表，供 validator 拒绝时回滚。
        skill 流水线将产物根目录写入 inputs["output_dir"] 即可启用本防护。
        """
        import shutil
        from pathlib import Path

        relocated: list[str] = []
        output_dir = str(inputs.get("output_dir") or "").strip()
        if not output_dir:
            return relocated
        out_root = Path(output_dir)
        source_root_raw = str(inputs.get("workspace_base") or "").strip()
        if not source_root_raw:
            source_root_raw = str(out_root.parent)
        source_root = Path(source_root_raw)
        out_root_resolved = out_root.resolve()
        source_root_resolved = source_root.resolve()
        path_key_suffixes = ("_path", "_file")
        for key, value in list(contract_result.items()):
            if not isinstance(value, str) or not value.strip():
                continue
            if not any(key.endswith(s) for s in path_key_suffixes):
                continue
            src = Path(value.strip())
            if not src.is_file():
                continue
            # resolve 展开 symlink 后判定，防止根内链接指向根外敏感文件
            src_real = src.resolve()
            if src_real.is_relative_to(out_root_resolved):
                continue
            if not src_real.is_relative_to(source_root_resolved):
                logger.warning(
                    "[DeepAgentFallbackHandler] fallback artifact outside source root, "
                    "skip relocate key=%s src=%s source_root=%s",
                    key,
                    src,
                    source_root,
                )
                continue
            dst = out_root / src.name
            if dst.exists():
                logger.warning(
                    "[DeepAgentFallbackHandler] fallback artifact relocate skipped, "
                    "dst exists key=%s src=%s dst=%s",
                    key,
                    src,
                    dst,
                )
                continue
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                logger.warning(
                    "[DeepAgentFallbackHandler] fallback artifact relocate failed "
                    "key=%s src=%s error=%s",
                    key,
                    src,
                    exc,
                )
                continue
            contract_result[key] = str(dst.resolve())
            relocated.append(contract_result[key])
            logger.warning(
                "[DeepAgentFallbackHandler] fallback artifact relocated key=%s src=%s dst=%s",
                key,
                src,
                dst,
            )
        return relocated

    @staticmethod
    def _rollback_relocated_artifacts(paths: list[str]) -> None:
        """validator 拒绝后回滚本次归位新建的产物，避免孤儿残留。"""
        from pathlib import Path

        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
                logger.warning(
                    "[DeepAgentFallbackHandler] fallback artifact rolled back path=%s",
                    path,
                )
            except OSError as exc:
                logger.warning(
                    "[DeepAgentFallbackHandler] fallback artifact rollback failed "
                    "path=%s error=%s",
                    path,
                    exc,
                )

    async def _execute_spawn_fallback(self, call: FallbackCall) -> str:
        """通过 adapter 的 ``spawn_fallback`` 执行 SkillTurbo fallback，返回子代理输出文本。"""
        query = self._build_fallback_query(
            call.node_name, call.instruction, call.inputs, call.error
        )
        return await self._adapter.spawn_fallback(
            query, parent_session=call.parent_session
        )

    async def fallback(self, call: FallbackCall) -> dict[str, Any]:
        """非流式 fallback 实现。"""
        node_name = call.node_name
        inputs = call.inputs
        error = call.error
        result_validator = call.result_validator
        try:
            fallback_output = await self._execute_spawn_fallback(call)
        except Exception as e:
            logger.error(
                "[DeepAgentFallbackHandler] fallback error node=%s error=%s",
                node_name,
                e,
            )
            raise FallbackContractError(
                node_name=node_name,
                reason=f"fallback spawn 执行异常: {e}",
                original_error=error,
            ) from e
        logger.warning(
            "[DeepAgentFallbackHandler] node fallback spawn finished node=%s error=%s",
            node_name,
            error,
        )
        self._log_degraded_result(node_name, fallback_output, error)

        # 契约校验：subagent 必须自证达成节点目标，否则视为 fallback 失败，
        # 抛出异常让 SkillTurbo 降级到 DeepAgent。
        success, contract_result = self._parse_fallback_output(fallback_output)
        if success:
            # 先归位产物再校验交付物，validator 才能看到正确位置的产物；
            # validator 拒绝时回滚本次归位，避免孤儿残留干扰重试与盘点
            relocated = self._reconcile_contract_artifacts(inputs, contract_result)
            success, contract_result = self._apply_result_validator(
                result_validator, node_name, inputs, contract_result
            )
            if not success and relocated:
                self._rollback_relocated_artifacts(relocated)
        if not success:
            reason = contract_result.get("reason", "fallback subagent 未达成节点契约")
            logger.error(
                "[DeepAgentFallbackHandler] node fallback contract failed node=%s reason=%s, "
                "degrading to DeepAgent",
                node_name,
                reason,
            )
            raise FallbackContractError(
                node_name=node_name,
                reason=reason,
                original_error=error,
            )

        logger.info(
            "[DeepAgentFallbackHandler] node fallback contract passed node=%s",
            node_name,
        )
        success_result = self._build_success_result(node_name, inputs, contract_result, error)
        # 与流式兜底（_fallback_stream_impl）对齐：契约结果必须写回共享计划上下文。
        # 嵌套场景父节点会丢弃 execute_subplan 返回值，不回写则下游节点读到旧值
        # （如 P4.1 读到空 search_mode 连锁失败直至 fallback limit exceeded）。
        inputs.update({
            key: value
            for key, value in success_result.items()
            if key not in {"node", "status"}
        })
        return success_result

    def fallback_stream(self, call: FallbackCall) -> AsyncIterator[dict[str, Any]]:
        """流式 fallback 实现。"""
        return self._fallback_stream_impl(call)

    async def _fallback_stream_impl(self, call: FallbackCall) -> AsyncIterator[dict[str, Any]]:
        """流式 fallback 的实际实现。

        adapter.spawn_fallback 为阻塞式 invoke，不向 parent_session 转发子代理流式
        过程；此处只负责 fallback 生命周期事件和最终结构化结果。
        """
        node_name = call.node_name
        inputs = call.inputs
        error = call.error
        result_validator = call.result_validator
        logger.warning(
            "[DeepAgentFallbackHandler] node fallback_stream via spawn node=%s error=%s",
            node_name,
            error,
        )

        yield {
            "event_type": "fallback.started",
            "node_name": node_name,
            "error": f"{type(error).__name__}: {error}",
            "timestamp": int(asyncio.get_event_loop().time() * 1000),
        }

        try:
            fallback_output = await self._execute_spawn_fallback(call)
        except Exception as e:
            logger.error(
                "[DeepAgentFallbackHandler] fallback_stream error node=%s error=%s",
                node_name,
                e,
            )
            # 与其他失败路径（契约不达成）保持一致：raise FallbackContractError 将异常
            # 向上传播，让 executor 的 execute_plan_stream 捕获并终止 plan，最终由 tool
            # 层返回 success=false 给 LLM 触发降级。不 yield chat.error：会被 executor
            # 透传并经 tool 转发到父会话，抢先于 tool result 到达前端终结会话，LLM 来不及
            # 转 skill_tool 降级。
            raise FallbackContractError(
                node_name=node_name,
                reason=f"fallback spawn 执行异常: {e}",
                original_error=error,
            ) from e
        finally:
            yield {
                "event_type": "fallback.finished",
                "node_name": node_name,
                "timestamp": int(asyncio.get_event_loop().time() * 1000),
            }

        self._log_degraded_result(node_name, fallback_output, error)

        # 契约校验：subagent 必须自证达成节点目标，否则视为 fallback 失败，
        # 抛出异常让 SkillTurbo 降级到 DeepAgent。
        contract_success, contract_result = self._parse_fallback_output(fallback_output)
        if contract_success:
            # 先归位产物再校验交付物，validator 才能看到正确位置的产物；
            # validator 拒绝时回滚本次归位，避免孤儿残留干扰重试与盘点
            relocated = self._reconcile_contract_artifacts(inputs, contract_result)
            contract_success, contract_result = self._apply_result_validator(
                result_validator, node_name, inputs, contract_result
            )
            if not contract_success and relocated:
                self._rollback_relocated_artifacts(relocated)
        if not contract_success:
            reason = contract_result.get("reason", "fallback subagent 未达成节点契约")
            logger.error(
                "[DeepAgentFallbackHandler] node fallback_stream contract failed node=%s reason=%s, "
                "degrading to DeepAgent",
                node_name,
                reason,
            )
            # 注意：此处不再 yield chat.error。该事件会被 executor 透传并经 tool 转发到父会话，
            # 抢先于 tool result 到达前端，导致会话以错误态终结，LLM 来不及按系统提示
            # （skill_prompt_rail）转 skill_tool 走标准降级流程。只 raise 异常，让 tool 层
            # 把失败包成 tool result 返回 LLM，由 LLM 自主降级。
            raise FallbackContractError(
                node_name=node_name,
                reason=reason,
                original_error=error,
            )

        success_result = self._build_success_result(node_name, inputs, contract_result, error)
        inputs.update({
            key: value
            for key, value in success_result.items()
            if key not in {"node", "status"}
        })
        logger.info(
            "[DeepAgentFallbackHandler] node fallback_stream contract passed node=%s",
            node_name,
        )

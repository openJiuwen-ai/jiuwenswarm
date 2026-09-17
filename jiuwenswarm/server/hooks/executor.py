# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Hook 执行器 —— 执行 command / prompt 两类 hook，返回统一 HookResult."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class HookOutcome:
    SUCCESS = "success"
    BLOCKING = "blocking"
    NON_BLOCKING_ERROR = "non_blocking_error"


@dataclass
class HookResult:
    outcome: str = HookOutcome.SUCCESS
    error: str = ""
    show_to_model: bool = False
    modified_input: dict | None = None
    additional_context: str = ""


class HookExecutor:
    """统一调度 command / prompt hook 执行."""

    async def run_all(
        self,
        hook_configs: list[dict],
        hook_input: dict,
        session_id: str = "",
    ) -> list[HookResult]:
        """并行执行同一 matcher 下的所有 hooks."""
        if not hook_configs:
            return []

        tasks = []
        for cfg in hook_configs:
            hook_type = cfg.get("type", "command")
            if hook_type == "command":
                tasks.append(self._run_command_hook(cfg, hook_input))
            elif hook_type == "prompt":
                tasks.append(self._run_prompt_hook(cfg, hook_input))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            r if isinstance(r, HookResult)
            else HookResult(outcome=HookOutcome.NON_BLOCKING_ERROR, error=str(r))
            for r in results
        ]

    async def _run_command_hook(self, config: dict, hook_input: dict) -> HookResult:
        """执行 command 类型 hook（子进程）."""
        command = config.get("command", "")
        if not command:
            return HookResult(outcome=HookOutcome.NON_BLOCKING_ERROR, error="empty command")

        timeout = config.get("timeout", 30)
        shell = config.get("shell", "bash")
        hook_input_json = json.dumps(hook_input, ensure_ascii=False)

        env = os.environ.copy()
        env["ARGUMENTS"] = hook_input_json
        tool_name = hook_input.get("tool_name", "")
        env["TOOL_NAME"] = tool_name

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                shell, "-c", command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=hook_input_json.encode()),
                timeout=timeout,
            )
            returncode = proc.returncode

        except asyncio.TimeoutError:
            try:
                if proc is not None:
                    proc.kill()
                    await proc.wait()
            except Exception:
                logger.debug("Failed to kill hook process after timeout", exc_info=True)
            return HookResult(
                outcome=HookOutcome.NON_BLOCKING_ERROR,
                error=f"hook timeout after {timeout}s",
            )
        except Exception as e:
            return HookResult(
                outcome=HookOutcome.NON_BLOCKING_ERROR,
                error=str(e),
            )

        stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

        if returncode is None:
            return HookResult(
                outcome=HookOutcome.NON_BLOCKING_ERROR,
                error="hook process killed",
            )

        if returncode == 0:
            return self.parse_command_output(stdout)
        elif returncode == 2:
            # exit 2 = blocking; try stdout JSON for reason, fallback to stderr
            parsed = self.parse_command_output(stdout)
            reason = ""
            if parsed.outcome == HookOutcome.BLOCKING:
                reason = parsed.error
            elif parsed.additional_context:
                reason = parsed.additional_context
            if not reason:
                reason = stderr.strip() or "hook blocked execution"
            return HookResult(
                outcome=HookOutcome.BLOCKING,
                error=reason,
                show_to_model=True,
            )
        else:
            return HookResult(
                outcome=HookOutcome.NON_BLOCKING_ERROR,
                error=stderr or f"exit code {returncode}",
            )

    @staticmethod
    def parse_command_output(stdout: str) -> HookResult:
        """解析 command hook 的 stdout JSON 协议."""
        if not stdout.strip():
            return HookResult(outcome=HookOutcome.SUCCESS)

        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return HookResult(outcome=HookOutcome.SUCCESS)

        if not isinstance(data, dict):
            return HookResult(outcome=HookOutcome.SUCCESS)

        decision = data.get("decision", "")
        if decision == "block":
            return HookResult(
                outcome=HookOutcome.BLOCKING,
                error=data.get("reason", "blocked by hook"),
                show_to_model=True,
            )

        result = HookResult(outcome=HookOutcome.SUCCESS)

        if "modifiedInput" in data:
            result.modified_input = data["modifiedInput"]
        if "additionalContext" in data:
            result.additional_context = data["additionalContext"]
        if "reason" in data and decision != "block":
            result.additional_context = data["reason"]

        return result

    async def _run_prompt_hook(self, config: dict, hook_input: dict) -> HookResult:
        """执行 prompt 类型 hook（LLM 审核）."""
        prompt_template = config.get("prompt", "")
        if not prompt_template:
            return HookResult(outcome=HookOutcome.NON_BLOCKING_ERROR, error="empty prompt")

        timeout = config.get("timeout", 15)
        model_name = config.get("model", "")

        hook_input_json = json.dumps(hook_input, ensure_ascii=False)
        final_prompt = prompt_template.replace("$ARGUMENTS", hook_input_json)
        tool_name = hook_input.get("tool_name", "")
        final_prompt = final_prompt.replace("$TOOL_NAME", tool_name)

        try:
            response_text = await asyncio.wait_for(
                self._query_llm(final_prompt, model_name),
                timeout=timeout,
            )

            data = self.extract_json_from_response(response_text)

            decision = data.get("decision", "allow") if isinstance(data, dict) else "allow"

            if decision == "block":
                return HookResult(
                    outcome=HookOutcome.BLOCKING,
                    error=data.get("reason", "blocked by prompt hook"),
                    show_to_model=True,
                )

            result = HookResult(outcome=HookOutcome.SUCCESS)
            if isinstance(data, dict):
                if "modifiedInput" in data:
                    result.modified_input = data["modifiedInput"]
                if "additionalContext" in data:
                    result.additional_context = data["additionalContext"]
            return result

        except asyncio.TimeoutError:
            return HookResult(
                outcome=HookOutcome.NON_BLOCKING_ERROR,
                error=f"prompt hook timeout after {timeout}s",
            )
        except Exception as e:
            return HookResult(
                outcome=HookOutcome.NON_BLOCKING_ERROR,
                error=str(e),
            )

    async def _query_llm(self, prompt: str, model_name: str = "") -> str:
        """调用 LLM 执行 hook 审查.

        使用 openjiuwen 的 Model 基础设施，默认使用轻量模型。
        Model.invoke() 是 async 方法，直接 await 即可。
        """
        from jiuwenswarm.common.config import get_config
        from openjiuwen.core.foundation.llm import Model, ModelClientConfig

        config_base = get_config()
        models_cfg = config_base.get("models", {})
        default_cfg = models_cfg.get("default", {})
        client_cfg = default_cfg.get("model_client_config", {})

        api_key = client_cfg.get("api_key", "")
        api_base = client_cfg.get("api_base", "")
        client_provider = client_cfg.get("client_provider", "")
        default_model = client_cfg.get("model_name", "")

        mcc = ModelClientConfig(
            api_key=api_key,
            api_base=api_base,
            client_provider=client_provider,
        )
        model = Model(model_client_config=mcc)

        response = await model.invoke(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=1024,
            model=model_name or default_model,
        )
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # content blocks list — extract text
            parts = []
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def extract_json_from_response(text: str) -> dict:
        """从 LLM 响应中提取 JSON 对象."""
        if not text:
            return {}
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return {}
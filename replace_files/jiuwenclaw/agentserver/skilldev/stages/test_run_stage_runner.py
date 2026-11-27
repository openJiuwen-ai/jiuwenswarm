# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""测试用例执行器 - SkillDevTestRunner

负责执行单个测试用例的 with_skill 和 baseline 变体。
提供完整的超时、重试、性能指标收集等功能。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from jiuwenclaw.agentserver.skilldev.common_utils import strip_agent_output_noise
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    """执行状态枚举"""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class ExecutionResult:
    """单次执行结果"""
    eval_id: int
    eval_name: str
    variant: str  # "with_skill" or "baseline"
    status: ExecutionStatus
    output: str
    error_message: Optional[str] = None
    tokens_used: int = 0
    duration_seconds: float = 0.0
    timestamp: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "eval_id": self.eval_id,
            "eval_name": self.eval_name,
            "variant": self.variant,
            "status": self.status.value,
            "output": self.output,
            "error_message": self.error_message,
            "tokens_used": self.tokens_used,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
        }


class SkillDevTestRunner:
    """测试用例执行器
    
    负责：
    1. 加载和验证 Skill
    2. 创建 with_skill 和 baseline 两个 Agent
    3. 执行测试用例并收集结果
    4. 记录性能指标
    """
    
    # 配置常量
    DEFAULT_TIMEOUT_SECONDS = 600
    DEFAULT_MAX_RETRIES = 2

    def __init__(
        self,
        ctx: SkillDevContext,
        skill_dir: Path,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        emit_thinking: bool = False,
    ):
        """初始化测试运行器

        Args:
            ctx: SkillDevContext 实例
            skill_dir: Skill 文件目录
            timeout_seconds: 单个任务超时时间（秒）
            max_retries: 失败重试次数
            emit_thinking: 是否向前端推送 token 流；并行执行时应设为 False 以避免日志混杂
        """
        self.ctx = ctx
        self.skill_dir = skill_dir
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.emit_thinking = emit_thinking

        # 缓存已加载的 Skill
        self._skill_cache: Dict[str, Any] = {}
    
    async def run_with_skill(
        self,
        case: Dict[str, Any],
        output_dir: Path
    ) -> Dict[str, Any]:
        """执行带 Skill 的测试

        Args:
            case: 测试用例数据
            output_dir: 输出目录

        Returns:
            执行结果字典
        """
        eval_id = case.get("id", 0)
        eval_name = case.get("name", f"eval-{eval_id}")

        logger.info("[session=%s] [SkillDevTestRunner] 开始执行 with_skill: %s", self.ctx.state.task_id, eval_name)

        try:
            variant_dir = output_dir / "with_skill"
            variant_dir.mkdir(parents=True, exist_ok=True)
            # 创建 Agent（通过 system prompt 指引其自行读取 skill 文件）
            agent = self.ctx.create_stage_agent(
                stage_name=f"test_run_{eval_id}_ws",
                whitelist_key="test_run",
                system_prompt=self._build_system_prompt_with_skill(self.skill_dir, variant_dir),
                tools=["file_read", "file_write", "shell", "code_execute", "file_glob", 
                    "shell", "web_search_free"],
                max_iterations=30,
            )

            # 执行测试
            result = await self._execute_agent(
                agent=agent,
                case=case,
                output_dir=variant_dir,
                variant="with_skill",
                skill_name=self.skill_dir.name,
            )
            
            logger.info(
                "[session=%s] [SkillDevTestRunner] with_skill 完成: %s (%.2fs)",
                self.ctx.state.task_id,
                eval_name,
                result.duration_seconds,
            )
            self.ctx.release_agent_tools(agent)
            return result.to_dict()
        
        except Exception as e:
            logger.exception("[session=%s] [SkillDevTestRunner] with_skill 执行失败: %s", 
            self.ctx.state.task_id, eval_name)
            return self._create_error_result(
                eval_id=eval_id,
                eval_name=eval_name,
                variant="with_skill",
                error=e,
                output_dir=output_dir
            )
    
    async def run_baseline(
        self,
        case: Dict[str, Any],
        output_dir: Path
    ) -> Dict[str, Any]:
        """执行基线测试（不使用 Skill）
        
        Args:
            case: 测试用例数据
            output_dir: 输出目录
            
        Returns:
            执行结果字典
        """
        eval_id = case.get("id", 0)
        eval_name = case.get("name", f"eval-{eval_id}")
        
        logger.info("[session=%s] [SkillDevTestRunner] 开始执行 baseline: %s", self.ctx.state.task_id, eval_name)
        
        try:
            variant_dir = output_dir / "baseline"
            variant_dir.mkdir(parents=True, exist_ok=True)
            # 如果为MODIFY 模式：寻找旧版skill（skill-v1/ 等版本目录）
            old_skill_dir = self._find_old_skill_dir()
            # 若发现旧skill，将其作为baseline
            if old_skill_dir is not None:
                baseline_prompt = self._build_system_prompt_with_skill(old_skill_dir, variant_dir)
            # 未发现旧skill或为其他模式，无skill作为baseline
            else:
                baseline_prompt = self._build_baseline_system_prompt(variant_dir)

            agent = self.ctx.create_stage_agent(
                stage_name=f"test_run_{eval_id}_bl",
                whitelist_key="test_run",
                system_prompt=baseline_prompt,
                tools=["file_read", "shell", "code_execute", "file_glob", 
                    "shell", "web_search_free", "file_write"],
                max_iterations=30,
            )
            
            # 执行测试
            result = await self._execute_agent(
                agent=agent,
                case=case,
                output_dir=variant_dir,
                variant="baseline",
                skill_name=None
            )
            
            logger.info(
                "[session=%s] [SkillDevTestRunner] baseline 完成: %s (%.2fs)",
                self.ctx.state.task_id,
                eval_name,
                result.duration_seconds,
            )
            self.ctx.release_agent_tools(agent)
            return result.to_dict() if isinstance(result, ExecutionResult) else result
        
        except Exception as e:
            logger.exception("[session=%s] [SkillDevTestRunner] baseline 执行失败: %s", self.ctx.state.task_id, eval_name)
            return self._create_error_result(
                eval_id=eval_id,
                eval_name=eval_name,
                variant="baseline",
                error=e,
                output_dir=output_dir
            )
    
    async def _execute_agent(
        self,
        agent,
        case: Dict[str, Any],
        output_dir: Path,
        variant: str,
        skill_name: Optional[str]
    ) -> ExecutionResult:
        """执行 Agent 并收集结果
        
        Args:
            agent: Agent 实例
            case: 测试用例
            output_dir: 输出目录
            variant: 执行变体（with_skill/baseline）
            skill_name: Skill 名称
            
        Returns:
            ExecutionResult 对象
        """
        eval_id = case.get("id", 0)
        eval_name = case.get("name", f"eval-{eval_id}")
        prompt = case.get("prompt", "")
        input_files = case.get("files", [])
        if input_files:
            prompt += "\n\n**本次测试提供以下输入文件（请使用文件工具读取后处理）：**\n"
            for f in input_files:
                abs_path = self.ctx.workspace / f
                prompt += f"- {abs_path}"

        start_time = datetime.now(timezone.utc)

        try:
            # 执行 Agent（带超时和重试），同时获取执行 trace
            # stage_name 包含 case/variant 信息，使每个并行 agent 的 conversation_id 唯一
            output, trace = await self._execute_with_retry(
                agent=agent,
                prompt=prompt,
                max_retries=self.max_retries,
                stage_name=f"test_run/{eval_name}/{variant}",
            )

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()

            # 清洗噪音（泄漏的推理块 / 未执行的文本格式工具调用）
            output = strip_agent_output_noise(output)
            trace = strip_agent_output_noise(trace)

            # 将 trace 写入 trace.txt，供 grader 评分时参考
            if trace:
                (output_dir / "trace.txt").write_text(trace, encoding="utf-8")

            result = ExecutionResult(
                eval_id=eval_id,
                eval_name=eval_name,
                variant=variant,
                status=ExecutionStatus.SUCCESS,
                output=output,
                error_message=None,
                tokens_used=self._estimate_tokens(output),  # 粗略估算
                duration_seconds=duration,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            logger.info(
                "[session=%s] [SkillDevTestRunner] %s 执行成功 (耗时 %.2fs, output=%d字符, trace=%d字符)",
                self.ctx.state.task_id,
                variant,
                duration,
                len(output),
                len(trace),
            )
        
        except asyncio.TimeoutError:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result = ExecutionResult(
                eval_id=eval_id,
                eval_name=eval_name,
                variant=variant,
                status=ExecutionStatus.TIMEOUT,
                output="",
                error_message=f"任务超时 ({self.timeout_seconds}s 后)",
                tokens_used=0,
                duration_seconds=duration,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            
            logger.warning(
                "[session=%s] [SkillDevTestRunner] %s 执行超时 (%ss)",
                self.ctx.state.task_id,
                variant,
                self.timeout_seconds,
            )
        
        except Exception as e:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result = ExecutionResult(
                eval_id=eval_id,
                eval_name=eval_name,
                variant=variant,
                status=ExecutionStatus.ERROR,
                output="",
                error_message=str(e),
                tokens_used=0,
                duration_seconds=duration,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            
            logger.error(
                "[session=%s] [SkillDevTestRunner] %s 执行出错: %s",
                self.ctx.state.task_id,
                variant,
                e,
                exc_info=True
            )
        
        # 保存结果
        self._save_result(output_dir, result)
        
        return result
    
    async def _execute_with_retry(
        self,
        agent,
        prompt: str,
        max_retries: int,
        stage_name: str = "test_run",
    ) -> tuple[str, str]:
        """执行 Agent，失败时重试（指数退避）

        Args:
            agent: Agent 实例
            prompt: 输入 prompt
            max_retries: 最大重试次数
            stage_name: 传给 run_stage_agent_streaming 的 stage 标识，同时决定 conversation_id；
                        并行执行时应传入含 case/variant 信息的唯一字符串以避免 conversation_id 碰撞

        Returns:
            (output, trace) 元组：
              output — Agent 最终输出文本
              trace  — 工具调用 / 工具结果的可读执行轨迹

        Raises:
            Exception: 所有重试都失败后抛出异常
        """
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "[session=%s] [SkillDevTestRunner] 执行尝试 %d/%d",
                    self.ctx.state.task_id,
                    attempt,
                    max_retries,
                )

                result = await asyncio.wait_for(
                    self.ctx.run_stage_agent_streaming(
                        agent,
                        stage_name=stage_name,
                        query=prompt,
                        capture_trace=True,
                        emit_thinking=self.emit_thinking,
                    ),
                    timeout=self.timeout_seconds
                )
                # capture_trace=True 返回 (output, trace) 元组
                output, trace = result if isinstance(result, tuple) else (result, "")
                return output, trace
            
            except asyncio.TimeoutError as e:
                timeout_error = asyncio.TimeoutError(
                    f"任务在 {self.timeout_seconds}s 后超时"
                )
                last_error = timeout_error
                
                if attempt == max_retries:
                    raise timeout_error from e
                
                wait_time = 2 ** (attempt - 1)  # 指数退避: 1s, 2s, 4s, ...
                logger.warning(
                    "[session=%s] [SkillDevTestRunner] 超时，%ss 后重试 (第 %d/%d 次)",
                    self.ctx.state.task_id,
                    wait_time,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(wait_time)
            
            except Exception as e:
                last_error = e
                
                if attempt == max_retries:
                    raise
                
                wait_time = 2 ** (attempt - 1)
                logger.warning(
                    "[session=%s] [SkillDevTestRunner] 执行失败: %s，%ss 后重试 (第 %d/%d 次)",
                    self.ctx.state.task_id,
                    e,
                    wait_time,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(wait_time)
        
        # 不应该到达这里，但以防万一
        if last_error:
            raise RuntimeError("执行失败（已重试）") from last_error
        raise RuntimeError("执行失败，原因未知")
    
    def _find_old_skill_dir(self) -> Path | None:
        """找到 MODIFY 模式下改动前的旧 skill 目录.

        generate stage 在改动已有 skill 时会将原 skill/ 重命名为 skill-v1/、skill-v2/ 等。
        取版本号最小的目录作为 baseline（即本次改动前的原始版本）。
        未找到则返回 None，调用方降级为无 skill 的 baseline。
        """
        workspace = self.ctx.workspace
        versioned = [
            d for d in workspace.iterdir()
            if d.is_dir() and re.match(r"^skill-v\d+$", d.name)
        ]
        if not versioned:
            logger.warning(
                "[session=%s] [SkillDevTestRunner] MODIFY 模式下未找到旧版本 skill 目录（skill-vN/），baseline 将回退为无 skill 模式",
                self.ctx.state.task_id,
            )
            return None
        return max(versioned, key=lambda d: int(d.name[len("skill-v"):]))

    @staticmethod
    def _build_system_prompt_with_skill(skill_dir: Path, output_dir: Optional[Path] = None) -> str:
        """构建带 Skill 的系统提示词，指引 Agent 自行读取 skill 文件.

        Args:
            skill_dir:  Skill 文件目录
            output_dir: 本次测试的输出目录；若提供，Agent 会被告知在此目录写入生成文件

        Returns:
            系统提示词
        """
        base = (
            f"你是一个强大的 AI 助手，拥有特殊的能力和文件读取等工具能力。\n\n"
            f"## 首要任务：读取 Skill 指令\n\n"
            f"当前 Skill 文件存储于：{skill_dir}\n\n"
            f"**在执行用户请求之前，请先使用文件工具读取 Skill 内容：**\n"
            f"1. 读取 `{skill_dir}/SKILL.md` 了解 Skill 的核心指令\n"
            f"2. 如有需要，用 glob/list_directory 查看目录下其他文件（scripts/、references/ 等）\n"
            f"3. 按照 Skill 中的指令完成用户请求\n\n"
            f"确保根据 Skill 中的指令和用户的请求，充分发挥 Skill 的能力来完成任务。"
            f"执行代码时，确保使用code_execute或shell工具执行脚本和代码。"
        )
        if output_dir is not None:
            base += (
                f"\n\n## 输出文件\n"
                f"如需生成任何文件，请将所有输出文件保存到以下目录：\n"
                f"{output_dir}\n"
            )
        return base

    @staticmethod
    def _build_baseline_system_prompt(output_dir: Optional[Path] = None) -> str:
        """构建基线系统提示词

        Args:
            output_dir: 本次测试的输出目录；若提供，Agent 会被告知在此目录写入生成文件

        Returns:
            系统提示词
        """
        base = (
            "你是一个聪慧的 AI 助手。\n\n"
            "请根据用户的请求，使用你的通用知识和能力来完成任务。"
            "涉及执行代码时，确保使用code_execution或shell工具执行脚本和代码。"
        )
        if output_dir is not None:
            base += (
                f"\n\n## 输出文件\n"
                f"如需生成任何文件，请将所有输出文件保存到以下目录：\n"
                f"{output_dir}\n"
            )
        return base

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数
        
        Args:
            text: 文本内容
            
        Returns:
            估算的 token 数
        
        Note:
            这是一个简单的估算，实际的 token 数应该从 Agent 的响应中获取。
        """
        # 粗略估算：1 token ≈ 4 个字符
        return max(1, len(text) // 4)
    
    def _save_result(self, variant_dir: Path, result: ExecutionResult) -> None:
        """保存执行结果
        
        Args:
            variant_dir: 变体目录
            result: 执行结果
        """
        try:
            # 保存 result.json
            result_file = variant_dir / "result.json"
            result_dict = result.to_dict()

            # 记录 output_dir，供 Grader 直接使用文件工具读取产出文件
            result_dict["output_dir"] = str(variant_dir)

            # 扫描 agent 在 output_dir 内创建的文件（排除框架自动写入的标准文件）
            # 使用 rglob 递归扫描子目录，存储相对路径以保留目录结构信息
            framework_files = {"result.json", "timing.json", "trace.txt"}
            files_created = sorted(
                str(f.relative_to(variant_dir)).replace("\\", "/")
                for f in variant_dir.rglob("*")
                if f.is_file() and f.name not in framework_files
            ) if variant_dir.exists() else []
            result_dict["files_created"] = files_created

            result_file.write_text(
                json.dumps(result_dict, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            
            # 保存 timing.json（性能指标）
            timing_file = variant_dir / "timing.json"
            timing_data = {
                "duration_seconds": result.duration_seconds,
                "tokens_used": result.tokens_used,
                "timestamp": result.timestamp,
            }
            
            timing_file.write_text(
                json.dumps(timing_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            
            logger.info(
                "[session=%s] [SkillDevTestRunner] 结果已保存到: %s",
                self.ctx.state.task_id,
                variant_dir,
            )
        
        except Exception as e:
            logger.error("[session=%s] [SkillDevTestRunner] 保存结果失败: %s", self.ctx.state.task_id, e)
    
    def _create_error_result(
        self,
        eval_id: int,
        eval_name: str,
        variant: str,
        error: Exception,
        output_dir: Path
    ) -> Dict[str, Any]:
        """创建错误结果
        
        Args:
            eval_id: 评估 ID
            eval_name: 评估名称
            variant: 执行变体
            error: 异常对象
            output_dir: 输出目录
            
        Returns:
            错误结果字典
        """
        variant_dir = output_dir / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        
        result = ExecutionResult(
            eval_id=eval_id,
            eval_name=eval_name,
            variant=variant,
            status=ExecutionStatus.ERROR,
            output="",
            error_message=str(error),
            tokens_used=0,
            duration_seconds=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        # 保存错误结果
        self._save_result(variant_dir, result)

        return result.to_dict()
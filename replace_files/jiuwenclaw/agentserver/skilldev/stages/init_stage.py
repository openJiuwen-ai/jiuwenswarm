# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""INIT 阶段处理器.

职责：
1. 创建工作区目录（resources/ref-files/ resources/ref-skills/ resources/tool_specs/ skill/ evals/ output/）
2. 分类解析上传内容（base64 → 文件 → 按类型解压/写入）
3. 判断任务模式（CREATE / CREATE_WITH_RESOURCES / MODIFY）
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.asset_utils import AssetDirs, build_asset_json
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import (
    SkillDevEventType,
    SkillDevStage,
    SkillDevTaskMode,
    # determine_task_mode,
)
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult
from jiuwenclaw.agentserver.skilldev.common_utils import safe_extract_zip, strip_agent_output_noise
from jiuwenclaw.agentserver.skilldev.stages.validate_stage import parse_skill_frontmatter
from jiuwenclaw.agentserver.skilldev.tool_utils import generate_tool_scripts_and_usage
from jiuwenclaw.agentserver.skilldev.utils.download_file_from_url import download_file

logger = logging.getLogger(__name__)

_MAX_SKILL_NAME_ATTEMPTS = 2
_SKILL_NAME_MAX_PLAUSIBLE_LEN = 48
_SKILL_NAME_MAX_SEGMENTS = 8
_SKILL_NAME_MAX_RAW_LEN = 200
_SKILL_NAME_KEBAB_RE = re.compile(r"^(?:[a-z0-9]+(?:-[a-z0-9]+)*)$")

_SKILL_NAME_SYSTEM_PROMPT = """你是命名助手。请基于用户需求与已上传资料生成一个简短的 skill 标识名。

要求：
1) 只输出 skill 名称本身，不要思考，不要解释。
2) 使用 kebab-case，仅允许小写字母、数字、短横线。
3) 语义清晰，长度建议 2-4 个词，总长度不超过30字符。
4) skill 名称中不要出现`skill`字样，只输出技能名称本身。
"""


class InitStageHandler(StageHandler):
    """INIT 阶段：分类写入参考资料与工具说明，推断任务模式。"""

    async def execute(self, ctx: SkillDevContext) -> StageResult:

        # 工作区已由 Pipeline 的 ensure_local 创建，此处直接使用
        ref_files_dir = ctx.workspace / "resources" / "ref-files"
        ref_skills_dir = ctx.workspace / "resources" / "ref-skills"
        tool_scripts_dir = ctx.workspace / "resources" / "available-tools"
                
        # 解析上传的资源文件（普通参考文件）
        ref_files = ctx.state.input.get("files") or []
        if ref_files:
            # 适配小艺的文件上传方式
            ref_files_dir.mkdir(parents=True, exist_ok=True)
            if ref_files[0].get("url", ""):
                try:
                    for file in ref_files:
                        file_name = file.get("filename", "")
                        download_url = file.get("url", "")
                        _ = await download_file(download_url, str(ref_files_dir / file_name))
                        if Path(file_name).suffix.lower() in (".zip", ".skill"):
                            safe_extract_zip(
                                ref_files_dir / file_name, ref_files_dir, extract_to_stem_dir=False
                                ) 
                except Exception as exc:
                    logger.warning("[session=%s] [InitStage] 参考文件下载失败: err=%s", ctx.state.task_id, exc)
                    raise
            else:
                await self._write_resources(
                    ref_files,
                    ref_files_dir,
                    extract_zip_to_subdir=True,
                    session_id=ctx.state.task_id,
                )

        # 解析上传的参考 skill 压缩包
        skill_packages = ctx.state.input.get("skill_packages") or []

        if skill_packages:
            ref_skills_dir.mkdir(parents=True, exist_ok=True)
            # 适配小艺
            if skill_packages[0].get("url", ""):
                try:
                    for skill_package in skill_packages:
                        skill_package_name = skill_package.get("filename", "")
                        if Path(skill_package_name).suffix.lower() not in (".zip", ".skill"):
                            raise ValueError(
                                f"参考 skill 包仅支持 .zip / .skill 格式，当前文件: {skill_package_name}"
                            )
                        download_url = skill_package.get("url", "")
                        _ = await download_file(download_url, str(ref_skills_dir / skill_package_name))
                        safe_extract_zip(
                            ref_skills_dir / skill_package_name, ref_skills_dir, extract_to_stem_dir=False
                        )
                except Exception as exc:
                    msg = f"参考 skill 包处理失败：{exc}"
                    logger.error("[session=%s] [InitStage] %s", ctx.state.task_id, msg)
                    await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "init"})
                    raise RuntimeError(msg) from exc
            else:
                try:
                    await self._write_resources(
                        skill_packages,
                        ref_skills_dir,
                        extract_zip_to_subdir=True,
                        allowed_suffixes=(".zip", ".skill"),
                        session_id=ctx.state.task_id,
                    )
                except ValueError as exc:
                    msg = f"参考 skill 包格式错误：{exc}"
                    logger.error("[session=%s] [InitStage] %s", ctx.state.task_id, msg)
                    await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "init"})
                    raise RuntimeError(msg) from exc

        # 解析上传的外部工具定义（list[dict]，每项为一个 tool）
        tool_specs = ctx.state.input.get("tool_spec_files") or []
        if tool_specs:
            tool_scripts_dir.mkdir(parents=True, exist_ok=True)
            try:
                # 适配小艺：直接传入结构化对象（含 protocol 字段）
                if tool_specs[0].get("protocol", ""):
                    ctx.state.external_tools = generate_tool_scripts_and_usage(tool_specs, tool_scripts_dir)
                    logger.info(
                    "[session=%s] [InitStage] 加载外部工具: %d 个",
                    ctx.state.task_id,
                    len(ctx.state.external_tools),
                )
                else:
                    # base64 路径：文件内容为 JSON 数组，需先解码再解析
                    for tool_info in tool_specs:
                        fname = tool_info.get("filename", "?")
                        content_b64 = tool_info.get("base64Data", "")
                        if not content_b64:
                            raise ValueError(f"工具定义文件 [{fname}] 内容为空（base64Data 字段缺失）")
                        try:
                            raw_bytes = base64.b64decode(content_b64)
                        except Exception as exc:
                            raise ValueError(
                                f"工具定义文件 [{fname}] base64 解码失败，请确认文件完整性"
                            ) from exc
                        try:
                            tool_info_list = json.loads(raw_bytes.decode("utf-8"))
                        except json.JSONDecodeError as exc:
                            raise ValueError(
                                f"工具定义文件 [{fname}] JSON 解析失败：{exc}，"
                                f"请确保传入的工具为合法JSON格式"
                            ) from exc
                        if not isinstance(tool_info_list, list):
                            raise ValueError(
                                f"工具定义文件 [{fname}] 内容应为 JSON 数组，"
                                f"实际类型：{type(tool_info_list).__name__}"
                            )
                        ctx.state.external_tools = generate_tool_scripts_and_usage(
                            tool_info_list, tool_scripts_dir
                        )
                logger.info(
                    "[session=%s] [InitStage] 加载外部工具: %d 个",
                    ctx.state.task_id,
                    len(ctx.state.external_tools or []),
                )
            except Exception as exc:
                msg = f"外部工具定义解析失败：{exc}"
                logger.error("[session=%s] [InitStage] %s", ctx.state.task_id, msg)
                await ctx.emit(SkillDevEventType.ERROR, {"message": msg, "stage": "init"})
                raise RuntimeError(msg) from exc


        # 更新目录空状态：skill和resources目录
        skill_dir = ctx.workspace / "skill"
        ctx.state.ref_files_dir_empty = not any(ref_files_dir.iterdir()) if ref_files_dir.exists() else True
        ctx.state.ref_skills_dir_empty = not any(ref_skills_dir.iterdir()) if ref_skills_dir.exists() else True
        ctx.state.skill_dir_empty = not any(skill_dir.iterdir()) if skill_dir.exists() else True
        ctx.state.tool_scripts_dir_empty = not any(tool_scripts_dir.iterdir()) if tool_scripts_dir.exists() else True

        # 写入 asset.json：记录用户上传的所有资源路径（ref_files / ref_skills / tools）
        # 在 _generate_skill_name 之前写入，使命名 agent 可直接读取 asset 内容。
        # skill_dir 留空，由 generate_stage 在 agent 生成完毕后填充。
        build_asset_json(
            ctx.workspace,
            ctx.state.iteration,
            AssetDirs(
                ref_files_dir=ref_files_dir,
                ref_skills_dir=ref_skills_dir,
                tool_scripts_dir=tool_scripts_dir,
                existing_skill_dir=skill_dir if not ctx.state.skill_dir_empty else None,
            ),
        )

        # 写完文件后，通过内容扫描推断任务模式
        logger.info(
            "[session=%s] [InitStage] mode=%s ref_files_dir_empty=%s "
            "ref_skills_dir_empty=%s skill_dir_empty=%s tool_scripts_dir_empty=%s",
            ctx.task_id,
            ctx.state.mode.value,
            ctx.state.ref_files_dir_empty,
            ctx.state.ref_skills_dir_empty,
            ctx.state.skill_dir_empty,
            ctx.state.tool_scripts_dir_empty,
        )

        if not ctx.state.skill_dir_empty:
            skill_md = skill_dir / "SKILL.md"
            skill_name, _, _ = parse_skill_frontmatter(skill_md)
            ctx.state.skill_name = skill_name
        else:
            skill_name = await self._generate_skill_name(ctx)
            logger.info("[session=%s] [InitStage] 基于指令与上传内容生成 skill_name: %s", ctx.state.task_id, skill_name)
            ctx.state.skill_name = skill_name
            await ctx.emit(
                SkillDevEventType.SKILL_NAME_READY,
                {"skill_name": skill_name},
            )

        return StageResult(next_stage=SkillDevStage.CLARIFY)

    async def _generate_skill_name(self, ctx: SkillDevContext) -> str:
        """根据用户输入生成规范化的 skill_name（kebab-case，ASCII）."""
        user_query = str(ctx.state.input.get("query", "")).strip()
        agent = ctx.create_stage_agent(
            stage_name="init",
            system_prompt=_SKILL_NAME_SYSTEM_PROMPT,
            tools=["file_read", "file_glob", "file_listdir"],
            max_iterations=30,
        )
        for attempt in range(1, _MAX_SKILL_NAME_ATTEMPTS + 1):

            query = self._build_skill_name_query(ctx, user_query=user_query, attempt=attempt)
            try:
                raw_name = await ctx.run_stage_agent_streaming(
                    agent,
                    stage_name="init",
                    query=query,
                    emit_thinking=False,
                )
                logger.info(
                    "[session=%s] [InitStage] 生成 skill_name (尝试 %d/%d): %s",
                    ctx.state.task_id,
                    attempt,
                    _MAX_SKILL_NAME_ATTEMPTS,
                    raw_name,
                )
            except Exception as exc:
                logger.warning(
                    "[session=%s] [InitStage] 生成 skill_name 失败 (尝试 %d/%d)，%s: %s",
                    ctx.state.task_id,
                    attempt,
                    _MAX_SKILL_NAME_ATTEMPTS,
                    "将重试" if attempt < _MAX_SKILL_NAME_ATTEMPTS else "使用兜底",
                    exc,
                )
                if attempt < _MAX_SKILL_NAME_ATTEMPTS:
                    continue
                name, _ = self._normalize_skill_name(user_query, fallback_text=user_query)
                ctx.release_agent_tools(agent)
                return name

            name, need_retry = self._normalize_skill_name(raw_name, fallback_text="")
            if not need_retry and name:
                ctx.release_agent_tools(agent)
                return name
            if attempt < _MAX_SKILL_NAME_ATTEMPTS:
                continue
            ctx.release_agent_tools(agent)
            return "generated-skill"
        ctx.release_agent_tools(agent)
        return "generated-skill"

    def _build_skill_name_query(
        self, ctx: SkillDevContext, *, user_query: str, attempt: int = 1
    ) -> str:
        """构造命名 query，要求 Agent 结合用户指令与上传内容."""
        parts = [
            "请生成一个准确的 skill_name。",
            f"用户需求：{user_query}",
        ]
        has_uploaded_resources = any(
            (
                not ctx.state.ref_files_dir_empty,
                not ctx.state.ref_skills_dir_empty,
                not ctx.state.tool_scripts_dir_empty,
            )
        )
        if has_uploaded_resources:
            parts.append(
                f"命名前请优先检查工作区{ctx.workspace}/resources/目录下的上传内容（至少查看目录与关键文件名，必要时读取文件内容）",
            )
        else:
            parts.append(
                f"没有上传参考资料，请直接生成skill name，禁止调用文件工具读取文件内容。",
            )
        if attempt > 1:
            parts.append(
                "本次只输出单独一行 kebab-case 标识，不要前缀、不要解释、不要换行列举多个方案。",
            )
        parts.append("只输出最终 skill_name，不要思考，不要解释，不要输出任何其他内容。")
        return "\n".join(parts)

    @staticmethod
    def _to_ascii_skill_kebab(s: str) -> str:
        """将文本转为小写 ASCII kebab（优先取末行中合法的 skill 行）."""
        t = (s or "").strip().lower()
        t = re.sub(r"^```[a-z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        for ln in reversed(lines):
            if _SKILL_NAME_KEBAB_RE.match(ln) and len(ln) <= 64:
                return ln
        t = re.sub(r"[^a-z0-9]+", "-", t)
        t = re.sub(r"-{2,}", "-", t).strip("-")
        return t[:64].strip("-")

    def _normalize_skill_name(self, candidate: str, *, fallback_text: str = "") -> tuple[str, bool]:
        """清洗为 kebab-case；返回 (名称, 是否需要重试/不可靠).

        当 fallback_text 非空时，若主文本无法得到有效名，会依次尝试从 fallback 派生、最后为 generated-skill
        （用于 Agent 连续失败后的路径）。
        """
        cleaned = strip_agent_output_noise(candidate or "")
        needs_retry = False
        if len(cleaned) > _SKILL_NAME_MAX_RAW_LEN:
            needs_retry = True
        if re.search(
            r"<redacted_thinking|</?redacted_thinking>|<tool_call|</tool_call>",
            cleaned,
            re.I,
        ):
            needs_retry = True

        text = self._to_ascii_skill_kebab(cleaned)
        if not text and fallback_text:
            text = self._to_ascii_skill_kebab(fallback_text)
        if not text and fallback_text:
            text = "generated-skill"
        if not text:
            return ("", True)

        if len(text) > _SKILL_NAME_MAX_PLAUSIBLE_LEN:
            needs_retry = True
        if len(text.split("-")) > _SKILL_NAME_MAX_SEGMENTS:
            needs_retry = True
        if not _SKILL_NAME_KEBAB_RE.match(text):
            needs_retry = True
        return (text, needs_retry)

    async def _write_resources(
        self,
        resources: list[dict],
        dest_dir: Path,
        *,
        extract_zip_to_subdir: bool,
        allowed_suffixes: tuple[str, ...] | None = None,
        session_id: str = "",
    ) -> None:
        """将所有资源文件 base64 解码后写入 dest_dir。

        对 .zip/.skill 文件执行 Python zipfile 解压；
        """
        for res in resources:
            name = res.get("filename", "unknown")
            content_b64 = res.get("base64Data", "")
            try:
                raw = base64.b64decode(content_b64)
                file_path = dest_dir / name
                suffix = file_path.suffix.lower()
                if allowed_suffixes and suffix not in allowed_suffixes:
                    raise ValueError(f"不支持的文件类型: {name}")
                file_path.write_bytes(raw)
                logger.info(
                    "[session=%s] [InitStage] 写入资源: %s (%d bytes)",
                    session_id,
                    name,
                    len(raw),
                )

                if suffix in (".zip", ".skill"):
                    safe_extract_zip(file_path, dest_dir, extract_to_stem_dir=extract_zip_to_subdir)
                elif suffix == ".rar":
                    raise NotImplementedError(f"RAR format not supported, please use zip instead.")
            except Exception as exc:
                logger.warning(
                    "[session=%s] [InitStage] 资源文件写入失败: name=%s error=%s",
                    session_id,
                    name,
                    exc,
                )
                raise

    def _parse_tools(self, tools_dir: Path, session_id: str = "") -> list[dict]:
        """从工具目录解析工具定义，返回合法的 tool dict 列表。

        在 tools_dir 中读取全部 .json 文件并合并（内容应为 list[dict]）。
        每个 tool dict 至少须包含 "name" 字段，不合规项会被跳过。
        """
        if not tools_dir.exists() or not tools_dir.is_dir():
            logger.warning("[session=%s] [InitStage] 工具目录不存在或不是目录: %s", session_id, tools_dir)
            return []

        json_files = sorted(tools_dir.glob("*.json"))
        if not json_files:
            logger.warning("[session=%s] [InitStage] 工具目录中未找到 JSON 文件: %s", session_id, tools_dir)
            return []

        valid: list[dict] = []
        for json_file in json_files:
            try:
                parsed = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("[session=%s] [InitStage] 读取工具文件失败: file=%s error=%s", session_id, json_file.name, exc)
                continue

            if not isinstance(parsed, list):
                logger.warning("[session=%s] [InitStage] 工具文件内容应为 list: file=%s", session_id, json_file.name)
                continue

            for i, item in enumerate(parsed):
                if not isinstance(item, dict):
                    logger.warning("[session=%s] [InitStage] %s[%d] 不是 dict，跳过", session_id, json_file.name, i)
                    continue
                if not item.get("name"):
                    logger.warning("[session=%s] [InitStage] %s[%d] 缺少 'name' 字段，跳过", session_id, json_file.name, i)
                    continue
                valid.append(item)

        return valid

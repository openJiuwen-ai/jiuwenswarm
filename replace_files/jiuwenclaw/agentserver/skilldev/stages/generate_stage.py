# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""GENERATE 阶段处理器.

职责：
- 创建 GENERATE 专属 ReActAgent（配备文件读写工具 + 生成 Prompt）
- Agent 自主完成完整 Skill 文件集生成
- 推送 ARTIFACT_READY 事件通知前端产物就绪（驱动右侧附件列表）

Agent 工具白名单：["file_read", "file_write"]
"""

from __future__ import annotations

import logging
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.asset_utils import (
    load_asset,
    save_asset,
    load_skill_content,
    load_asset_content
)
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult
from jiuwenclaw.agentserver.skilldev.utils.skill_description_fix import (
    fix_skill_md_description,
)

logger = logging.getLogger(__name__)

# 注入 user query 时：强调澄清问答对生成结果具有约束力，避免模型忽略或与补充信息矛盾。
GENERATE_USER_CLARIFICATION_BINDING = """【必须遵守·用户补充信息】以下澄清内容与「用户需求」具有同等效力；其中已明确约定的点，对本次生成具有约束力，优于常识默认或笼统计划表述。

- 须在 SKILL.md（及 scripts/、references/ 等配套文件）中逐项落实：能力范围、触发与流程、输入输出形态、工具/数据依赖、禁止项、术语、验收标准等——凡用户在补充信息中已选定、确认或声明的内容，必须在正文、步骤、检查清单或可执行约定中显式体现，不得忽略、省略、弱化或仅一笔带过。
- 禁止编写与补充信息相矛盾的能力描述、工作流步骤、示例或隐含默认行为；禁止用「通用做法」或自行推测的默认方案覆盖用户已在补充信息中给出的明确选择。
- 若下文中「skill 开发计划」或既有草稿与补充信息冲突，以补充信息为准，并相应修正实现与表述，不得保留冲突内容。"""


GENERATE_SYSTEM_PROMPT_TEMPLATE = """你是一个 Skill 开发专家。根据用户需求、澄清信息和工作区资料，设计并生成完整的 Skill 文件集。

## SKILL.md 格式要求（必须严格遵守）

**YAML Frontmatter（必填）：**
```
---
name: skill-name-here
description: 用祈使句描述何时触发、做什么。描述应聚焦用户意图而非实现细节。≤1024 字符。
---
```

规则：
- name 必须是 kebab-case（小写字母、数字、连字符），≤30 字符
- description 必须是**单行纯文本**，以祈使句描述何时触发、做什么；禁止以 `>`、`-`、`*`、`#` 等 Markdown 标记开头；禁止使用 YAML 块标量（如 `>-`、`|`）；长度 ≤1024 字符
- 仅允许的 frontmatter key: name, description, license, allowed-tools, metadata, compatibility
- frontmatter 必须是 YAML 对象，且 key 不可重复；若存在未知 key，必须移除后再提交
- 若包含 allowed-tools，必须是字符串数组

## Skill 目录结构

```
skill/
├── SKILL.md (必需)
├── scripts/    - 确定性/重复性任务的可执行脚本
├── references/ - 按需加载的领域文档
└── assets/     - 输出中使用的模板、图标、字体等
```

## 写作原则

### 渐进式信息展示 (Progressive Disclosure)
1. **元数据**（name + description）— 始终在上下文中（~100 词）
2. **SKILL.md 正文** — 触发时加载（<500 行为佳）
3. **捆绑资源** — 按需加载（无大小限制，脚本可不加载直接执行）

### 写作风格
- 使用祈使句式（"执行 X" 而非 "这个 skill 会执行 X"）
- 解释 **为什么** 而非堆砌规则；避免过度使用 MUST/NEVER/ALWAYS
- 使用心理模型让模型理解意图，比死板指令更有效
- 保持 SKILL.md ≤500 行；超过时拆分到 references/ 并标明何时查阅

### 输出格式定义
明确定义预期输出结构，使用模板或示例：
```markdown
## 报告结构
请严格使用以下模板：
# [Title]
## Executive summary
## Key findings
## Recommendations
```

### 发现重复工作 → 捆绑脚本
如果测试中发现模型反复独立编写类似的辅助脚本，应将其捆绑到 scripts/ 中。

### description 的触发性
当前模型倾向于"不够主动触发"skill。description 应略微"推进式"——
除了说明 skill 做什么，还要列举具体触发场景，即使用户没有明确提到 skill 名称。

## 文件范围约束
你只能生成与 Skill 本身直接相关的文件（例如 SKILL.md、scripts/、references/、assets/）。
禁止生成与 Skill 交付无关的文件，例如 `README.md`、`implement_report.md`、实现总结、CHANGELOG、开发说明、复盘文档等。
仅允许写入以下路径：
- skill/SKILL.md
- skill/scripts/**
- skill/references/**
- skill/assets/**

## 原则性要求
请务必将文件写入 skill/ 目录下（如 skill/SKILL.md），并确保 YAML frontmatter 格式正确（name 为 kebab-case）。

## 生成流程要求
1. 先给出“计划写入的文件清单”
2. 再执行文件写入
3. 完成后进行自检并输出结果

## 自检清单（必须逐项输出通过/失败）
- `skill/SKILL.md` 存在
- frontmatter 中 `name` 为 kebab-case 且长度 ≤30
- `description` 为单行纯文本，不以 Markdown 标记（`>`、`-`、`*`、`#`）开头，长度 ≤1024
- frontmatter 仅包含允许 key
- 所有输出文件均在 `skill/` 目录下
- 未生成任何与 Skill 无关文件（如 `README.md`、`implement_report.md`）

## 最终输出格式（固定）
请按以下结构输出：
1. 本次写入文件列表（逐行路径）
2. 每个文件一句用途说明
3. 自检结果（对应“自检清单”逐项列出）

## 工作区
工作区路径：{workspace}。
工作区中已经创建了skill文件夹，无需重复创建。
禁止在工作区外进行操作。
"""


class GenerateStageHandler(StageHandler):
    """GENERATE 阶段：Agent 直接生成完整skill 文件集."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        skill_dir = ctx.workspace / "skill"
        generated_files = await self._generate_all_files(ctx, skill_dir)

        await ctx.emit(
            SkillDevEventType.ARTIFACT_READY,
            {
                "artifact": {
                    "id": "skill_files",
                    "name": ctx.state.skill_name or "skill",
                    "type": "skill_md",
                    "files": generated_files,
                    "browsable": True,
                    "downloadable": False,
                },
            },
        )
        return StageResult(next_stage=SkillDevStage.VALIDATE)

    async def _generate_all_files(
        self,
        ctx: SkillDevContext,
        skill_dir: Path,
    ) -> list[str]:
        """调用 Agent 生成完整 Skill 文件集，并回收实际产物列表."""

        system_prompt = GENERATE_SYSTEM_PROMPT_TEMPLATE.format(
            workspace=ctx.workspace
        )
        agent = ctx.create_stage_agent(
            stage_name="generate",
            system_prompt=system_prompt,
            tools=["file_read", "file_write", "file_edit", "file_glob", "file_grep", 
            "file_listdir", "web_search_free", "web_fetch", "shell", "code_execute"],
            max_iterations=50,
        )
        query = self._build_user_query(ctx)
        await ctx.run_stage_agent_streaming(agent, stage_name="generate", query=query)
        skill_md_path = skill_dir / "SKILL.md"
        if skill_md_path.exists():
            fix_skill_md_description(skill_md_path)
        if not skill_dir.exists():
            ctx.release_agent_tools(agent)
            return []
        generated_files = [
            str(path.relative_to(skill_dir)).replace("\\", "/")
            for path in skill_dir.rglob("*")
            if path.is_file()
        ]
        # 将 agent 生成的 skill 目录路径写入 asset.json，供下游 stage 直接读取
        self._update_asset_skill_dir(ctx.workspace, skill_dir)
        ctx.release_agent_tools(agent)
        return sorted(generated_files)

    @staticmethod
    def _update_asset_skill_dir(workspace: Path, skill_dir: Path) -> None:
        """在 agent 生成完毕后，将 skill_dir 写入 asset.json."""
        asset = load_asset(workspace)
        asset["skill_dir"] = str(skill_dir)
        save_asset(workspace, asset)
        logger.info("[GenerateStage] asset.json skill_dir 已更新: %s", skill_dir)

    def _build_user_query(self, ctx: SkillDevContext) -> str:
        """构建传给 ReActAgent 的 query 字符串."""
        query = ctx.state.input.get("query", "")
        parts = [f"## 用户需求：{query}", f"预生成的skill name: {ctx.state.skill_name}。如果用户明确指出修改skill name，请根据用户需求进行修改。"]

        # 将 QA 问答内容以可读形式注入
        if ctx.state.clarification_questions and ctx.state.clarification_answers:
            q_map = {q["id"]: q["question"] for q in ctx.state.clarification_questions}
            qa_lines = [
                f"- {q_map.get(a['question_id'], a['question_id'])}: {a['answer']}"
                for a in ctx.state.clarification_answers
            ]
            parts.append("## 用户补充信息（澄清问答）：\n" + "\n".join(qa_lines))
            parts.append(GENERATE_USER_CLARIFICATION_BINDING)
        elif ctx.state.clarification_answers:
            qa_lines = [
                f"- {a['question_id']}: {a['answer']}"
                for a in ctx.state.clarification_answers
            ]
            parts.append("## 用户补充信息：\n" + "\n".join(qa_lines))
            parts.append(GENERATE_USER_CLARIFICATION_BINDING)

        asset_json = load_asset(ctx.workspace)
        has_preloaded_content = False
        if not ctx.state.skill_dir_empty:
            has_preloaded_content = True
            # 预加载当前 skill 内容
            if ctx.state.generate_retries == 0:
                skill_dir_str = asset_json.get("skill_dir", "")
                skill_dir = Path(skill_dir_str) if skill_dir_str else ctx.workspace / "skill"
                skill_content_str, skill_failed = load_skill_content(skill_dir=skill_dir)
                if skill_failed:
                    logger.warning(
                        "[session=%s] [PlanStage] skill 文件部分未能预加载: %s",
                        ctx.state.task_id, skill_failed,
                    )
                skill_content_str = skill_content_str or "（未找到 skill 文件）"
                parts.append(
                    "## 当前 Skill 内容\n"
                    f"工作区 {ctx.workspace} 中的skill文件夹下存放了最近一轮生成的skill内容。以下为当前 Skill 文件的完整内容（已预加载，无需再调用工具读取）："
                    f"{skill_content_str}"
                    "在生成新的skill时，请先将原始 skill/ 目录重命名为 skill-vx/（x 表示递增版本号），再新建一个全新的 skill/ 目录，"
                    "并将本次生成的完整 Skill 文件保存到新的 skill/ 目录下。"
                )
            if not (ctx.state.ref_files_dir_empty and ctx.state.ref_skills_dir_empty):
                parts.append(f"## 参考资料\n工作区 {ctx.workspace} 中的resources/文件夹下存放了用户原始上传的参考资料，请根据需求自行判断是否需要查看。")
            if not ctx.state.tool_scripts_dir_empty:
                parts.append(
                    "## 预加载工具\n"
                    f"用户提供了以下工具，可以在生成的skill中使用：\n{ctx.state.external_tools}\n"
                    "工具是通过python脚本的方式调用的。如果需要使用某个工具，请将对应的脚本复制到skill/scripts/目录下，再在SKILL.md中使用。"
                    "不用去阅读脚本的内容，只需要知道工具的名称和参数即可。"
                    )
        else:
            # 预加载参考资料
            if not (ctx.state.ref_files_dir_empty and ctx.state.ref_skills_dir_empty):
                content, _ = load_asset_content(
                    asset=asset_json,
                    include_ref_files=True,
                    include_ref_skills=True,
                    include_tools=False,
                )
                if content:
                    parts.append(
                        f"## 预加载参考资料\n"
                        f"以下为用户上传的参考资料，存放于工作区 {ctx.workspace} 中的resources/目录。"
                        f"现已预加载，无需再调用工具读取：\n\n{content}"
                        "请阅读以上预加载的资料后再规划"
                    )
                    has_preloaded_content = True
            if not ctx.state.tool_scripts_dir_empty:
                parts.append(
                    "## 预加载工具\n"
                    f"用户提供了以下工具，可以在生成的skill中使用：\n{ctx.state.external_tools}\n"
                    "工具是通过python脚本的方式调用的。如果需要使用某个工具，请将对应的脚本复制到skill/scripts/目录下，再在SKILL.md中使用。"
                    "不用去阅读脚本的内容，只需要知道工具的名称和参数即可。"
                    )
                has_preloaded_content = True
        if has_preloaded_content:
            parts.append(
                "## 约束\n1. 创建Skill可供参考的文件内容已尽量完整预加载。如预加载未覆盖某些文件，可酌情使用文件工具补充查看。\n"
                "2. 严禁主动读取其他文件内容。\n3. 请根据用户需求与参考资料，生成skill相关内容"
            )
        else:
            parts.append("## 约束\n请根据用户需求，生成skill相关内容。不需主动调用工具读取文件内容。")

        if ctx.state.last_validate_error:
            parts.append(
                f"## 重要信息补充\n⚠️ 上次生成的 SKILL.md 校验失败（第 {ctx.state.generate_retries} 次重试）：\n"
                f"失败原因：{ctx.state.last_validate_error}\n"
                f"请务必将文件写入 {ctx.workspace} 中的 skill/ 目录下（如 skill/SKILL.md），"
                f"并确保 YAML frontmatter 格式正确（name 为 kebab-case）。"
            )

        return "\n\n".join(parts)

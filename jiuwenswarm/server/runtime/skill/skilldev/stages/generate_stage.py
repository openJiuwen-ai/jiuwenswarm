# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""GENERATE 阶段处理器.

职责：
- 创建 GENERATE 专属 ReActAgent（配备文件读写工具 + 生成 Prompt）
- 按确认后的 plan 的 directory_structure 创建目录
- Agent 按依赖顺序逐文件生成（SKILL.md 优先，其次 scripts/，最后 assets/）
- 推送 ARTIFACT_READY 事件通知前端产物就绪（驱动右侧附件列表）

Agent 工具白名单：["file_read", "file_write"]
"""

from __future__ import annotations

import logging
from pathlib import Path

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)

GENERATE_SYSTEM_PROMPT = """你是一个 Skill 开发专家。根据已确认的开发计划，生成完整的 Skill 文件集。

## SKILL.md 格式要求（必须严格遵守）

**YAML Frontmatter（必填）：**
```
---
name: skill-name-here
description: 用祈使句描述何时触发、做什么。描述应聚焦用户意图而非实现细节。≤1024 字符。
---
```

规则：
- name 必须是 kebab-case（小写字母、数字、连字符），≤64 字符
- description 不能包含 < 或 >
- 仅允许的 frontmatter key: name, description, license, allowed-tools, metadata, compatibility

## Skill 目录结构

```
skill-name/
├── SKILL.md (必需)
├── scripts/    - 确定性/重复性任务的可执行脚本
├── references/ - 按需加载的领域文档
└── assets/     - 输出中使用的模板、图标、字体等
```

## 写作原则（对齐官方 Skill Writing Guide）

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
ALWAYS use this exact template:
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
"""


class GenerateStageHandler(StageHandler):
    """GENERATE 阶段：Agent 按 plan 生成完整 skill 文件集."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        plan = ctx.state.plan
        if not plan:
            raise ValueError("GENERATE 阶段缺少 plan，请先完成 PLAN 阶段")

        skill_dir = ctx.workspace / "skill"
        generation_order = self._resolve_generation_order(plan)

        await ctx.emit(
            SkillDevEventType.PROGRESS,
            {
                "message": f"正在生成 {len(generation_order)} 个文件...",
                "files_total": len(generation_order),
                "files_done": 0,
            },
        )

        generated_files = await self._generate_all_files(
            ctx, skill_dir, generation_order
        )

        await ctx.emit(
            SkillDevEventType.ARTIFACT_READY,
            {
                "artifact": {
                    "id": "skill_files",
                    "name": (plan or {}).get("skill_name", "skill"),
                    "type": "skill_md",
                    "files": generated_files,
                    "browsable": True,
                    "downloadable": False,
                },
            },
        )
        return StageResult(next_stage=SkillDevStage.VALIDATE)

    def _resolve_generation_order(self, plan: dict) -> list[tuple[str, str]]:
        """确定文件生成顺序：SKILL.md 优先，scripts/ 其次，其余最后.

        Returns:
            [(filepath, role_description), ...]，按生成顺序排列
        """
        directory_structure: dict = plan.get("directory_structure", {})
        order: list[tuple[str, str]] = []

        # SKILL.md 必须最先生成（其他文件生成时需参考它）
        if "SKILL.md" in directory_structure:
            order.append(("SKILL.md", directory_structure["SKILL.md"]))

        # scripts/ 次之
        for path, role in directory_structure.items():
            if path != "SKILL.md" and path.startswith("scripts/"):
                order.append((path, role))

        # 其余文件
        for path, role in directory_structure.items():
            if path != "SKILL.md" and not path.startswith("scripts/"):
                order.append((path, role))

        return order

    async def _generate_all_files(
        self,
        ctx: SkillDevContext,
        skill_dir: Path,
        generation_order: list[tuple[str, str]],
    ) -> list[str]:
        """逐文件调用 Agent 生成内容.

        待实现: 接入 create_stage_agent + 逐文件生成逻辑
        """
        # 待实现:
        # agent = ctx.create_stage_agent(
        #     stage_name="generate",
        #     system_prompt=GENERATE_SYSTEM_PROMPT,
        #     tools=["file_read", "file_write"],
        #     max_iterations=30,
        # )
        # for idx, (filepath, role) in enumerate(generation_order):
        #     (skill_dir / filepath).parent.mkdir(parents=True, exist_ok=True)
        #     content = await self._generate_single_file(agent, ctx, filepath, role)
        #     (skill_dir / filepath).write_text(content, encoding="utf-8")
        #     await ctx.emit(SkillDevEventType.PROGRESS, {
        #         "message": f"已生成: {filepath}",
        #         "files_done": idx + 1,
        #         "files_total": len(generation_order),
        #     })

        logger.warning("[GenerateStage] _generate_all_files 尚未实现，创建占位文件")
        generated = []
        for filepath, role in generation_order:
            full_path = skill_dir / filepath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(
                f"# {filepath}\n\n<!-- 待实现: 由 Agent 生成，职责：{role} -->\n",
                encoding="utf-8",
            )
            generated.append(filepath)
        return generated

    async def _generate_single_file(
        self, agent, ctx: SkillDevContext, filepath: str, role: str
    ) -> str:
        """为单个文件生成内容.

        待实现: 构造 per-file prompt，调用 Agent，返回文件内容
        """
        raise NotImplementedError

    async def _validate_scripts(self, skill_dir: Path) -> None:
        """验证生成的 Python 脚本语法正确性.

        待实现: 使用 py_compile 或 ast.parse 检查语法
        """
        # for py_file in skill_dir.rglob("*.py"):
        #     import ast
        #     try:
        #         ast.parse(py_file.read_text(encoding="utf-8"))
        #     except SyntaxError as e:
        #         raise ValueError(f"脚本语法错误 {py_file}: {e}") from e
        pass

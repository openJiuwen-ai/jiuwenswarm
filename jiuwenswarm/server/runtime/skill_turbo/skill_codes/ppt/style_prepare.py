from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

logger = logging.getLogger(__name__)


_PRESET_STYLES = {"business-classic", "tech-minimal", "elegant-narrative", "industrial-tech"}


@dataclass(frozen=True)
class CustomStyleRequest:
    topic: str
    audience: str
    style_id: str
    style_description: str
    user_query: str = ""
    style_constraints: str = ""


class StylePrepareNode(PlanNode):
    def __init__(self) -> None:
        super().__init__(
            plan_name="p7_style_prepare",
            instruction=(
                "## P7 风格规范\n"
                "\n"
                "### 节点职责\n"
                "1. 根据用户选择的 `style_id` 取得风格定义内容\n"
                "2. 将风格内容落盘到 `{output_dir}/style-{style_id}.md`\n"
                "3. 通过 `style_file_path` 单字段交付给下游 P8（设计师节点）\n"
                "4. `style_mode == template_canvas` 时跳过风格文件生成，直接透传 `pack_dir` 给下游\n"
                "\n"
                "### 前置条件\n"
                "- `read_file` / `write_file` 工具可用（skill_codes/ 内禁止直接 IO，必须走工具）\n"
                "- `output_dir` 已由 P0 写入上下文\n"
                "- `style_id` 来自上游 P2 需求收集（缺省时按 `custom` 处理）\n"
                "\n"
                "### 输入\n"
                "- `style_id`（必填）: 取值范围 business-classic/tech-minimal/elegant-narrative/industrial-tech/custom；"
                "历史 `free` 输入与空值均归一化为 `custom`\n"
                "- `output_dir`（必填）: 工作目录绝对路径（落盘 style 文件用）\n"
                "- `topic`（可选）: PPT 主题，LLM 自定义生成时作为推断依据\n"
                "- `style_description`（可选）: `custom` 模式下用户自描述；自由发挥时通常为空\n"
                "\n"
                "### 输出\n"
                "```json\n"
                '{\"style_file_path\": \"{output_dir}/style-{style_id}.md\"}\n'
                "```\n"
                "失败场景下 `style_file_path` 为空字符串，由下游 P8 显式判空。\n"
                "\n"
                "### 执行流程\n"
                "1. **校验 output_dir**：为空直接返回空 style_file_path（记录 error）\n"
                "2. **模板画布分支**（`style_mode == template_canvas`）：跳过风格文件生成，校验 `pack_dir` 非空后透传给下游 P8\n"
                "3. **预设风格分支**（`style_id` ∈ {business-classic/tech-minimal/elegant-narrative/industrial-tech}）：\n"
                "   - 从 `pptx_root/references/styles/{style_id}/style.md` 读取外部新版 skill 风格定义\n"
                "   - 读取成功且非空 → 跳到步骤 5\n"
                "   - 读取失败/为空 → 落入步骤 4（降级）\n"
                "4. **自定义风格分支**（custom 或预设降级）：\n"
                "   - 调用 LLM 生成符合新版 `style-custom.md` 契约的风格规范 Markdown：\n"
                "     - 合法 YAML `font-family` frontmatter\n"
                "     - 整体风格描述\n"
                "     - 配色方案（主色/辅色/背景色/文字色/强调色，HEX）\n"
                "     - 字体（标题字体 + 正文字体）\n"
                "     - 排版与组件规范（页面尺寸 1280×720px、字号、卡片风格）\n"
                "     - CSS 主题变量和图表约束\n"
                "     - 设计禁忌（禁止未定义颜色/字体、禁止动画）\n"
                "   - 自由发挥时仅靠 `topic` 推断；有用户描述时参考 `style_description`\n"
                "5. **统一落盘**：调用 `write_file(file_path={output_dir}/style-{style_id}.md, content=...)`\n"
                "6. **返回**：`style_file_path` = `{output_dir}/style-{style_id}.md`\n"
                "\n"
                "### 失败兜底（按短路顺序）\n"
                "- `output_dir` 为空：直接返回空 `style_file_path` + error 日志\n"
                "- `style_mode == template_canvas` 但 `pack_dir` 为空：返回空 `style_file_path` + 空 `pack_dir` + error 日志\n"
                "- read_file 工具不可用 / 预设文件读取失败 / 内容为空：降级走 LLM 自定义生成路径\n"
                "- LLM 自定义生成失败/返回空：返回空 `style_file_path`，**不擅自构造默认风格**，由 P8 决定降级\n"
                "- write_file 不可用 / 写入异常：返回 `style_file_path` 但仅记录 error 日志，下游若拿到不存在的文件需自行处理\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        style_mode = str(inputs.get("style_mode") or "").strip()
        style_id = str(inputs.get("style_id", "")).strip() or "custom"
        style_description = str(inputs.get("style_description", "")).strip()
        if style_id.casefold() == "free":
            style_id = "custom"
        elif style_id not in _PRESET_STYLES and style_id != "custom":
            style_description = style_description or style_id
            style_id = "custom"
        inputs["style_id"] = style_id
        if style_description:
            inputs["style_description"] = style_description
        if style_mode == "free":
            style_mode = "custom"
            inputs["style_mode"] = style_mode
        topic = str(inputs.get("topic", "")).strip()
        audience = str(inputs.get("audience", "")).strip()
        output_dir = str(inputs.get("output_dir", "")).strip()
        user_query = PptCommon.collect_user_text(inputs)

        if not output_dir:
            logger.error("[P7] output_dir 为空，无法落盘风格文件")
            return {"style_file_path": ""}

        # 模板画布模式：跳过风格文件读取，直接透传 pack_dir
        if style_mode == "template_canvas":
            pack_dir = str(inputs.get("pack_dir") or "").strip()
            if not pack_dir:
                logger.error("[P7] style_mode=template_canvas 但 pack_dir 为空")
                return {"style_file_path": "", "pack_dir": ""}
            logger.info("[P7] 模板包模式，跳过风格文件生成，pack_dir=%s", pack_dir)
            return {
                "style_file_path": "",
                "pack_dir": pack_dir,
                "style_constraints": str(inputs.get("style_constraints") or "").strip(),
                "__artifact__": {"files": [{"path": pack_dir, "desc": "PPT模板包目录"}]},
            }

        pptx_root = str(inputs.get("pptx_root", "")).strip()

        # 模板包降级：模板不完整时从 pack_dir 读取模板 md 内容作为 style_description
        if inputs.get("template_pack_degraded"):
            pack_dir = str(inputs.get("pack_dir") or "").strip()
            if pack_dir:
                template_md = await self._read_template_md(pack_dir)
                if template_md:
                    style_description = template_md
                    logger.info("[P7] 模板包降级模式，使用模板 md 内容作为风格描述")

        style_content = ""
        style_constraints = str(inputs.get("style_constraints") or "").strip()
        if style_id in _PRESET_STYLES:
            style_content = await self._load_preset_style(style_id, pptx_root)

        if not style_content:
            if style_id in _PRESET_STYLES:
                logger.warning("[P7] 预设风格 %s 加载失败，降级为自定义生成", style_id)
            style_content = await self._generate_custom_style(
                CustomStyleRequest(
                    topic=topic,
                    audience=audience,
                    style_id=style_id,
                    style_description=style_description,
                    user_query=user_query,
                    style_constraints=style_constraints,
                )
            )

        if not style_content:
            logger.error(
                "[P7] 预设加载与 LLM 自定义生成均失败 style_id=%s，返回空 style_file_path",
                style_id,
            )
            return {"style_file_path": "", "style_constraints": style_constraints}

        style_file_path = f"{output_dir}/style-{style_id}.md"
        await self._write_style_file(style_file_path, style_content)

        # preset：不改写 style.md；constraints 原样透传给 P8
        return {
            "style_file_path": style_file_path,
            "style_constraints": style_constraints,
            "__artifact__": {"files": [{"path": style_file_path, "desc": "PPT风格文件"}]},
        }

    async def _read_template_md(self, pack_dir: str) -> str:
        """读取模板包目录下的模板 md 文件内容（降级时使用）。"""
        pack_path = Path(pack_dir)
        md_files = sorted(p for p in pack_path.iterdir() if p.suffix == ".md" and p.is_file())
        if not md_files:
            logger.warning("[P7] 模板包目录下未找到 md 文件: %s", pack_dir)
            return ""
        md_path = md_files[0]
        if not self.has_tool("read_file"):
            logger.warning("[P7] read_file 工具不可用，无法读取模板 md")
            return ""
        try:
            result = await self.call_tool("read_file", file_path=str(md_path))
            content = PptCommon.parse_tool_file_content(result)
            if content:
                logger.info("[P7] 读取模板 md 成功: %s", md_path)
                return content
            logger.warning("[P7] 模板 md 文件为空: %s", md_path)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P7] 读取模板 md 失败 %s: %s", md_path, e)
        return ""

    async def _load_preset_style(self, style_id: str, pptx_root: str = "") -> str:
        candidates: list[Path] = []
        if pptx_root:
            candidates.append(Path(pptx_root) / "references" / "styles" / style_id / "style.md")

        if not self.has_tool("read_file"):
            logger.warning("[P7] read_file 工具不可用，无法加载预设风格")
            return ""

        for preset_path in candidates:
            if not preset_path.is_file():
                continue
            try:
                result = await self.call_tool("read_file", file_path=str(preset_path))
                content = PptCommon.parse_tool_file_content(result)
                if content:
                    logger.info("[P7] 加载预设风格成功：%s", preset_path)
                    return content
                logger.warning("[P7] 预设风格文件为空：%s", preset_path)
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P7] 读取预设风格失败 %s: %s", preset_path, e)
        return ""

    async def _generate_custom_style(self, request: CustomStyleRequest) -> str:
        user_query_clause = ""
        if request.user_query:
            user_query_clause = f"用户原始 query：{request.user_query}\n"
        constraints_clause = ""
        if request.style_constraints.strip():
            constraints_clause = (
                "### 用户显式版式要求（必须烘焙进本文件，优先级最高）\n"
                f"{request.style_constraints.strip()}\n"
                "- 将上述约束写入排版与组件规范 / CSS 变量 / 设计禁忌中对应字段\n"
                "- 若点名字号：写死具体 px，禁止用区间敷衍\n"
                "- 若点名页码：必须在规范中二选一写死「保留页码（含格式）」或「不显示页码」；"
                "禁止并列两种方案\n"
                "- 未点名的维度仍按主题与风格描述推断\n\n"
            )
        prompt = (
            "你是 PPT 视觉设计师。请根据主题和风格描述生成一份风格规范 Markdown 文件，"
            "供后续 HTML 幻灯片生成使用。\n\n"
            f"PPT 主题：{request.topic or '（未提供）'}\n"
            f"目标受众：{request.audience or '（未提供）'}\n"
            f"风格标识：{request.style_id}\n"
            f"用户风格描述：{request.style_description or '（未提供，请根据主题自由发挥）'}\n"
            f"{user_query_clause}"
            f"{constraints_clause}"
            "### 输出要求（严格按以下结构生成，不得省略 frontmatter 或 CSS 变量）\n"
            "---\n"
            "font-family:\n"
            "  - Noto Sans SC\n"
            "  - sans-serif\n"
            "---\n"
            f"# 风格规范：{request.style_id}\n"
            "\n"
            "## 整体风格描述\n"
            "{一句话风格定调，例如：现代简约科技风、温暖人文风、专业商务风}\n"
            "\n"
            "## 配色方案\n"
            "- 主色：#XXXXXX（用途说明）\n"
            "- 辅色：#XXXXXX（用途说明）\n"
            "- 背景色：#XXXXXX\n"
            "- 文字色：#XXXXXX\n"
            "- 强调色：#XXXXXX\n"
            "\n"
            "## 字体\n"
            "- 标题字体：{从 frontmatter 完整字体栈中选择}\n"
            "- 正文字体：{与 frontmatter 使用同一完整字体栈}\n"
            "（仅可使用 `Noto Sans SC`、`WenYuan Sans SC`/`文源黑体`、"
            "`NanxiXinyuanti`/`南西新圆体`、`WenJin Mincho Plane 0`/`文津宋体 第0平面`、"
            "`Frex Sans GB`/`械黑 GB`，以及置于最后的 CSS 通用回退字体；"
            "禁止 Arial、Microsoft YaHei、PingFang SC、Noto Serif SC 等白名单外字体。）\n"
            "\n"
            "## 排版与组件规范\n"
            "- 页面尺寸：1280×720px\n"
            "- 标题字号：{px}\n"
            "- 正文字号：{px}\n"
            "- 卡片/分隔/图表风格：{描述}\n"
            "\n"
            "## CSS 主题变量\n"
            "```css\n"
            ":root {\n"
            "  --color-primary: #XXXXXX;\n"
            "  --color-secondary: #XXXXXX;\n"
            "  --color-background: #XXXXXX;\n"
            "  --color-surface: #XXXXXX;\n"
            "  --color-text: #XXXXXX;\n"
            "  --color-muted: #XXXXXX;\n"
            "  --color-accent: #XXXXXX;\n"
            '  --font-family: "Noto Sans SC", sans-serif;\n'
            "  --radius-card: 12px;\n"
            "  --shadow-card: 0 6px 20px rgba(23, 32, 51, 0.08);\n"
            "  --content-density: comfortable;\n"
            "}\n"
            "```\n"
            "`--font-family` 必须与 frontmatter 数组序列化后的完整字体栈完全一致。\n"
            "\n"
            "## 图表约束\n"
            "- 图表字体、颜色、标签和背景必须复用上述字体栈与 CSS 主题变量\n"
            "- 禁止动画和渐变填充，保证 PPTX 矢量导出\n"
            "\n"
            "## 设计禁忌\n"
            "- 禁止使用本文件未定义的颜色或字体\n"
            "- 禁止动画\n"
            "- 其他禁忌（如有）\n"
            "\n"
            "直接输出 Markdown 内容，不要输出解释或代码块包裹。"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是 PPT 视觉设计师，直接输出 Markdown 风格规范，不要输出其他内容。",
            )
            content = (result or "").strip()
            if content.startswith("```"):
                lines = content.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                content = "\n".join(lines).strip()
            if content:
                logger.info("[P7] 自定义风格生成成功 style_id=%s", request.style_id)
            return content
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning(
                "[P7] 自定义风格 LLM 生成失败 style_id=%s: %s",
                request.style_id,
                e,
            )
            return ""

    async def _write_style_file(self, path: str, content: str) -> None:
        if not self.has_tool("write_file"):
            logger.error("[P7] write_file 工具不可用，无法落盘风格文件 %s", path)
            return
        try:
            await self.call_tool("write_file", file_path=path, content=content)
            logger.info("[P7] 风格文件已落盘：%s", path)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P7] 写入风格文件失败 %s: %s", path, e)

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok",
            "message": f"风格规范已落盘：{result.get('style_file_path', '')}",
        }

"""SlideDesignerWorker — per-page slide-designer equivalent for skill_turbo P8.1."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
    PptCommon,
    resolve_layout_density,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.template_fill import (
    FillMode,
    PageGenPolicy,
    build_page_template_map,
    detect_page_type,
    resolve_fill_mode,
    resolve_skill_root,
    resolve_template_dir,
    should_seed_pages,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    cli_path,
    combined_output,
    quote_path,
    run_bash,
)

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
        PageGenContext,
        PageWorkerNode,
    )

logger = logging.getLogger(__name__)

# 仅限制同时 in-flight 的 check-layout（node+Chromium）进程数，防止 gather 全页
# 同时起浏览器打满宿主。不限制：页级 LLM 填槽/修补、asyncio.gather 本身。
# 信号量只包住 run_bash(check-layout)；释放后本页可立刻并行做 layout-patch LLM。
_CHECK_LAYOUT_CLI_CONCURRENCY = 3
_check_layout_sem: asyncio.Semaphore | None = None


def _get_check_layout_sem() -> asyncio.Semaphore:
    """懒初始化，避免 import 时绑定错误 event loop。"""
    global _check_layout_sem
    if _check_layout_sem is None:
        _check_layout_sem = asyncio.Semaphore(_CHECK_LAYOUT_CLI_CONCURRENCY)
    return _check_layout_sem


_STRUCTURAL_TYPES = frozenset(
    {"cover", "intro", "agenda", "section", "chapter", "ending", "conclusion", "transition"}
)
_DOM_BROKEN_FILL_REASONS = frozenset({
    "invalid_html",
    "invalid_dom",
    "content_template_chrome_changed",
    "head_chrome_changed",
    "header_chrome_changed",
    "footer_chrome_changed",
    "no_placeholders",
})


@dataclass
class SlideDesignerResult:
    page_num: int
    ok: bool
    html: str = ""
    fail_reason: str = ""
    path: str = ""
    layout_warning: bool = False


def _parse_cli_path_output(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.lower().startswith("exit code"):
            return stripped
    return ""


class SlideDesignerWorker:
    """Single-page worker: read seed → slot fill / free gen → validate → write → check-layout."""

    def __init__(self, host: PageWorkerNode, policy: PageGenPolicy) -> None:
        self._host = host
        self._policy = policy

    async def run(self, ctx: PageGenContext) -> SlideDesignerResult:
        page_type = detect_page_type(ctx.outline_page)
        try:
            mode = resolve_fill_mode(
                style_mode=ctx.style_mode,
                style_id=ctx.style_id,
                pages_seeded=ctx.pages_seeded,
                page_type=page_type,
                policy=self._policy,
            )
        except ValueError as exc:
            return SlideDesignerResult(
                page_num=ctx.page_num,
                ok=False,
                fail_reason=str(exc),
                path=ctx.page_path,
            )

        path = ctx.page_path or f"{ctx.pages_dir}/page-{ctx.page_num}.pptx.html"

        if mode == FillMode.FREE_GENERATE:
            html, fail_reason = await self._run_free_generate(ctx)
            if not html:
                return SlideDesignerResult(
                    page_num=ctx.page_num,
                    ok=False,
                    fail_reason=fail_reason or "free_generate_failed",
                    path=path,
                )
            ok = await self._host.write_file_for_worker(path, html)
            if not ok:
                return SlideDesignerResult(
                    page_num=ctx.page_num,
                    ok=False,
                    fail_reason="write_failed",
                    path=path,
                )
            return SlideDesignerResult(page_num=ctx.page_num, ok=True, html=html, path=path)

        seed_html = await self._load_seed_html(ctx, page_type)
        if not seed_html.strip():
            if self._policy.allow_free_gen_fallback:
                logger.warning(
                    "[SlideDesignerWorker] seed 缺失，降级 free_generate page=%d",
                    ctx.page_num,
                )
                return await self.run(replace(ctx, pages_seeded=False))
            return SlideDesignerResult(
                page_num=ctx.page_num,
                ok=False,
                fail_reason="seed_missing",
                path=path,
            )

        last_raw = ""
        last_reason = ""
        html = ""
        chart_activation_warning = False
        for attempt in range(max(self._policy.max_fill_attempts, 1)):
            rewrite_hint = ""
            if attempt > 0 and (last_raw or last_reason):
                rewrite_hint = self._build_rewrite_hint(last_reason)
            html, last_raw, last_reason = await self._fill_template_once(
                ctx,
                seed_html=seed_html,
                mode=mode,
                page_type=page_type,
                rewrite_hint=rewrite_hint,
            )
            if html:
                break

        if not html:
            # 契约重试耗尽：仍交付 HTML，不进 missing（对齐 mount 软化，避免 P9 拒导）
            if last_reason == "chart_scaffold_not_activated" and last_raw.strip():
                logger.warning(
                    "[SlideDesignerWorker] 图表 scaffold 未激活，重试耗尽仍交付 page=%d",
                    ctx.page_num,
                )
                html = last_raw
                chart_activation_warning = True
            elif self._policy.allow_free_gen_fallback:
                logger.warning(
                    "[SlideDesignerWorker] 填槽失败，降级 free_generate page=%d reason=%s",
                    ctx.page_num,
                    last_reason,
                )
                fb_html, fb_reason = await self._run_free_generate(
                    replace(ctx, pages_seeded=False)
                )
                if fb_html:
                    ok = await self._host.write_file_for_worker(path, fb_html)
                    return SlideDesignerResult(
                        page_num=ctx.page_num,
                        ok=ok,
                        html=fb_html if ok else "",
                        fail_reason="" if ok else "write_failed",
                        path=path,
                    )
                last_reason = fb_reason or last_reason
                return SlideDesignerResult(
                    page_num=ctx.page_num,
                    ok=False,
                    html=last_raw,
                    fail_reason=last_reason or "fill_failed",
                    path=path,
                )
            else:
                return SlideDesignerResult(
                    page_num=ctx.page_num,
                    ok=False,
                    html=last_raw,
                    fail_reason=last_reason or "fill_failed",
                    path=path,
                )

        ok = await self._host.write_file_for_worker(path, html)
        if not ok:
            return SlideDesignerResult(
                page_num=ctx.page_num,
                ok=False,
                html=html,
                fail_reason="write_failed",
                path=path,
            )

        layout_ok, layout_reason, layout_warning = await self._layout_loop(
            ctx, path, html, seed_html, mode
        )
        if not layout_ok:
            return SlideDesignerResult(
                page_num=ctx.page_num,
                ok=False,
                html=html,
                fail_reason=layout_reason or "layout_failed",
                path=path,
            )

        if layout_warning or chart_activation_warning:
            warn_reason = layout_reason or last_reason or "chart_scaffold_not_activated"
            logger.warning(
                "[SlideDesignerWorker] 页面 %d 已标记警告并继续交付: %s",
                ctx.page_num,
                warn_reason,
            )
            return SlideDesignerResult(
                page_num=ctx.page_num,
                ok=True,
                html=html,
                fail_reason=warn_reason,
                path=path,
                layout_warning=True,
            )

        logger.info(
            "[SlideDesignerWorker] 页面 %d 完成 mode=%s",
            ctx.page_num,
            mode,
        )
        return SlideDesignerResult(page_num=ctx.page_num, ok=True, html=html, path=path)

    async def _load_seed_html(self, ctx: PageGenContext, page_type: str) -> str:
        if ctx.pages_seeded and ctx.page_path:
            seeded = await self._host.read_file_for_worker(ctx.page_path)
            if seeded.strip():
                return seeded
        if not ctx.pptx_root:
            return ""
        from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
            _STRUCTURAL_TEMPLATE_PAGE_TYPES,
            _resolve_style_page_template_path,
        )

        style_key = ctx.style_id
        if ctx.style_mode == "custom" or ctx.style_id == "custom":
            style_key = "custom"
        if page_type in _STRUCTURAL_TYPES:
            template_page_type = _STRUCTURAL_TEMPLATE_PAGE_TYPES.get(page_type, page_type)
        else:
            template_page_type = "content"
        template_path = _resolve_style_page_template_path(
            ctx.pptx_root,
            style_key,
            page_type=template_page_type,
        )
        return await self._host.read_file_for_worker(template_path)

    async def _fill_template_once(
        self,
        ctx: PageGenContext,
        *,
        seed_html: str,
        mode: FillMode,
        page_type: str,
        rewrite_hint: str = "",
    ) -> tuple[str, str, str]:
        from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import ppt_page_gen as pg

        is_structural = page_type in _STRUCTURAL_TYPES

        # 预设/custom 内容页 → 完整 content-template fill
        if mode in {FillMode.PRESET_TEMPLATE, FillMode.CUSTOM_TEMPLATE} and not is_structural:
            return await self._host.generate_content_template_fill_for_worker(
                ctx,
                rewrite_hint=rewrite_hint,
                seed_html_override=seed_html,
            )

        # 预设/custom 结构页 → structural template fill
        if mode in {FillMode.PRESET_TEMPLATE, FillMode.CUSTOM_TEMPLATE} and is_structural:
            filled = await self._host.generate_structural_template_fill_for_worker(
                ctx,
                page_type,
                seed_html_override=seed_html,
                rewrite_hint=rewrite_hint,
            )
            if not filled:
                return "", "", "structural_fill_failed"
            if (
                self._host.normalize_template_whitespace_for_worker(seed_html)
                == self._host.normalize_template_whitespace_for_worker(filled)
            ):
                return "", filled, "seed_not_modified"
            return filled, "", ""

        # 内容页 / 结构页均已在上方分流；此处不应再落到 JSON 槽位路径
        logger.error(
            "[SlideDesignerWorker] unexpected template fill path mode=%s page_type=%s",
            mode,
            page_type,
        )
        return "", "", "unexpected_fill_mode"

    async def _run_free_generate(self, ctx: PageGenContext) -> tuple[str, str]:
        html, _raw, reason = await self._host.generate_one_for_worker(ctx)
        return html, reason

    async def _layout_loop(
        self,
        ctx: PageGenContext,
        path: str,
        html: str,
        seed_html: str,
        mode: FillMode,
    ) -> tuple[bool, str, bool]:
        """返回 (export_ok, reason, layout_warning)。"""
        from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import ppt_page_gen as pg

        current = html
        page_type = detect_page_type(ctx.outline_page)
        max_attempts = max(self._policy.max_layout_attempts, 1)
        for attempt in range(max_attempts):
            static_hints = self._host.post_check_layout_hints_for_worker(current)
            async with self._host.page_path_lock(path):
                cli_ok, cli_issues = await self._run_check_layout_cli(ctx)
            if cli_ok:
                return True, "", False
            cli_text = "; ".join(x for x in cli_issues if x)
            fix_hint = self._host.layout_fix_hint_from_cli_output_for_worker(cli_text) or cli_text
            merged_issues = [fix_hint] + [h for h in static_hints if h and h not in fix_hint]
            fail_text = "; ".join(x for x in merged_issues if x) or "layout_failed"
            if attempt + 1 >= max_attempts:
                return True, fail_text, True

            fix_text = fail_text

            # P1: 优先 §3.5 原位修补
            patched, _, patch_reason = await self._host.generate_layout_patch_for_worker(
                ctx,
                current_html=current,
                seed_html=seed_html,
                fix_hint=fix_text,
            )
            if patched:
                # 写盘前再剥一次：防 layout-patch 把 CHART_SCAFFOLD 注释带回
                candidate = self._host.fix_chart_scaffold_activation_for_worker(patched)
                # 若修补把已填 option 打成 null/空骨架，或仍未填 null，丢弃并保留 current
                if self._host.layout_patch_regressed_chart_options_for_worker(current, candidate):
                    logger.warning(
                        "[SlideDesignerWorker] 丢弃 layout_patch：图表 option 回退 page=%d",
                        ctx.page_num,
                    )
                    patch_reason = "layout_patch_chart_option_regressed"
                elif self._host.layout_patch_still_unfilled_chart_options_for_worker(
                    current, candidate
                ):
                    logger.warning(
                        "[SlideDesignerWorker] 丢弃 layout_patch：图表 option 仍未填 page=%d",
                        ctx.page_num,
                    )
                    patch_reason = "layout_patch_chart_option_still_null"
                else:
                    current = candidate
                    ok = await self._host.write_file_for_worker(path, current)
                    if not ok:
                        return False, "write_failed_after_layout_patch", False
                    continue

            # 末位兜底：DOM/chrome 损坏时才全槽重填
            if patch_reason in _DOM_BROKEN_FILL_REASONS:
                fixed, _, reason = await self._fill_template_once(
                    ctx,
                    seed_html=seed_html,
                    mode=mode,
                    page_type=page_type,
                    rewrite_hint=f"布局问题需修复：{fix_text}",
                )
                if not fixed:
                    if attempt + 1 >= max_attempts:
                        return True, reason or patch_reason or fix_text, True
                    continue
                current = fixed
                ok = await self._host.write_file_for_worker(path, current)
                if not ok:
                    return False, "write_failed_after_layout_fix", False
                continue

            if attempt + 1 >= max_attempts:
                return True, patch_reason or fail_text, True
            continue
        return True, "", False

    async def _run_check_layout_cli(self, ctx: PageGenContext) -> tuple[bool, list[str]]:
        if not self._policy.run_check_layout:
            return True, []
        if not self._host.has_tool("bash") or not ctx.pptx_root or not ctx.pages_dir:
            return True, []
        # 契约不变：逐页 --pages {N} + 按页 density；仅限流 Chromium 进程峰值。
        density = resolve_layout_density(ctx.research_page or None)
        cmd = (
            f"{cli_path('check-layout', ctx.pptx_root)} "
            f"{quote_path(ctx.pages_dir)} --pages {ctx.page_num} --density {density}"
        )
        try:
            async with _get_check_layout_sem():
                result = await run_bash(
                    self._host,
                    cmd,
                    timeout_seconds=120,
                    required=False,
                    workdir=ctx.pptx_root,
                )
            if result.exit_code == 0:
                return True, []
            return False, [combined_output(result)[:2000]]
        except BashExecError as exc:
            logger.warning(
                "[SlideDesignerWorker] check-layout CLI 异常 page=%d: %s",
                ctx.page_num,
                exc,
            )
            return False, [str(exc)]

    @staticmethod
    def _build_rewrite_hint(reason: str) -> str:
        mapping = {
            "invalid_html": "输出完整合法 HTML，须含闭合 </body></html>，且仅 1 个 .ppt-slide",
            "unfilled_placeholders": "不得残留任何 {{PLACEHOLDER}}",
            "content_template_chrome_changed": "不得修改 Page Chrome，只替换占位符文本和 PAGE_CONTENT",
            "head_chrome_changed": "禁止改动 <head>；仅填 body 内占位符",
            "header_chrome_changed": "禁止改动 header 结构；仅替换 PAGE_TITLE",
            "footer_chrome_changed": "禁止改动 footer 结构；仅替换 PAGE_FOOTER",
            "custom_page_content_blocks": "PAGE_CONTENT 至少 2 个直接子块",
            "seed_not_modified": "必须填入真实内容，不能与预铺模板逐字相同",
            "chart_scaffold_not_activated": (
                "本页已有 chart 容器：必须成对删除 CHART_SCAFFOLD_* 定界符，"
                "并将 const option = null 替换为配置对象 const option = {…}；"
                "禁止手写 echarts.init / var optionN"
            ),
        }
        return mapping.get(reason, reason)


class PresetTemplateSeedNode(PlanNode):
    """P8.0.5 — CLI 预铺 official/custom 脚手架到 pages/."""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p8_0_5_template_seed",
            instruction="预铺 preset/custom 模板到 pages/",
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        style_mode = str(inputs.get("style_mode") or "").strip()
        if not should_seed_pages(style_mode):
            return {
                "pages_seeded": False,
                "seed_skipped_reason": f"style_mode={style_mode}",
                "page_template_map": "",
            }

        pptx_root = str(inputs.get("pptx_root") or "").strip()
        output_dir = str(inputs.get("output_dir") or "").strip()
        style_id = str(inputs.get("style_id") or "").strip()
        outline_pages: dict[int, str] = inputs.get("outline_pages") or {}
        total_pages = int(inputs.get("total_pages") or 0)

        if not pptx_root or not output_dir or total_pages <= 0:
            logger.error("[P8.0.5] 缺少 pptx_root/output_dir/total_pages")
            return {
                "pages_seeded": False,
                "seed_skipped_reason": "missing_inputs",
                "page_template_map": "",
            }

        page_map = build_page_template_map(outline_pages, total_pages)
        if not page_map:
            return {
                "pages_seeded": False,
                "seed_skipped_reason": "empty_page_map",
                "page_template_map": "",
            }

        template_dir = resolve_template_dir(pptx_root, style_mode, style_id)
        cmd = (
            f"{cli_path('ensure-output-dir', pptx_root)} "
            f"{quote_path(output_dir)} "
            f"--template-dir {quote_path(template_dir)} "
            f"--page-templates {quote_path(page_map)}"
        )
        try:
            result = await run_bash(
                self,
                cmd,
                timeout_seconds=120,
                required=True,
                workdir=pptx_root,
            )
            pages_dir = _parse_cli_path_output(combined_output(result))
            logger.info("[P8.0.5] 预铺完成 map=%s pages_dir=%s", page_map, pages_dir)
            return {
                "pages_seeded": True,
                "seed_skipped_reason": "",
                "page_template_map": page_map,
                "pages_dir": pages_dir or str(inputs.get("pages_dir") or ""),
            }
        except BashExecError as exc:
            logger.error("[P8.0.5] ensure-output-dir 失败: %s", exc)
            return {
                "pages_seeded": False,
                "seed_skipped_reason": str(exc),
                "page_template_map": page_map,
            }


class DesignerTasksNode(PlanNode):
    """P8.0.6 — 生成 slide-designer-common 与 page-N-task.md。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p8_0_6_designer_tasks",
            instruction="generate-slide-designer-tasks CLI",
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        style_mode = str(inputs.get("style_mode") or "").strip()
        if not should_seed_pages(style_mode):
            return {"designer_tasks_ok": False, "designer_tasks_skipped": True}

        pptx_root = str(inputs.get("pptx_root") or "").strip()
        output_dir = str(inputs.get("output_dir") or "").strip()
        style_file_path = str(inputs.get("style_file_path") or "").strip()
        style_id = str(inputs.get("style_id") or "").strip()
        outline_path = str(inputs.get("outline_path") or f"{output_dir}/outline.md").strip()
        image_map_path = str(inputs.get("image_map_path") or "").strip()
        total_pages = int(inputs.get("total_pages") or 0)
        outline_pages: dict[int, str] = inputs.get("outline_pages") or {}

        if not all((pptx_root, output_dir, style_file_path)):
            logger.warning("[P8.0.6] 缺少必填路径，跳过 designer tasks")
            return {"designer_tasks_ok": False, "designer_tasks_skipped": True}

        skill_root = resolve_skill_root(pptx_root)
        cmd = (
            f"{cli_path('generate-slide-designer-tasks', pptx_root)} "
            f"--outline {quote_path(outline_path)} "
            f"--output-dir {quote_path(output_dir)} "
            f"--skill-root {quote_path(skill_root)} "
            f"--style {quote_path(style_file_path)}"
        )
        if style_mode == "custom" or style_id == "custom":
            template_dir = resolve_template_dir(pptx_root, style_mode, style_id)
            cmd += f" --template-dir {quote_path(template_dir)}"
        if image_map_path:
            cmd += f" --image-map {quote_path(image_map_path)}"

        try:
            result = await run_bash(
                self,
                cmd,
                timeout_seconds=120,
                required=False,
                workdir=pptx_root,
            )
            if result.exit_code != 0:
                detail = combined_output(result)
                logger.warning(
                    "[P8.0.6] designer tasks 失败（不阻塞） exit=%d: %s",
                    result.exit_code,
                    detail,
                )
                return {"designer_tasks_ok": False, "designer_tasks_skipped": False}

            missing_tasks: list[int] = []
            if total_pages > 0:
                out_path = Path(output_dir)
                for page_num in range(1, total_pages + 1):
                    outline_page = outline_pages.get(page_num, "")
                    page_type = detect_page_type(outline_page)
                    if page_type in _STRUCTURAL_TYPES:
                        continue
                    task_path = out_path / f"page-{page_num}-task.md"
                    if not task_path.is_file() or task_path.stat().st_size == 0:
                        missing_tasks.append(page_num)
            if missing_tasks:
                logger.warning(
                    "[P8.0.6] 缺失 page-N-task.md pages=%s",
                    missing_tasks,
                )

            logger.info("[P8.0.6] designer tasks 生成完成")
            return {
                "designer_tasks_ok": True,
                "designer_tasks_skipped": False,
                "designer_tasks_missing_pages": missing_tasks,
            }
        except BashExecError as exc:
            logger.warning("[P8.0.6] designer tasks 失败（不阻塞）: %s", exc)
            return {"designer_tasks_ok": False, "designer_tasks_skipped": False}

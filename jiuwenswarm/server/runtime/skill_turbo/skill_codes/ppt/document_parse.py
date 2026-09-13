from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
    PptCommon,
    _strip_line_numbers,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    cli_path,
    quote_path,
    run_bash,
)

logger = logging.getLogger(__name__)

_collect_user_text = PptCommon.collect_user_text

_DOC_RAW_NAME = "doc_raw.md"
_DOC_SUMMARY_NAME = "doc_summary.md"
_DOC_MANIFEST_NAME = "doc_manifest.json"
_MATERIAL_MAP_NAME = "source_material_map.json"
_SOURCE_ASSETS_DIRNAME = "source_assets"
_MAX_PARSE_ATTEMPTS = 2
_DOC_SUMMARY_MAX_CHARS = 8000
_VISION_PROBE_REL = Path("references") / "assets" / "vision-probe.png"

_TOPIC_LLM_SYSTEM_PROMPT = """你是 PPT 主题推断助手。根据用户请求与文档摘要，推断最适合作为演示文稿标题的主题。

规则：
1. 主题应简洁（通常 6~30 字），适合作为 PPT 标题。
2. 仅根据文档摘要与用户请求推断，不要编造文档中不存在的主线。
3. 若文档内容过少、过于杂乱或无法判断合适主题，topic 必须为空字符串。
4. 用户已在请求中明确给出主题时，优先采用用户主题（可略作规范化）。

必须只输出 JSON：
{"topic": "推断的主题或空字符串"}"""


class DocumentParseError(RuntimeError):
    """P3 文档解析失败。"""


def _normalize_tool_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return _strip_line_numbers(result)
    if isinstance(result, dict):
        for key in ("content", "output", "result", "stdout", "text", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_line_numbers(value)
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("ocr_text", "answer", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return _strip_line_numbers(value)
        if result.get("success") is False:
            error = result.get("error") or result.get("message")
            if isinstance(error, str):
                return f"[ERROR]: {error}"
    if hasattr(result, "data"):
        data_attr = result.data
        if isinstance(data_attr, dict):
            for key in ("content", "ocr_text", "answer", "text", "output", "result"):
                value = data_attr.get(key)
                if isinstance(value, str) and value.strip():
                    return _strip_line_numbers(value)
        if isinstance(data_attr, str) and data_attr.strip():
            return _strip_line_numbers(data_attr)
    return _strip_line_numbers(str(result))


def _user_has_topic(inputs: dict[str, Any]) -> bool:
    topic = inputs.get("topic")
    return isinstance(topic, str) and bool(topic.strip())


def _build_topic_inference_prompt(user_text: str, doc_excerpt: str) -> str:
    parts = ["请根据以下信息推断 PPT 主题。\n"]
    if user_text:
        parts.append(f"用户请求：\n{user_text}\n")
    if doc_excerpt:
        parts.append(f"文档摘要：\n{doc_excerpt}\n")
    parts.append('只输出 JSON：{"topic":"..."}')
    return "\n".join(parts)


def _parse_topic_from_llm_response(raw: str) -> str:
    payload = PptCommon.parse_json_payload(raw)
    if isinstance(payload, dict):
        topic = payload.get("topic")
        if isinstance(topic, str):
            return topic.strip()
    return ""


def _filter_parseable_paths(doc_paths: list[str]) -> list[str]:
    """过滤演示文稿路径；parse-docs 不处理 .ppt/.pptx。"""
    return [p for p in doc_paths if not PptCommon.is_presentation_path(p)]


def _manifest_converted_count(manifest: dict[str, Any]) -> int:
    docs = manifest.get("documents") or manifest.get("files") or manifest.get("items")
    if isinstance(docs, list):
        count = 0
        for item in docs:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status == "converted":
                count += 1
        return count
    # 兼容聚合计数
    for key in ("converted", "convertedCount", "converted_count"):
        value = manifest.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


class DocumentParseNode(PlanNode):
    """P3 — 条件文档解析：调用 parse-docs，产出 summary / raw / manifest。

    主路径：``node …/cli.js parse-docs --output-dir {output_dir} [--extract-images] <docs>``
    仅当 VQA 探针成功时加 ``--extract-images``。
    对齐 content-material.md 门禁 #7：CLI 两轮失败后不得 read_file 降级，素材通道直接失败。
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p3_document_parse",
            instruction=(
                "## P3 条件文档解析（parse-docs）\n"
                "\n"
                "### 节点职责\n"
                "1. has_documents=True 时调用 pptx-craft CLI `parse-docs`\n"
                "2. 验收 doc_summary.md / doc_raw.md / doc_manifest.json\n"
                "3. VQA 探针成功才加 --extract-images\n"
                "4. 用户未指定 topic 时，基于 doc_summary 推断主题\n"
                "\n"
                "### 铁律\n"
                "- 禁止用 read_file 打开用户文档冒充主解析（含 CLI 失败后的降级直读）\n"
                "- parse-docs 重试 1 次仍失败：doc_parse_ok=False 并报告，不得手写 manifest\n"
                "- 不得把完整 doc_raw 写进下游 prompt 变量；默认只消费 summary\n"
                "\n"
                "### 输出\n"
                "- doc_raw_path / doc_summary_path / doc_manifest_path\n"
                "- material_map_path / source_assets_dir（可选）\n"
                "- doc_parse_ok / vision_verified / images_extracted / parse_degraded\n"
                "- topic / topic_inferred\n"
            ),
        )

    def _artifact_paths(self, output_dir: Path) -> dict[str, Path]:
        return {
            "summary": output_dir / _DOC_SUMMARY_NAME,
            "raw": output_dir / _DOC_RAW_NAME,
            "manifest": output_dir / _DOC_MANIFEST_NAME,
            "material_map": output_dir / _MATERIAL_MAP_NAME,
            "source_assets": output_dir / _SOURCE_ASSETS_DIRNAME,
        }

    async def _probe_vision(self, inputs: dict[str, Any]) -> bool:
        """对 skill 包内 vision-probe.png 做一次 VQA；失败则不加 --extract-images。"""
        if not self.has_tool("visual_question_answering"):
            logger.info("[P3] visual_question_answering 不可用，跳过提图")
            return False
        pptx_root = str(inputs.get("pptx_root") or "").strip()
        if not pptx_root:
            return False
        probe = Path(pptx_root) / _VISION_PROBE_REL
        if not probe.is_file():
            logger.info("[P3] vision-probe.png 不存在: %s", probe)
            return False
        try:
            raw = await self.call_tool(
                "visual_question_answering",
                image_path_or_url=str(probe),
                question="Describe this image in one short sentence.",
            )
            text = _normalize_tool_text(raw).strip()
            if text and not text.startswith("[ERROR]"):
                return True
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            logger.warning("[P3] VQA 探针失败: %s", exc)
        return False

    async def _run_parse_docs(
        self,
        inputs: dict[str, Any],
        doc_paths: list[str],
        *,
        extract_images: bool,
    ) -> None:
        pptx_root = str(inputs.get("pptx_root") or "").strip()
        output_dir = str(inputs.get("output_dir") or "").strip()
        if not pptx_root or not output_dir:
            raise DocumentParseError("缺少 pptx_root 或 output_dir，无法执行 parse-docs")

        path_args = " ".join(quote_path(p) for p in doc_paths)
        extract_flag = " --extract-images" if extract_images else ""
        cmd = (
            f"{cli_path('parse-docs', pptx_root)}"
            f" --output-dir {quote_path(output_dir)}"
            f"{extract_flag} {path_args}"
        )
        await run_bash(
            self,
            cmd,
            timeout_seconds=600,
            required=True,
            workdir=pptx_root,
        )

    async def _read_manifest(self, manifest_path: Path) -> dict[str, Any]:
        text = await PptCommon.read_file(
            self,
            manifest_path,
            required=False,
            label=_DOC_MANIFEST_NAME,
            error_type=DocumentParseError,
        )
        if not text.strip():
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _validate_artifacts(
        self,
        paths: dict[str, Path],
        *,
        expect_images: bool,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        missing: list[str] = []
        required_paths = (
            ("doc_summary.md", paths["summary"]),
            ("doc_raw.md", paths["raw"]),
            ("doc_manifest.json", paths["manifest"]),
        )
        for name, path in required_paths:
            if not path.is_file():
                missing.append(name)
        if missing:
            return False, f"缺少产物: {', '.join(missing)}", {}

        summary = await PptCommon.read_file(
            self,
            paths["summary"],
            required=True,
            label=_DOC_SUMMARY_NAME,
            error_type=DocumentParseError,
        )
        raw = await PptCommon.read_file(
            self,
            paths["raw"],
            required=True,
            label=_DOC_RAW_NAME,
            error_type=DocumentParseError,
        )
        if not summary.strip() and not raw.strip():
            return False, "doc_summary.md 与 doc_raw.md 均为空", {}

        manifest = await self._read_manifest(paths["manifest"])
        if _manifest_converted_count(manifest) < 1 and not manifest.get("degraded"):
            # 部分 CLI 版本可能用不同 schema；文件非空时放行并记警告
            if not raw.strip():
                return False, "manifest 无 converted 文档且 doc_raw 为空", manifest

        if expect_images:
            if not paths["material_map"].is_file():
                return False, "已请求 --extract-images 但缺少 source_material_map.json", manifest

        return True, None, manifest

    def _apply_paths_to_inputs(
        self,
        inputs: dict[str, Any],
        paths: dict[str, Path],
        *,
        vision_verified: bool,
        images_extracted: bool,
        parse_degraded: bool,
        summary_text: str,
    ) -> None:
        inputs["doc_raw_path"] = str(paths["raw"])
        inputs["doc_summary_path"] = str(paths["summary"])
        inputs["doc_manifest_path"] = str(paths["manifest"])
        inputs["doc_summary"] = summary_text[:_DOC_SUMMARY_MAX_CHARS]
        inputs["vision_verified"] = vision_verified
        inputs["images_extracted"] = images_extracted
        inputs["parse_degraded"] = parse_degraded
        if images_extracted and paths["material_map"].is_file():
            inputs["material_map_path"] = str(paths["material_map"])
        else:
            inputs["material_map_path"] = ""
        if paths["source_assets"].is_dir():
            inputs["source_assets_dir"] = str(paths["source_assets"])
        else:
            inputs["source_assets_dir"] = ""

    async def _infer_topic_from_summary(
        self,
        inputs: dict[str, Any],
        summary_path: Path,
    ) -> str:
        user_text = _collect_user_text(inputs)
        doc_excerpt = await PptCommon.read_file(
            self,
            summary_path,
            max_chars=_DOC_SUMMARY_MAX_CHARS,
            error_type=DocumentParseError,
        )
        if not user_text and not doc_excerpt:
            return ""
        response = await self.stream_llm_collect(
            _build_topic_inference_prompt(user_text, doc_excerpt),
            system_prompt=_TOPIC_LLM_SYSTEM_PROMPT,
        )
        return _parse_topic_from_llm_response(response)

    def _clear_topic_if_needed(self, inputs: dict[str, Any]) -> None:
        if not _user_has_topic(inputs):
            inputs["topic"] = ""
            inputs["topic_inferred"] = False

    async def _parse_with_retry(
        self,
        inputs: dict[str, Any],
        doc_paths: list[str],
        paths: dict[str, Path],
    ) -> tuple[bool, str | None]:
        vision_verified = await self._probe_vision(inputs)
        inputs["vision_verified"] = vision_verified
        last_error: str | None = None

        for attempt in range(_MAX_PARSE_ATTEMPTS):
            if attempt:
                inputs["failure_reason"] = last_error or "parse-docs 产物无效"
            try:
                await self._run_parse_docs(
                    inputs,
                    doc_paths,
                    extract_images=vision_verified,
                )
                ok, err, _manifest = await self._validate_artifacts(
                    paths, expect_images=vision_verified
                )
                if ok:
                    inputs["parse_degraded"] = False
                    inputs["images_extracted"] = bool(
                        vision_verified and paths["material_map"].is_file()
                    )
                    return True, None
                last_error = err or "parse-docs 验收失败"
            except BashExecError as exc:
                last_error = str(exc)
                logger.warning("[P3] parse-docs 失败 (attempt=%d): %s", attempt + 1, exc)
            except DocumentParseError as exc:
                last_error = str(exc)
            except Exception as exc:
                if isinstance(exc, AbortError):
                    raise
                last_error = f"{type(exc).__name__}: {exc}"

        # content-material.md 门禁 #7：不得降级为 read_file 直读；素材通道直接失败。
        inputs["parse_degraded"] = False
        logger.error(
            "[P3] parse-docs 两轮失败，按契约停止素材通道（不 read_file 降级）: %s",
            last_error or "parse-docs 失败",
        )
        return False, last_error or "parse-docs 失败"

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        PptCommon.ensure_phase1_defaults(inputs)
        if not inputs.get("has_documents"):
            inputs["doc_parse_ok"] = False
            inputs["doc_parse_error"] = None
            self._clear_topic_if_needed(inputs)
            return inputs

        output_dir_raw = inputs.get("output_dir")
        doc_paths_raw = inputs.get("doc_paths")
        if not output_dir_raw:
            inputs["doc_parse_ok"] = False
            inputs["doc_parse_error"] = "缺少 output_dir"
            self._clear_topic_if_needed(inputs)
            return inputs
        if not isinstance(doc_paths_raw, list) or not doc_paths_raw:
            inputs["doc_parse_ok"] = False
            inputs["doc_parse_error"] = "缺少 doc_paths"
            self._clear_topic_if_needed(inputs)
            return inputs

        doc_paths = _filter_parseable_paths([str(p) for p in doc_paths_raw])
        if not doc_paths:
            inputs["doc_parse_ok"] = False
            inputs["doc_parse_error"] = "无可解析文档（演示文稿不进入 parse-docs）"
            inputs["has_documents"] = False
            self._clear_topic_if_needed(inputs)
            PptCommon.apply_content_branch(inputs)
            return inputs

        output_dir = Path(str(output_dir_raw)).expanduser().resolve()
        paths = self._artifact_paths(output_dir)
        ok, error = await self._parse_with_retry(inputs, doc_paths, paths)

        summary_text = ""
        if ok:
            summary_text = await PptCommon.read_file(
                self,
                paths["summary"],
                max_chars=_DOC_SUMMARY_MAX_CHARS,
                error_type=DocumentParseError,
            )

        self._apply_paths_to_inputs(
            inputs,
            paths,
            vision_verified=bool(inputs.get("vision_verified")),
            images_extracted=bool(inputs.get("images_extracted")),
            parse_degraded=bool(inputs.get("parse_degraded")),
            summary_text=summary_text,
        )
        inputs["doc_parse_ok"] = ok
        inputs["doc_parse_error"] = error if not ok else None

        if ok and not _user_has_topic(inputs):
            inferred = await self._infer_topic_from_summary(inputs, paths["summary"])
            if inferred:
                inputs["topic"] = inferred
                inputs["topic_inferred"] = True
            else:
                inputs["topic"] = ""
                inputs["topic_inferred"] = False
        elif not _user_has_topic(inputs):
            inputs["topic"] = ""
            inputs["topic_inferred"] = False

        PptCommon.apply_content_branch(inputs)

        if ok:
            files = [
                {"path": inputs["doc_summary_path"], "desc": "文档摘要"},
                {"path": inputs["doc_raw_path"], "desc": "解析后文档原文"},
                {"path": inputs["doc_manifest_path"], "desc": "解析清单"},
            ]
            if inputs.get("material_map_path"):
                files.append(
                    {"path": inputs["material_map_path"], "desc": "文档内嵌图映射"}
                )
            inputs["__artifact__"] = {
                "files": files,
                "info": {
                    "topic_inferred": inputs.get("topic_inferred", False),
                    "parse_degraded": inputs.get("parse_degraded", False),
                    "images_extracted": inputs.get("images_extracted", False),
                },
            }
        return inputs

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if not inputs.get("has_documents"):
            result = await self._execute(inputs)
            yield {
                **result,
                "node": self.plan_name,
                "status": "ok",
                "message": "未检测到待解析文档，跳过文档解析",
            }
            return

        doc_paths = inputs.get("doc_paths")
        doc_count = len(doc_paths) if isinstance(doc_paths, list) else 0
        yield {
            "node": self.plan_name,
            "status": "progress",
            "message": f"开始 parse-docs 解析 {doc_count} 个文档...",
        }

        result = await self._execute(inputs)
        status = "ok" if result.get("doc_parse_ok") else "warning"
        if result.get("doc_parse_ok"):
            message = "文档解析完成（parse-docs）"
        else:
            message = f"文档解析未完成：{result.get('doc_parse_error') or '未知原因'}"
        yield {
            **result,
            "node": self.plan_name,
            "status": status,
            "message": message,
        }

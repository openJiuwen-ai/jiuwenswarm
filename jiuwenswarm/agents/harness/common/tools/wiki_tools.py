# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any, List, Optional, Dict
from pathlib import Path
import datetime
import hashlib
import json
import shutil
import logging

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import Tool, ToolCard, McpServerConfig, tool
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.core.session.agent import Session
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import SysOperationRail
from openjiuwen.harness.schema.config import SubAgentConfig
from jiuwenswarm.common.config import get_default_models
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.common.utils import get_agent_workspace_dir


logger = logging.getLogger(__name__)

# Constants for wiki workspace resolution
DEFAULT_WIKI_DIR = ".llm_wiki"

DEFAULT_WIKI_AGENT_SYSTEM_PROMPT_EN = (
    "You are an LLM Wiki Maintainer. You manage a workspace consisting of three primary directories:\n"
    "1. `sources/`: where immutable raw text and PDF files are placed.\n"
    "2. `wiki/`: the destination for compiled, structured markdown entity pages.\n"
    "3. `schema/`: contains an `AGENT.md` document defining the architecture and rules.\n\n"
    "CRITICAL RULES:\n"
    "- Always read `schema/AGENT.md` first to understand operational rules.\n"
    "- Always read `wiki/index.md` before making modifications to ensure you append new items properly.\n"
    "- You MUST update `wiki/log.md` with an entry describing every major action or ingestion you perform!\n"
    "- To ingest PDFs, use the `read_pdf` tool. For large documents, start by reading the FIRST page to"
    " understand the structure and total page count. Then, read subsequent pages in chunks as needed to"
    " ensure complete synthesis without exceeding your context limit.\n"
    "- Prioritize ingesting new, unprocessed files from `sources/` before performing secondary cross-linking.\n"
    "- When calling tools (especially `edit_file`), you MUST use the exact parameter names defined in the"
    " tool schema (e.g., `old_string`, `new_string`). Do not append symbols like `=` to keys, and do not provide lists"
    " or indices as string values."
    " Only use `replace_all: true` if you are providing the COMPLETE new content for a section.\n"
    "- DO NOT create subdirectories within `wiki/`. Always save pages directly in the `wiki/` root folder."
)

DEFAULT_WIKI_AGENT_SYSTEM_PROMPT_CN = (
    "你是 LLM Wiki 维护者。你负责管理一个包含三个主要目录的工作区：\n"
    "1. `sources/`：存放不可变的原始文档和 PDF。\n"
    "2. `wiki/`：存放编译后结构化的 markdown 主题页面。\n"
    "3. `schema/`：包含一个 `AGENT.md` 文档，定义了架构和操作规则。\n\n"
    "关键规则：\n"
    "- 务必首先读取 `schema/AGENT.md`，以了解操作规范。\n"
    "- 在进行任何修改之前，务必先读取 `wiki/index.md` 和 `wiki/log.md`，以确保正确追加新项并维护一致的交叉引用。\n"
    "- 对于 PDF 摄取，请使用 `read_pdf` 工具。对于大型文档，请先阅读第一页以了解结构和总页数，然后根据需要分块阅读后续页面，以确保在不超出上下文限制的情况下完成综合。\n"
    "- 在对现有 `wiki/` 页面进行二次交叉引用或 lint 之前，优先从 `sources/` 摄取新的、未经处理的文件。\n"
    "- 调用工具（特别是 `edit_file`）时，必须使用工具架构中确化的准确参数名称（例如 `old_string`、`new_string`）。不要在键名后添加 `=` 等符号，也不要提供列表或索引作为字符串值。"
    "仅当你为某个部分提供完整的全新内容时，才使用 `replace_all: true`。\n"
    "- 不要在 `wiki/` 中创建子目录。始终将页面直接保存在 `wiki/` 根文件夹中。"
)

DEFAULT_WIKI_AGENT_SYSTEM_PROMPT: Dict[str, str] = {
    "cn": DEFAULT_WIKI_AGENT_SYSTEM_PROMPT_CN,
    "en": DEFAULT_WIKI_AGENT_SYSTEM_PROMPT_EN,
}

DEFAULT_WIKI_AGENT_DESCRIPTION_EN = (
    "You are a Wiki Maintainer agent."
    " You ingest raw documents and continuously compile them into a structured markdown wiki knowledge base."
)

DEFAULT_WIKI_AGENT_DESCRIPTION_CN = (
    "你是 Wiki 维护代理。负责摄取原始文档，并不断将它们编译成结构化的 markdown Wiki 知识库。"
)

DEFAULT_WIKI_AGENT_DESCRIPTION: Dict[str, str] = {
    "cn": DEFAULT_WIKI_AGENT_DESCRIPTION_CN,
    "en": DEFAULT_WIKI_AGENT_DESCRIPTION_EN,
}


class _SourceManifest:
    """Tracks ingested sources by SHA-256 content hash."""

    _MANIFEST_NAME = "manifest.json"

    def __init__(self, sources_dir: Path, sys_operation: Optional[SysOperation] = None) -> None:
        self._path = sources_dir / self._MANIFEST_NAME
        self._sys_op = sys_operation
        self._data: Dict[str, Dict[str, str]] = {}

    async def ensure_initialized(self) -> None:
        self._data = await self._load()

    @staticmethod
    def sha256_of(file_path: Path) -> str:
        if not file_path.is_file():
            raise ValueError(f"Path is not a valid file: {file_path}")
        h = hashlib.sha256()
        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def is_known(self, sha256: str) -> bool:
        return sha256 in self._data

    def get(self, sha256: str) -> Optional[Dict[str, str]]:
        return self._data.get(sha256)

    async def record(self, sha256: str, name: str, destination: Path) -> None:
        if sha256 in self._data:
            return
        self._data[sha256] = {
            "name": name,
            "destination": str(destination),
            "ingested_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
        }
        await self._save()

    def all_entries(self) -> Dict[str, Dict[str, str]]:
        return dict(self._data)

    async def _load(self) -> Dict[str, Dict[str, str]]:
        if self._sys_op:
            res = await self._sys_op.fs().read_file(str(self._path))
            if res.code == 0:
                try:
                    return json.loads(res.data.content)
                except json.JSONDecodeError:
                    return {}
            return {}
        else:
            if not self._path.exists():
                return {}
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}

    async def _save(self) -> None:
        if not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)

        content = json.dumps(self._data, indent=2, ensure_ascii=False)
        if self._sys_op:
            await self._sys_op.fs().write_file(str(self._path), content)
        else:
            self._path.write_text(content, encoding="utf-8")


class LLMWiki:
    def __init__(
        self,
        workspace: str | Path,
        model: Model,
        *,
        session_id: Optional[str] = None,
        card: Optional[AgentCard] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[List[Tool | ToolCard]] = None,
        mcps: Optional[List[McpServerConfig]] = None,
        subagents: Optional[List[SubAgentConfig | DeepAgent]] = None,
        rails: Optional[List[AgentRail]] = None,
        enable_task_loop: bool = True,
        max_iterations: int = 15,
        skills: Optional[List[str]] = None,
        backend: Optional[Any] = None,
        sys_operation: Optional[SysOperation] = None,
        language: Optional[str] = None,
        prompt_mode: Optional[str] = None,
        **config_kwargs: Any,
    ):
        self.workspace = Path(workspace)
        self.sources_dir = self.workspace / "sources"
        self.wiki_dir = self.workspace / "wiki"
        self.schema_dir = self.workspace / "schema"
        self._sys_op = sys_operation

        self._manifest = _SourceManifest(self.sources_dir, sys_operation=self._sys_op)

        resolved_language = resolve_language(language)
        final_card = card or AgentCard(
            name="wiki_agent",
            description=DEFAULT_WIKI_AGENT_DESCRIPTION.get(
                resolved_language, DEFAULT_WIKI_AGENT_DESCRIPTION["cn"]
            ),
        )
        final_prompt = system_prompt or DEFAULT_WIKI_AGENT_SYSTEM_PROMPT.get(
            resolved_language, DEFAULT_WIKI_AGENT_SYSTEM_PROMPT["cn"]
        )
        final_tools = tools if tools is not None else []
        final_rails = rails if rails is not None else [SysOperationRail()]

        self.agent = create_deep_agent(
            model=model,
            card=final_card,
            system_prompt=final_prompt,
            tools=final_tools,
            mcps=mcps,
            subagents=subagents,
            rails=final_rails,
            enable_task_loop=enable_task_loop,
            max_iterations=max_iterations,
            workspace=str(self.workspace),
            skills=skills,
            backend=backend,
            sys_operation=sys_operation,
            language=resolved_language,
            prompt_mode=prompt_mode,
            **config_kwargs,
        )

        _sid = session_id or hashlib.sha256(str(self.workspace.resolve()).encode()).hexdigest()[:16]
        self._session_id: str = _sid
        self._session: Session = Session(session_id=_sid)

    async def ensure_initialized(self):
        for d in [self.sources_dir, self.wiki_dir, self.schema_dir]:
            d.mkdir(parents=True, exist_ok=True)

        await self._manifest.ensure_initialized()

        schema_file = self.schema_dir / "AGENT.md"
        schema_content = (
            "# Wiki Maintainer Rules\n"
            "1. Never modify files inside `sources/`.\n"
            "2. All pages you generate MUST be saved directly inside the `wiki/` directory root.\n"
            "3. You must maintain a `wiki/index.md` listing all topics.\n"
            "4. You must maintain a `wiki/log.md` with an append-only timeline of ingestions.\n"
            "5. Break concepts down into modular topic pages.\n"
            "6. Make heavy use of markdown links to interconnect pages within `wiki/`.\n"
            "7. DO NOT create subdirectories (like `wiki/entity/`). Save all files in the `wiki/` root.\n"
        )
        if self._sys_op:
            res = await self._sys_op.fs().read_file(str(schema_file))
            if res.code != 0:
                await self._sys_op.fs().write_file(
                    str(schema_file), schema_content, create_if_not_exist=True
                )
        else:
            if not schema_file.exists():
                schema_file.write_text(schema_content)

        index_file = self.wiki_dir / "index.md"
        index_content = (
            "# Wiki Index\n\n"
            "This index lists all topics covered in the wiki.\n\n"
            "## Entities\n\n"
            "<!-- entity pages go here, one bullet per page -->\n\n"
            "## Concepts\n\n"
            "<!-- concept pages go here -->\n\n"
            "## Sources\n\n"
            "<!-- one bullet per ingested source, link to its summary page -->\n"
        )
        if self._sys_op:
            res = await self._sys_op.fs().read_file(str(index_file))
            if res.code != 0:
                await self._sys_op.fs().write_file(
                    str(index_file), index_content, create_if_not_exist=True
                )
        else:
            if not index_file.exists():
                index_file.write_text(index_content)

        log_file = self.wiki_dir / "log.md"
        log_content = (
            "# Wiki Log\n\n"
            "Append-only timeline of all wiki operations.\n"
            "Each entry starts with `## [YYYY-MM-DD] <operation> | <title>`\n"
            "so it is grep-parseable:\n"
            "<!-- append new entries below this line -->\n"
        )
        if self._sys_op:
            res = await self._sys_op.fs().read_file(str(log_file))
            if res.code != 0:
                await self._sys_op.fs().write_file(
                    str(log_file), log_content, create_if_not_exist=True
                )
        else:
            if not log_file.exists():
                log_file.write_text(log_content)

        await self.agent.ensure_initialized()

    def list_sources(self) -> Dict[str, Dict[str, str]]:
        return self._manifest.all_entries()

    async def ingest(self, source_path: str | Path, *, force: bool = False) -> Dict[str, Any]:
        source_path = Path(source_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        sha256 = _SourceManifest.sha256_of(source_path)
        if not force and self._manifest.is_known(sha256):
            entry = self._manifest.get(sha256)
            logger.warning("Skipping already-ingested source '%s'", source_path.name)
            return {
                "output": "Skipped duplicate source",
                "skipped": True,
                "sha256": sha256,
                "ingested_at": entry["ingested_at"],
            }

        destination = self.sources_dir / f"{sha256[:8]}_{source_path.name}"
        if source_path.resolve() != destination.resolve():
            shutil.copy2(source_path, destination)

        query = (
            f"Read the rules in your `schema/AGENT.md`."
            f" Process the new raw source document '{destination.name}' inside `sources/` into the wiki."
            f" CRITICAL: You MUST read `wiki/index.md` and other relevant `.md` files to discover existing topics."
            f" Actively interconnect them by adding deep Markdown cross-links."
            f" FINALLY, you MUST append a detailed summary of what knowledge you extracted into `wiki/log.md`!"
        )
        result = await self.agent.invoke({"query": query}, session=self._session)

        if "error" in result or str(result.get("output", "")).startswith("[ERROR"):
            # Do not record the hash if the agent explicitly failed!
            return result

        await self._manifest.record(sha256=sha256, name=source_path.name, destination=destination)
        return result

    async def query(self, question: str) -> Dict[str, Any]:
        query = (
            f"Answer this strictly based on `wiki/`: '{question}'."
            f" If your answer forms a valuable new insight, write it back into the wiki."
        )
        return await self.agent.invoke({"query": query}, session=self._session)

    async def lint(self) -> Dict[str, Any]:
        query = (
            "Health-check the `wiki/` directory."
            " Actively evaluate all topic pages to discover orphaned documents or related, un-linked concepts."
            " You MUST proactively use your file editing tools to forge new Markdown cross-links between related"
            " pages, transforming isolated files into a highly connected knowledge graph."
            " Report the links you established."
            " FINALLY, you MUST append a detailed summary of the repairs and link established into `wiki/log.md`!"
        )
        return await self.agent.invoke({"query": query}, session=self._session)


def _get_default_model() -> Model:
    defaults = get_default_models()
    model_conf = next((m for m in defaults if m.get("is_default")), defaults[0] if defaults else {})

    client_config = model_conf.get("model_client_config", {})
    req_config = model_conf.get("model_config_obj", {})

    if client_config.get("custom_headers") == "":
        del client_config["custom_headers"]

    # Provide safe fallbacks for required fields to prevent ValidationError
    client_config.setdefault("api_key", "")
    client_config.setdefault("api_base", "")
    client_config.setdefault("client_provider", "OpenAI")
    client_config.setdefault("model_name", "default")
    model_name = req_config.get("model") or client_config["model_name"]
    req_config.setdefault("model", model_name)

    runtime_req_config = build_reasoning_model_request_kwargs(
        model_client_config=client_config,
        model_config_obj=req_config,
        model_name=model_name,
    )

    return Model(
        model_client_config=ModelClientConfig(**client_config),
        model_config=ModelRequestConfig(**runtime_req_config),
    )


def _resolve_workspace(workspace: str) -> Path:
    """
    Ensure workspace strings are safely mapped into the central or target workspace as absolute paths.
    """
    cleaned_workspace = (workspace or "").strip()
    base_dir = get_agent_workspace_dir()

    if not cleaned_workspace or cleaned_workspace == DEFAULT_WIKI_DIR:
        return (base_dir / DEFAULT_WIKI_DIR).resolve()

    workspace_path = Path(cleaned_workspace).expanduser()
    if not workspace_path.is_absolute():
        workspace_path = base_dir / workspace_path

    if workspace_path.name != DEFAULT_WIKI_DIR:
        workspace_path = workspace_path / DEFAULT_WIKI_DIR

    return workspace_path.resolve()


@tool(
    name="wiki_ingest",
    description="Ingest a source file (PDF, TXT, MD) or a directory into the LLM Wiki."
    " By default, identical files (by SHA-256) are skipped perfectly (deduplication)."
    " Set `force=True` if the user explicitly asks to re-ingest, rebuild, or force the ingestion."
    " The `workspace` parameter is the root folder where the `.llm_wiki` will be created."
    " If the user specifies a target directory like 'wiki_lib' or 'bibliography', pass that exact path as `workspace`.",
)
async def wiki_ingest(
    source: str,
    workspace: str = "",
    force: bool = False,
    sys_operation: Optional[SysOperation] = None,
) -> str:
    """Ingests a file or directory of files into the LLM Wiki."""
    try:
        model = _get_default_model()
        final_workspace = _resolve_workspace(workspace)
        wiki = LLMWiki(workspace=str(final_workspace), model=model, sys_operation=sys_operation)
        await wiki.ensure_initialized()

        src_path = Path(source).expanduser()
        if not src_path.is_absolute():
            src_path = get_agent_workspace_dir() / src_path

        if not src_path.exists():
            return f"Error: Source {source} not found."

        # Ensure we avoid circular ingestion by skipping the .llm_wiki directory itself
        protected_root = str(final_workspace).lower()

        targets = []
        if src_path.is_dir():
            for ext in (".pdf", ".md", ".txt"):
                for p in src_path.glob(f"**/*{ext}"):
                    posix_path = p.as_posix()
                    # Skip if the file is located inside the .llm_wiki workspace
                    if posix_path.lower().startswith(protected_root):
                        continue
                    targets.append(p)
        else:
            targets.append(src_path)

        all_results = {}
        for file_path in targets:
            res = await wiki.ingest(source_path=file_path, force=force)
            if "error" in res:
                all_results[str(file_path)] = f"[Failed]: {res['error']}"
            elif res.get("skipped"):
                all_results[str(file_path)] = f"[Skipped]: Deduplicated"
            else:
                all_results[str(file_path)] = "[Success]"

        return "Wiki Ingestion Results:\n" + json.dumps(all_results, indent=2)
    except Exception as e:
        return f"Wiki Ingest Error: {str(e)}"


@tool(
    name="wiki_query",
    description="Query the LLM Wiki's compiled knowledge base directly via Natural Language."
    " The `workspace` parameter is the folder containing the `.llm_wiki`."
    " If the user specifies a target directory like 'wiki_lib' or 'bibliography', pass that exact path as `workspace`.",
)
async def wiki_query(
    query: str, workspace: str = "", sys_operation: Optional[SysOperation] = None
) -> str:
    """Queries the LLM Wiki."""
    if not query or not query.strip():
        return "Error: Query cannot be empty."
    try:
        model = _get_default_model()
        final_workspace = _resolve_workspace(workspace)
        if not final_workspace.exists():
            return (
                f"Error: The workspace '{workspace}' does not have an initialized LLM Wiki."
                " You must use 'wiki_ingest' first to create the knowledge base."
            )
        wiki = LLMWiki(workspace=str(final_workspace), model=model, sys_operation=sys_operation)
        await wiki.ensure_initialized()

        result = await wiki.query(question=query)
        if "output" in result:
            return str(result["output"])
        elif "error" in result:
            return f"Error querying wiki: {result['error']}"
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Wiki Query Error: {str(e)}"


@tool(
    name="wiki_lint",
    description="Health-check and trigger automatic repairs of broken links, orphans, or anomalies in the LLM Wiki."
    " The `workspace` parameter is the root folder containing the `.llm_wiki`."
    " If the user specifies a target directory like 'wiki_lib' or 'bibliography', pass that exact path as `workspace`.",
)
async def wiki_lint(workspace: str = "", sys_operation: Optional[SysOperation] = None) -> str:
    """Lints the LLM Wiki."""
    try:
        model = _get_default_model()
        final_workspace = _resolve_workspace(workspace)
        if not final_workspace.exists():
            return (
                f"Error: The workspace '{workspace}' does not have an initialized LLM Wiki."
                " You must use 'wiki_ingest' first to create the knowledge base."
            )
        wiki = LLMWiki(workspace=str(final_workspace), model=model, sys_operation=sys_operation)
        await wiki.ensure_initialized()

        result = await wiki.lint()
        if "output" in result:
            return str(result["output"])
        elif "error" in result:
            return f"Error linting wiki: {result['error']}"
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Wiki Lint Error: {str(e)}"

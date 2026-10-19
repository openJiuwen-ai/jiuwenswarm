# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-tenant DeepResearch configuration and child-process lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Dict

from jiuwenswarm.common.local_env_config import (
    get_local_config,
    read_default_headers_raw,
)

logger = logging.getLogger(__name__)


class DeepResearchManagerClosedError(RuntimeError):
    """Raised when a child is registered after manager shutdown has started."""


class DeepResearchTaskManager:
    """Own DeepResearch child processes for one normalized tenant."""

    def __init__(self, *, service_id: str = "default", agent_id: str = "default"):
        self.service_id = (service_id or "default").strip() or "default"
        self.agent_id = (agent_id or "default").strip() or "default"
        self._processes: dict[str, set[Any]] = {}
        self._closing = False

    def track_process(self, session_id: str, process: Any) -> None:
        """Register a child process in a session-owned set.

        A caller that already spawned ``process`` must immediately stop it when
        :class:`DeepResearchManagerClosedError` is raised.
        """
        if self._closing:
            raise DeepResearchManagerClosedError("deepresearch_manager_closed")
        session_key = session_id or "default"
        self._processes.setdefault(session_key, set()).add(process)

    def untrack_process(self, session_id: str, process: Any) -> None:
        """Forget a child process and drop its empty session bucket."""
        session_key = session_id or "default"
        processes = self._processes.get(session_key)
        if processes is None:
            return
        processes.discard(process)
        if not processes:
            self._processes.pop(session_key, None)

    @staticmethod
    def _process_pid(process: Any) -> int | str | None:
        try:
            pid = getattr(process, "pid", None)
        except Exception:
            return None
        return pid if isinstance(pid, (int, str)) else None

    @classmethod
    def _warn_cleanup_error(
        cls,
        process: Any,
        *,
        stage: str,
        error: BaseException,
    ) -> None:
        logger.warning(
            "[DeepResearchTaskManager] child cleanup failed "
            "stage=%s error_type=%s pid=%s",
            stage,
            type(error).__name__,
            cls._process_pid(process),
        )

    @classmethod
    async def _stop_process(cls, process: Any, *, timeout: float) -> None:
        try:
            if process.returncode is not None:
                return
        except Exception as exc:
            cls._warn_cleanup_error(
                process,
                stage="inspect",
                error=exc,
            )
            return

        try:
            process.terminate()
        except ProcessLookupError:
            return
        except Exception as exc:
            cls._warn_cleanup_error(
                process,
                stage="terminate",
                error=exc,
            )
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return
        except ProcessLookupError:
            return
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            cls._warn_cleanup_error(
                process,
                stage="terminate_wait",
                error=exc,
            )
            return

        try:
            process.kill()
        except ProcessLookupError:
            return
        except Exception as exc:
            cls._warn_cleanup_error(
                process,
                stage="kill",
                error=exc,
            )
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except ProcessLookupError:
            return
        except asyncio.TimeoutError as exc:
            cls._warn_cleanup_error(
                process,
                stage="kill_wait",
                error=exc,
            )
        except Exception as exc:
            cls._warn_cleanup_error(
                process,
                stage="kill_wait",
                error=exc,
            )

    @classmethod
    async def _cleanup_processes(
        cls,
        processes: tuple[Any, ...],
        *,
        timeout: float,
    ) -> None:
        results = await asyncio.gather(
            *(cls._stop_process(process, timeout=timeout) for process in processes),
            return_exceptions=True,
        )
        for process, result in zip(processes, results):
            if isinstance(result, BaseException):
                cls._warn_cleanup_error(
                    process,
                    stage="cleanup_task",
                    error=result,
                )

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Boundedly stop the current snapshot; shutdown is permanently closing."""
        self._closing = True
        processes = tuple(
            dict.fromkeys(
                process
                for session_processes in self._processes.values()
                for process in session_processes
            )
        )
        self._processes.clear()
        cleanup_task = asyncio.create_task(
            self._cleanup_processes(processes, timeout=timeout)
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                cleanup_task.result()
            except BaseException as exc:
                self._warn_cleanup_error(
                    None,
                    stage="cleanup_task",
                    error=exc,
                )
            raise

    @staticmethod
    def _read_config_value(
        name: str,
        default: str = "",
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Read an explicit snapshot, or active tenant-aware configuration."""
        if env is not None:
            return str(env.get(name, default) or default)
        return str(get_local_config(name, default) or default)

    @staticmethod
    def _resolve_petal_search_url(env: Mapping[str, str] | None = None) -> str:
        """Build Petal Search URL from LLM API_BASE."""

        def read(name: str, default: str = "") -> str:
            return DeepResearchTaskManager._read_config_value(name, default, env)

        petal_api_url = read("PETAL_API_URL").strip()
        if petal_api_url:
            return petal_api_url
        api_base = (
            read("API_BASE")
            or read("OPENAI_BASE_URL")
            or read("OPENAI_API_BASE")
            or ""
        )
        if isinstance(api_base, str):
            api_base = api_base.strip()
        else:
            api_base = str(api_base or "").strip()
        if not api_base:
            return ""
        trimmed = api_base.rstrip("/")
        if trimmed.lower().endswith("/v2"):
            trimmed = trimmed[:-3]
        trimmed = trimmed.rstrip("/")
        return f"{trimmed}/v1/ai-tools/web-search"

    @staticmethod
    def _detect_configured_search_engines(
        env: Mapping[str, str] | None = None,
    ) -> Dict[str, str]:
        """Return configured search engines in source precedence order."""

        def read(name: str, default: str = "") -> str:
            return DeepResearchTaskManager._read_config_value(name, default, env)

        configured_engines: Dict[str, str] = {}

        serper_api_key = read("SERPER_API_KEY").strip()
        if serper_api_key:
            configured_engines["serper"] = serper_api_key

        jina_api_key = read("JINA_API_KEY").strip()
        if jina_api_key:
            configured_engines["jina"] = jina_api_key

        bocha_api_key = read("BOCHA_API_KEY").strip()
        if bocha_api_key:
            configured_engines["bocha"] = bocha_api_key

        perplexity_api_key = read("PERPLEXITY_API_KEY").strip()
        if perplexity_api_key:
            configured_engines["perplexity"] = perplexity_api_key

        petal_url = read("PETAL_SEARCH_URL").strip()
        petal_headers = read("PETAL_SEARCH_HEADERS").strip()
        if petal_url and petal_headers:
            configured_engines["petal"] = petal_url
        return configured_engines

    @staticmethod
    def _load_config(env: Mapping[str, str] | None = None) -> Dict[str, str]:
        """Load DeepSearch config with source-compatible precedence."""

        def read(name: str, default: str = "") -> str:
            return DeepResearchTaskManager._read_config_value(name, default, env)

        llm_model_name = read("LLM_MODEL_NAME").strip() or read("MODEL_NAME").strip()
        llm_model_type = (
            read("LLM_MODEL_TYPE").strip().lower()
            or read("MODEL_PROVIDER").strip().lower()
        )
        llm_base_url = read("LLM_BASE_URL").strip() or read("API_BASE").strip()
        llm_api_key = read("LLM_API_KEY").strip() or read("API_KEY").strip()

        configured_engines = DeepResearchTaskManager._detect_configured_search_engines(env)
        web_search_engine_name = read("WEB_SEARCH_ENGINE_NAME").strip().lower()
        if not web_search_engine_name and configured_engines:
            web_search_engine_name = next(iter(configured_engines.keys()))
        if not web_search_engine_name:
            web_search_engine_name = "petal"

        web_search_api_key = read("WEB_SEARCH_API_KEY").strip()
        if not web_search_api_key and web_search_engine_name:
            web_search_api_key = read(f"{web_search_engine_name.upper()}_API_KEY").strip()
        if (
            not web_search_api_key
            and configured_engines
            and web_search_engine_name in configured_engines
        ):
            web_search_api_key = configured_engines[web_search_engine_name]
        if not web_search_api_key:
            for header_name in (
                "default_headers",
                "DEFAULT_HEADERS",
                "OPENAI_DEFAULT_HEADERS",
            ):
                web_search_api_key = read(header_name).strip()
                if web_search_api_key:
                    break
        if not web_search_api_key and env is None:
            web_search_api_key = read_default_headers_raw()

        web_search_url = read("WEB_SEARCH_URL").strip()
        if not web_search_url and web_search_engine_name == "petal":
            web_search_url = DeepResearchTaskManager._resolve_petal_search_url(env)

        execution_method = read("EXECUTION_METHOD", "parallel").strip()
        vision_api_key = read("VISION_API_KEY").strip()
        vision_api_base = read("VISION_API_BASE").strip()
        vision_provider = read("VISION_PROVIDER").strip().lower()
        vision_model_name = read("VISION_MODEL_NAME").strip()
        has_valid_vision_config = all(
            (vision_api_key, vision_api_base, vision_provider, vision_model_name)
        )
        _vlm_chart_generator_enable = (
            "True" if has_valid_vision_config else "False"
        )

        return {
            "LLM_MODEL_NAME": llm_model_name,
            "LLM_MODEL_TYPE": llm_model_type,
            "LLM_BASE_URL": llm_base_url,
            "LLM_API_KEY": llm_api_key,
            "WEB_SEARCH_ENGINE_NAME": web_search_engine_name,
            "WEB_SEARCH_API_KEY": web_search_api_key,
            "WEB_SEARCH_URL": web_search_url,
            "MAX_WEB_SEARCH_RESULTS": "5",
            "EXECUTION_METHOD": execution_method,
            "OUTLINER_MAX_SECTION_NUM": "5",
            "WORKFLOW_HUMAN_IN_THE_LOOP": "False",
            "OUTLINE_INTERACTION_ENABLED": "False",
            "SOURCE_TRACER_INFER_SWITCHES": "True",
            # Keep exact final source behavior: the computed value is not enabled here.
            "VLM_CHART_GENERATOR_ENABLE": "False",
            "VLM_CHART_GENERATOR_MAX_ITERATIONS": 3,
            "VISION_API_KEY": vision_api_key,
            "VISION_API_URL": vision_api_base,
            "VISION_PROVIDER": vision_provider,
            "VISION_MODEL_NAME": vision_model_name,
        }

    load_config = _load_config

    @staticmethod
    def _extract_section_titles(outline_content: str) -> dict[str, str]:
        """Extract section indices and titles from JSON or Markdown outlines."""
        section_titles: dict[str, str] = {}
        if not outline_content or not outline_content.strip():
            return section_titles

        stripped_content = outline_content.strip()
        if stripped_content.startswith("{") or stripped_content.startswith("["):
            try:
                data = json.loads(stripped_content)
                json_titles = DeepResearchTaskManager._extract_titles_from_json(data)
                if json_titles:
                    return json_titles
            except (json.JSONDecodeError, TypeError):
                pass

        title_index = 0
        for line in outline_content.split("\n"):
            stripped = line.strip()
            heading = ""
            if stripped.startswith("## "):
                heading = stripped[3:].strip()
            elif stripped.startswith("### "):
                heading = stripped[4:].strip()
            elif stripped.startswith("# "):
                heading = stripped[2:].strip()
                if not re.match(r"第\d+[章节篇部]", heading) and not re.match(
                    r"\d+[.、)]\s", heading
                ):
                    continue
            else:
                continue

            if not heading:
                continue
            title_index += 1
            idx = str(title_index)
            match = re.match(r"第(\d+)[章节篇部]\s*[：:]*\s*(.+)", heading)
            if match:
                section_titles[match.group(1)] = match.group(2).strip()
                continue
            match = re.match(r"(\d+)[.、)]\s*(.+)", heading)
            if match:
                section_titles[match.group(1)] = match.group(2).strip()
                continue
            match = re.match(
                r"[Pp](\d+)(?:\s*[：:、.)-]\s*|\s+)(.+)", heading
            )
            if match:
                section_titles[match.group(1)] = match.group(2).strip()
                continue
            section_titles[idx] = heading
        return section_titles

    extract_section_titles = _extract_section_titles

    @staticmethod
    def _extract_titles_from_json(data: Any) -> dict[str, str]:
        """Extract titles from the supported DeepSearch JSON outline shapes."""
        section_titles: dict[str, str] = {}
        sections: list[Any] = []
        if isinstance(data, dict):
            for key in ("sections", "outline", "chapters", "章节"):
                if key in data and isinstance(data[key], list):
                    sections = data[key]
                    break
            if not sections:
                for value in data.values():
                    if isinstance(value, list):
                        sections = value
                        break
        elif isinstance(data, list):
            sections = data

        for index, item in enumerate(sections, start=1):
            title = ""
            if isinstance(item, dict):
                title = (
                    item.get("title")
                    or item.get("name")
                    or item.get("heading")
                    or ""
                )
            elif isinstance(item, str):
                title = item
            if title:
                section_titles[str(index)] = title.strip()
        return section_titles


class DeepResearchTaskManagerPool:
    """Process-level pool of per-tenant DeepResearch managers."""

    _managers: dict[tuple[str, str, str], DeepResearchTaskManager] = {}
    _lock = asyncio.Lock()

    @classmethod
    def _normalize_tenant(cls, scope: Any) -> tuple[str, str, str]:
        tenant = getattr(scope, "tenant", None)
        if callable(tenant):
            values = tenant()
            if isinstance(values, (tuple, list)) and len(values) >= 2:
                workspace_key = values[2] if len(values) >= 3 else "default"
                return (
                    (str(values[0] or "default").strip() or "default"),
                    (str(values[1] or "default").strip() or "default"),
                    (str(workspace_key or "default").strip() or "default"),
                )
        if isinstance(scope, (tuple, list)) and len(scope) >= 2:
            workspace_key = scope[2] if len(scope) >= 3 else "default"
            return (
                (str(scope[0] or "default").strip() or "default"),
                (str(scope[1] or "default").strip() or "default"),
                (str(workspace_key or "default").strip() or "default"),
            )
        sid = getattr(scope, "service_id", None)
        aid = getattr(scope, "agent_id", None)
        workspace_key = getattr(scope, "workspace_key", None)
        if sid is not None or aid is not None or workspace_key is not None:
            return (
                (str(sid or "default").strip() or "default"),
                (str(aid or "default").strip() or "default"),
                (str(workspace_key or "default").strip() or "default"),
            )
        return ("default", "default", "default")

    @classmethod
    async def get_or_create(cls, scope: Any) -> DeepResearchTaskManager:
        key = cls._normalize_tenant(scope)
        async with cls._lock:
            manager = cls._managers.get(key)
            if manager is None:
                manager = DeepResearchTaskManager(service_id=key[0], agent_id=key[1])
                cls._managers[key] = manager
            return manager

    @classmethod
    async def remove(
        cls,
        service_id: str,
        agent_id: str,
        workspace_key: str = "default",
    ) -> bool:
        key = (
            (str(service_id or "default").strip() or "default"),
            (str(agent_id or "default").strip() or "default"),
            (str(workspace_key or "default").strip() or "default"),
        )
        async with cls._lock:
            manager = cls._managers.pop(key, None)
        if manager is None:
            return False
        try:
            await manager.shutdown()
        except Exception:
            logger.warning(
                "[DeepResearchTaskManagerPool] shutdown failed tenant=(%s,%s,%s)",
                key[0],
                key[1],
                key[2],
                exc_info=True,
            )
        return True

    @classmethod
    def get_or_create_sync(cls, scope: Any) -> DeepResearchTaskManager:
        key = cls._normalize_tenant(scope)
        manager = cls._managers.get(key)
        if manager is None:
            manager = DeepResearchTaskManager(service_id=key[0], agent_id=key[1])
            cls._managers[key] = manager
        return manager

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._managers.clear()


def get_deepresearch_manager(scope: Any) -> DeepResearchTaskManager:
    """Return the sync-compatible manager for a tenant scope."""
    return DeepResearchTaskManagerPool.get_or_create_sync(scope)


def load_deepresearch_config(env: Mapping[str, str] | None = None) -> Dict[str, str]:
    """Return DeepResearch configuration using the manager's resolution rules."""
    return DeepResearchTaskManager.load_config(env)


def extract_deepresearch_section_titles(text: str) -> Dict[str, str]:
    """Return section title metadata using the manager's outline parser."""
    return DeepResearchTaskManager.extract_section_titles(text)


__all__ = [
    "DeepResearchManagerClosedError",
    "DeepResearchTaskManager",
    "DeepResearchTaskManagerPool",
    "extract_deepresearch_section_titles",
    "get_deepresearch_manager",
    "load_deepresearch_config",
]

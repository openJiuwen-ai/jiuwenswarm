# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import importlib.util
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import uuid
import zipfile
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.agentserver.runtime_scope import RuntimeScopeKey
from jiuwenclaw.agentserver.tools.deepresearch_task_manager import (
    DeepResearchTaskRequest,
    extract_deepresearch_section_titles,
    get_deepresearch_manager,
    load_deepresearch_config,
)
from jiuwenclaw.config import get_config
from jiuwenclaw.agentserver.tools.deepresearch.tls import (
    DEEPRESEARCH_TLS_ENV_LOCK as _REPORT_STYLE_LLM_INIT_LOCK,
    scoped_deepresearch_tls_env,
)
from jiuwenclaw.agentserver.tools.deepresearch.todo_progress import (
    deepresearch_todo_path,
    persist_deepresearch_task_update,
)
from jiuwenclaw.local_env_config import (
    build_effective_env_overlay,
    export_spawn_environ,
    get_task_env_overlay,
    read_env,
)

logger = logging.getLogger(__name__)
_DEEPRESEARCH_DEPENDENCY = "openjiuwen_deepsearch"
_REPORT_PUBLICATION_ATTEMPTS = 8
_DEEPRESEARCH_CONFIG_PROTOCOL_VERSION = 1
_DEEPRESEARCH_CONFIG_MAX_BYTES = 64 * 1024
_DEEPRESEARCH_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


@dataclass
class _KeyedLock:
    lock: threading.Lock
    references: int = 0


@dataclass
class _CreatedArtifact:
    path: Path
    metadata: os.stat_result
    parent_fd: int | None = None
    entry_name: str | None = None
    directory_fd: int | None = None


_REPORT_OUTPUT_LOCKS: dict[Path, _KeyedLock] = {}
_REPORT_OUTPUT_LOCKS_GUARD = threading.Lock()
_OUTLINE_TITLE_CACHES: dict[tuple[str, str, str], dict[str, dict[str, str]]] = {}
_OUTLINE_TITLE_CACHES_GUARD = threading.Lock()

# 使用 contextvars
_deepresearch_route_ctx: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "jiuwenclaw_deepresearch_route", default=None
)
_deepresearch_router_state_ctx: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "jiuwenclaw_deepresearch_router_state", default=None
)


def push_deepresearch_route(
    request_id: str,
    channel_id: str,
    session_id: str,
    *,
    service_id: str = "default",
    agent_id: str = "default",
) -> contextvars.Token:
    """设置 DeepResearch 路由上下文（含租户维度）。"""
    return _deepresearch_route_ctx.set({
        "request_id": request_id or "",
        "channel_id": channel_id or "",
        "session_id": session_id or "",
        "service_id": (service_id or "default").strip() or "default",
        "agent_id": (agent_id or "default").strip() or "default",
    })


def reset_deepresearch_route(token: contextvars.Token) -> None:
    """恢复 DeepResearch 路由上下文。"""
    _deepresearch_route_ctx.reset(token)


def _get_route() -> dict[str, object]:
    """获取当前路由上下文（含 service_id / agent_id）。"""
    route = _deepresearch_route_ctx.get()
    if route is None:
        return {
            "request_id": "",
            "channel_id": "",
            "session_id": "",
            "service_id": "default",
            "agent_id": "default",
        }
    return {
        "request_id": route.get("request_id", ""),
        "channel_id": route.get("channel_id", ""),
        "session_id": route.get("session_id", ""),
        "service_id": route.get("service_id", "default") or "default",
        "agent_id": route.get("agent_id", "default") or "default",
    }


def _route_scope():
    """Build RuntimeScopeKey from the current DeepResearch route context."""
    route = _get_route()
    return RuntimeScopeKey.from_ids(
        route.get("service_id"),
        route.get("agent_id"),
        route.get("session_id"),
    )


def _outline_title_cache(route: dict[str, object]) -> dict[str, dict[str, str]]:
    key = (
        str(route.get("service_id") or "default"),
        str(route.get("agent_id") or "default"),
        str(route.get("session_id") or ""),
    )
    with _OUTLINE_TITLE_CACHES_GUARD:
        return _OUTLINE_TITLE_CACHES.setdefault(key, {})


def _clear_outline_title_cache(
    route: dict[str, object],
    conversation_id: object,
) -> None:
    cid = str(conversation_id or "").strip()
    if not cid:
        return
    key = (
        str(route.get("service_id") or "default"),
        str(route.get("agent_id") or "default"),
        str(route.get("session_id") or ""),
    )
    with _OUTLINE_TITLE_CACHES_GUARD:
        cache = _OUTLINE_TITLE_CACHES.get(key)
        if cache is None:
            return
        cache.pop(cid, None)
        if not cache:
            _OUTLINE_TITLE_CACHES.pop(key, None)


def _normalize_citation_artifacts(value: object) -> dict[str, str]:
    """Keep only non-empty hidden citation paths allowed in provenance/events."""
    if not isinstance(value, dict):
        return {}
    artifacts: dict[str, str] = {}
    for key in ("raw_report_path", "citations_preview_path"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            artifacts[key] = item.strip()
    return artifacts


def _build_related_artifact_bundle(
    value: object, markdown_index: int
) -> dict | None:
    """Build the hidden companion contract for one visible Markdown file."""
    artifacts = _normalize_citation_artifacts(value)
    related_artifacts = []
    raw_report_path = artifacts.get("raw_report_path")
    if raw_report_path:
        related_artifacts.append({
            "type": "raw_report",
            "path": raw_report_path,
            "contentType": "text/markdown",
            "relatedToPathIndex": markdown_index,
        })
    preview_path = artifacts.get("citations_preview_path")
    if preview_path:
        related_artifacts.append({
            "type": "citations_preview",
            "path": preview_path,
            "contentType": "application/json",
            "schemaVersion": "1.1",
            "relatedToPathIndex": markdown_index,
        })
    if not related_artifacts:
        return None
    return {"schemaVersion": "1.0", "relatedArtifacts": related_artifacts}


def _write_report_markdown(
    final_result: dict,
    file_name: str,
    conversation_id: str,
    citation_artifacts: object = None,
) -> str:
    """Build and write the completed report bundle into the request output directory."""
    from jiuwenclaw.agentserver.tools.deepresearch_plugin.artifact_naming import (
        allocate_initial_paths,
    )
    from jiuwenclaw.agentserver.tools.deepresearch_plugin.report_bundle import (
        build_report_bundle,
        serialize_final_result_snapshot,
    )
    from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
        get_effective_request_output_dir,
    )

    output_dir = get_effective_request_output_dir()
    if not output_dir:
        raise RuntimeError("current request output_dir is unavailable")

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    with _report_output_lock(output_path):
        allocation_reservations: list[_CreatedArtifact] = []
        try:
            for _attempt in range(_REPORT_PUBLICATION_ATTEMPTS):
                paths = allocate_initial_paths(output_path, file_name)
                created_paths: list[_CreatedArtifact] = []
                try:
                    with tempfile.TemporaryDirectory(
                        prefix=".deepresearch-report-", dir=output_path
                    ) as staging_name:
                        staging_dir = Path(staging_name)
                        staging_base = staging_dir / paths.markdown_path.stem
                        bundle = build_report_bundle(final_result, staging_base)
                        markdown_bytes = bundle.markdown_text.encode("utf-8")
                        snapshot_bytes = serialize_final_result_snapshot(
                            bundle.final_result_snapshot
                        )
                        provenance = {
                            "schema_version": 2,
                            "document_id": f"doc_{uuid.uuid4().hex}",
                            "revision_id": f"rev_{uuid.uuid4().hex}",
                            "parent_revision_id": None,
                            "conversation_id": conversation_id,
                            "markdown_path": str(paths.markdown_path),
                            "content_sha256": hashlib.sha256(
                                markdown_bytes
                            ).hexdigest(),
                            "final_result_path": paths.final_result_path.name,
                            "final_result_sha256": hashlib.sha256(
                                snapshot_bytes
                            ).hexdigest(),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "operation": {"action": "deepresearch_generate"},
                            "version_number": paths.version.version_number,
                            "version_base_stem": paths.version.base_stem,
                            "citations": bundle.citations,
                            "inference_manifest": bundle.inference_manifest,
                            "chart_manifest": bundle.chart_manifest,
                            "rewrite_history": [],
                        }
                        normalized_artifacts = _normalize_citation_artifacts(
                            citation_artifacts
                        )
                        if normalized_artifacts:
                            provenance["citation_artifacts"] = (
                                normalized_artifacts
                            )
                        provenance_bytes = json.dumps(
                            provenance, ensure_ascii=False, indent=2
                        ).encode("utf-8")

                        report_base = paths.markdown_path.with_suffix("")
                        for staged_directory, final_directory in (
                            (
                                bundle.infer_dir,
                                report_base.with_name(
                                    f"{report_base.name}_infer"
                                ),
                            ),
                            (
                                bundle.chart_dir,
                                report_base.with_name(
                                    f"{report_base.name}_charts"
                                ),
                            ),
                        ):
                            if staged_directory:
                                _publish_staged_asset_directory(
                                    Path(staged_directory),
                                    final_directory,
                                    created_paths,
                                )

                        _verify_created_directories(created_paths)
                        created_paths.append(_CreatedArtifact(
                            path=paths.final_result_path,
                            metadata=_atomic_create_bytes(
                                paths.final_result_path, snapshot_bytes
                            ),
                        ))
                        created_paths.append(_CreatedArtifact(
                            path=paths.provenance_path,
                            metadata=_atomic_create_bytes(
                                paths.provenance_path, provenance_bytes
                            ),
                        ))
                        created_paths.append(_CreatedArtifact(
                            path=paths.markdown_path,
                            metadata=_atomic_create_bytes(
                                paths.markdown_path, markdown_bytes
                            ),
                        ))
                        _verify_created_directories(created_paths)
                except FileExistsError:
                    _remove_created_artifacts(created_paths)
                    allocation_reservations.append(
                        _create_allocation_reservation(paths.markdown_path)
                    )
                    continue
                except BaseException:
                    _remove_created_artifacts(created_paths)
                    raise
                _close_created_artifact_handles(created_paths)
                return str(paths.markdown_path)
        finally:
            _remove_created_artifacts(allocation_reservations)

    raise FileExistsError("report publication attempts exhausted")


def _build_styled_export_llm_config() -> dict:
    """Build llm_config dict for the SDK's report_style_llm_context().

    Resolves LLM credentials from the active request/tenant configuration,
    using the same DeepSearch-specific to project-global fallback rules as
    the task manager.
    The SDK's LLMConfig only accepts ``"openai"`` or ``"siliconflow"``
    as model_type; most providers are OpenAI-compatible and default to
    ``"openai"``.
    """
    resolved = load_deepresearch_config()
    api_key = resolved["LLM_API_KEY"].strip()
    model_name = resolved["LLM_MODEL_NAME"].strip()
    base_url = resolved["LLM_BASE_URL"].strip()
    model_type = _map_provider_to_type(resolved["LLM_MODEL_TYPE"]).lower()

    if (
        not api_key
        or not model_name
        or not base_url
    ):
        raise ValueError("styled HTML LLM configuration is invalid")
    if "example.com" in base_url.lower():
        raise ValueError("styled HTML LLM configuration is invalid")

    # LLMConfig.model_type only allows "openai" or "siliconflow";
    # map everything else to "openai" (OpenAI-compatible).
    if model_type not in ("openai", "siliconflow"):
        model_type = "openai"

    return {
        "general": {
            "model_name": model_name,
            "model_type": model_type,
            "base_url": base_url,
            "api_key": bytearray(api_key, encoding="utf-8"),
            "extension": {
                "extra_body": {
                    "thinking": {"type": "disabled"},
                },
            },
            "verify_ssl": False,
        },
    }


@asynccontextmanager
async def _scoped_report_style_llm_context(context_factory, llm_config):
    """Scope the SDK's env-based TLS setting to runtime LLM initialization."""
    async with AsyncExitStack() as stack:
        async with scoped_deepresearch_tls_env(
            lambda: {
                "LLM_SSL_VERIFY": read_env("LLM_SSL_VERIFY", "false")
            }
        ):
            llm = await stack.enter_async_context(context_factory(llm_config))

        yield llm


def _validate_zip_member(member_name: str) -> str:
    """Normalize and validate a ZIP member path to prevent traversal attacks."""
    from pathlib import PurePosixPath, PureWindowsPath

    normalized_name = member_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(member_name)
    if posix_path.is_absolute() or windows_path.is_absolute() or ".." in posix_path.parts:
        raise ValueError(f"unsafe ZIP member: {member_name}")
    return normalized_name


def _extract_styled_bundle(convert_content: str, destination: Path) -> Path:
    """Decode base64 ZIP payload from SDK and extract to *destination*."""
    try:
        archive_bytes = base64.b64decode(convert_content, validate=True)
    except ValueError as exc:
        raise ValueError("invalid styled report base64 payload") from exc

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        normalized_names = {
            _validate_zip_member(member.filename)
            for member in archive.infolist()
        }
        if "report_bundle/report.html" not in normalized_names:
            raise ValueError("styled report bundle is missing report_bundle/report.html")
        archive.extractall(destination)

    return destination / "report_bundle"


def _install_styled_bundle(bundle_root: Path, html_path: Path) -> None:
    """Install styled assets without mutating the provenance-bearing bundle."""
    report_base = html_path.with_suffix("")
    infer_dir = report_base.with_name(f"{report_base.name}_html_infer")
    chart_dir = report_base.with_name(f"{report_base.name}_html_charts")
    html = (bundle_root / "report.html").read_text(encoding="utf-8")
    html = html.replace('href="infer/', f'href="{infer_dir.name}/')
    html = html.replace("href='infer/", f"href='{infer_dir.name}/")
    html = html.replace('src="charts/', f'src="{chart_dir.name}/')
    html = html.replace("src='charts/", f"src='{chart_dir.name}/")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    created_paths: list[_CreatedArtifact] = []
    try:
        for staged_directory, final_directory in (
            (bundle_root / "infer", infer_dir),
            (bundle_root / "charts", chart_dir),
        ):
            if staged_directory.is_dir() and any(staged_directory.iterdir()):
                _publish_staged_asset_directory(
                    staged_directory, final_directory, created_paths
                )
        _verify_created_directories(created_paths)
        created_paths.append(_CreatedArtifact(
            path=html_path,
            metadata=_atomic_create_bytes(
                html_path, html.replace("\r\n", "\n").encode("utf-8")
            ),
        ))
        _verify_created_directories(created_paths)
    except BaseException:
        _remove_created_artifacts(created_paths)
        raise
    _close_created_artifact_handles(created_paths)


async def _generate_report_html(
    final_result: dict,
    report_path_md: Path,
    fallback_markdown: str | None,
) -> Path | None:
    """Generate styled report HTML, falling back to safe offline conversion."""
    report_path_html = report_path_md.with_suffix(".html")
    try:
        from openjiuwen_deepsearch.algorithm.report_style.service import (
            stylize_report,
        )
        from openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime import (
            report_style_llm_context,
        )

        llm_config = _build_styled_export_llm_config()
        async with _scoped_report_style_llm_context(
            report_style_llm_context, llm_config
        ) as llm:
            result = await stylize_report(final_result, llm)

        with tempfile.TemporaryDirectory(prefix="jiuwenclaw_report_") as temporary_dir:
            bundle_root = _extract_styled_bundle(
                result.convert_content, Path(temporary_dir)
            )
            _install_styled_bundle(bundle_root, report_path_html)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "SDK styled HTML export failed; using offline conversion. type=%s",
            type(exc).__name__,
        )
        try:
            from jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline import (
                convert_md_to_html,
            )

            if not isinstance(fallback_markdown, str):
                raise TypeError("invalid fallback report content") from exc
            with tempfile.TemporaryDirectory(
                prefix="jiuwenclaw_report_fallback_"
            ) as temporary_dir:
                staging_root = Path(temporary_dir)
                staged_markdown = staging_root / "report.md"
                staged_html = staging_root / "report.html"
                staged_markdown.write_text(
                    fallback_markdown, encoding="utf-8", newline="\n"
                )
                convert_md_to_html(str(staged_markdown), str(staged_html))
                html_bytes = staged_html.read_bytes()
            _atomic_create_bytes(report_path_html, html_bytes)
        except Exception as fallback_exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Offline HTML conversion failed. type=%s",
                type(fallback_exc).__name__,
            )
            return None

    return report_path_html


async def _write_report_artifacts_stream(
    final_result: dict,
    file_name: str,
    conversation_id: str,
    citation_artifacts: object = None,
) -> dict[str, str]:
    """Build and write the completed report bundle as MD + styled HTML.

    Follows the same pattern as ``DeepResearchTaskManager._write_report_artifacts``:
    Markdown is always written; HTML is a best-effort conversion that
    logs a warning on failure but never blocks the primary MD delivery.

    The primary HTML path directly calls the SDK's
    ``report_style_llm_context`` + ``stylize_report`` to produce an
    LLM-styled report bundle, then extracts and installs it locally.
    If that fails (e.g. SDK unavailable, LLM error), the function falls
    back to the lightweight offline converter (``convert_md_to_html``).

    Returns:
        dict mapping format key (``"md"``, ``"html"``) to the
        absolute file path of the generated artifact.  ``"md"`` is always
        present; ``"html"`` is included only when conversion succeeds.
    """
    # Reuse the rewrite-aware Markdown writer so the hidden final-result and
    # provenance sidecars are created before any visible artifact is delivered.
    report_path_md = Path(
        await asyncio.to_thread(
            _write_report_markdown,
            final_result,
            file_name,
            conversation_id,
            citation_artifacts,
        )
    )
    artifacts: dict[str, str] = {"md": str(report_path_md)}

    try:
        fallback_markdown: str | None = report_path_md.read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        logger.warning(
            "HTML fallback Markdown read failed. type=%s",
            type(exc).__name__,
        )
        fallback_markdown = None
    report_path_html = await _generate_report_html(
        final_result, report_path_md, fallback_markdown
    )
    if report_path_html is not None:
        artifacts["html"] = str(report_path_html)

    return artifacts


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace one file without exposing a partially written artifact."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _report_output_lock(output_dir: Path):
    """Serialize one output directory and retire the keyed lock when unused."""
    with _REPORT_OUTPUT_LOCKS_GUARD:
        keyed_lock = _REPORT_OUTPUT_LOCKS.get(output_dir)
        if keyed_lock is None:
            keyed_lock = _KeyedLock(lock=threading.Lock())
            _REPORT_OUTPUT_LOCKS[output_dir] = keyed_lock
        keyed_lock.references += 1
    keyed_lock.lock.acquire()
    try:
        yield
    finally:
        keyed_lock.lock.release()
        with _REPORT_OUTPUT_LOCKS_GUARD:
            keyed_lock.references -= 1
            if (
                keyed_lock.references == 0
                and _REPORT_OUTPUT_LOCKS.get(output_dir) is keyed_lock
            ):
                del _REPORT_OUTPUT_LOCKS[output_dir]


def _uses_windows_path_publication() -> bool:
    """Return whether publication must avoid POSIX descriptor APIs."""
    return os.name == "nt"


def _rename_windows_no_replace(source: Path, destination: Path) -> None:
    """Rename on Windows, where os.rename refuses to replace a destination."""
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"publication target already exists: {destination}")
    os.rename(source, destination)


def _atomic_create_bytes_windows(
    path: Path, payload: bytes
) -> os.stat_result:
    """Publish a complete file using Windows-compatible path operations."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            metadata = os.fstat(stream.fileno())
        _rename_windows_no_replace(temp_path, path)
        return metadata
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _atomic_create_bytes_posix(
    path: Path, payload: bytes
) -> os.stat_result:
    """Publish a complete immutable file without replacing an existing target."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            metadata = os.fstat(stream.fileno())
        os.link(temp_name, path, follow_symlinks=False)
        return metadata
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_create_bytes(path: Path, payload: bytes) -> os.stat_result:
    """Publish a complete immutable file without replacing an existing target."""
    if _uses_windows_path_publication():
        return _atomic_create_bytes_windows(path, payload)
    return _atomic_create_bytes_posix(path, payload)


def _atomic_create_bytes_at(
    directory_fd: int, name: str, payload: bytes
) -> os.stat_result:
    """Publish one immutable child relative to a retained directory handle."""
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"

    def open_temporary_child(path: str, flags: int) -> int:
        return os.open(
            path,
            flags | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )

    try:
        with open(
            temporary_name, "xb", opener=open_temporary_child
        ) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            metadata = os.fstat(stream.fileno())
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        return metadata
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _same_identity(
    left: os.stat_result, right: os.stat_result
) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _as_created_artifact(
    value: _CreatedArtifact | tuple[Path, os.stat_result],
) -> _CreatedArtifact:
    if isinstance(value, _CreatedArtifact):
        return value
    path, metadata = value
    return _CreatedArtifact(path=path, metadata=metadata)


def _close_created_artifact_handles(
    created_paths: list[_CreatedArtifact | tuple[Path, os.stat_result]],
) -> None:
    for value in created_paths:
        artifact = _as_created_artifact(value)
        if artifact.directory_fd is not None:
            try:
                os.close(artifact.directory_fd)
            except OSError:
                pass
            artifact.directory_fd = None


def _verify_created_directories(
    created_paths: list[_CreatedArtifact | tuple[Path, os.stat_result]],
) -> None:
    """Ensure every public directory name still denotes its retained handle."""
    for value in created_paths:
        artifact = _as_created_artifact(value)
        if artifact.directory_fd is None:
            if (
                _uses_windows_path_publication()
                and stat.S_ISDIR(artifact.metadata.st_mode)
            ):
                try:
                    namespace_metadata = os.lstat(artifact.path)
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        "report asset directory namespace changed: "
                        f"{artifact.path}"
                    ) from exc
                if not _same_identity(
                    namespace_metadata, artifact.metadata
                ):
                    raise RuntimeError(
                        "report asset directory namespace changed: "
                        f"{artifact.path}"
                    )
            continue
        handle_metadata = os.fstat(artifact.directory_fd)
        try:
            namespace_metadata = os.stat(
                artifact.path, follow_symlinks=False
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"report asset directory namespace changed: {artifact.path}"
            ) from exc
        if not _same_identity(handle_metadata, namespace_metadata):
            raise RuntimeError(
                f"report asset directory namespace changed: {artifact.path}"
            )


def _make_quarantine_directory(parent_fd: int, entry_name: str) -> str:
    for _attempt in range(8):
        quarantine_name = (
            f".{entry_name}.quarantine-{uuid.uuid4().hex}"
        )
        try:
            os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        return quarantine_name
    raise FileExistsError("unable to allocate report cleanup quarantine")


def _restore_quarantined_entry(
    quarantine_fd: int,
    parent_fd: int,
    entry_name: str,
) -> bool:
    """Restore a non-directory entry without replacing a concurrent writer."""
    try:
        os.link(
            "entry",
            entry_name,
            src_dir_fd=quarantine_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        return False
    os.unlink("entry", dir_fd=quarantine_fd)
    return True


def _restore_quarantined_directory(
    quarantine_fd: int,
    parent_fd: int,
    entry_name: str,
    metadata: os.stat_result,
) -> bool:
    """Recreate a quarantined directory only after claiming the public name."""
    try:
        os.mkdir(
            entry_name,
            mode=stat.S_IMODE(metadata.st_mode),
            dir_fd=parent_fd,
        )
    except FileExistsError:
        return False

    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(
            "entry",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=quarantine_fd,
        )
        destination_fd = os.open(
            entry_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(destination_fd, stat.S_IMODE(metadata.st_mode))
        public_metadata = os.stat(
            entry_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if not _same_identity(os.fstat(destination_fd), public_metadata):
            return False
        for child_name in os.listdir(source_fd):
            os.rename(
                child_name,
                child_name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
        os.rmdir("entry", dir_fd=quarantine_fd)
        return True
    except OSError:
        return False
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _quarantine_created_artifact(artifact: _CreatedArtifact) -> None:
    parent_fd = artifact.parent_fd
    close_parent_fd = False
    entry_name = artifact.entry_name or artifact.path.name
    if parent_fd is None:
        parent_fd = os.open(
            artifact.path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            mode=0o600,
        )
        close_parent_fd = True

    quarantine_name = ""
    quarantine_fd = -1
    try:
        quarantine_name = _make_quarantine_directory(
            parent_fd, entry_name
        )
        try:
            quarantine_fd = os.open(
                quarantine_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                mode=0o600,
                dir_fd=parent_fd,
            )
        except BaseException:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        try:
            os.rename(
                entry_name,
                "entry",
                src_dir_fd=parent_fd,
                dst_dir_fd=quarantine_fd,
            )
        except FileNotFoundError:
            return

        quarantined_metadata = os.stat(
            "entry", dir_fd=quarantine_fd, follow_symlinks=False
        )
        if _same_identity(quarantined_metadata, artifact.metadata):
            if stat.S_ISDIR(quarantined_metadata.st_mode):
                os.rmdir("entry", dir_fd=quarantine_fd)
            else:
                os.unlink("entry", dir_fd=quarantine_fd)
            return

        if stat.S_ISDIR(quarantined_metadata.st_mode):
            restored = _restore_quarantined_directory(
                quarantine_fd,
                parent_fd,
                entry_name,
                quarantined_metadata,
            )
        else:
            restored = _restore_quarantined_entry(
                quarantine_fd, parent_fd, entry_name
            )
        if restored:
            return
        logger.warning(
            "Report cleanup quarantined a replaced entry and left it intact. "
            "path=%s quarantine=%s",
            artifact.path,
            artifact.path.parent / quarantine_name / "entry",
        )
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)
        if quarantine_name:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError:
                pass
        if close_parent_fd:
            os.close(parent_fd)


def _remove_quarantined_windows_path(
    path: Path, metadata: os.stat_result
) -> None:
    """Remove a quarantined path whose identity was already verified."""
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _quarantine_created_artifact_windows(
    artifact: _CreatedArtifact,
) -> None:
    """Quarantine one Windows artifact and remove it only if still owned."""
    quarantine = artifact.path.with_name(
        f".{artifact.path.name}.quarantine-{uuid.uuid4().hex}"
    )
    try:
        os.rename(artifact.path, quarantine)
    except FileNotFoundError:
        return

    quarantined_metadata = os.lstat(quarantine)
    if _same_identity(quarantined_metadata, artifact.metadata):
        _remove_quarantined_windows_path(quarantine, quarantined_metadata)
        return

    try:
        _rename_windows_no_replace(quarantine, artifact.path)
    except OSError:
        logger.warning(
            "Report cleanup quarantined a replaced entry and left it intact. "
            "path=%s quarantine=%s",
            artifact.path,
            quarantine,
        )


def _remove_created_artifacts(
    created_paths: list[_CreatedArtifact | tuple[Path, os.stat_result]],
) -> None:
    """Quarantine names atomically, then delete only matching owned entries."""
    for value in reversed(created_paths):
        artifact = _as_created_artifact(value)
        try:
            if _uses_windows_path_publication():
                _quarantine_created_artifact_windows(artifact)
            else:
                _quarantine_created_artifact(artifact)
        except OSError as exc:
            logger.warning(
                "Unable to clean current report publication path. "
                "path=%s error=%s",
                artifact.path,
                exc,
            )
        finally:
            if artifact.directory_fd is not None:
                try:
                    os.close(artifact.directory_fd)
                except OSError:
                    pass
                artifact.directory_fd = None


def _publish_staged_asset_directory_posix(
    staged_directory: Path,
    final_directory: Path,
    created_paths: list[_CreatedArtifact],
) -> None:
    """Publish a flat asset directory entirely through a retained dir handle."""
    parent_fd = os.open(
        final_directory.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        mode=0o600,
    )
    directory_fd = -1
    try:
        os.mkdir(final_directory.name, dir_fd=parent_fd)
        namespace_metadata = os.stat(
            final_directory.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        directory_artifact = _CreatedArtifact(
            path=final_directory,
            metadata=namespace_metadata,
        )
        created_paths.append(directory_artifact)
        directory_fd = os.open(
            final_directory.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=parent_fd,
        )
        handle_metadata = os.fstat(directory_fd)
        if not _same_identity(namespace_metadata, handle_metadata):
            raise RuntimeError(
                f"report asset directory namespace changed: {final_directory}"
            )
        directory_artifact.directory_fd = directory_fd
        directory_fd = -1
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)

    retained_directory_fd = directory_artifact.directory_fd
    if retained_directory_fd is None:
        raise RuntimeError(
            f"report asset directory handle unavailable: {final_directory}"
        )
    for staged_path in sorted(staged_directory.iterdir()):
        metadata = os.lstat(staged_path)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                f"staged report asset is not a regular file: {staged_path.name}"
            )
        child_metadata = _atomic_create_bytes_at(
            retained_directory_fd,
            staged_path.name,
            staged_path.read_bytes(),
        )
        created_paths.append(_CreatedArtifact(
            path=final_directory / staged_path.name,
            metadata=child_metadata,
            parent_fd=retained_directory_fd,
            entry_name=staged_path.name,
        ))


def _publish_staged_asset_directory_windows(
    staged_directory: Path,
    final_directory: Path,
    created_paths: list[_CreatedArtifact],
) -> None:
    """Publish a complete asset directory with Windows-compatible operations."""
    staging_directory = Path(tempfile.mkdtemp(
        prefix=f".{final_directory.name}.",
        dir=final_directory.parent,
    ))
    published = False
    try:
        for staged_path in sorted(staged_directory.iterdir()):
            metadata = os.lstat(staged_path)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    "staged report asset is not a regular file: "
                    f"{staged_path.name}"
                )
            _atomic_create_bytes_windows(
                staging_directory / staged_path.name,
                staged_path.read_bytes(),
            )
        _rename_windows_no_replace(staging_directory, final_directory)
        published = True
        created_paths.append(_CreatedArtifact(
            path=final_directory,
            metadata=os.lstat(final_directory),
        ))
    finally:
        if not published:
            shutil.rmtree(staging_directory, ignore_errors=True)


def _publish_staged_asset_directory(
    staged_directory: Path,
    final_directory: Path,
    created_paths: list[_CreatedArtifact],
) -> None:
    """Publish a complete flat asset directory without replacing a target."""
    if _uses_windows_path_publication():
        _publish_staged_asset_directory_windows(
            staged_directory, final_directory, created_paths
        )
        return
    _publish_staged_asset_directory_posix(
        staged_directory, final_directory, created_paths
    )


def _create_allocation_reservation(
    markdown_path: Path,
) -> _CreatedArtifact:
    """Make a hidden marker that causes the allocator to skip one rejected base."""
    descriptor, reservation_name = tempfile.mkstemp(
        prefix=f".{markdown_path.name}.", dir=markdown_path.parent
    )
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return _CreatedArtifact(path=Path(reservation_name), metadata=metadata)


def _deepresearch_dependency_available() -> bool:
    """Return whether DeepResearch optional runtime is importable."""
    return importlib.util.find_spec(_DEEPRESEARCH_DEPENDENCY) is not None


def _get_task_manager_cls():
    """Resolve the manager class used by shared configuration helpers."""
    from jiuwenclaw.agentserver.tools.deepresearch_task_manager import (
        DeepResearchTaskManager,
    )

    return DeepResearchTaskManager


async def _get_task_manager():
    """Resolve the tenant-scoped DeepResearch task manager."""
    return await get_deepresearch_manager(_route_scope())


def _task_request_cls():
    return DeepResearchTaskRequest


@tool(
    name="deepresearch_create_task",
    description=(
        "创建深度研究任务，生成独立的长文研究报告。"
        "适用场景：独立的深度研究报告生成、全面市场调研、行业分析报告、政策解读报告。"
        "任务将在后台异步运行且耗时长，完成后通过 WebSocket 推送结果。"
        "返回任务 ID，可用于后续查询状态、取消任务或获取结果，但由于任务执行时间较长，创建后无需立刻查询。"
        "⚠不适用场景：PPT制作辅助研究或准备内容素材、单点数据查询、快速搜索"
    ),
)
async def deepresearch_create_task(
    query: str,
    file_name: str,
) -> str:
    """创建 DeepResearch 任务.

    Args:
        query: 研究查询
        file_name: 报告文件名，不带后缀

    Returns:
        任务 ID
    """
    manager = await _get_task_manager()
    route = _get_route()
    task_id = await manager.create_task(
        _task_request_cls()(
            query=query,
            file_name=file_name,
            session_id=route.get("session_id", ""),
            channel_id=route.get("channel_id", ""),
            request_id=route.get("request_id", ""),
            service_id=route.get("service_id", "default"),
            agent_id=route.get("agent_id", "default"),
        )
    )

    # 任务已在 create_task() 返回前写入 _tasks，无需等待
    return f"已创建 DeepResearch 任务，任务 ID: {task_id}"


@tool(
    name="deepresearch_get_status",
    description=(
        "查询 DeepResearch 任务的状态。"
        "DeepResearch任务是一个长时间的任务，建议不要频繁查询，以避免对系统造成过大压力。"
        "返回任务的详细信息，包括状态、创建时间、开始时间、完成时间等。"
    ),
)
async def deepresearch_get_status(task_id: str) -> str:
    """获取任务状态.

    Args:
        task_id: 任务 ID

    Returns:
        任务状态信息（JSON 格式字符串）
    """
    manager = await _get_task_manager()
    route = _get_route()
    task_info = await manager.get_task_status(
        task_id,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if task_info is None:
        return f"未找到任务 ID: {task_id} 或无权访问"

    return json.dumps(task_info, ensure_ascii=False, indent=2)


@tool(
    name="deepresearch_list_tasks",
    description=(
        "列出所有 DeepResearch 任务。"
        "支持按状态过滤（running/completed/cancelled/error）。"
        "返回任务列表，按创建时间倒序排列。"
    ),
)
async def deepresearch_list_tasks(status: str = "") -> str:
    """列出当前会话的任务.

    Args:
        status: 可选的状态过滤器

    Returns:
        任务列表（JSON 格式字符串）
    """
    manager = await _get_task_manager()
    route = _get_route()
    status_filter = status if status else None
    tasks = await manager.list_tasks(
        status_filter=status_filter,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if not tasks:
        return "暂无 DeepResearch 任务"

    return json.dumps(tasks, ensure_ascii=False, indent=2)


@tool(
    name="deepresearch_cancel_task",
    description=(
        "取消正在运行的 DeepResearch 任务。"
        "取消后任务状态将变为 cancelled，已生成的内容将被保留。"
        "用于取消不需要的独立研究报告任务。"
    ),
)
async def deepresearch_cancel_task(task_id: str) -> str:
    """取消任务.

    Args:
        task_id: 任务 ID

    Returns:
        操作结果
    """
    manager = await _get_task_manager()
    route = _get_route()
    success = await manager.cancel_task(
        task_id,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if success:
        return f"已取消任务 ID: {task_id}"
    return f"取消任务失败，任务不存在、已完成或无权访问: {task_id}"


@tool(
    name="deepresearch_get_result",
    description=(
        "获取已完成任务的详细结果。"
        "如果任务未完成，返回提示信息。"
    ),
)
async def deepresearch_get_result(task_id: str) -> str:
    """获取任务结果.

    Args:
        task_id: 任务 ID

    Returns:
        任务结果
    """
    manager = await _get_task_manager()
    route = _get_route()
    result = await manager.get_task_result(
        task_id,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if result is None:
        task_info = await manager.get_task_status(
            task_id,
            caller_session_id=route.get("session_id", ""),
            caller_channel_id=route.get("channel_id", ""),
        )
        if task_info:
            return f"任务 {task_id} 尚未完成，当前状态: {task_info['status']}"
        else:
            return f"未找到任务 ID: {task_id} 或无权访问"

    return result


@tool(
    name="deepresearch_run_task",
    description=(
        "旧版兼容入口。deepresearch skill 不应调用本工具,必须使用 deepresearch_stream。"
        "执行深度研究任务，并阻塞等待生成独立的长文研究报告。"
        "适用场景：独立的深度研究报告生成、全面市场调研、行业分析报告、政策解读报告。"
        "与异步版本的区别：不提交到后台任务池，直接在当前协程执行，阻塞等待完成后返回结果。"
        "会进行多源检索、分析和报告导出，因此执行时间较长（通常约 20-30 分钟），会阻塞当前 Agent 会话直至完成。"
        "⚠不适用场景：PPT制作辅助研究、PPT准备内容素材、单点数据查询、快速搜索"
    ),
)
async def deepresearch_run_task(
    query: str,
    file_name: str,
) -> str:
    """阻塞执行 DeepResearch 任务并返回结果.

    与 deepresearch_create_task 的区别：
    - 不创建后台任务，直接在当前协程执行
    - 不返回任务 ID，直接返回报告保存路径
    - 阻塞等待完成，适合需要即时获取结果的场景
    - 不受任务池资源限制

    Args:
        query: 研究查询
        file_name: 报告文件名，不带后缀

    Returns:
        报告保存路径信息字符串
    """
    manager = await _get_task_manager()
    route = _get_route()
    result = await manager.run_task_direct(
        _task_request_cls()(
            query=query,
            file_name=file_name,
            session_id=route.get("session_id", ""),
            channel_id=route.get("channel_id", ""),
            request_id=route.get("request_id", ""),
            service_id=route.get("service_id", "default"),
            agent_id=route.get("agent_id", "default"),
        )
    )
    return result


# ---- 凭据桥接 ----
# 把 request-scoped 配置桥接成 run_deepsearch.py 读取的 DeepSearch 专属名，
# 只选择 runner 必需字段，不复制同一租户的其他凭据。
_PROVIDER_TO_TYPE = {
    "openai": "openai", "openrouter": "openai",
    "zhipu": "zhipu", "glm": "zhipu",
    "qwen": "qwen", "dashscope": "qwen",
    "deepseek": "deepseek",
    "modelarts": "qwen",  # 华为云 ModelArts,spike 确认
}


def _map_provider_to_type(provider: str) -> str:
    return _PROVIDER_TO_TYPE.get(provider.strip().lower(), provider.strip().lower())


def _build_deepresearch_config(source: dict[str, str]) -> dict[str, str]:
    """Resolve the minimal runner config without copying unrelated credentials."""
    env: dict[str, str] = {}
    resolved = load_deepresearch_config(source)
    api_key = resolved["LLM_API_KEY"]
    model = resolved["LLM_MODEL_NAME"]
    base_url = resolved["LLM_BASE_URL"]
    model_type = _map_provider_to_type(resolved["LLM_MODEL_TYPE"])
    if api_key:
        env["LLM_API_KEY"] = api_key
    if model:
        env["LLM_MODEL_NAME"] = model
    if base_url:
        env["LLM_BASE_URL"] = base_url
    if model_type:
        env["LLM_MODEL_TYPE"] = model_type

    engine = resolved["WEB_SEARCH_ENGINE_NAME"]
    skey = resolved["WEB_SEARCH_API_KEY"]
    surl = resolved["WEB_SEARCH_URL"]
    search_is_complete = bool(engine and skey and (engine != "petal" or surl))
    if search_is_complete:
        env["WEB_SEARCH_API_KEY"] = skey
        env["WEB_SEARCH_ENGINE_NAME"] = engine
        if surl:
            env["WEB_SEARCH_URL"] = surl

    for key in ("MAX_WEB_SEARCH_RESULTS", "EXECUTION_METHOD"):
        value = resolved.get(key, "")
        if value:
            env[key] = str(value)

    for source_key, target_key in (
        ("VISION_API_KEY", "VISION_API_KEY"),
        ("VISION_API_URL", "VISION_API_BASE"),
        ("VISION_PROVIDER", "VISION_PROVIDER"),
        ("VISION_MODEL_NAME", "VISION_MODEL_NAME"),
    ):
        value = resolved.get(source_key, "")
        if value:
            env[target_key] = str(value)

    for header_name in ("default_headers", "DEFAULT_HEADERS", "OPENAI_DEFAULT_HEADERS"):
        value = str(source.get(header_name, "") or "").strip()
        if value:
            env["default_headers"] = value
            break

    # LLM_SSL_VERIFY:JiuwenClaw Python 中的 openjiuwen 0.1.10+ factory 从 env 读
    # verify_ssl(os.getenv("LLM_SSL_VERIFY","true")),sidecar 不设此项 → 子进程默认
    # true → 需 ssl_cert → "ssl_cert is required when verify_ssl is True" 构建失败。
    # in-process manager 因 sidecar 的 openjiuwen 版本不同不受影响;子进程必须显式 false
    # (匹配 manager 的 verify_ssl=False 实际效果)。sidecar 若显式设了则尊重。
    env["LLM_SSL_VERIFY"] = source.get("LLM_SSL_VERIFY", "false")
    env["TOOL_SSL_VERIFY"] = source.get("TOOL_SSL_VERIFY", "false")

    return env


def _build_deepresearch_request_config(
    *,
    interactive_ask: bool,
    service_id: str = "default",
    agent_id: str = "default",
) -> dict[str, str]:
    """Resolve one tenant request into the runner's in-memory config payload."""
    task_overlay = get_task_env_overlay()
    if task_overlay is None:
        source = build_effective_env_overlay(
            service_id=service_id,
            agent_id=agent_id,
        )
    else:
        source = {
            str(key): str(value)
            for key, value in task_overlay.items()
            if value is not None
        }
    config = _build_deepresearch_config(source)
    config["DEEPSEARCH_HITL"] = "true" if interactive_ask else "false"
    return config


def _build_deepresearch_child_env(
    *,
    interactive_ask: bool,
    service_id: str = "default",
    agent_id: str = "default",
    runtime_config: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child env containing runtime flags but no tenant credentials."""
    del service_id, agent_id
    config = runtime_config or {}
    env = export_spawn_environ()
    for key in _DEEPRESEARCH_PROXY_ENV_KEYS:
        value = os.environ.get(key, "")
        if value:
            env[key] = value
    env["DEEPSEARCH_HITL"] = "true" if interactive_ask else "false"
    env["LLM_SSL_VERIFY"] = config.get("LLM_SSL_VERIFY", "false")
    env["TOOL_SSL_VERIFY"] = config.get("TOOL_SSL_VERIFY", "false")
    env["PYTHONUNBUFFERED"] = "1"
    # Force UTF-8 stdout/stderr (PEP 540). On Chinese Windows the child's default
    # stdout encoding is cp936/GBK, so NDJSON with Chinese outline text is emitted
    # as GBK bytes; the sidecar reads stdout as UTF-8 -> decode fails -> mojibake.
    env["PYTHONUTF8"] = "1"
    return env


def _validate_deepresearch_search_config(env: dict[str, str]) -> str | None:
    """Return a user-facing error when the child cannot perform web search."""
    engine = env.get("WEB_SEARCH_ENGINE_NAME", "").strip().lower()
    search_key = env.get("WEB_SEARCH_API_KEY", "").strip()
    search_url = env.get("WEB_SEARCH_URL", "").strip()
    has_credentials = bool(engine and search_key)
    if has_credentials and (engine != "petal" or search_url):
        return None
    return (
        "DeepResearch 搜索配置缺失：请配置完整的 Petal（URL 与凭据）"
        "或 Bocha（API Key）搜索凭据。"
    )


def _encode_deepresearch_config(config: dict[str, str]) -> bytes:
    """Encode one bounded JSON frame for the runner's anonymous stdin pipe."""
    frame = json.dumps(
        {"version": _DEEPRESEARCH_CONFIG_PROTOCOL_VERSION, "config": config},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(frame) > _DEEPRESEARCH_CONFIG_MAX_BYTES:
        raise ValueError("DeepResearch config frame exceeds 64 KiB")
    return frame


async def _write_deepresearch_config(proc, frame: bytes) -> None:
    """Write and close the one-shot config channel without logging its content."""
    if proc.stdin is None:
        raise RuntimeError("DeepResearch config stdin is unavailable")
    try:
        proc.stdin.write(frame)
        await proc.stdin.drain()
    finally:
        proc.stdin.close()
        wait_closed = getattr(proc.stdin, "wait_closed", None)
        if callable(wait_closed):
            with suppress(BrokenPipeError, ConnectionResetError):
                await wait_closed()


async def _stop_deepresearch_process(proc, timeout: float = 10) -> None:
    """Terminate, then kill and reap a DeepResearch child within bounded waits."""
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=timeout)


async def _iter_ndjson_lines(stream, read_size: int = 64 * 1024):
    """Read newline-delimited subprocess output without StreamReader's line limit."""
    if stream is None:
        return

    read = getattr(stream, "read", None)
    if not callable(read):
        async for line in stream:
            yield line
        return

    pending = bytearray()
    while True:
        chunk = await read(read_size)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            yield bytes(pending[:newline])
            del pending[:newline + 1]

    if pending:
        yield bytes(pending)


_SKIPPED_FEEDBACK_HANDLER_PAYLOAD = (
    '{"feedback":"","interaction_status":"skipped"}'
)


def _interaction_answer_has_user_input(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    selected = item.get("selected_options")
    if isinstance(selected, list):
        if any(
            not isinstance(option, str) or bool(option.strip())
            for option in selected
        ):
            return True
    elif selected is not None:
        return True
    custom_input = item.get("custom_input")
    if custom_input is None:
        return False
    return not isinstance(custom_input, str) or bool(custom_input.strip())


def _normalize_feedback_handler_resume_feedback(
    feedback: str,
    interaction_result: str,
) -> str:
    """Normalize ask_user_question output at the DeepResearch resume boundary."""
    if not interaction_result:
        return feedback
    try:
        result = json.loads(interaction_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("interaction_result 必须是合法 JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("interaction_result 必须是 JSON 对象")

    status = str(result.get("status") or "").strip().lower()
    if status not in {"answered", "skipped"}:
        raise ValueError(
            f"feedback_handler interaction_result status={status or 'missing'}"
        )
    answers = result.get("answers", [])
    if not isinstance(answers, list):
        raise ValueError("interaction_result.answers 必须是数组")
    has_user_input = any(
        _interaction_answer_has_user_input(item) for item in answers
    )
    if status == "skipped" and has_user_input:
        raise ValueError("skipped interaction_result 不能包含有效回答")
    if status == "skipped" or not has_user_input:
        return _SKIPPED_FEEDBACK_HANDLER_PAYLOAD
    return feedback


async def _call_deepresearch_stream_impl(
    *, _router_state: object | None = None, **kwargs
) -> str:
    token = _deepresearch_router_state_ctx.set(_router_state)
    try:
        return await getattr(deepresearch_stream, "_func")(**kwargs)
    finally:
        _deepresearch_router_state_ctx.reset(token)


@tool(
    name="deepresearch_stream",
    description=(
        "deepresearch skill 的首选且唯一入口。流式执行 DeepResearch 深度研究,"
        "进度经 chat 通道(chat.reasoning/task.start/task.complete/"
        "processing_status)实时推送到前端。执行到人机交互节点时返回 interrupted outcome,"
        "由 agent 调 ask_user_question 处理后,再以 action=resume 调本工具恢复。"
        "outline_interaction 中断返回 Main Agent，由 Main Agent 调 ask_user_question 处理后再以 action=resume 恢复；"
        "feedback_handler 恢复时须把 ask_user_question 完整返回值作为"
        " interaction_result 传入；工具会把 skipped 或 answered+空答案"
        ' 归一化为 feedback={"feedback":"","interaction_status":"skipped"}，'
        "不得默认选择任何选项或改写为自然语言反馈。"
        "不返回中间 chunk,只返回 outcome,避免污染 agent context。"
        "⚠不适用场景:PPT制作辅助研究、单点数据查询、快速搜索"
    ),
)
async def deepresearch_stream(  # pylint: disable=huawei-too-many-arguments
    action: str,
    query: str = "",
    conversation_id: str = "",
    feedback: str = "",
    node: str = "",
    file_name: str = "",
    interaction_result: str = "",
) -> str:
    """流式执行 DeepResearch,进度走 chat 通道,中断/完成返短 outcome.

    Args:
        action: "start"(首轮)或 "resume"(中断恢复)
        query: action=start 时的研究主题
        conversation_id: action=resume 必填;start 时空则脚本自生成并在 outcome 回传
        feedback: action=resume 时的 per-node 反馈 JSON(见 SKILL.md 映射表)
        node: action=resume 时的中断节点 id
        file_name: 报告文件名(不带后缀)
        interaction_result: feedback_handler 的 ask_user_question 完整返回 JSON

    Returns:
        JSON 串:
            {"status":"interrupted","conversation_id":"...","node_id":"...","marker":{...},"prompt":"..."}
            feedback_handler 的 marker 结构化透传给 agent 建交互卡；
            outline_interaction 中断返回 Main Agent，marker.outline_markdown 含格式化大纲供卡片 preview。
          {"status":"completed","conversation_id":"...","report_delivered":true,"report_chars":123}
            正常 chat 路由下报告已通过 chat.file 作为 Markdown 文件交付,不进入 tool outcome。
          {"status":"error","error":"..."}
    """
    from jiuwenclaw.agentserver.gateway_push.transport import (  # pylint: disable=import-outside-toplevel
        WebSocketGatewayPushTransport,
    )
    from jiuwenclaw.agentserver.deep_agent.ask_user_question_registry import (  # pylint: disable=import-outside-toplevel
        get_ask_request_context,
    )
    # ponytail: 跨模块私有访问，stream_router 重构需同步
    from jiuwenclaw.agentserver.tools.deepresearch.stream_router import (  # pylint: disable=import-outside-toplevel
        RouterState,
        _as_json_object,
        _format_outline_markdown,
        advance_stage,
        build_interrupt_prompt,
        collected_questions,
        complete_final_report_processing,
        is_outline_status_placeholder,
        route_chunk,
    )

    route = _get_route()
    interactive_ask, _, _, _ = get_ask_request_context()
    # Force HITL on: deepresearch SKILL.md requires feedback_handler
    # interruption for research direction clarification.  The upstream
    # ContextVar defaults to False when the frontend omits interactiveAsk,
    # which causes DEEPSEARCH_HITL="false" and the SDK skips the
    # feedback_handler node entirely.
    if not interactive_ask:
        interactive_ask = True
    outline_title_cache = _outline_title_cache(route)
    python_bin = _resolve_jiuwenclaw_python()
    script = _resolve_run_script()
    if not script:
        return '{"status":"error","error":"run_deepsearch.py not found"}'

    progress_file = os.path.join(
        tempfile.gettempdir(), f"dr_progress_{os.getpid()}_{conversation_id or 'new'}.jsonl"
    )

    if action == "start":
        argv = [
            python_bin, script, "run", "--config-stdin",
            "--query", query, "--progress-file", progress_file,
        ]
        if conversation_id:
            argv += ["--conversation-id", conversation_id]
    elif action == "resume":
        if not conversation_id or not node:
            return '{"status":"error","error":"resume requires conversation_id and node"}'
        if node == "feedback_handler" and interaction_result:
            try:
                feedback = _normalize_feedback_handler_resume_feedback(
                    feedback,
                    interaction_result,
                )
            except ValueError as exc:
                return json.dumps(
                    {"status": "error", "error": str(exc)},
                    ensure_ascii=False,
                )
        argv = [python_bin, script, "resume", "--config-stdin",
                "--conversation-id", conversation_id,
                "--feedback", feedback or "", "--node", node,
                "--interrupt-feedback", "", "--progress-file", progress_file]
    else:
        return f'{{"status":"error","error":"unknown action: {action}"}}'

    try:
        deepresearch_config = _build_deepresearch_request_config(
            interactive_ask=interactive_ask,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
        )
        child_env = _build_deepresearch_child_env(
            interactive_ask=interactive_ask,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
            runtime_config=deepresearch_config,
        )
        config_frame = _encode_deepresearch_config(deepresearch_config)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return json.dumps(
            {"status": "error", "error": f"child config build failed: {exc}"},
            ensure_ascii=False,
        )
    search_config_error = _validate_deepresearch_search_config(deepresearch_config)
    if search_config_error:
        return json.dumps(
            {
                "status": "error",
                "error_code": "search_config_missing",
                "error": search_config_error,
            },
            ensure_ascii=False,
        )

    push = WebSocketGatewayPushTransport()
    cached_titles = outline_title_cache.get(conversation_id, {}) if action == "resume" else {}
    existing_state = _deepresearch_router_state_ctx.get()
    resume_stage = (
        1
        if action == "resume" and node == "feedback_handler"
        else 0
    )
    resumes_final_report = (
        action == "resume" and node == "user_feedback_processor"
    )
    state = (
        existing_state
        if existing_state is not None
        else RouterState(
            section_titles=dict(cached_titles),
            current_stage=resume_stage,
            final_report_started=resumes_final_report,
        )
    )
    outcome_cid = conversation_id
    todo_path = None
    if route.get("session_id"):
        try:
            todo_path = deepresearch_todo_path(
                session_id=str(route["session_id"]),
                service_id=str(route.get("service_id") or "default"),
                agent_id=str(route.get("agent_id") or "default"),
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[deepresearch_stream] resolve todo path failed: %s",
                exc,
            )

    async def _send(payload: dict) -> bool:
        if todo_path is not None and payload.get("event_type") == "task.update":
            try:
                persist_deepresearch_task_update(payload, todo_path=todo_path)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "[deepresearch_stream] persist task.update failed: %s",
                    exc,
                )
        if not route.get("session_id") or not route.get("channel_id"):
            return False
        msg = {
            "request_id": route.get("request_id", ""),
            "channel_id": route["channel_id"],
            "session_id": route["session_id"],
            "payload": payload,
            "is_complete": False,
        }
        try:
            await push.send_push(msg)
            logger.info(
                "[deepresearch_stream] send_push event_type=%s agent=%s current_task=%s",
                payload.get("event_type", ""),
                payload.get("agent", ""),
                payload.get("current_task", ""),
            )
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[deepresearch_stream] send_push failed: %s", exc)
            return False

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f'{{"status":"error","error":"spawn failed: {exc}"}}'

    try:
        await _write_deepresearch_config(proc, config_frame)
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(_stop_deepresearch_process(proc))
        await asyncio.shield(cleanup_task)
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        await _stop_deepresearch_process(proc)
        return json.dumps(
            {"status": "error", "error": f"config delivery failed: {exc}"},
            ensure_ascii=False,
        )

    stderr_tail = bytearray()

    async def _drain_stderr() -> None:
        stream = proc.stderr
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 20000:
                del stderr_tail[:-20000]

    stderr_task = asyncio.create_task(_drain_stderr())
    outcome = {"status": "error", "error": "no terminal marker"}
    try:
        async for raw in _iter_ndjson_lines(proc.stdout):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 日志行(Parsed args / Loaded env 等)skip

            status = chunk.get("__deepsearch_status__")
            if status in ("started", "resuming"):
                cid = chunk.get("conversation_id", "")
                if cid:
                    outcome_cid = cid
                initial_stage = 1
                if action == "resume" and node == "outline_interaction":
                    initial_stage = 2
                elif action == "resume" and node == "user_feedback_processor":
                    initial_stage = 4
                for stage_payload in advance_stage(state, initial_stage):
                    await _send(stage_payload)
                await _send({"event_type": "chat.processing_status",
                             "is_processing": True,
                             "current_task": status})
                continue
            if status == "interrupted":
                node_id = chunk.get("agent", state.interrupt_node_id)
                # marker 结构化透传:agent 按 (1) §Stage3 读 marker.content/questions/prompt
                # 建 free_input/preview 卡(OutlineContent→Markdown、outline_ref=conversation_id、meta 轮次)
                marker = {k: v for k, v in chunk.items() if k != "__deepsearch_status__"}
                if node_id == "feedback_handler" and not marker.get("questions"):
                    questions = collected_questions(state)
                    if questions:
                        marker["questions"] = questions
                has_outline_payload = bool(marker.get("outline"))
                content_is_placeholder = (
                    not marker.get("content")
                    or is_outline_status_placeholder(marker.get("content"))
                )
                should_inject_outline = all((
                    node_id == "outline_interaction",
                    not has_outline_payload,
                    content_is_placeholder,
                    state.outline_parts,
                ))
                if should_inject_outline:
                    marker["outline"] = "".join(state.outline_parts)
                resolved_cid = (
                    chunk.get("conversation_id", outcome_cid)
                    or state.interrupt_conversation_id
                )
                if node_id == "outline_interaction":
                    outline_content = marker.get("outline") or marker.get("content")
                    if outline_content:
                        section_titles = extract_deepresearch_section_titles(
                            outline_content if isinstance(outline_content, str)
                            else json.dumps(outline_content, ensure_ascii=False)
                        )
                        if section_titles and resolved_cid:
                            outline_title_cache[str(resolved_cid)] = section_titles
                # UFP 报告不在 marker(脚本不透传),tool 注入累积的 report_parts(截断 6000)
                if node_id == "user_feedback_processor" and state.report_parts:
                    rpt = "".join(state.report_parts)
                    marker["report"] = rpt[:6000] + ("…\n(完整报告见最终产物)" if len(rpt) > 6000 else "")
                if node_id == "outline_interaction":
                    outline_json_str = "".join(state.outline_parts)
                    outline_data = _as_json_object(outline_json_str)
                    markdown_text = _format_outline_markdown(outline_data) if outline_data is not None else None
                    if not markdown_text:
                        markdown_text = "(大纲内容未能从流中获取，请直接确认或手动输入修改意见)"
                    marker["outline_markdown"] = markdown_text
                outcome = {"status": "interrupted",
                           "conversation_id": resolved_cid,
                           "node_id": node_id,
                           "marker": marker,
                           "prompt": build_interrupt_prompt(node_id, state, marker, query)}
                # The runner emits this marker before its async cleanup persists the
                # graph checkpoint. Keep consuming to EOF so it can exit naturally;
                # breaking here makes finally terminate the resumable subprocess.
                continue
            if status == "completed":
                final_result = chunk.get("final_result")
                response_content = (
                    final_result.get("response_content", "")
                    if isinstance(final_result, dict)
                    else ""
                )
                if not response_content:
                    outcome = {
                        "status": "error",
                        "conversation_id": chunk.get("conversation_id", outcome_cid),
                        "error_code": "empty_report",
                        "error": "completed marker missing final_result.response_content",
                    }
                    break
                if not state.final_report_started:
                    outcome = {
                        "status": "error",
                        "conversation_id": chunk.get("conversation_id", outcome_cid),
                        "error_code": "incomplete_section_progress",
                        "error": (
                            "completed marker arrived before all expected chapter "
                            "completion events"
                        ),
                    }
                    break
                for stage_payload in complete_final_report_processing(state):
                    await _send(stage_payload)
                for stage_payload in advance_stage(state, 4):
                    await _send(stage_payload)
                has_chat_route = bool(
                    route.get("session_id") and route.get("channel_id")
                )
                if response_content and has_chat_route:
                    citation_artifacts = _normalize_citation_artifacts(chunk)
                    try:
                        artifacts = await _write_report_artifacts_stream(
                            final_result,
                            file_name,
                            chunk.get("conversation_id", outcome_cid),
                            citation_artifacts,
                        )
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        outcome = {
                            "status": "error",
                            "conversation_id": chunk.get("conversation_id", outcome_cid),
                            "error_code": "report_file_write_failed",
                            "error": str(exc),
                        }
                        break
                    # Deliver all generated artifacts (MD + HTML)
                    files_to_deliver = [
                        {"path": v, "name": os.path.basename(v)}
                        for v in artifacts.values()
                    ]
                    file_payload = {
                        "event_type": "chat.file",
                        "files": files_to_deliver,
                    }
                    markdown_index = list(artifacts).index("md")
                    bundle = _build_related_artifact_bundle(
                        citation_artifacts, markdown_index
                    )
                    if bundle:
                        file_payload["metadata"] = {"artifactBundle": bundle}
                    report_delivered = await _send(file_payload)
                else:
                    outcome = {
                        "status": "error",
                        "conversation_id": chunk.get("conversation_id", outcome_cid),
                        "error_code": "report_file_delivery_failed",
                        "error": "Markdown report file route is unavailable",
                    }
                    break

                if report_delivered:
                    for stage_payload in advance_stage(state, 4, complete=True):
                        await _send(stage_payload)
                    outcome = {
                        "status": "completed",
                        "conversation_id": chunk.get("conversation_id", outcome_cid),
                        "report_delivered": True,
                        "report_chars": len(response_content),
                    }
                elif response_content and has_chat_route:
                    outcome = {
                        "status": "error",
                        "conversation_id": chunk.get("conversation_id", outcome_cid),
                        "error_code": "report_file_delivery_failed",
                        "error": "Report files could not be delivered",
                        "report_path": artifacts.get("md", ""),
                    }
                break
            if status == "error":
                outcome = {
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": chunk.get("error_code", "workflow_error"),
                    "error": chunk.get("error", "deepresearch workflow failed"),
                }
                break
            # progress chunk
            if chunk.get("agent") == "outline":
                outline_content = chunk.get("content")
                if outline_content:
                    section_titles = extract_deepresearch_section_titles(
                        outline_content if isinstance(outline_content, str)
                        else json.dumps(outline_content, ensure_ascii=False)
                    )
                    if section_titles:
                        state.section_titles.update(section_titles)
                        state.authoritative_section_indices.update(section_titles)
            for payload in route_chunk(chunk, state):
                await _send(payload)
    finally:
        await _stop_deepresearch_process(proc)
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except Exception:  # pylint: disable=broad-exception-caught
            stderr_task.cancel()
        if outcome.get("status") == "error":
            outcome["returncode"] = proc.returncode
            captured_stderr = stderr_tail.decode("utf-8", errors="replace")
            if captured_stderr.strip():
                outcome["stderr_tail"] = captured_stderr
        try:
            os.remove(progress_file)
        except OSError:
            pass

    # Tool failures must reach the Main Agent as tool results. ``chat.error``
    # terminates the parent request before the Agent can explain the failure.
    if outcome.get("status") in {"completed", "error", "cancelled"}:
        _clear_outline_title_cache(route, outcome.get("conversation_id", outcome_cid))
    # outline_interaction interrupted outcome is returned to Main Agent for review.
    # Loop guard fires ONLY on TRUE accepted-loop (accepted resume → still interrupted).
    # All other cases (revise_*, missing, malformed JSON) fail-open: return outcome
    # so the Main Agent can dispatch ask_user_question and resume with the user's choice.
    if (
        outcome.get("status") == "interrupted"
        and outcome.get("node_id") == "outline_interaction"
        and action == "resume"
        and node == "outline_interaction"
    ):
        try:
            envelope = json.loads(feedback) if feedback else {}
            interrupt_feedback = (
                envelope.get("interrupt_feedback")
                if isinstance(envelope, dict)
                else None
            )
        except (json.JSONDecodeError, TypeError):
            interrupt_feedback = None
        if interrupt_feedback == "accepted":
            loop_error = {
                "error_code": "outline_auto_resume_loop",
                "error": "outline_interaction repeated after automatic acceptance",
            }
            return json.dumps(
                {
                    "status": "error",
                    "conversation_id": outcome.get("conversation_id", outcome_cid),
                    **loop_error,
                },
                ensure_ascii=False,
            )
    return json.dumps(outcome, ensure_ascii=False)


def _resolve_skill_root() -> str:
    """定位 deepresearch skill 目录。

    优先 JIUWENCLAW_SHARED_SKILLS_DIRS(sidecar 的 cwd 不一定是仓根——sidecar 常跑在
    vendor/jiuwenclaw 下,os.getcwd() 会落空);使用当前平台的路径分隔符。
    fallback: cwd/office-claw-skills(仅当 sidecar cwd 恰为仓根时命中)。
    """
    candidates: list[str] = []
    dirs_env = read_env("JIUWENCLAW_SHARED_SKILLS_DIRS", "")
    if dirs_env:
        for d in dirs_env.split(os.pathsep):
            d = d.strip()
            if d:
                candidates.append(os.path.join(d, "deepresearch"))
    # fallback:cwd 相对(仅仓根 cwd 命中)
    candidates.append(os.path.join(os.getcwd(), "office-claw-skills", "deepresearch"))
    for c in candidates:
        if os.path.exists(os.path.join(c, "scripts", "run_deepsearch.py")):
            return c
    return ""


def _resolve_jiuwenclaw_python() -> str:
    """复用当前 JiuwenClaw 进程的 Python 解释器。"""
    return sys.executable


def _resolve_run_script() -> str:
    """定位 run_deepsearch.py。"""
    root = _resolve_skill_root()
    if not root:
        return ""
    p = os.path.join(root, "scripts", "run_deepsearch.py")
    return p if os.path.exists(p) else ""


def enable_deepresearch() -> bool:
    """检查 DeepResearch 工具是否启用.

    Returns:
        是否启用 DeepResearch 工具
    """

    try:
        cfg = get_config()
        if not bool(cfg.get("enable_deepresearch", True)):
            return False
        return True
    except Exception:
        return False


def get_deepresearch_tools() -> list:
    """获取 DeepResearch 工具列表.

    Returns:
        工具列表
    """
    if not enable_deepresearch():
        return []
    if not _deepresearch_dependency_available():
        logger.warning(
            "DeepResearch tools disabled because optional dependency is missing: %s",
            _DEEPRESEARCH_DEPENDENCY,
        )
        return []
    from jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools import (  # pylint: disable=import-outside-toplevel
        deepresearch_commit_rewrite,
        deepresearch_generate_rewrite_html,
        deepresearch_prepare_rewrite,
    )

    return [
        deepresearch_stream,
        deepresearch_prepare_rewrite,
        deepresearch_commit_rewrite,
        deepresearch_generate_rewrite_html,
    ]


__all__ = [
    "deepresearch_create_task",
    "deepresearch_get_status",
    "deepresearch_list_tasks",
    "deepresearch_cancel_task",
    "deepresearch_get_result",
    "deepresearch_run_task",
    "deepresearch_stream",
    "get_deepresearch_tools",
    "push_deepresearch_route",
    "reset_deepresearch_route",
]

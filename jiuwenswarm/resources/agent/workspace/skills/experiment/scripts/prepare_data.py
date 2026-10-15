"""Prepare declared datasets without silently substituting another source."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from _network import (
    download_retries,
    download_timeout_seconds,
    download_variants,
    max_auto_download_bytes,
    probe_size,
)
from contracts import ExperimentModuleInput
from dataset_resolver import DatasetResolution, resolve_dataset_source
from io_utils import (
    build_subprocess_environment,
    expand_placeholders,
    portable_arguments,
    portable_command,
    read_json,
    relative_to_run,
    safe_join,
    slugify,
    write_json_atomic,
    write_text_atomic,
)
from runtime_models import (
    DatasetPreparationDefinition,
    DownloadProgress,
    ImplementationManifest,
    StageBlocker,
)


DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024 * 1024

#: 许可字段里的括注（"MIT (research use)" / "CC-BY-NC-4.0（研究用途）"）不是许可本身。
_LICENSE_ANNOTATION = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")


def _license_key(value: object) -> str:
    """许可比较用的归一键：去掉括注与大小写/分隔符差异。

    模块二常把许可写成 "MIT (research use)"、"CC-BY-NC-4.0（研究用途）"，
    而仓库元数据只写 "mit"、"CC-BY-NC-4.0"。两者是同一个许可，字面不等
    并不代表许可冲突；按原文比较会把这类正常写法误判成阻断项。
    真正的差异（MIT vs Apache-2.0）归一后仍然不等，照旧阻断。
    """
    if not value:
        return ""
    text = _LICENSE_ANNOTATION.sub(" ", str(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)

#: 体积探测最多探几个文件（超出即视为「总量未知」，直接放行）。
MAX_SIZE_PROBES = 25
#: 体积探测的总时长上限。探测是为了**避免**白跑大下载，它自己绝不能成为新的超时源。
MAX_SIZE_PROBE_SECONDS = 20


def _human_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return f"{value:.1f} TiB"

log = logging.getLogger("experiment.prepare_data")


def prepare_datasets(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
    *,
    allow_downloads: bool = True,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    time_budget_seconds: float | None = None,
) -> tuple[dict[str, str], list[str], StageBlocker | None, DownloadProgress | None]:
    prepared: dict[str, str] = {}
    metadata: list[dict[str, object]] = []
    blockers: list[str] = []
    suggestions: list[str] = []
    warnings: list[str] = []
    started = time.monotonic()
    budget = float(time_budget_seconds) if time_budget_seconds else 0.0
    deadline = started + budget if budget > 0 else None
    cache_root = _dataset_cache_root()

    def _progress(dataset_name: str, total: int, done: list[str], pending: list[str]) -> DownloadProgress:
        return DownloadProgress(
            dataset_name=dataset_name,
            completed_files=done,
            pending_files=pending,
            total_files=total,
            bytes_on_disk=_directory_size(run_dir / "data"),
            elapsed_seconds=round(time.monotonic() - started, 2),
            time_budget_seconds=budget,
            resume_hint=(
                "用**完全相同的参数**（同一个 run_id、同一个 max_download_gb）再调用一次 "
                "experiment_prepare_data 即可续跑：已下完的文件会跳过，未下完的从断点继续。"
                "**不要**改小 max_download_gb，**不要**加 --no-downloads，**不要**直接 REPLAN。"
            ),
        )

    for dataset in request.data_plan.datasets:
        if deadline is not None and time.monotonic() > deadline and prepared:
            # 时间预算用尽但已有数据落盘：如实回报进度，让 agent 再来一次，
            # 而不是把"没下完"包装成失败（那会把整轮实验判死）。
            log.info("下载时间预算 %.0fs 用尽，回报进度等待续跑", budget)
            return prepared, warnings, None, _progress(dataset.name, len(request.data_plan.datasets), [], [])
        configured = manifest.datasets.get(
            dataset.name,
            f"data/{slugify(dataset.name)}",
        )
        try:
            dataset_path = safe_join(run_dir, configured)
        except ValueError as exc:
            blockers.append(f"数据集{dataset.name}路径无效: {exc}")
            suggestions.append("把manifest.datasets路径改为run_dir内的相对路径")
            continue

        declared_source_url = str(dataset.source_url) if dataset.source_url else None
        source_reference = declared_source_url
        resolution: DatasetResolution | None = None
        cache_path = (
            _dataset_cache_path(cache_root, dataset.name, declared_source_url)
            if cache_root is not None else None
        )
        if (
            not dataset_path.exists()
            and cache_path is not None
            and _validated_cache_entry(
                cache_path,
                dataset_name=dataset.name,
                declared_source_url=declared_source_url,
                cache_root=cache_root,
            )
        ):
            _materialize_cached_dataset(
                cache_path,
                dataset_path,
                use_hardlinks=not _requires_preprocessing(
                    request.data_plan.preprocessing_pipeline
                ),
            )
            warnings.append(
                f"数据集{dataset.name}已从校验通过的共享缓存复用，无需重复下载"
            )
        marker = _download_marker(dataset_path)
        if _download_is_complete(
            dataset_path,
            dataset_name=dataset.name,
            declared_source_url=declared_source_url,
        ):
            prepared[dataset.name] = str(dataset_path)
            source_reference = str(
                marker.get("source_reference") or marker.get("source_url") or ""
            ) or declared_source_url
        elif dataset_path.exists() and _contains_user_data(dataset_path):
            if dataset.readiness == "download":
                blockers.append(
                    f"数据集{dataset.name}路径已有内容，但缺少匹配的下载完成标记"
                )
                suggestions.append(
                    _manual_dataset_suggestion(dataset.name, configured)
                )
                continue
            prepared[dataset.name] = str(dataset_path)
        elif dataset.readiness == "apply":
            blockers.append(f"数据集{dataset.name}需要申请权限，当前不可直接执行")
            suggestions.append(
                "完成数据访问申请后，把真实数据放入指定目录并把readiness更新为available"
            )
            suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
            continue
        elif not allow_downloads:
            blockers.append(
                f"数据集{dataset.name}本地不存在，且当前未授权联网下载"
            )
            suggestions.append(
                "当前调用处于离线模式；移除--no-downloads或把allow_downloads设为true"
            )
            suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
            continue
        else:
            resolution = resolve_dataset_source(
                dataset.name,
                source_url=declared_source_url,
                allow_compatible_substitutes=True,
                search_context=_dataset_search_context(request, dataset.name),
            )
            resolution_path = safe_join(
                run_dir,
                f"data/source-resolution/{slugify(dataset.name)}.json",
            )
            write_json_atomic(resolution_path, resolution.to_dict())
            if resolution.status != "RESOLVED" or resolution.selected is None:
                candidate_text = ", ".join(
                    f"{item.provider}:{item.dataset_id}"
                    for item in resolution.candidates[:5]
                )
                detail = (
                    f"；候选为 {candidate_text}" if candidate_text else ""
                )
                provider_errors = "; ".join(
                    f"{name}={error}"
                    for name, error in resolution.provider_errors.items()
                )
                if provider_errors:
                    detail += f"；检索错误: {provider_errors}"
                blockers.append(
                    f"数据集{dataset.name}无法根据名称唯一确定可下载来源"
                    f"（{resolution.status}）{detail}"
                )
                if "declared_source" in resolution.provider_errors:
                    # 两种原因都用这个键：落地页（不是数据文件）与文件不存在
                    # （仓库对、文件名猜错）。后者的真实文件清单就在上面的
                    # 「检索错误」里，让 planner 直接照抄，别再靠猜。
                    suggestions.append(
                        "模块二提供的 source_url 不是可直接下载的数据文件——要么是论文/仓库"
                        "落地页，要么是该仓库里不存在的文件路径（真实文件清单见上面的"
                        "「检索错误: declared_source=…」）；请按清单里的真实文件名逐字符"
                        "修正 source_url，或重新规划为已公开且可执行的数据集"
                    )
                if resolution.status == "SUBSTITUTE_AVAILABLE":
                    replacements = "; ".join(
                        f"{item.title} ({item.landing_url})"
                        for item in resolution.candidates[:5]
                    )
                    suggestions.append(
                        "已找到可下载的替代候选: " + replacements
                        + "。请模块二核对任务字段与指标后，整体替换数据集名称、"
                          "实验矩阵、基线和成功标准；模块三不会把替代数据冒充原数据执行。"
                    )
                suggestions.append(
                    "让模块二补充公开数据直链，或从检索候选中人工确认准确数据版本"
                )
                suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
                # 一个必需数据集已经确定不可得时，继续下载后面的（可能很大的）
                # 数据集不会让本轮变得可执行，只会浪费时间/流量，并掩盖首个可修复
                # 的 planning blocker。立即把完整解析证据交回模块二。
                primary_experiments = getattr(
                    request.experiment_plan, "primary_experiments", None
                )
                if not primary_experiments:
                    primary_experiments = [
                        str(row[0])
                        for row in getattr(
                            request.experiment_plan, "experiment_matrix", []
                        )
                        if isinstance(row, (list, tuple)) and row
                    ]
                return prepared, warnings, StageBlocker(
                    reason="数据准备失败",
                    affected_experiment_ids=list(dict.fromkeys(primary_experiments)),
                    blockers=blockers,
                    suggested_changes=list(dict.fromkeys(suggestions)),
                ), None
            selected = resolution.selected
            if declared_source_url is None and not dataset.license and not selected.license:
                blockers.append(
                    f"数据集{dataset.name}已检索到{selected.provider}:{selected.dataset_id}，"
                    "但模块二和数据仓库都没有明确许可信息"
                )
                suggestions.append("确认数据集许可后在data_plan.datasets[].license中登记")
                suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
                continue
            source_reference = selected.landing_url
            planned_urls = list(selected.download_urls)
            # 下载前预估闸门：仓库根 URL / 按名检索会绑到该仓库**全部**数据文件，
            # 大仓库（多 config / 多 split / 历史版本）会一次拉下几十上百 GB，
            # 既超配额又必然撞宿主 300s 工具超时。确知超阈值时**不下载**，
            # 把候选文件清单交回模块二挑一个，而不是先跑一半再失败。
            estimated_total, planned_files = estimate_bundle_size(planned_urls)
            auto_limit = max_auto_download_bytes()
            if estimated_total is not None and auto_limit and estimated_total > auto_limit:
                listing = "\n".join(
                    f"  - {_url_tail(url)}  ~{_human_bytes(size) if size else '未知'}"
                    for url, size in planned_files
                )
                blockers.append(
                    f"数据集{dataset.name}解析到{len(planned_urls)}个文件，预估总量 "
                    f"{_human_bytes(estimated_total)} 超过自动下载上限 "
                    f"{_human_bytes(auto_limit)}（可用 JIUWENSWARM_MAX_AUTO_DOWNLOAD_GB 覆盖）。"
                    f"候选文件：\n{listing}"
                )
                suggestions.append(
                    "请模块二把 source_url 改成**单个具体文件**的直链"
                    "（Hugging Face 用 `.../resolve/<rev>/<file>`），"
                    "或换用字段与规模匹配的公开数据集；"
                    "确实需要整套文件时，把数据手动放入本次 run_dir 并保持相同 run_id 续跑。"
                )
                suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
                _write_download_plan(
                    run_dir, dataset.name,
                    planned_files=planned_files,
                    estimated_total=estimated_total,
                    auto_limit=auto_limit,
                    decision="blocked",
                )
                continue
            _write_download_plan(
                run_dir, dataset.name,
                planned_files=planned_files,
                estimated_total=estimated_total,
                auto_limit=auto_limit,
                decision="download",
            )
            try:
                download_target = cache_path or dataset_path
                downloaded_names, extracted = _download_dataset_bundle_atomically(
                    planned_urls,
                    download_target,
                    dataset_name=dataset.name,
                    source_reference=source_reference,
                    declared_source_url=declared_source_url,
                    provider=selected.provider,
                    dataset_id=selected.dataset_id,
                    revision=selected.revision,
                    max_download_bytes=max_download_bytes,
                    planned_files=planned_files,
                    deadline=deadline,
                )
                if cache_path is not None:
                    _seal_cache_entry(cache_path)
                    _materialize_cached_dataset(
                        cache_path,
                        dataset_path,
                        use_hardlinks=not _requires_preprocessing(
                            request.data_plan.preprocessing_pipeline
                        ),
                    )
                if not extracted:
                    warnings.append(
                        f"数据集{dataset.name}下载完成；文件未检测为压缩包，无需自动解压: "
                        + ", ".join(downloaded_names[:5])
                    )
                if (
                    dataset.license
                    and selected.license
                    and _license_key(dataset.license) != _license_key(selected.license)
                ):
                    # 许可冲突**阻断**而不是告警：比赛要求可核验来源与合规复现，
                    # "先下下来再人工核对"等于把合规判断留给后面某个可能不看的环节。
                    # 数据已经落盘（中断也无害，下次可复用），只是不登记为 prepared。
                    blockers.append(
                        f"[data] 数据集{dataset.name}许可冲突：模块二声明="
                        f"{dataset.license}，仓库元数据={selected.license}。"
                        f"两者不一致时不得自动继续——请模块二核实后统一为一个可核验的许可。"
                    )
                    suggestions.append(
                        f"核对 {dataset.name} 的官方许可页后，把 data_plan.datasets[].license "
                        f"改成与数据仓库一致的值（或换用许可明确的等价数据集）"
                    )
                    suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
                    continue
                prepared[dataset.name] = str(dataset_path)
            except _TransferDeadlineReached as exc:
                # 时间预算用尽：**不是失败**。已落盘的文件与 .partial 都还在，
                # 下次调用（同一 run_id）继续。绝不能落到下面的按名称检索回退——
                # 那会拿着半个包去换数据集，正是我们要避免的。
                log.info("下载时间预算用尽，回报进度等待续跑: %s", exc)
                pending = [item for item in planned_urls
                           if _url_filename(item) not in {
                               _url_filename(u) for u in planned_urls[:len(prepared)]
                           }]
                return prepared, warnings, None, _progress(
                    dataset.name, len(planned_urls), [], pending,
                )
            except Exception as primary_exc:
                # 模块二直链优先；若它在传输阶段失败，再按精确数据集名称检索。
                # 只有唯一匹配才自动回退，歧义仍 fail-closed，且两次尝试都落审计记录。
                fallback = (
                    resolve_dataset_source(
                        dataset.name,
                        source_url=None,
                        allow_compatible_substitutes=True,
                        search_context=_dataset_search_context(request, dataset.name),
                    )
                    if declared_source_url is not None
                    else None
                )
                if fallback is None or fallback.status != "RESOLVED" or fallback.selected is None:
                    detail = (
                        f"；备用检索状态={fallback.status}"
                        if fallback is not None else ""
                    )
                    blockers.append(
                        f"数据集{dataset.name}下载或解压失败: {primary_exc}{detail}"
                    )
                    suggestions.append("核对数据来源、访问权限、许可、下载上限和磁盘空间")
                    suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
                    continue
                fallback_selected = fallback.selected
                source_reference = fallback_selected.landing_url
                fallback_record = fallback.to_dict()
                fallback_record.update({
                    "fallback_from": declared_source_url,
                    "primary_download_error": str(primary_exc),
                })
                write_json_atomic(resolution_path, fallback_record)
                try:
                    download_target = cache_path or dataset_path
                    downloaded_names, extracted = _download_dataset_bundle_atomically(
                        list(fallback_selected.download_urls),
                        download_target,
                        dataset_name=dataset.name,
                        source_reference=source_reference,
                        declared_source_url=declared_source_url,
                        provider=fallback_selected.provider,
                        dataset_id=fallback_selected.dataset_id,
                        revision=fallback_selected.revision,
                        max_download_bytes=max_download_bytes,
                    )
                    if cache_path is not None:
                        _seal_cache_entry(cache_path)
                        _materialize_cached_dataset(
                            cache_path,
                            dataset_path,
                            use_hardlinks=not _requires_preprocessing(
                                request.data_plan.preprocessing_pipeline
                            ),
                        )
                except Exception as fallback_exc:
                    blockers.append(
                        f"数据集{dataset.name}主地址与唯一备用来源均下载失败: "
                        f"primary={primary_exc}; fallback={fallback_exc}"
                    )
                    suggestions.append("核对数据来源、访问权限、许可、下载上限和磁盘空间")
                    suggestions.append(_manual_dataset_suggestion(dataset.name, configured))
                    continue
                warnings.append(
                    f"数据集{dataset.name}主地址下载失败，已从唯一匹配的"
                    f"{fallback_selected.provider}备用源下载: "
                    + ", ".join(downloaded_names[:5])
                )
                if not extracted:
                    warnings.append(
                        f"数据集{dataset.name}备用源文件不是压缩包，无需自动解压"
                    )
                prepared[dataset.name] = str(dataset_path)

        if dataset_path.exists():
            if not _contains_user_data(dataset_path):
                blockers.append(
                    f"数据集{dataset.name}路径存在但没有任何非空数据文件"
                )
                suggestions.append(
                    "放入真实数据文件，或把readiness改为与实际状态一致"
                )
                continue
            prepared[dataset.name] = str(dataset_path)

        if _requires_preprocessing(request.data_plan.preprocessing_pipeline):
            preparation = manifest.dataset_preparations[dataset.name]
            try:
                _run_preparation(
                    dataset.name,
                    Path(prepared[dataset.name]),
                    preparation,
                    request.data_plan.preprocessing_pipeline,
                    request.data_plan.split_strategy,
                    run_dir,
                    source_url=source_reference,
                    scale_estimate=dataset.scale_estimate,
                    timeout_seconds=(
                        request.execution_config.timeout_seconds
                        or request.resource_constraints.time_budget_days * 86400
                    ),
                )
            except Exception as exc:
                blockers.append(f"数据集{dataset.name}预处理失败: {exc}")
                suggestions.append("查看数据预处理日志并修复实现，不要跳过计划步骤")
                prepared.pop(dataset.name, None)
                continue

        try:
            fingerprint = dataset_fingerprint(Path(prepared[dataset.name]))
        except (OSError, ValueError) as exc:
            blockers.append(f"数据集{dataset.name}完整性计算失败: {exc}")
            suggestions.append("检查数据文件、符号链接和读取权限")
            prepared.pop(dataset.name, None)
            continue

        metadata.append(
            {
                "name": dataset.name,
                "declared_source_url": declared_source_url,
                "resolved_source_url": source_reference,
                "source_resolution": resolution.to_dict() if resolution else None,
                "license": dataset.license,
                "readiness": dataset.readiness,
                "path": relative_to_run(Path(prepared[dataset.name]), run_dir),
                "preprocess_required": dataset.preprocess_required,
                "split_strategy": request.data_plan.split_strategy,
                "preprocessing_pipeline": request.data_plan.preprocessing_pipeline,
                "fingerprint": fingerprint,
            }
        )

    write_json_atomic(run_dir / "data" / "datasets.json", metadata)
    if blockers:
        return {}, warnings, StageBlocker(
            reason="数据准备未通过",
            affected_experiment_ids=sorted(
                {row[0] for row in request.experiment_plan.experiment_matrix}
            ),
            blockers=blockers,
            suggested_changes=list(dict.fromkeys(suggestions)),
        ), None
    return prepared, warnings, None, None


def _dataset_search_context(request: ExperimentModuleInput, dataset_name: str) -> str:
    """Build a domain-agnostic discovery query from explicit planning fields."""

    values: list[str] = [dataset_name]
    domain = getattr(request, "domain", None)
    domain_name = getattr(domain, "domain_name", None)
    if isinstance(domain_name, str):
        values.append(domain_name)
    method = getattr(request, "method_design", None)
    for field in ("research_goal", "core_mechanism"):
        value = getattr(method, field, None)
        if isinstance(value, str):
            values.append(value)
    plan = getattr(request, "experiment_plan", None)
    for field in ("metrics", "objectives"):
        value = getattr(plan, field, None)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
    data_plan = getattr(request, "data_plan", None)
    expected_size = getattr(data_plan, "expected_size", None)
    if isinstance(expected_size, dict):
        for key in ("task_type", "label_column", "modality"):
            if expected_size.get(key):
                values.append(str(expected_size[key]))
    return " ".join(item for item in values if item).strip()


def _run_preparation(
    dataset_name: str,
    dataset_path: Path,
    definition: DatasetPreparationDefinition,
    preprocessing_pipeline: list[str],
    split_strategy: dict[str, object],
    run_dir: Path,
    *,
    source_url: str | None,
    scale_estimate: str,
    timeout_seconds: int | None,
) -> None:
    variables = {
        "run_dir": str(run_dir),
        "dataset": dataset_name,
        "dataset_path": str(dataset_path),
    }
    metadata_path = safe_join(
        run_dir,
        f"data/{slugify(dataset_name)}/preparation-input.json",
    )
    marker_path = _resolve_run_artifact(
        expand_placeholders(definition.marker_path, variables),
        run_dir,
    )
    marker_payload = {
        "dataset": dataset_name,
        "source_url": source_url,
        "scale_estimate": scale_estimate,
        "preprocessing_pipeline": preprocessing_pipeline,
        "split_strategy": split_strategy,
        "implementation_command": portable_arguments(
            definition.command, run_dir=run_dir
        ),
        "implementation_cwd": definition.cwd,
        "notes": definition.notes,
    }
    if marker_path.is_file() and read_json(marker_path) == marker_payload:
        return
    write_json_atomic(metadata_path, marker_payload)
    variables["metadata_path"] = str(metadata_path)
    variables["marker_path"] = str(marker_path)
    command = [
        expand_placeholders(argument, variables) for argument in definition.command
    ]
    cwd = safe_join(run_dir, definition.cwd)
    if not cwd.is_dir():
        raise ValueError(f"preparation cwd does not exist: {definition.cwd}")
    environment = build_subprocess_environment(
        definition.env,
        definition.required_env,
    )
    log_path = safe_join(run_dir, f"logs/data-{slugify(dataset_name)}.log")
    display_command = portable_command(command, run_dir=run_dir)
    write_text_atomic(
        log_path,
        "command: " + display_command + "\n--- combined output ---\n",
    )
    with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        log_file.write(f"\n--- returncode: {completed.returncode} ---\n")
    if completed.returncode != 0:
        raise ValueError(
            f"preparation command exited with code {completed.returncode}; "
            f"see {relative_to_run(log_path, run_dir)}"
        )
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(marker_path, marker_payload)


def _resolve_run_artifact(value: str, run_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        root = run_dir.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"preparation artifact escapes run directory: {value}")
        return resolved
    return safe_join(run_dir, value)


def _requires_preprocessing(steps: list[str]) -> bool:
    no_op_values = {"identity", "none", "noop", "no-op", "无需预处理"}
    return any(step.strip().lower() not in no_op_values for step in steps)


def _download_marker(dataset_path: Path) -> dict[str, object]:
    marker = dataset_path / ".download-complete.json"
    if not marker.is_file():
        return {}
    try:
        payload = read_json(marker)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _download_is_complete(
    dataset_path: Path,
    *,
    dataset_name: str,
    declared_source_url: str | None,
) -> bool:
    payload = _download_marker(dataset_path)
    if not payload:
        return False
    source_matches = (
        payload.get("source_reference") == declared_source_url
        or payload.get("source_url") == declared_source_url
        or payload.get("declared_source_url") == declared_source_url
    ) if declared_source_url else payload.get("dataset_name") == dataset_name
    return bool(
        payload.get("completed") is True
        and source_matches
        and _contains_user_data(dataset_path)
    )


def _dataset_cache_root() -> Path | None:
    """Return the optional cross-run raw-dataset cache.

    The cache is deliberately opt-in through ``JIUWENSWARM_DATASET_CACHE``.
    paper-gen sets it to a project-local directory. A direct Module 3 caller
    can leave it unset and retain the original run-local behavior.
    """

    configured = os.environ.get("JIUWENSWARM_DATASET_CACHE", "").strip()
    if not configured:
        return None
    root = Path(configured).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    _prune_dataset_cache(root)
    return root


def _prune_dataset_cache(cache_root: Path) -> None:
    """Remove cache debris that cannot be reused safely.

    Completed entries are validated lazily when their dataset is requested, so
    startup does not hash every large cached dataset. Incomplete staging data is
    retained only when it has matching resume metadata *and* at least one byte
    of reusable payload. Everything else is an abandoned failed-run artifact.
    """

    root = cache_root.resolve()
    for child in root.iterdir():
        try:
            if child.is_symlink():
                _remove_cache_entry(child, root)
                continue
            if not child.is_dir():
                _remove_cache_entry(child, root)
                continue
            if child.name.startswith(".") and child.name.endswith("-download"):
                if not _staging_download_is_resumable(child):
                    log.info("删除不可续传的数据缓存暂存目录: %s", child)
                    _remove_cache_entry(child, root)
                continue
            # A published entry must at least have a completion marker. Full
            # content verification remains lazy in _validated_cache_entry().
            if not (child / ".download-complete.json").is_file():
                log.info("删除未完成且不可续传的数据缓存条目: %s", child)
                _remove_cache_entry(child, root)
        except OSError as exc:
            log.warning("清理数据缓存条目失败（本轮忽略）%s: %s", child, exc)


def _staging_download_is_resumable(staging: Path) -> bool:
    marker_path = staging / ".download-resume.json"
    try:
        marker = read_json(marker_path)
    except (OSError, ValueError):
        return False
    if not isinstance(marker, dict) or not isinstance(marker.get("urls"), list):
        return False
    urls = marker["urls"]
    if not urls or not all(isinstance(url, str) and url for url in urls):
        return False
    completed = marker.get("completed_files") or []
    if isinstance(completed, list) and any(
        isinstance(name, str)
        and (staging / name).is_file()
        and (staging / name).stat().st_size > 0
        for name in completed
    ):
        return True
    # A partial file is reusable only under the same safety conditions used by
    # _download_once: meaningful size, matching URL and exact byte count.
    for partial in staging.glob(".*.partial"):
        if not partial.is_file() or partial.stat().st_size < _RESUME_MIN_BYTES:
            continue
        base_name = partial.name[1:-len(".partial")]
        meta = _read_partial_meta(staging / f".{base_name}{_PARTIAL_META_SUFFIX}") or {}
        if (
            meta.get("url") in urls
            and meta.get("bytes_done") == partial.stat().st_size
        ):
            return True
    return False


def _dataset_cache_path(
    cache_root: Path,
    dataset_name: str,
    declared_source_url: str | None,
) -> Path:
    identity = json.dumps(
        {
            "dataset_name": dataset_name.strip(),
            "declared_source_url": declared_source_url or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return cache_root / f"{slugify(dataset_name)}-{digest}"


def _validated_cache_entry(
    cache_path: Path,
    *,
    dataset_name: str,
    declared_source_url: str | None,
    cache_root: Path,
) -> bool:
    """Accept only complete cache entries whose stored fingerprint still matches."""

    if not _download_is_complete(
        cache_path,
        dataset_name=dataset_name,
        declared_source_url=declared_source_url,
    ):
        return False
    marker = _download_marker(cache_path)
    expected = marker.get("fingerprint")
    if not isinstance(expected, dict) or not expected.get("sha256"):
        _remove_cache_entry(cache_path, cache_root)
        return False
    try:
        actual = _cache_payload_fingerprint(cache_path)
    except (OSError, ValueError):
        _remove_cache_entry(cache_path, cache_root)
        return False
    if actual != expected:
        log.warning("数据缓存指纹不匹配，删除损坏条目: %s", cache_path)
        _remove_cache_entry(cache_path, cache_root)
        return False
    return True


def _seal_cache_entry(cache_path: Path) -> None:
    """Attach an immutable-content fingerprint after an atomic download completes."""

    marker_path = cache_path / ".download-complete.json"
    marker = _download_marker(cache_path)
    if not marker:
        raise ValueError(f"download cache has no completion marker: {cache_path}")
    marker["fingerprint"] = _cache_payload_fingerprint(cache_path)
    marker["cache_schema_version"] = 1
    write_json_atomic(marker_path, marker)


def _materialize_cached_dataset(
    cache_path: Path,
    dataset_path: Path,
    *,
    use_hardlinks: bool,
) -> None:
    """Materialize a verified cache entry without symbolic links.

    Identity/no-op pipelines prefer hard links on the same volume, which keeps
    multi-GB datasets reusable without storing another physical copy. Pipelines
    that may preprocess in place receive independent copies so the raw cache
    cannot be modified by preparation code.
    """

    if dataset_path.exists():
        if dataset_path.is_dir() and not any(dataset_path.iterdir()):
            dataset_path.rmdir()
        else:
            raise ValueError(f"dataset target is already non-empty: {dataset_path}")
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = dataset_path.with_name(f".{dataset_path.name}.cache-materialize-{os.getpid()}")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        if use_hardlinks:
            shutil.copytree(cache_path, temporary, copy_function=os.link)
        else:
            shutil.copytree(cache_path, temporary, copy_function=shutil.copy2)
        os.replace(temporary, dataset_path)
    except OSError:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        shutil.copytree(cache_path, temporary, copy_function=shutil.copy2)
        os.replace(temporary, dataset_path)


def _remove_cache_entry(cache_path: Path, cache_root: Path) -> None:
    root = cache_root.resolve()
    target = cache_path.resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"refusing to remove path outside dataset cache: {target}")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _contains_user_data(dataset_path: Path) -> bool:
    if dataset_path.is_file():
        return dataset_path.stat().st_size > 0
    if not dataset_path.is_dir():
        return False
    return any(
        item.is_file()
        and item.name != ".download-complete.json"
        and item.stat().st_size > 0
        for item in dataset_path.rglob("*")
    )


def dataset_fingerprint(dataset_path: Path) -> dict[str, object]:
    if dataset_path.is_symlink():
        raise ValueError("dataset root cannot be a symbolic link")
    files = [dataset_path] if dataset_path.is_file() else sorted(
        item for item in dataset_path.rglob("*") if item.is_file()
    )
    if not files:
        raise ValueError("dataset contains no files")
    digest = hashlib.sha256()
    total_bytes = 0
    for file_path in files:
        if file_path.is_symlink():
            raise ValueError(f"dataset file cannot be a symbolic link: {file_path.name}")
        relative = (
            file_path.name
            if dataset_path.is_file()
            else file_path.relative_to(dataset_path).as_posix()
        )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with file_path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                total_bytes += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return {
        "algorithm": "sha256-tree-v1",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def _cache_payload_fingerprint(cache_path: Path) -> dict[str, object]:
    """Fingerprint dataset payload while excluding cache bookkeeping files."""

    ignored = {".download-complete.json", ".download-resume.json"}
    files = sorted(
        item for item in cache_path.rglob("*")
        if item.is_file()
        and item.name not in ignored
        and not item.name.endswith(_PARTIAL_META_SUFFIX)
        and not item.name.endswith(".partial")
    )
    if not files:
        raise ValueError("dataset cache contains no payload files")
    digest = hashlib.sha256()
    total_bytes = 0
    for file_path in files:
        if file_path.is_symlink():
            raise ValueError(f"dataset cache file cannot be a symbolic link: {file_path.name}")
        relative = file_path.relative_to(cache_path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with file_path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                total_bytes += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return {
        "algorithm": "sha256-tree-v1",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def _download_dataset_atomically(
    url: str,
    dataset_path: Path,
    *,
    max_download_bytes: int,
) -> tuple[str, bool]:
    """Backward-compatible single-file wrapper around the bundle downloader."""

    names, extracted = _download_dataset_bundle_atomically(
        [url],
        dataset_path,
        dataset_name=dataset_path.name,
        source_reference=url,
        provider="module2",
        dataset_id=dataset_path.name,
        revision=None,
        max_download_bytes=max_download_bytes,
    )
    return names[0], extracted


def _url_tail(url: str) -> str:
    """URL 的可读尾段（`.../resolve/main/data/x.parquet` → `data/x.parquet`）。"""
    path = urlparse(url).path
    parts = [part for part in path.split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else url)


def _write_download_plan(
    run_dir: Path,
    dataset_name: str,
    *,
    planned_files: list[tuple[str, int | None]],
    estimated_total: int | None,
    auto_limit: int,
    decision: str,
) -> None:
    """把「打算下哪些文件、各多大、为什么下/不下」写进审计产物。

    与 `data/source-resolution/<ds>.json` 平行：那个记**解析**结果，这个记**下载决策**。
    报告要求「把将下载的文件清单、总大小写入审计产物」，否则事后无法解释为什么
    某个数据集被判超限。
    """
    try:
        write_json_atomic(
            safe_join(run_dir, f"data/download-plan/{slugify(dataset_name)}.json"),
            {
                "dataset": dataset_name,
                "decision": decision,
                "estimated_total_bytes": estimated_total,
                "auto_download_limit_bytes": auto_limit,
                "files": [
                    {"url": url, "declared_bytes": size} for url, size in planned_files
                ],
            },
        )
    except (OSError, ValueError) as exc:  # 审计写失败不该阻断主流程
        log.warning("下载计划审计写入失败（忽略）: %s", exc)


def estimate_bundle_size(
    urls: list[str], *, timeout_seconds: int | None = None
) -> tuple[int | None, list[tuple[str, int | None]]]:
    """下载前逐个探测文件大小。返回 ``(已知总量或 None, [(url, 字节数或 None)])``。

    探测失败（网络 / 服务器不支持 HEAD）记为 ``None``，**不阻断**——未知不等于超限。
    有界：最多探 ``MAX_SIZE_PROBES`` 个，避免把主流程拖成一次串行扫描。
    """
    deadline = time.monotonic() + MAX_SIZE_PROBE_SECONDS
    probes: list[tuple[str, int | None]] = []
    total = 0
    unknown = False
    for url in urls[:MAX_SIZE_PROBES]:
        if time.monotonic() >= deadline:
            # 探测本身必须**有界**：25 个 URL × 2 次尝试 × 单次超时足以吃掉整个
            # 工具时间预算，那比"总量未知"糟得多。超时就当未知（放行）。
            log.info("体积探测超时预算（%ds），剩余文件按未知处理", MAX_SIZE_PROBE_SECONDS)
            unknown = True
            break
        size = probe_size(url, timeout_seconds)
        probes.append((url, size))
        if size is None:
            unknown = True
        else:
            total += size
    if len(urls) > MAX_SIZE_PROBES:
        unknown = True
    return (None if unknown else total), probes


def _download_dataset_bundle_atomically(
    urls: list[str],
    dataset_path: Path,
    *,
    dataset_name: str,
    source_reference: str,
    declared_source_url: str | None = None,
    provider: str,
    dataset_id: str,
    revision: str | None,
    max_download_bytes: int,
    planned_files: list[tuple[str, int | None]] | None = None,
    deadline: float | None = None,
) -> tuple[list[str], bool]:
    """Download one resolved dataset bundle and publish it only on success.

    ``planned_files`` 是 :func:`estimate_bundle_size` 的探测结果，只用于**审计**
    （写进下载计划，让「打算下哪些文件、各多大」可追溯）。
    """

    if not urls:
        raise ValueError("resolved dataset has no downloadable files")
    if len(urls) > 256:
        raise ValueError("resolved dataset exceeds the 256-file automatic limit")

    parent = dataset_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ staging **必须确定性命名**：旧实现用 `uuid4()`，每次调用都是新目录，
    # 于是 `.partial` 根本不可能跨调用续传（下次进来是另一个目录）。
    # 复用条件是"同一批 URL"——换了源或改了选择就重头来，避免拼出错误的包。
    staging = parent / f".{slugify(dataset_path.name)}-download"
    resume_marker = staging / ".download-resume.json"
    if staging.exists():
        previous = None
        try:
            previous = json.loads(resume_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        if isinstance(previous, dict) and previous.get("urls") == urls:
            done = [name for name in previous.get("completed_files") or [] if (staging / name).is_file()]
            log.info(
                "复用未完成的下载目录：已完成 %d/%d 个文件，继续续传 %s",
                len(done), len(urls), dataset_name,
            )
        elif isinstance(previous, dict):
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
        else:
            # 断点标记缺失/不可读 ≠ 换了数据源。单个文件大于单次调用预算时，工具会在
            # 该文件下完之前就被 deadline/宿主超时打断，此时 marker 一次都还没写过；
            # 若按"换了源"清空，就变成每次调用都从零开始，大文件永远下不完。
            # 这里保留 .partial 与已下完的文件继续续传。
            log.info(
                "断点标记缺失，保留已有下载目录继续续传（待下 %d 个文件）: %s",
                len(urls), dataset_name,
            )
    else:
        staging.mkdir()
    try:
        downloaded_files: list[Path] = []
        extracted = False
        completed_names: set[str] = set()
        try:
            previous = json.loads(resume_marker.read_text(encoding="utf-8"))
            completed_names = {
                name for name in (previous.get("completed_files") or [])
                if isinstance(name, str) and (staging / name).is_file()
            }
        except (OSError, ValueError):
            completed_names = set()
        # 先把本次的 URL 集合落盘：否则"文件还没下完"的调用一次 marker 都不写，
        # 下次进来会被上面的分支误判为换了源而清空目录，大文件永远下不完。
        write_json_atomic(resume_marker, {
            "dataset_name": dataset_name,
            "urls": urls,
            "completed_files": sorted(completed_names),
            "total_files": len(urls),
            "updated_at_utc": _utc_now_iso(),
        })
        for index, url in enumerate(urls):
            used = _directory_size(staging)
            remaining = max_download_bytes - used
            if remaining <= 0:
                raise ValueError("dataset bundle exceeded the configured download limit")
            # 多源回退：同一文件按配置的端点族展开成候选（主源在前），逐个尝试；
            # 每个候选内部还有 _download 自己的重试。全部失败才向上抛，让解析层的
            # 「按名称回退」照常触发。
            name_prefix = f"{index:04d}-" if len(urls) > 1 else ""
            expected_name = f"{name_prefix}{_url_filename(url)}"
            if expected_name in completed_names:
                existing = staging / expected_name
                downloaded_files.append(existing)
                log.info("跳过已完成的文件（断点续跑）: %s", expected_name)
                extracted = _extract_if_supported(
                    existing, staging, max_extract_bytes=max_download_bytes,
                ) or extracted
                continue
            if deadline is not None and time.monotonic() > deadline:
                raise _TransferDeadlineReached(
                    f"download time budget exhausted before {dataset_name} file {index + 1}/{len(urls)}"
                )
            downloaded = _download_from_variants(
                url,
                staging,
                max_download_bytes=remaining,
                filename_prefix=name_prefix,
                deadline=deadline,
            )
            downloaded_files.append(downloaded)
            completed_names.add(downloaded.name)
            write_json_atomic(resume_marker, {
                "dataset_name": dataset_name,
                "urls": urls,
                "completed_files": sorted(completed_names),
                "total_files": len(urls),
                "updated_at_utc": _utc_now_iso(),
            })
            extracted = _extract_if_supported(
                downloaded,
                staging,
                max_extract_bytes=max_download_bytes,
            ) or extracted
            if _directory_size(staging) > max_download_bytes:
                raise ValueError("downloaded and extracted dataset exceeds size limit")
        write_json_atomic(
            staging / ".download-complete.json",
            {
                "completed": True,
                "dataset_name": dataset_name,
                "source_reference": source_reference,
                "declared_source_url": declared_source_url,
                "provider": provider,
                "dataset_id": dataset_id,
                "revision": revision,
                "download_urls": urls,
                "downloaded_files": [item.name for item in downloaded_files],
                "extracted": extracted,
            },
        )
        if dataset_path.exists():
            if dataset_path.is_dir() and not any(dataset_path.iterdir()):
                dataset_path.rmdir()
            else:
                raise ValueError(
                    "target dataset path became non-empty during download"
                )
        os.replace(staging, dataset_path)
        return [item.name for item in downloaded_files], extracted
    except BaseException:
        # 失败时**保留** staging：里面的 .partial 与 .download-resume.json 就是续传依据，
        # 而时间预算用尽（单文件大于单次调用预算）是最常见的中断。原实现在 finally 里
        # 无条件 rmtree，等于每次调用都从零开始，这类文件永远下不完。
        # 真正换源时由下次调用的 URL 比对分支负责清理。
        raise


def _download_from_variants(
    url: str,
    destination: Path,
    *,
    max_download_bytes: int,
    filename_prefix: str = "",
    deadline: float | None = None,
) -> Path:
    """多源下载：同一文件依次尝试端点族里的每个 host，全失败才抛出。

    候选由 ``_network.download_variants`` 生成——只有 URL 属于配置的 HF 端点族
    （默认 huggingface.co / hf-mirror.com）时才有多个；其他来源原样单元素。
    错误信息汇总每个候选的失败原因，便于判断到底是「全网都不通」还是「文件不存在」。
    """
    candidates = download_variants(url)
    if not candidates:
        candidates = [url]
    failures: list[str] = []
    for candidate in candidates:
        try:
            downloaded = _download(
                candidate,
                destination,
                max_download_bytes=max_download_bytes,
                filename_prefix=filename_prefix,
                deadline=deadline,
            )
        except (_DeterministicDownloadError, _TransferDeadlineReached):
            # 4xx / HTML 落地页 / 超限：换 host 也是同样结果，别浪费一轮往返。
            # 时间预算用尽同理——换源不会让剩下的时间变多。
            raise
        except Exception as exc:
            failures.append(f"{_url_host(candidate)}: {type(exc).__name__}: {exc}")
            continue
        if len(candidates) > 1:
            log.info(
                "多源下载命中: %s（共 %d 个候选源）", _url_host(candidate), len(candidates)
            )
        return downloaded
    raise ValueError(
        f"all {len(candidates)} download source(s) failed for {url}: " + "; ".join(failures)
    )


def _url_host(url: str) -> str:
    return urlparse(url).hostname or url


class _DeterministicDownloadError(ValueError):
    """换源也不可能成功的失败：4xx 拒绝、返回 HTML、超出大小上限。

    与瞬时网络错误分开，才能让多源回退既不漏掉真正可用的镜像，
    也不为「文件根本不存在」在多个 host 上各烧一次超时。
    """


def _url_filename(url: str) -> str:
    """URL → 落盘文件名（与 :func:`_download` 用同一套规则，断点续跑要靠它对齐）。"""
    path = urlparse(url).path
    name = Path(path).name
    # Zenodo 文件 API 的直链形如 ``/api/records/<id>/files/<file>/content``：
    # 末段是固定的 ``content``，真正的文件名在倒数第二段。不做这一步映射会把
    # 数据落成无扩展名的 ``content``，下游只认 ``*.csv`` 的数据准备器就看不到文件。
    if name == "content":
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            name = parts[-2]
    return slugify(name or "dataset.download")


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _download(
    url: str,
    destination: Path,
    *,
    max_download_bytes: int,
    filename_prefix: str = "",
    deadline: float | None = None,
) -> Path:
    """下载单个 URL 到 destination，按配置重试瞬时失败。

    多源回退在外层 ``_download_from_variants``；这里只管「同一个 URL 多试几次」。

    断点续传在 :func:`_download_once`：单次调用带 ``deadline``，到期时保留
    ``.partial`` 与进度元数据，下次调用从已落盘字节继续（详见那里的三道安全阀）。
    """
    _validate_download_url(url)
    filename = filename_prefix + _url_filename(url)
    target = destination / filename

    attempts = download_retries()
    last_exc: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            return _download_once(
                url,
                destination,
                target=target,
                filename=filename,
                max_download_bytes=max_download_bytes,
                deadline=deadline,
            )
        except _DeterministicDownloadError:
            raise
        except _TransferDeadlineReached:
            # 时间到就是时间到：重试只会把剩余预算也烧掉。保留片段向上抛，
            # 由调用方决定"下次再来"。
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                delay = 1.0 * (2 ** attempt)
                log.warning(
                    "下载失败（%s），%.1fs 后重试 %d/%d: %s",
                    type(exc).__name__, delay, attempt + 1, attempts, url,
                )
                time.sleep(delay)
    assert last_exc is not None
    raise ValueError(
        f"download failed after {attempts + 1} attempt(s) for {url}: "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


class _TransferDeadlineReached(Exception):
    """单次调用的时间预算用尽。

    **不是确定性失败**：换源、重试都不会变快，所以不触发重试也不换源；
    但下次调用可以从已落盘的字节继续——所以绝不能和
    :class:`_DeterministicDownloadError` 混为一谈（后者会被判成"换个源也一样"）。
    """


#: 落盘进度元数据（与 `.partial` 同名 + `.json`）。
#: 记 URL / 已落盘字节 / ETag / 总长，续传前逐项核对——对不上就从头重下。
_PARTIAL_META_SUFFIX = ".partial.json"
#: 小于这个大小不值得走 Range 续传（协商成本高于重下）。
_RESUME_MIN_BYTES = 1024 * 1024

#: 下载过程中每隔多少字节刷新一次 `.partial` 元数据。宿主机对单次工具调用有硬超时，
#: 进程可能被直接杀掉；只在 deadline/结束时写元数据的话，被杀掉的调用会留下"元数据
#: 与实际片段不符"的残骸，下次只能从 0 重下。定期刷新让这种硬中断也能续传。
_PARTIAL_META_FLUSH_BYTES = 8 * 1024 * 1024


def _partial_paths(destination: Path, filename: str) -> tuple[Path, Path]:
    partial = destination / f".{filename}.partial"
    return partial, destination / f".{filename}{_PARTIAL_META_SUFFIX}"


def _read_partial_meta(meta_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_partial_meta(meta_path: Path, payload: dict[str, Any]) -> None:
    try:
        meta_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # 元数据写失败只影响续传，不该打断下载
        log.warning("下载进度元数据写入失败（忽略）: %s", exc)


def _download_once(
    url: str,
    destination: Path,
    *,
    target: Path,
    filename: str,
    max_download_bytes: int,
    deadline: float | None = None,
) -> Path:
    """下载单个文件；``deadline`` 到期时保留 ``.partial`` 供下次续传。

    续传的三道安全阀（宁可重下，也不能拼出一个损坏的文件）：
      1. `.partial` 与元数据都在，且 URL 一致、记录的字节数与实际文件大小一致；
      2. 服务器返回 **206 Partial Content**（返回 200 说明它忽略了 Range，
         此时**丢弃**已有片段从 0 重写）；
      3. 带 ETag 时用 ``If-Range``，资源变了服务器会直接回 200，走第 2 条分支。
    """
    partial, meta_path = _partial_paths(destination, filename)
    offset = 0
    etag: str | None = None
    if partial.is_file() and partial.stat().st_size >= _RESUME_MIN_BYTES:
        meta = _read_partial_meta(meta_path) or {}
        if meta.get("url") == url and meta.get("bytes_done") == partial.stat().st_size:
            offset = partial.stat().st_size
            etag = meta.get("etag")
        else:
            log.info("续传元数据与实际片段不符，从头下载: %s", filename)

    headers = {"User-Agent": "jiuwenswarm-experiment/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        if isinstance(etag, str) and etag:
            headers["If-Range"] = etag
    request = urllib.request.Request(url, headers=headers)

    def _record(bytes_done: int, response_etag: str | None, total: int | None) -> None:
        _write_partial_meta(meta_path, {
            "url": url,
            "bytes_done": bytes_done,
            "etag": response_etag,
            "content_length": total,
        })

    try:
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(request, timeout=download_timeout_seconds()) as response:  # noqa: S310
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                raise _DeterministicDownloadError(
                    "download URL returned HTML instead of a dataset file; provide a direct file URL"
                )
            declared = response.headers.get("Content-Length")
            declared_total = int(declared) if declared and str(declared).strip().isdigit() else None
            full_size = (offset + declared_total) if (declared_total is not None and offset) else declared_total
            if full_size is not None and full_size > max_download_bytes:
                raise _DeterministicDownloadError(
                    f"download is larger than limit ({full_size} > {max_download_bytes})"
                )
            resumed = offset > 0 and getattr(response, "status", 200) == 206
            if offset and not resumed:
                offset = 0  # 服务器忽略 Range → 丢弃已有片段，防拼接损坏
            response_etag = response.headers.get("ETag")
            total = offset
            last_flush = offset
            mode = "ab" if resumed else "wb"
            with partial.open(mode) as file:
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        _record(total, response_etag, full_size)
                        raise _TransferDeadlineReached(
                            f"download time budget exhausted after {total} bytes for {url}"
                        )
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_download_bytes:
                        raise _DeterministicDownloadError(
                            f"download exceeded limit of {max_download_bytes} bytes"
                        )
                    file.write(chunk)
                    if total - last_flush >= _PARTIAL_META_FLUSH_BYTES:
                        file.flush()
                        _record(total, response_etag, full_size)
                        last_flush = total
        partial.replace(target)
        try:
            meta_path.unlink()
        except OSError:
            pass
    except urllib.error.HTTPError as exc:
        # 4xx（除 429 限流）是确定性拒绝——重试与换源都不会变好
        if exc.code not in {429, 500, 502, 503, 504}:
            raise _DeterministicDownloadError(
                f"download rejected with HTTP {exc.code} for {url}"
            ) from exc
        raise
    except _TransferDeadlineReached:
        raise  # 保留 .partial 与元数据，等下次调用续传
    finally:
        # 时间预算用尽时**保留**片段；其余失败照旧清理（半截文件留着只会误导）
        if partial.exists() and not (
            sys.exc_info()[0] is _TransferDeadlineReached
        ):
            partial.unlink()
            try:
                meta_path.unlink()
            except OSError:
                pass
    return target


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _manual_dataset_suggestion(dataset_name: str, configured: str) -> str:
    return (
        f"若需手动下载，请把{dataset_name}的真实数据文件放入本次run_dir下的"
        f"{configured}，将readiness改为available，并使用相同run_id续跑"
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _allowed_private_hosts() -> set[str]:
    """Explicit opt-in allowlist for private/local dataset mirrors.

    Default is empty: the SSRF guard below stays fully in force. Setting
    ``JIUWENSWARM_ALLOW_PRIVATE_DATASET_HOSTS=127.0.0.1`` (comma separated
    hostnames or literal addresses) permits a deliberately hosted local
    mirror of an already-public dataset without loosening the default.
    """
    raw = os.environ.get("JIUWENSWARM_ALLOW_PRIVATE_DATASET_HOSTS", "")
    return {item.strip().rstrip(".").lower() for item in raw.split(",") if item.strip()}


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("dataset download URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("dataset download URL cannot contain credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("dataset download URL has no hostname")
    lowered = hostname.rstrip(".").lower()
    allowlist = _allowed_private_hosts()
    if lowered in allowlist:
        return
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        raise ValueError("dataset download URL cannot target a local hostname")
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = {literal}
    else:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError(f"dataset hostname cannot be resolved: {hostname}") from exc
        addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
    unsafe = sorted(
        str(address)
        for address in addresses
        if not address.is_global and str(address) not in allowlist
    )
    if unsafe:
        raise ValueError(
            "dataset download URL resolves to a non-public address: "
            + ", ".join(unsafe)
        )


def _extract_if_supported(
    archive: Path,
    destination: Path,
    *,
    max_extract_bytes: int,
) -> bool:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            total_size = sum(member.file_size for member in zipped.infolist())
            if total_size > max_extract_bytes:
                raise ValueError(
                    f"zip expands beyond limit ({total_size} > {max_extract_bytes})"
                )
            for member in zipped.infolist():
                target = _safe_archive_target(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return True
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            total_size = sum(member.size for member in members if member.isfile())
            if total_size > max_extract_bytes:
                raise ValueError(
                    f"tar expands beyond limit ({total_size} > {max_extract_bytes})"
                )
            for member in members:
                if member.issym() or member.islnk():
                    raise ValueError("tar archive links are not allowed")
                target = _safe_archive_target(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read archive member: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return True
    return False


def _safe_archive_target(destination: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
        raise ValueError(f"unsafe archive path: {member_name}")
    target = (destination / normalized).resolve()
    root = destination.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"archive member escapes destination: {member_name}")
    return target

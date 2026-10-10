"""Resolve a missing dataset URL through public, auditable registries.

Only exact-name matches are eligible for automatic download.  Ambiguous search
results are returned to the caller instead of selecting the most popular item.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

from _network import (  # noqa: E402
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    fetch_json_over_bases,
    fetch_json_with_retry,
    hf_bases,
    hf_parquet_bases,
    is_approved_hf_url,
    is_configured_hf_host,
    search_timeout_seconds,
)


log = logging.getLogger("experiment.dataset_resolver")

MAX_SEARCH_RESULTS = 10
MAX_RESOLVED_FILES = 256
MAX_METADATA_BYTES = 4 * 1024 * 1024
# 检索超时默认值统一由 _network 提供，可用 JIUWENSWARM_SEARCH_TIMEOUT_S 覆盖
# （见 _network.search_timeout_seconds）。签名默认 None = 运行时取配置值。

JsonFetcher = Callable[[str, int], Any]


class DeclaredReferenceUnresolvable(ValueError):
    """声明的引用**暂时**解析不了（元数据/网络不可用）——不是"数据不存在"。

    与 :class:`DeclaredFileMissing` 的区别是**归因**：那个是"查过了，仓库里确实没这个
    文件"，这个是"根本没查到，原因在网络侧"。两者都降级到按名称检索，但写进审计的
    原因不同——否则一次网络抖动会被记成"数据不可得"，把 planner 引向换数据集的错误修法。
    """


class DeclaredFileMissing(ValueError):
    """模块二声明的数据文件在该仓库里不存在（但仓库本身可达）。

    不是"数据不可得"，而是"文件名猜错了"——异常消息里带上**真实文件清单**，
    让调用方把它写进 ``provider_errors``，最终经 ``[data]`` blocker 回到模块二
    planner 手里，它下一轮就能照抄真实文件名。
    """

    def __init__(self, dataset_id: str, declared_path: str, available: list[str]) -> None:
        self.dataset_id = dataset_id
        self.declared_path = declared_path
        self.available = list(available)
        listing = ", ".join(self.available[:20]) or "(该仓库没有可下载的数据文件)"
        super().__init__(
            f"declared data file {declared_path!r} does not exist in "
            f"Hugging Face dataset {dataset_id!r}; available files: {listing}"
        )


@dataclass(frozen=True)
class DatasetCandidate:
    provider: str
    dataset_id: str
    title: str
    landing_url: str
    download_urls: tuple[str, ...]
    license: str | None = None
    revision: str | None = None
    paper_url: str | None = None
    replacement_for: str | None = None
    compatibility_tags: tuple[str, ...] = ()
    selection_reason: str | None = None
    #: 这个候选的 download_urls 是否**真的被验证过存在**。
    #: ``provider="module2"`` 是模块二声明的 URL 原样透传，模块三从没查过它——
    #: 标 False，避免下游（尤其 REPLAN 的「已验证候选直链」回喂）把它当成已验证。
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["download_urls"] = list(self.download_urls)
        payload["compatibility_tags"] = list(self.compatibility_tags)
        return payload


@dataclass(frozen=True)
class DatasetResolution:
    status: str
    query: str
    selected: DatasetCandidate | None = None
    candidates: tuple[DatasetCandidate, ...] = ()
    provider_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [item.to_dict() for item in self.candidates],
            "provider_errors": dict(self.provider_errors),
        }


def resolve_dataset_source(
    dataset_name: str,
    *,
    source_url: str | None = None,
    timeout_seconds: int | None = None,
    fetch_json: JsonFetcher | None = None,
    allow_compatible_substitutes: bool = False,
    search_context: str | None = None,
) -> DatasetResolution:
    """Resolve an explicit reference or search public registries by exact name.

    ``timeout_seconds=None``（默认）表示取 ``JIUWENSWARM_SEARCH_TIMEOUT_S``
    （未设置时 30s）；显式传值仍然优先。
    """

    if timeout_seconds is None:
        timeout_seconds = search_timeout_seconds()
    fetcher = fetch_json or _fetch_json
    name = dataset_name.strip()
    if not name:
        return DatasetResolution(status="NOT_FOUND", query=dataset_name)

    # 声明的 URL 有问题时（落地页 / 仓库里没这个文件）要记下原因再往下走名称检索，
    # 不能在这里 return —— 否则「数据其实可得、只是文件名猜错了」会被判成无解。
    declared_errors: dict[str, str] = {}
    if source_url and not _is_dataset_landing_page(source_url):
        try:
            candidate = _resolve_explicit_reference(
                name,
                source_url,
                timeout_seconds=timeout_seconds,
                fetch_json=fetcher,
            )
        except DeclaredFileMissing as exc:
            # 仓库可达、只是没有这个文件。带真实文件清单降级到名称检索：检索命中就
            # 用真文件继续跑，命中不了清单也会经 provider_errors 回到 planner。
            # 带 URL 前缀：审计文件里能直接看出"是哪条声明地址出了问题"，
            # 而 REPLAN 从 blocker 抽失败 URL 黑名单的正则也认这个格式。
            declared_errors["declared_source"] = f"{source_url}: {_short_error(exc)}"
            log.warning(
                "声明的数据文件不存在，降级为按名称检索: dataset=%r url=%s（%s）",
                name, source_url, _short_error(exc),
            )
        except DeclaredReferenceUnresolvable as exc:
            declared_errors["declared_source"] = f"{source_url}: {_short_error(exc)}"
            log.warning(
                "声明的引用暂时解析不了（元数据/网络），降级为按名称检索: "
                "dataset=%r url=%s（%s）",
                name, source_url, _short_error(exc),
            )
        except (OSError, TimeoutError) as exc:
            # 网络层故障同理：**绝不能**记成"数据不可得"，否则一次抖动会让 planner
            # 去换数据集。降级到名称检索，原因如实写进审计。
            declared_errors["declared_source"] = f"{source_url}: {_short_error(exc)}"
            log.warning(
                "解析声明地址时网络失败，降级为按名称检索: dataset=%r url=%s（%s）",
                name, source_url, _short_error(exc),
            )
        except ValueError as exc:
            # 提供方返回了可解释的“这个声明无法形成数据候选”（例如仓库只有控制
            # 文件、文件数超限、Zenodo 记录无公开文件）。这只是**一个提供方/一个
            # URL 的失败**，不能截断后续的官方别名、同名注册表和其他提供方检索。
            # 安全校验仍由下载层执行；这里只改变故障隔离边界，不把失败伪装成成功。
            declared_errors["declared_source"] = f"{source_url}: {_short_error(exc)}"
            log.warning(
                "声明来源无法形成可下载数据候选，继续按名称检索: "
                "dataset=%r url=%s（%s）",
                name, source_url, _short_error(exc),
            )
        except Exception as exc:  # caller persists the precise lookup failure
            return DatasetResolution(
                status="ERROR",
                query=name,
                provider_errors={"explicit": _short_error(exc)},
            )
        else:
            return DatasetResolution(
                status="RESOLVED",
                query=name,
                selected=candidate,
                candidates=(candidate,),
            )

    # 模块二有时只给论文/代码落地页，而不是数据文件。此类 URL 只能作为
    # 溯源证据，不能直接下载；转而按数据集名称查询受控公开仓库。这样既避免
    # 把 HTML 保存成“数据集”，也不会因为存在一个无效 URL 而跳过名称检索。
    resolution = _search_public_registries(
        name,
        timeout_seconds=timeout_seconds,
        fetch_json=fetcher,
    )
    if declared_errors:
        # 名称检索的结果要带着"声明地址哪里不对"一起出去：命中时它是审计线索，
        # 没命中时它就是 planner 修 URL 的唯一依据（含真实文件清单）。
        resolution = DatasetResolution(
            status=resolution.status,
            query=resolution.query,
            selected=resolution.selected,
            candidates=resolution.candidates,
            provider_errors={**resolution.provider_errors, **declared_errors},
        )
    if resolution.status != "RESOLVED" and allow_compatible_substitutes:
        try:
            substitutes = _search_compatible_substitutes(
                name,
                search_context=search_context,
                timeout_seconds=timeout_seconds,
                fetch_json=fetcher,
            )
        except Exception as exc:
            substitutes = []
            errors = dict(resolution.provider_errors)
            errors["substitute_registry"] = _short_error(exc)
            resolution = DatasetResolution(
                status=resolution.status,
                query=resolution.query,
                candidates=resolution.candidates,
                provider_errors=errors,
            )
        if substitutes:
            resolution = DatasetResolution(
                status="SUBSTITUTE_AVAILABLE",
                query=name,
                selected=None,
                candidates=tuple(substitutes),
                provider_errors=dict(resolution.provider_errors),
            )

    if source_url and resolution.status != "RESOLVED":
        errors = dict(resolution.provider_errors)
        # 别覆盖上面记下的"文件不存在"——那是更有信息量的原因（含真实文件清单），
        # 而落地页判定是进这个分支前就做过的。
        if "declared_source" not in errors:
            errors["declared_source"] = (
                "declared URL is a paper/repository landing page, not a direct dataset file: "
                + source_url
            )
        return DatasetResolution(
            status=resolution.status,
            query=resolution.query,
            selected=resolution.selected,
            candidates=resolution.candidates,
            provider_errors=errors,
        )
    return resolution


def _search_compatible_substitutes(
    dataset_name: str,
    *,
    search_context: str | None,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> list[DatasetCandidate]:
    """Discover replacement candidates without auto-executing them.

    Replacement changes the scientific contract, so callers receive candidates
    for a planning rewrite instead of downloading them under the old dataset
    name. Audited mappings are preferred, followed by general public-registry
    discovery using the task context. Every returned candidate must expose real
    downloadable files, a licence and an immutable revision.
    """

    candidates: list[DatasetCandidate] = []
    seen = {(item.provider, item.dataset_id) for item in candidates}
    discoverers = (_discover_huggingface_substitutes, _discover_zenodo_substitutes)
    for discover in discoverers:
        for item in discover(
            dataset_name,
            search_context=search_context,
            timeout_seconds=timeout_seconds,
            fetch_json=fetch_json,
        )[:3]:
            key = (item.provider, item.dataset_id)
            if key not in seen:
                candidates.append(item)
                seen.add(key)
            if len(candidates) >= 5:
                break
        if len(candidates) >= 5:
            break
    return candidates[:5]


def _discover_huggingface_substitutes(
    dataset_name: str,
    *,
    search_context: str | None,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> list[DatasetCandidate]:
    """Search public Hugging Face metadata for versioned replacement options."""

    ranked: list[tuple[int, DatasetCandidate]] = []
    seen_ids: set[str] = set()
    context_tokens = set(_search_tokens(search_context or dataset_name))
    for term in _substitute_search_terms(dataset_name, search_context):
        query = urlencode({"search": term, "limit": MAX_SEARCH_RESULTS, "full": "true"})
        payload = fetch_json_over_bases(
            f"/api/datasets?{query}", timeout_seconds, fetch_json=fetch_json
        )
        if not isinstance(payload, list):
            continue
        for metadata in payload:
            if not isinstance(metadata, dict):
                continue
            dataset_id = str(metadata.get("id") or "").strip()
            if not dataset_id or dataset_id in seen_ids:
                continue
            if metadata.get("private") is True or metadata.get("gated") not in {None, False, "false"}:
                continue
            revision = str(metadata.get("sha") or "").strip()
            license_name = _huggingface_license(metadata)
            if not revision or not license_name:
                continue
            try:
                candidate = _huggingface_candidate(
                    dataset_id,
                    title=dataset_id,
                    metadata=metadata,
                    timeout_seconds=timeout_seconds,
                    fetch_json=fetch_json,
                )
            except Exception:
                continue
            candidate_tokens = set(_search_tokens(
                " ".join([
                    dataset_id,
                    str(metadata.get("description") or ""),
                    " ".join(str(tag) for tag in metadata.get("tags", []) if isinstance(tag, str)),
                ])
            ))
            overlap = sorted(context_tokens & candidate_tokens)
            score = len(overlap)
            # 替代项不会在模块三自动执行，只作为带版本/许可/文件清单的
            # REPLAN 候选交给模块二核对。因此一个领域关键词即可进入候选池，
            # 排名仍优先多信号重合，避免因为上游只有简短数据集名而完全搜不到。
            if score < 1:
                continue
            ranked.append((score, replace(
                candidate,
                provider="registry-compatible-candidate",
                replacement_for=dataset_name,
                compatibility_tags=tuple(overlap[:12]),
                selection_reason=(
                    "公共注册表候选；元数据与任务上下文的共同关键词为 "
                    + ", ".join(overlap[:12])
                    + "。规划模块必须核对字段/指标后才能替换。"
                ),
            )))
            seen_ids.add(dataset_id)
    ranked.sort(key=lambda item: (-item[0], item[1].dataset_id.casefold()))
    return [item for _, item in ranked[:5]]


def _discover_zenodo_substitutes(
    dataset_name: str,
    *,
    search_context: str | None,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> list[DatasetCandidate]:
    """Search Zenodo's domain-neutral dataset catalogue for replacement options."""

    context_tokens = set(_search_tokens(search_context or dataset_name))
    if not context_tokens:
        return []
    ranked: list[tuple[int, DatasetCandidate]] = []
    seen_ids: set[str] = set()
    for term in _substitute_search_terms(dataset_name, search_context)[:4]:
        query = urlencode({"q": term, "type": "dataset", "size": MAX_SEARCH_RESULTS})
        payload = fetch_json(f"https://zenodo.org/api/records?{query}", timeout_seconds)
        hits = payload.get("hits", {}).get("hits", []) if isinstance(payload, dict) else []
        for item in hits:
            candidate = _zenodo_candidate(item)
            if candidate is None or candidate.dataset_id in seen_ids:
                continue
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            if not isinstance(metadata, dict) or not candidate.license:
                continue
            candidate_tokens = set(_search_tokens(" ".join([
                candidate.title,
                str(metadata.get("description") or ""),
                " ".join(
                    str(value) for value in metadata.get("keywords", [])
                    if isinstance(value, str)
                ),
            ])))
            overlap = sorted(context_tokens & candidate_tokens)
            if len(overlap) < 1:
                continue
            ranked.append((len(overlap), replace(
                candidate,
                provider="zenodo-compatible-candidate",
                replacement_for=dataset_name,
                compatibility_tags=tuple(overlap[:12]),
                selection_reason=(
                    "Zenodo 公开数据候选；元数据与任务上下文的共同关键词为 "
                    + ", ".join(overlap[:12])
                    + "。规划模块必须核对字段/指标后才能替换。"
                ),
            )))
            seen_ids.add(candidate.dataset_id)
    ranked.sort(key=lambda pair: (-pair[0], pair[1].dataset_id.casefold()))
    return [candidate for _, candidate in ranked[:5]]


def _substitute_search_terms(dataset_name: str, search_context: str | None) -> tuple[str, ...]:
    tokens = _search_tokens(search_context or "")
    phrases = [dataset_name]
    if tokens:
        phrases.append(" ".join(tokens[:6]))
        phrases.extend(tokens[:4])
    return tuple(dict.fromkeys(item.strip() for item in phrases if item.strip()))


def _search_tokens(value: str) -> list[str]:
    stop = {
        "dataset", "data", "benchmark", "task", "method", "model", "experiment",
        "the", "and", "for", "with", "from", "using", "research", "metric",
        "agent", "agents", "language", "large", "llm", "long", "term",
    }
    # 同时拆 CamelCase、连字符和下划线。规划常只给 ``IFCMemoryBench``、
    # ``recall_at_k`` 这类名字；把它们当成一个整词会令公开仓库检索失效。
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    tokens = [item.casefold() for item in re.findall(r"[A-Za-z0-9]{3,}", expanded)]
    return list(dict.fromkeys(item for item in tokens if item not in stop))


def _search_public_registries(
    dataset_name: str,
    *,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> DatasetResolution:
    """Search audited aliases first, then public registries.

    Curated aliases are restricted to publisher/author-owned records whose
    identity and licence were checked in advance. They take precedence over
    similarly named mirrors, avoiding an unsafe popularity-based guess.
    """

    provider_errors: dict[str, str] = {}
    try:
        curated = _search_curated(
            dataset_name,
            timeout_seconds=timeout_seconds,
            fetch_json=fetch_json,
        )
    except Exception as exc:
        curated = []
        provider_errors["curated"] = _short_error(exc)
    if len(curated) == 1:
        return DatasetResolution(
            status="RESOLVED",
            query=dataset_name,
            selected=curated[0],
            candidates=(curated[0],),
            provider_errors=provider_errors,
        )

    candidates: list[DatasetCandidate] = []
    for provider, search in (
        ("huggingface", _search_huggingface),
        ("zenodo", _search_zenodo),
    ):
        try:
            candidates.extend(
                search(
                    dataset_name,
                    timeout_seconds=timeout_seconds,
                    fetch_json=fetch_json,
                )
            )
        except Exception as exc:
            provider_errors[provider] = _short_error(exc)

    unique: dict[tuple[str, str], DatasetCandidate] = {}
    for candidate in candidates:
        unique[(candidate.provider, candidate.dataset_id)] = candidate
    exact = tuple(unique.values())
    if len(exact) == 1:
        return DatasetResolution(
            status="RESOLVED",
            query=dataset_name,
            selected=exact[0],
            candidates=exact,
            provider_errors=provider_errors,
        )
    return DatasetResolution(
        status="AMBIGUOUS" if exact else "NOT_FOUND",
        query=dataset_name,
        candidates=exact,
        provider_errors=provider_errors,
    )


def _search_curated(
    dataset_name: str,
    *,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> list[DatasetCandidate]:
    """Return audited mirrors only for an exact known dataset alias.

    The mirror is a transport fallback; ``landing_url`` remains the canonical
    UCI record so provenance does not pretend GitHub is the dataset publisher.
    Network arguments are accepted to share the registry-search interface.
    """
    normalized = _normalized_name(dataset_name)
    aliases = {
        _normalized_name("breast_cancer_wisconsin_hf"),
        _normalized_name("breast-cancer-wisconsin"),
        _normalized_name("breast cancer wisconsin diagnostic"),
        _normalized_name("scikit-learn/breast-cancer-wisconsin"),
    }
    if normalized in aliases:
        return [DatasetCandidate(
            provider="curated-public-mirror",
            dataset_id="uci-17-breast-cancer-wisconsin-diagnostic",
            title="Breast Cancer Wisconsin (Diagnostic)",
            landing_url="https://archive.ics.uci.edu/dataset/17/breast+cancer+wisconsin+diagnostic",
            download_urls=(
                "https://raw.githubusercontent.com/gmineo/"
                "Breast-Cancer-Prediction-Project/master/data.csv",
            ),
            license="CC BY 4.0",
        )]

    # LongMemEval 的官方仓库将 2025 清洗版命名为 longmemeval-cleaned，
    # 严格同名搜索会漏掉它。这里登记作者官方 Hugging Face 数据集，而不是
    # 自动选择搜索结果中的第三方镜像。三个文件分别提供 oracle、S、M 规模，
    # 可支撑规划中的规模效应实验。
    if normalized == _normalized_name("LongMemEval"):
        dataset_id = "xiaowu0162/longmemeval-cleaned"
        metadata = fetch_json_over_bases(
            "/api/datasets/" + dataset_id, timeout_seconds, fetch_json=fetch_json
        )
        if not isinstance(metadata, dict):
            raise ValueError("LongMemEval official Hugging Face metadata is invalid")
        revision = str(metadata.get("sha") or "").strip()
        if not revision:
            raise ValueError("LongMemEval official Hugging Face revision is missing")
        # 审计产物里的 URL 固定用主源（可复现、可溯源）；镜像兜底交给下载层
        # download_variants —— 避免同一数据集因为「当时镜像活着」而写出镜像域 URL。
        base = hf_bases()[0].rstrip("/") + "/datasets/" + dataset_id
        pinned_base = base + "/resolve/" + quote(revision, safe="")
        return [DatasetCandidate(
            provider="curated-official",
            dataset_id=dataset_id,
            title="LongMemEval (cleaned)",
            landing_url=base,
            download_urls=(
                pinned_base + "/longmemeval_oracle.json",
                pinned_base + "/longmemeval_s_cleaned.json",
                pinned_base + "/longmemeval_m_cleaned.json",
            ),
            license=_huggingface_license(metadata) or "MIT",
            revision=revision,
        )]

    # LoCoMo 的官方 Hugging Face 仓库（snap-research/locomo）是 gated 仓库：匿名元数据
    # 与文件请求都返回 401，按名称检索只会命中第三方同名镜像，落到 AMBIGUOUS。
    # 论文作者自己的 GitHub 仓库 snap-research/locomo 公开同一份官方数据文件，
    # 因此登记为 curated 别名：landing_url 指向官方仓库做溯源，下载固定到 commit，
    # 保证来源可核验、内容可复现。
    if normalized in {
        _normalized_name("LoCoMo"),
        _normalized_name("LoCoMo10"),
        _normalized_name("snap-research/locomo"),
    }:
        revision = "cbfbc1dba6bc53d00625212a0f22d55ffee7c1fc"
        landing = "https://github.com/snap-research/locomo"
        pinned = (
            "https://raw.githubusercontent.com/snap-research/locomo/"
            + revision
            + "/data/locomo10.json"
        )
        return [DatasetCandidate(
            provider="curated-official",
            dataset_id="snap-research/locomo",
            title="LoCoMo (Long-term Conversational Memory benchmark)",
            landing_url=landing,
            download_urls=(pinned,),
            license="CC-BY-NC-4.0",
            revision=revision,
        )]

    # Multi-Session-Chat：规划模块常把仓库名写成带连字符的 `nayohan/multi-session-chat`
    # （该名在 Hugging Face 上不存在，匿名请求 401），并把 parquet 文件名写成
    # `test-00000-of-00001.parquet`，而真实文件名带内容哈希后缀。两者都会 404。
    # 这里登记作者官方仓库与真实文件名，避免因拼写差异被误判成"数据集不可得"。
    # 数据卡未声明 license，故如实留空，不做任何许可断言。
    if normalized in {
        _normalized_name("Multi-Session-Chat"),
        _normalized_name("Multi-Session Chat"),
        _normalized_name("MultiSessionChat"),
        _normalized_name("MSC-MultiSessionChat"),
        _normalized_name("MSC"),
        _normalized_name("nayohan/multi_session_chat"),
    }:
        dataset_id = "nayohan/multi_session_chat"
        revision = "78b67491c43823fc169cab827ab3f82805e0235b"
        base = hf_bases()[0].rstrip("/") + "/datasets/" + dataset_id
        pinned_base = base + "/resolve/" + quote(revision, safe="")
        return [DatasetCandidate(
            provider="curated-official",
            dataset_id=dataset_id,
            title="Multi-Session-Chat",
            landing_url=base,
            download_urls=(
                pinned_base + "/data/test-00000-of-00001-af129ca1e7829daf.parquet",
                pinned_base + "/data/validation-00000-of-00001-5e685f0241d31faf.parquet",
                pinned_base + "/data/train-00000-of-00001-537fda2f9aae8ae7.parquet",
            ),
            license=None,
            revision=revision,
        )]
    return []


def _huggingface_metadata(
    dataset_id: str,
    *,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> dict[str, Any]:
    """拉 HF 数据集元数据（``full=true``，含 ``siblings`` 真实文件清单）。

    失败直接向上抛：调用方决定是「原样透传但不声称已验证」还是「带着真实清单降级」。
    """
    payload = fetch_json_over_bases(
        f"/api/datasets/{quote(dataset_id, safe='/')}?{urlencode({'full': 'true'})}",
        timeout_seconds,
        fetch_json=fetch_json,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"Hugging Face dataset {dataset_id!r} metadata is not an object")
    return payload


def _huggingface_file_names(metadata: dict[str, Any]) -> list[str]:
    """从元数据里取全部 ``rfilename``（不过后缀过滤，用于存在性判定与报错清单）。"""

    siblings = metadata.get("siblings") if isinstance(metadata, dict) else None
    names: list[str] = []
    for item in siblings or []:
        filename = str(item.get("rfilename") or "") if isinstance(item, dict) else ""
        if filename and filename not in names:
            names.append(filename)
    return names


#: 可作为数据集下载的文件后缀（长的排前面，避免 ``.gz`` 抢在 ``.tar.gz`` 前匹配）。
_DATA_FILE_SUFFIXES = (
    ".tar.gz", ".parquet", ".jsonl", ".ndjson", ".csv", ".tsv",
    ".json", ".zip", ".tgz", ".gz",
)
#: HF 分片命名：``<name>-00000-of-00005.parquet`` / ``<name>_00000_of_00005.parquet``。
_SHARD_SUFFIX_RE = re.compile(r"[-_]\d{2,5}[-_]of[-_]\d{2,5}$", re.IGNORECASE)

# 仓库说明/构建/许可文件不是数据。无扩展名兼容只能在排除这些明确控制文件后启用，
# 否则把 Makefile、LICENSE 或模型卡当成数据会造成“下载成功但实验读不了”的假成功。
_HF_CONTROL_BASENAMES = frozenset({
    "readme", "license", "licence", "copying", "notice", "citation",
    "authors", "contributors", "changelog", "makefile", "dockerfile",
    "requirements", "codeowners", "datasetinfos", "metadata",
})
_HF_NON_DATA_DIRS = frozenset({
    ".git", ".github", "docs", "doc", "scripts", "src", "tests", "test",
    "examples", "example", "assets", "images", "figures",
})


def _file_extension(path: str) -> str:
    lower = path.rsplit("/", 1)[-1].casefold()
    for suffix in _DATA_FILE_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return ""


def _is_huggingface_data_sibling(
    item: Any, *, dataset_id: str
) -> bool:
    """Conservatively accept normal and extensionless HF dataset files.

    Positive evidence for an extensionless file is one of: LFS metadata, a positive
    byte size, a conventional data/split basename, or a basename derived from the
    repository's dataset name. This is provider metadata inspection, not a
    task-specific alias table.
    """
    if not isinstance(item, dict):
        return False
    filename = str(item.get("rfilename") or "").strip()
    if not filename or filename.endswith("/"):
        return False
    parts = [part for part in filename.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    basename = parts[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    normalized_stem = _normalized_name(stem)
    if (
        normalized_stem in _HF_CONTROL_BASENAMES
        or any(part.casefold() in _HF_NON_DATA_DIRS for part in parts[:-1])
        or basename.startswith(".")
    ):
        return False
    if _file_extension(filename):
        return True
    # 有未知扩展名时不放行；这里专门兼容**真正无扩展名**的仓库数据文件。
    if "." in basename:
        return False
    lfs = item.get("lfs")
    size = item.get("size")
    if isinstance(lfs, dict):
        size = lfs.get("size", size)
    if isinstance(size, (int, float)) and size > 0:
        return True
    conventional = {
        "data", "dataset", "train", "training", "test", "testing", "validation",
        "valid", "dev", "corpus", "records", "samples", "examples",
    }
    stem_tokens = {
        token for token in re.split(r"[^a-z0-9]+", stem.casefold()) if token
    }
    if stem_tokens & conventional:
        return True
    dataset_leaf = dataset_id.rsplit("/", 1)[-1]
    normalized_dataset = _normalized_name(dataset_leaf)
    return bool(
        normalized_dataset
        and len(normalized_dataset) >= 4
        and normalized_dataset in normalized_stem
    )


def _normalized_file_stem(path: str) -> str:
    """把仓库内文件路径归一到"逻辑文件名"：去目录、去扩展名、去分片后缀、只留字母数字。

    ``data/Conflict_Resolution-00000-of-00001.parquet`` → ``conflictresolution``，
    与声明的 ``data/conflict_resolution.json`` 归一后同名。
    """
    basename = path.rsplit("/", 1)[-1]
    extension = _file_extension(basename)
    if extension:
        basename = basename[: -len(extension)]
    return _normalized_name(_SHARD_SUFFIX_RE.sub("", basename))


def _match_declared_file_by_name(declared_path: str, real_files: list[str]) -> list[str]:
    """在真实清单里按归一化名找声明的那个逻辑文件（分片全部纳入）。

    只在**同一个仓库内**做这件事——仓库是 planner 自己指定的，所以这不是"换数据集"，
    只是把写错的文件名对回真实名字。找不到唯一逻辑文件时返回 ``[]``（不猜）。
    """
    declared_stem = _normalized_file_stem(declared_path)
    if not declared_stem:
        return []
    data_files = [name for name in real_files if _file_extension(name)]
    matched = [
        name for name in data_files if _normalized_file_stem(name) == declared_stem
    ]
    if not matched:
        return []
    declared_dir = declared_path.rsplit("/", 1)[0] if "/" in declared_path else ""
    directories = {name.rsplit("/", 1)[0] if "/" in name else "" for name in matched}
    if len(directories) > 1:
        # 同名逻辑文件散在多个目录 → 有歧义，不猜（交回 planner）
        same_dir = [
            name
            for name in matched
            if (name.rsplit("/", 1)[0] if "/" in name else "") == declared_dir
        ]
        if len(same_dir) != len(matched) and same_dir:
            matched = same_dir
        else:
            return []
    # 优先与声明同扩展名：声明 .json 就别顺手把 .parquet 也拖下来
    declared_extension = _file_extension(declared_path)
    same_extension = [
        name for name in matched if _file_extension(name) == declared_extension
    ]
    return same_extension or matched


def _declared_huggingface_file_candidate(
    dataset_id: str,
    declared_paths: list[str],
    *,
    declared_revision: str | None,
    metadata: dict[str, Any],
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> DatasetCandidate:
    """校验模块二声明的 HF 文件路径是否真的存在，存在则**只**绑定这一个文件。

    为什么要单文件绑定而不是复用 ``_huggingface_candidate``：后者会返回仓库里
    全部数据文件。模块二声明的是一条具体文件的直链（常常是某个 split），把整仓
    拖下来是语义漂移（例如 MemoryAgentBench 的 conflict-resolution split 会连带
    另外 3 个 split）。

    文件不存在时**不立刻放弃**：先在同一个仓库里按归一化名找唯一匹配的逻辑文件
    （planner 常常只是把文件名写错：``conflict_resolution.json`` vs 真实
    ``Conflict_Resolution-00000-of-00001.parquet``）。仓库是 planner 自己指认的，
    所以这不算换数据集；但匹配必须是唯一的，且选择理由写进 ``selection_reason``
    留审计——不静默。

    Raises:
        DeclaredFileMissing: 仓库可达、但既没有该文件、也没有唯一可对的真实文件名。
    """
    real_files = _huggingface_file_names(metadata)
    declared_path = next((path for path in declared_paths if path), "")
    selection_reason: str | None = None
    # 1) 逐字符命中：声明的就是真实路径，直接采用
    resolved_paths = [path for path in declared_paths if path and path in real_files][:1]
    if not resolved_paths:
        # 2) 归一化命中：planner 把文件名写错了，但仓库是它自己指认的，可以对
        for path in declared_paths:
            resolved_paths = _match_declared_file_by_name(path, real_files)
            if resolved_paths:
                selection_reason = (
                    f"模块二声明的文件 {path!r} 不在仓库 {dataset_id!r} 中；"
                    f"按归一化文件名唯一匹配到: {', '.join(resolved_paths)}"
                )
                log.warning(
                    "声明文件名不存在，按归一化名唯一匹配到真实文件: "
                    "dataset=%s declared=%s → %s",
                    dataset_id, path, resolved_paths,
                )
                break
    if not resolved_paths:
        raise DeclaredFileMissing(dataset_id, declared_path, real_files)
    # 声明的是可移动分支（main 之类）时改用仓库当前 SHA 固定版本，保证可复现；
    # 声明的已经是具体 revision 就原样沿用，不擅自改写用户给的地址。
    revision = str(metadata.get("sha") or "").strip() or declared_revision
    base = hf_bases()[0].rstrip("/") + "/datasets/" + quote(dataset_id, safe="/") + "/resolve/"
    if revision:
        urls = tuple(
            base + quote(revision, safe="") + "/" + quote(path, safe="/")
            for path in resolved_paths
        )
    else:  # pragma: no cover — 元数据正常时 sha 必然存在
        urls = tuple(
            base
            + quote(declared_revision or "main", safe="")
            + "/"
            + quote(path, safe="/")
            for path in resolved_paths
        )
    return DatasetCandidate(
        provider="huggingface",
        dataset_id=dataset_id,
        title=dataset_id,
        landing_url=hf_bases()[0].rstrip("/") + "/datasets/" + quote(dataset_id, safe="/"),
        download_urls=urls,
        license=_huggingface_license(metadata),
        revision=revision or None,
        selection_reason=selection_reason,
        # 文件清单是刚查过的，这条候选算已验证
        verified=True,
    )


def _resolve_explicit_reference(
    dataset_name: str,
    source_url: str,
    *,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> DatasetCandidate:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    # 用配置的 HF 站点判定，这样模块二/上游给出的 ``hf-mirror.com/datasets/...``
    # 也能被认成 HF 引用并解析出全部文件，而不是退化成「单 URL 直链」候选。
    if is_configured_hf_host(host) and len(parts) >= 2 and parts[0] == "datasets":
        marker_index = next(
            (index for index, part in enumerate(parts) if part in {"resolve", "blob"}),
            None,
        )
        dataset_id = "/".join(parts[1:3]) if len(parts) >= 3 else parts[1]
        try:
            metadata = _huggingface_metadata(
                dataset_id, timeout_seconds=timeout_seconds, fetch_json=fetch_json
            )
        except Exception:
            # 元数据拿不到（离线 / 镜像故障）：保持旧的「原样透传」行为，
            # 但标记未验证，别再让下游把它当成已经查过的直链。
            metadata = {}
        if marker_index is not None:
            # ``resolve/<rev>/<path>`` 与 ``blob/<rev>/<path>`` 都指向仓库里的一个
            # 具体文件；HF 的 resolve 需要带 revision，所以先按带 revision 解释，
            # 退一步再按不带 revision 解释。
            after_marker = parts[marker_index + 1:]
            candidates = ["/".join(after_marker[1:]), "/".join(after_marker)]
            declared_revision = after_marker[0] if len(after_marker) > 1 else None
            if metadata:
                # 命中 / 归一化命中 / 都对不上（抛 DeclaredFileMissing）全交给它判
                return _declared_huggingface_file_candidate(
                    dataset_id,
                    candidates,
                    declared_revision=declared_revision,
                    metadata=metadata,
                    timeout_seconds=timeout_seconds,
                    fetch_json=fetch_json,
                )
            # 元数据不可用：透传，但明确标记未验证
            return DatasetCandidate(
                provider="module2",
                dataset_id=dataset_name,
                title=dataset_name,
                landing_url=source_url,
                download_urls=(source_url,),
                verified=False,
            )
        try:
            return _huggingface_candidate(
                dataset_id,
                title=dataset_id,
                metadata=metadata,
                timeout_seconds=timeout_seconds,
                fetch_json=fetch_json,
            )
        except Exception as exc:
            if metadata:
                # 元数据拿到了，是真的列不出可下载文件（空仓库 / 超过 MAX_RESOLVED_FILES）
                # ——这是关于数据本身的结论，原样上抛。
                raise
            # 元数据拿不到（网络/镜像故障），`_huggingface_candidate` 会退到 parquet 端点；
            # 连它也失败时**不能**硬失败：一次网络抖动会被记成"数据不可得"，
            # 把 planner 引向换数据集，而真正该做的是按名称检索（那条路能命中
            # `_search_curated` 的官方 pin）。
            # 2026-09-17 实测：正是这条把 LoCoMo / LongMemEval 两条解析判成 ERROR。
            raise DeclaredReferenceUnresolvable(
                f"Hugging Face dataset {dataset_id!r} metadata unavailable and no file "
                f"listing could be derived for repository URL {source_url!r}: {exc}"
            ) from exc
    if host in {"zenodo.org", "www.zenodo.org"} and len(parts) >= 2:
        if parts[0] in {"records", "record"} and parts[1].isdigit():
            payload = fetch_json(
                f"https://zenodo.org/api/records/{parts[1]}", timeout_seconds
            )
            candidate = _zenodo_candidate(payload)
            if candidate is None:
                raise ValueError("Zenodo record has no public downloadable dataset files")
            return candidate
    # 兜底：非 HF / 非 Zenodo 的声明 URL 原样透传。模块三**没有**查过它是否存在
    # （不联网猜数据是否可下载是有意设计），所以标记 verified=False —— 别让
    # REPLAN 的「已验证候选直链」把这句没验证过的话再喂回 planner。
    return DatasetCandidate(
        provider="module2",
        dataset_id=dataset_name,
        title=dataset_name,
        landing_url=source_url,
        download_urls=(source_url,),
        verified=False,
    )


def _search_huggingface(
    dataset_name: str,
    *,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> list[DatasetCandidate]:
    results: list[DatasetCandidate] = []
    seen_ids: set[str] = set()
    for search_term in _dataset_search_terms(dataset_name):
        query = urlencode(
            {"search": search_term, "limit": MAX_SEARCH_RESULTS, "full": "true"}
        )
        payload = fetch_json_over_bases(
            f"/api/datasets?{query}", timeout_seconds, fetch_json=fetch_json
        )
        if not isinstance(payload, list):
            raise ValueError("Hugging Face search returned a non-list response")
        for item in payload:
            if not isinstance(item, dict):
                continue
            dataset_id = str(item.get("id") or "").strip()
            if (
                not dataset_id
                or dataset_id in seen_ids
                or not _exact_dataset_name(dataset_name, dataset_id)
            ):
                continue
            if item.get("private") is True or item.get("gated") not in {None, False, "false"}:
                continue
            seen_ids.add(dataset_id)
            metadata = item
            if not _huggingface_file_names(item):
                # 搜索接口**不返回** siblings（实测 hf-mirror 的
                # ``/api/datasets?search=…&full=true`` 每条结果都没有）。少了它
                # `_huggingface_candidate` 只能退到 parquet 端点，而
                # datasets-server.huggingface.co **没有镜像**——huggingface.co 直连
                # 一旦不通，整条按名称检索就全军覆没（2026-09-17 实测）。
                # 这里补一次详情查询：同一个 host 的 ``/api/datasets/{id}?full=true``
                # 走镜像拿得到真实文件清单。
                try:
                    metadata = _huggingface_metadata(
                        dataset_id, timeout_seconds=timeout_seconds, fetch_json=fetch_json
                    )
                except Exception:  # 详情拿不到就退回搜索结果的元数据
                    metadata = item
            try:
                candidate = _huggingface_candidate(
                    dataset_id,
                    title=dataset_id,
                    metadata=metadata,
                    timeout_seconds=timeout_seconds,
                    fetch_json=fetch_json,
                )
            except Exception:
                # 单个候选取不到可下载文件不该炸掉整轮检索：跳过它继续看别的候选。
                # 真的一个都没有时，调用方仍会拿到 NOT_FOUND + 各 provider 的错误。
                continue
            results.append(candidate)
    return results


#: 计划里的数据集名常带"装饰"——括号里的 split 说明、结尾的 split 词元。
#: 2026-09-17 事故：`MemoryAgentBench (conflict-resolution split)` 按原名去 HF
#: 检索永远返回 0 条（括号+空格+split 都不在仓库名里），而 `ai-hyz/MemoryAgentBench`
#: 一直好好地存在。检索前先把装饰剥掉。
_DECORATION_GROUPS_RE = re.compile(r"[（(][^（()）]*[)）]")
#: 只剥这些**明确的**切分说明词，且必须落在结尾、且剥完还剩东西。
#: 不碰 test/train/val 这类可能是数据集本名一部分的词。
_SPLIT_SUFFIX_TOKENS = frozenset({"split", "subset", "part", "portion", "sample"})
_SPLIT_SUFFIX_RE = re.compile(
    r"[\s\-]+(?:" + "|".join(sorted(_SPLIT_SUFFIX_TOKENS)) + r")\s*$",
    re.IGNORECASE,
)
#: 一次检索最多展开几个写法（每个写法一次 HTTP 请求，别无限膨胀）。
MAX_SEARCH_TERMS = 4


def _core_dataset_name(value: str) -> str:
    """剥掉计划名里的装饰，得到检索用的核心名。

    只做两件可解释的事，绝不猜：
      1. 去掉成对的括号组（中英文括号都认）——连同里面的 split 说明；
      2. 去掉结尾的 split/subset/part/portion/sample 词元（空格或 `-` 分隔）。
    只删不改：没被删掉的部分保持原写法（连字符不换成空格），剥完为空则原样返回
    ——宁可不剥，也不能把名字抹没了。

    例：``MemoryAgentBench (conflict-resolution split)`` → ``MemoryAgentBench``；
        ``Multi-Session-Chat (MSC)`` → ``Multi-Session-Chat``；
        ``LongMemEval-Cleaned`` → 原样（"cleaned" 不在可剥词表里，保守性不能丢）。
    """
    original = (value or "").strip()
    text = _DECORATION_GROUPS_RE.sub(" ", original).strip()
    previous = None
    while previous != text:
        previous = text
        text = _SPLIT_SUFFIX_RE.sub("", text).strip()
    return " ".join(text.split()).strip(" -_/") or original


def _dataset_search_terms(dataset_name: str) -> tuple[str, ...]:
    """Return conservative spelling variants for registry search only.

    核心名（剥装饰）的写法**优先**，原始写法兜底——装饰名本身也可能是真实仓库名，
    不能直接丢掉。总展开数受 ``MAX_SEARCH_TERMS`` 限制。
    """

    raw = dataset_name.strip()
    core = _core_dataset_name(raw)
    terms: list[str] = []
    for base in dict.fromkeys([core, raw]):  # core 优先且去重
        if not base:
            continue
        compact = "".join(character for character in base if character.isalnum())
        spaced = " ".join(part for part in base.replace("_", "-").split("-") if part)
        for term in (base, compact, spaced):
            if term and term not in terms:
                terms.append(term)
            if len(terms) >= MAX_SEARCH_TERMS:
                return tuple(terms)
    return tuple(terms)


def _is_dataset_landing_page(source_url: str) -> bool:
    """Recognise known paper/repository pages that are not dataset files."""

    parsed = urlparse(source_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if host in {"arxiv.org", "www.arxiv.org", "doi.org", "dx.doi.org"}:
        return True
    if host in {"openreview.net", "www.openreview.net", "aclanthology.org", "www.aclanthology.org"}:
        return True
    if host in {"github.com", "www.github.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        return len(parts) <= 2 or (len(parts) >= 3 and parts[2] in {"blob", "tree"})
    return path.endswith(("/", "/abs"))


def _huggingface_candidate(
    dataset_id: str,
    *,
    title: str,
    metadata: dict[str, Any],
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> DatasetCandidate:
    revision = str(metadata.get("sha") or "").strip() or None
    urls: list[str] = []
    siblings = metadata.get("siblings", []) if isinstance(metadata, dict) else []
    # 优先仓库中的原始文件并绑定 dataset SHA；自动转换 parquet 分支可能独立更新。
    # URL host 固定用主源（审计产物稳定 + 可复现），镜像回退由下载层负责。
    file_base = hf_bases()[0].rstrip("/") + "/datasets/"
    for item in siblings:
        filename = str(item.get("rfilename") or "") if isinstance(item, dict) else ""
        if _is_huggingface_data_sibling(item, dataset_id=dataset_id):
            urls.append(
                file_base
                + quote(dataset_id, safe="/")
                + "/resolve/"
                + quote(revision or "main", safe="")
                + "/"
                + quote(filename, safe="/")
            )
    if not urls:
        parquet_query = urlencode({"dataset": dataset_id})
        try:
            parquet_payload = fetch_json_over_bases(
                f"/parquet?{parquet_query}",
                timeout_seconds,
                bases=hf_parquet_bases(),
                fetch_json=fetch_json,
            )
        except Exception:
            parquet_payload = {}
        parquet_files = (
            parquet_payload.get("parquet_files", [])
            if isinstance(parquet_payload, dict)
            else []
        )
        urls = [
            str(item.get("url"))
            for item in parquet_files
            if isinstance(item, dict) and _approved_provider_url(item.get("url"), "huggingface")
        ]
        if revision:
            urls = [
                url.replace("/resolve/main/", "/resolve/" + quote(revision, safe="") + "/")
                for url in urls
            ]
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError(f"Hugging Face dataset {dataset_id!r} has no downloadable data files")
    if len(urls) > MAX_RESOLVED_FILES:
        raise ValueError(
            f"Hugging Face dataset {dataset_id!r} exposes {len(urls)} files; "
            f"automatic limit is {MAX_RESOLVED_FILES}"
        )
    return DatasetCandidate(
        provider="huggingface",
        dataset_id=dataset_id,
        title=title,
        landing_url=hf_bases()[0].rstrip("/") + "/datasets/" + quote(dataset_id, safe="/"),
        download_urls=tuple(urls),
        license=_huggingface_license(metadata),
        revision=revision,
    )


def _search_zenodo(
    dataset_name: str,
    *,
    timeout_seconds: int,
    fetch_json: JsonFetcher,
) -> list[DatasetCandidate]:
    query = urlencode(
        {"q": f'metadata.title:"{dataset_name}"', "type": "dataset", "size": MAX_SEARCH_RESULTS}
    )
    payload = fetch_json(f"https://zenodo.org/api/records?{query}", timeout_seconds)
    hits = payload.get("hits", {}).get("hits", []) if isinstance(payload, dict) else []
    results: list[DatasetCandidate] = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        title = str(item.get("metadata", {}).get("title") or "").strip()
        if not _exact_dataset_name(dataset_name, title):
            continue
        candidate = _zenodo_candidate(item)
        if candidate is not None:
            results.append(candidate)
    return results


def _zenodo_candidate(item: Any) -> DatasetCandidate | None:
    if not isinstance(item, dict):
        return None
    access = item.get("access", {})
    if isinstance(access, dict) and access.get("status") not in {None, "open"}:
        return None
    urls: list[str] = []
    for file_item in item.get("files", []):
        links = file_item.get("links", {}) if isinstance(file_item, dict) else {}
        value = links.get("download") or links.get("content") or links.get("self")
        if _approved_provider_url(value, "zenodo"):
            urls.append(str(value))
    urls = list(dict.fromkeys(urls))
    if not urls or len(urls) > MAX_RESOLVED_FILES:
        return None
    metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
    links = item.get("links", {}) if isinstance(item.get("links"), dict) else {}
    record_id = str(item.get("id") or item.get("record_id") or "").strip()
    landing_url = str(
        links.get("self_html") or links.get("html") or f"https://zenodo.org/records/{record_id}"
    )
    return DatasetCandidate(
        provider="zenodo",
        dataset_id=record_id,
        title=str(metadata.get("title") or record_id),
        landing_url=landing_url,
        download_urls=tuple(urls),
        license=_zenodo_license(metadata),
        # Zenodo record id 指向一个不可变版本记录；即使作者未填 version，
        # 仍用 record id 作可复现修订标识，而不使用可漂移的概念 DOI。
        revision=str(metadata.get("version") or "").strip() or record_id or None,
    )


def _fetch_json(url: str, timeout_seconds: int) -> Any:
    """默认 JSON 拉取器（带指数退避重试，见 ``_network.fetch_json_with_retry``）。

    保持 ``(url, timeout)`` 签名不变——它同时是 ``resolve_dataset_source(fetch_json=...)``
    的依赖注入缝，测试用假 fetcher 替换它，签名一变全挂。
    """
    return fetch_json_with_retry(url, timeout_seconds)


def _exact_dataset_name(query: str, candidate: str) -> bool:
    """名字相等判定。两侧都先剥装饰，再比归一化名。

    保守性仍在：只剥括号组与结尾 split 词元，``LongMemEval-Cleaned`` 不会
    因此匹配上 ``LongMemEval``（"cleaned" 不在 _SPLIT_SUFFIX_TOKENS 里）。
    """
    query_text = _core_dataset_name(query).casefold()
    candidate_text = _core_dataset_name(candidate).casefold()
    if "/" in query_text:
        return _normalized_name(query_text) == _normalized_name(candidate_text)
    return _normalized_name(query_text) == _normalized_name(candidate_text.rsplit("/", 1)[-1])


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _approved_provider_url(value: Any, provider: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if provider == "huggingface":
        # 配置的镜像站点（默认 hf-mirror.com）也算数——否则镜像返回的 parquet URL
        # 会被这里静默丢弃，镜像回退等于白做。
        return is_approved_hf_url(value)
    if provider == "zenodo":
        return host == "zenodo.org" or host.endswith(".zenodo.org")
    return False


def _huggingface_license(metadata: dict[str, Any]) -> str | None:
    card = metadata.get("cardData")
    if isinstance(card, dict) and card.get("license"):
        value = card["license"]
        return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
    for tag in metadata.get("tags", []):
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


def _zenodo_license(metadata: dict[str, Any]) -> str | None:
    license_value = metadata.get("license")
    if isinstance(license_value, dict):
        return str(license_value.get("id") or license_value.get("title") or "").strip() or None
    rights = metadata.get("rights")
    if isinstance(rights, list) and rights and isinstance(rights[0], dict):
        return str(rights[0].get("id") or rights[0].get("title") or "").strip() or None
    return str(license_value).strip() if license_value else None


def _short_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"

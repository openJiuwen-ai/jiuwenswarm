from __future__ import annotations

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any, Callable, Dict, List, Sequence

from models.retrieval import (
    RetrieverCandidate,
    RetrieverItem,
    RetrieverNode,
    RetrieverTrace,
    RetrieverTraceEvent,
    RetrieverChoice,
)
from .render.disclosure import (
    DisclosureConfig,
    DisclosurePromptParts,
    ExposedFragment,
    SelectableResolution,
    build_disclosure_messages,
    build_disclosure_prompt_parts,
    build_exposed_fragment,
    parse_selected_codes,
)
from ..llm import (
    GenerationConfig,
    GenerationConstraints,
    PrefixCacheUnavailable,
    ProgressiveLLMClient,
    PromptCacheHint,
    TrieConstraint,
    generation_config_to_debug_dict,
)
from .select.selection import GenerateFragmentSelector, LogitSelectionFragmentSelector
from .engine import RecursiveSearchEngine
from .expand import DefaultTargetExpander
from .reduce import DefaultBranchReducer
from .render import DefaultSubtreeRenderer
from .select import DefaultTopKSelector
from .subtree import DefaultCurrentSubtreeProvider
from .types import (
    ProgressiveRetrieverConfig,
    ProgressiveRetrieverResult,
    SearchCursor,
)

_FROM_PREFIX_RE = re.compile(r"^\s*From\s+[^:]+:\s*", re.IGNORECASE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class _RetrieverNodeStats:
    subtree_item_count: int = 0
    subtree_depth: int = 0


def _scoring_enabled(config: ProgressiveRetrieverConfig) -> bool:
    return _normalized_selection_mode(config.selection_mode) == "logit_selection"


def _normalized_selection_mode(value: str | None) -> str:
    normalized = str(value or "generate").strip().lower()
    return normalized or "generate"


def _format_sender_message(sender: str, message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    if _FROM_PREFIX_RE.match(text):
        return text
    sender_text = str(sender or "User").strip() or "User"
    return f"From {sender_text}: {text}"


def _normalize_query_messages(query: str | Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    if isinstance(query, str):
        text = _format_sender_message("User", query)
        return [{"role": "user", "content": text or "From User: (empty)"}]

    lines: List[str] = []
    for message in query:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        content = str(message.get("content") or "").strip()
        if not content or role == "system":
            continue
        if role == "assistant":
            sender = "Assistant"
        elif role == "user":
            sender = "User"
        else:
            sender = role[:1].upper() + role[1:] if role else "Runtime"
        lines.append(_format_sender_message(sender, content))
    if not lines:
        return [{"role": "user", "content": "From User: (empty)"}]
    return [{"role": "user", "content": "\n".join(lines)}]


def _build_node_selection_prompt(*, node: RetrieverNode, top_k: int) -> str:
    lines = [
        "You are a retriever that selects the most relevant child branches from the current node.",
        "Only choose from the direct child categories shown below.",
        "Do not explain your reasoning.",
        f"Current node: {node.label} ({node.node_id})",
    ]
    if node.description:
        lines.append(f"Current node description: {node.description}")
    lines.extend(
        [
            "",
            "Rules:",
            f"- Select up to {top_k} child categories.",
            "- Prefer branches that directly match the user's object or task type.",
            "- Output one node id per line.",
            "- Only output node ids from the list below.",
            "- Do not output explanations, JSON, Markdown, or numbering.",
            "",
            "Candidate child categories:",
        ]
    )
    for child in node.children:
        detail = f": {child.description}" if child.description else ""
        lines.append(f"- {child.node_id} | {child.label}{detail}")
    return "\n".join(lines)


def _build_item_selection_prompt(*, node: RetrieverNode, items: Sequence[RetrieverItem], top_k: int) -> str:
    lines = [
        "You are a retriever that selects the most relevant executable items from the current node.",
        "Only choose from the items shown below.",
        "Do not explain your reasoning.",
        f"Current node: {node.label} ({node.node_id})",
    ]
    if node.description:
        lines.append(f"Current node description: {node.description}")
    lines.extend(
        [
            "",
            "Rules:",
            f"- Select up to {top_k} items.",
            "- Prefer the most directly executable items for the user request.",
            "- Output one item id per line.",
            "- Only output item ids from the list below.",
            "- Do not output explanations, JSON, Markdown, or numbering.",
            "",
            "Candidate items:",
        ]
    )
    for item in items:
        label = item.label or item.item_id
        detail = f": {item.description}" if item.description else ""
        lines.append(f"- {item.item_id} | {label}{detail}")
    return "\n".join(lines)


@dataclass(frozen=True)
class _VisibleOption:
    display_name: str
    canonical_id: str
    label: str
    description: str
    kind: str
    prompt_text: str = ""


def _path_segments(value: str) -> List[str]:
    text = str(value or "").strip().replace("/", ".")
    return [part for part in text.split(".") if part]


def _to_pascal_case(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _CAMEL_BOUNDARY_RE.sub("-", text)
    text = text.replace("_", "-")
    text = _NON_ALNUM_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    parts = [part.lower() for part in text.strip("-").split("-") if part]
    if not parts:
        return ""
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _encode_boundary_code(index: int, *, width: int) -> str:
    del width
    return str(max(0, int(index)) + 1)


def _build_compact_boundary_codes(count: int) -> List[str]:
    if count <= 0:
        return []
    return [_encode_boundary_code(index, width=0) for index in range(count)]


def _compact_code_generation_max_tokens(top_k: int, *, generated_decimal_codes: bool) -> int:
    code_token_budget = 8 if generated_decimal_codes else 1
    return max(1, (code_token_budget + 1) * max(1, int(top_k)) - 1)


def _build_visible_options(
    *,
    branches: Sequence[RetrieverNode] = (),
    items: Sequence[RetrieverItem] = (),
    compact_codes_enabled: bool = False,
) -> List[_VisibleOption]:
    raw_entries: List[Dict[str, str]] = []
    for child in branches:
        raw_entries.append(
            {
                "canonical_id": child.node_id,
                "label": child.label or child.node_id,
                "description": child.description or "",
                "kind": "branch",
            }
        )
    for item in items:
        canonical_id = item.payload or item.item_id
        raw_entries.append(
            {
                "canonical_id": canonical_id,
                "label": item.label or canonical_id,
                "description": item.description or "",
                "kind": "item",
            }
        )

    if not raw_entries:
        return []

    if compact_codes_enabled:
        display_names = _build_compact_boundary_codes(len(raw_entries))
    else:
        segment_lists = [_path_segments(entry["canonical_id"]) for entry in raw_entries]
        display_names = [
            _initial_visible_display_name(entry=entry, segments=segments)
            for entry, segments in zip(raw_entries, segment_lists)
        ]

        while len(set(display_names)) < len(display_names):
            grouped: Dict[str, List[int]] = {}
            for index, name in enumerate(display_names):
                grouped.setdefault(name, []).append(index)
            for indices in grouped.values():
                if len(indices) <= 1:
                    continue
                for index in indices:
                    segments = segment_lists[index]
                    if len(segments) > 1:
                        current_depth = len(display_names[index].split("/"))
                        next_depth = min(len(segments), current_depth + 1)
                        display_names[index] = "/".join(
                            (_to_pascal_case(segment) or segment)
                            for segment in segments[-next_depth:]
                        )
                    else:
                        display_names[index] = f"{display_names[index]}__{index + 1}"

    options: List[_VisibleOption] = []
    for entry, display_name in zip(raw_entries, display_names):
        prompt_text = f"- {display_name} | {entry['kind']} | {entry['label']}"
        if entry["description"]:
            prompt_text = f"{prompt_text} | {entry['description']}"
        options.append(
            _VisibleOption(
                display_name=display_name,
                canonical_id=entry["canonical_id"],
                label=entry["label"],
                description=entry["description"],
                kind=entry["kind"],
                prompt_text=prompt_text,
            )
        )
    return options


def _initial_visible_display_name(*, entry: Dict[str, str], segments: Sequence[str]) -> str:
    fallback = segments[-1] if segments else entry["canonical_id"]
    return _to_pascal_case(fallback) or fallback


def _build_visible_subtree_prompt(
    *,
    node: RetrieverNode,
    options: Sequence[_VisibleOption],
    top_k: int,
    compact_codes_enabled: bool,
) -> str:
    lines = [
        "You are a retrieval router.",
        "You can only see the current visible subtree shown below.",
        "Select the most relevant visible boundary nodes for the user request.",
        f"Return at most {top_k} {'codes' if compact_codes_enabled else 'names'}.",
        f"Output one {'code' if compact_codes_enabled else 'display name'} per line.",
        f"Only output the {'codes' if compact_codes_enabled else 'display names'} exactly as shown.",
        "If none are relevant, output 0.",
        "Do not output explanations, JSON, Markdown, numbering, or full paths.",
        "",
        f"Current visible subtree root: {node.label} ({node.node_id})",
    ]
    if node.description:
        lines.append(f"Root description: {node.description}")
    lines.extend(["", "Visible boundary nodes:"])
    for option in options:
        lines.append(option.prompt_text)
    return "\n".join(lines)


def _is_abstain_output(output: str) -> bool:
    text = str(output or "").strip()
    return text == "0"


class ProgressiveRetriever:
    def __init__(
        self,
        *,
        llm: ProgressiveLLMClient,
        config: ProgressiveRetrieverConfig | None = None,
        debug_event_hook: Any | None = None,
    ) -> None:
        self._llm = llm
        self._config = config or ProgressiveRetrieverConfig()
        self._debug_event_hook = debug_event_hook
        self._node_stats_cache: Dict[int, _RetrieverNodeStats] = {}
        self._node_stats_lock = threading.Lock()
        self._current_subtree_cache: Dict[tuple[int, tuple[str, ...]], Any] = {}
        self._current_subtree_cache_lock = threading.Lock()

    def search(
        self,
        *,
        model: str,
        query: str | Sequence[Dict[str, str]],
        root: RetrieverNode,
        top_k: int | None = None,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> ProgressiveRetrieverResult:
        started = perf_counter()
        resolved_top_k = max(1, int(top_k if top_k is not None else self._config.top_k))
        query_messages = _normalize_query_messages(query)
        trace = RetrieverTrace()
        engine = self._build_recursive_search_engine(before_llm_call_hook=before_llm_call_hook)
        candidates = engine.search(
            model=model,
            query_messages=query_messages,
            root_cursor=SearchCursor(
                node=root,
                depth=0,
                branch_path=(root.node_id,),
                top_k=resolved_top_k,
            ),
            trace=trace,
        )
        ranked = [
            RetrieverCandidate(
                rank=index,
                item_id=candidate.item_id,
                payload=candidate.payload,
                branch_path=candidate.branch_path,
                label=candidate.label,
                description=candidate.description,
            )
            for index, candidate in enumerate(self._dedupe_candidates(candidates)[:resolved_top_k], start=1)
        ]
        trace.record(
            "search_complete",
            node_id=root.node_id,
            depth=0,
            detail={"candidate_count": len(ranked), "top_k": resolved_top_k},
        )
        return ProgressiveRetrieverResult(
            candidates=ranked,
            trace=trace,
            candidate_records=[
                {
                    "rank": candidate.rank,
                    "raw_output": candidate.item_id,
                    "resolved_payload": candidate.payload,
                    "valid": True,
                    "selected": candidate.rank == 1,
                    "choice_id": candidate.item_id,
                }
                for candidate in ranked
            ],
            summary_lines=[
                f"{candidate.rank}. {candidate.item_id} -> {candidate.payload} (ok)"
                for candidate in ranked
            ],
            selected_payload=ranked[0].payload if ranked else None,
            selected_rank=ranked[0].rank if ranked else -1,
            raw_outputs=[],
            request_messages=query_messages,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
        )

    def prepare_root(self, *, root: RetrieverNode, top_k: int | None = None) -> None:
        resolved_top_k = max(1, int(top_k if top_k is not None else self._config.top_k))
        provider = self._build_current_subtree_provider()
        for node, branch_path in _iter_nodes(root, (root.node_id,)):
            subtree = provider.get_current_subtree(
                cursor=SearchCursor(
                    node=node,
                    depth=max(0, len(branch_path) - 1),
                    branch_path=branch_path,
                    top_k=resolved_top_k,
                )
            )
            if subtree.selectable_targets:
                build_disclosure_prompt_parts(
                    fragment=subtree.fragment,
                    query_messages=(),
                    top_k=resolved_top_k,
                )

    def _build_recursive_search_engine(
        self,
        *,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> RecursiveSearchEngine:
        return RecursiveSearchEngine(
            subtree_provider=self._build_current_subtree_provider(),
            renderer=DefaultSubtreeRenderer(render_messages=False),
            selector=DefaultTopKSelector(
                config=self._config,
                build_generate_selector=lambda: self._build_fragment_selector(
                    before_llm_call_hook=before_llm_call_hook,
                ),
            ),
            expander=DefaultTargetExpander(config=self._config),
            reducer=DefaultBranchReducer(config=self._config),
            enable_parallel_branches=self._parallel_branches_enabled(),
            max_parallel_branches=max(1, int(self._config.max_parallel_branches)),
        )

    def _build_current_subtree_provider(self) -> DefaultCurrentSubtreeProvider:
        return DefaultCurrentSubtreeProvider(
            config=self._config,
            subtree_item_count=lambda current: self._analyze_node(current).subtree_item_count,
            cache=self._current_subtree_cache,
            cache_lock=self._current_subtree_cache_lock,
        )

    def _parallel_branches_enabled(self) -> bool:
        return bool(self._config.enable_parallel_branches) and bool(
            getattr(self._llm.capabilities, "thread_safe", True)
        )

    def _search_node(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
        branch_path: tuple[str, ...],
    ) -> List[RetrieverCandidate]:
        fragment = self._build_fragment(node=node, branch_path=branch_path)
        trace.record(
            "fragment_built",
            node_id=node.node_id,
            depth=depth,
            detail={
                "selectable_count": len(fragment.code_to_resolution),
                "rendered_tree": fragment.rendered_tree,
                "max_exposure_depth_per_call": int(self._config.max_exposure_depth_per_call),
                "exposure_threshold": int(self._config.exposure_threshold),
            },
        )
        selectable_resolutions = list(fragment.code_to_resolution.values())
        if not selectable_resolutions:
            return []
        if len(selectable_resolutions) == 1:
            only = selectable_resolutions[0]
            trace.record(
                "fragment_selected",
                node_id=node.node_id,
                depth=depth,
                detail={
                    "mode": "single_selectable_shortcut",
                    "selected_codes": [only.code],
                    "selected_canonical_ids": [only.canonical_id],
                },
            )
            return self._continue_from_resolutions(
                model=model,
                query_messages=query_messages,
                node=node,
                selected=selectable_resolutions,
                depth=depth,
                top_k=top_k,
                trace=trace,
            )
        output, selected = self._select_from_fragment(
            model=model,
            query_messages=query_messages,
            node=node,
            depth=depth,
            top_k=top_k,
            trace=trace,
            fragment=fragment,
        )
        if not selected and _is_abstain_output(output):
            trace.record(
                "fragment_continue",
                node_id=node.node_id,
                depth=depth,
                detail={
                    "selected_codes": [],
                    "selected_terminal_ids": [],
                    "selected_branch_ids": [],
                    "mode": "abstain",
                },
            )
            return []
        return self._continue_from_resolutions(
            model=model,
            query_messages=query_messages,
            node=node,
            selected=selected,
            depth=depth,
            top_k=top_k,
            trace=trace,
        )

    def _build_fragment(
        self,
        *,
        node: RetrieverNode,
        branch_path: tuple[str, ...],
    ) -> ExposedFragment:
        return build_exposed_fragment(
            root=node,
            branch_path=branch_path,
            config=DisclosureConfig(
                max_exposure_depth_per_call=max(0, int(self._config.max_exposure_depth_per_call)),
                exposure_threshold=max(0, int(self._config.exposure_threshold)),
                compact_boundary_codes_enabled=bool(self._config.compact_boundary_codes_enabled),
                compact_boundary_codebook=tuple(str(code) for code in self._config.compact_boundary_codebook),
                flatten_full_tree_in_prompt=bool(self._config.flatten_full_tree_in_prompt),
            ),
            subtree_item_count=lambda current: self._analyze_node(current).subtree_item_count,
        )

    def _select_from_fragment(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
        fragment: ExposedFragment,
    ) -> tuple[str, List[SelectableResolution]]:
        selector = self._build_fragment_selector()
        output, selected = selector.select(
            model=model,
            query_messages=query_messages,
            node=node,
            depth=depth,
            top_k=top_k,
            trace=trace,
            fragment=fragment,
        )
        trace.record(
            "fragment_selected",
            node_id=node.node_id,
            depth=depth,
            detail={
                "selected_codes": [item.code for item in selected],
                "selected_canonical_ids": [item.canonical_id for item in selected],
                "raw_output": output,
            },
        )
        return output, selected

    def _build_fragment_selector(
        self,
        *,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> GenerateFragmentSelector | LogitSelectionFragmentSelector:
        def generate_fn(**kwargs):
            return self._select_from_fragment_generate(
                before_llm_call_hook=before_llm_call_hook,
                **kwargs,
            )

        generate_selector = GenerateFragmentSelector(generate_fn=generate_fn)
        if not _scoring_enabled(self._config):
            return generate_selector
        if _normalized_selection_mode(self._config.selection_mode) != "logit_selection":
            return generate_selector
        return LogitSelectionFragmentSelector(
            client=self._llm,
            require_single_token_codes=bool(self._config.scoring_require_single_token_codes),
            fallback_mode=str(self._config.scoring_fallback_mode or "error"),
            generate_selector=generate_selector,
            max_candidates=max(1, int(self._config.scoring_max_candidates)),
            min_probability=self._config.scoring_min_probability,
            trace_top_n=max(1, int(self._config.scoring_trace_top_n)),
        )

    def _select_from_fragment_generate(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
        fragment: ExposedFragment,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> tuple[str, List[SelectableResolution]]:
        prompt_parts = build_disclosure_prompt_parts(
            fragment=fragment,
            query_messages=query_messages,
            top_k=top_k,
        )
        generation_config = self._build_generation_config(
            choice_id_to_payload={
                code: resolution.canonical_id
                for code, resolution in fragment.code_to_resolution.items()
            },
            excluded_choice_ids=[],
            top_k=top_k,
        )
        generation_config = self._attach_prompt_cache_hint(
            prompt_parts=prompt_parts,
            generation_config=generation_config,
        )
        output = self._complete(
            model=model,
            system_prompt=str(prompt_parts.full_messages[0]["content"]),
            query_messages=[dict(prompt_parts.full_messages[1])],
            max_tokens=self._generate_selection_max_tokens(top_k=top_k),
            trace=trace,
            node_id=node.node_id,
            depth=depth,
            stage="select_fragment",
            generation_config=generation_config,
            before_llm_call_hook=before_llm_call_hook,
        )
        selected = parse_selected_codes(fragment=fragment, output=output)[:max(1, int(top_k))]
        if fragment.compact_codes_enabled and selected:
            return "\n".join(item.code for item in selected), selected
        return output, selected

    def _attach_prompt_cache_hint(
        self,
        *,
        prompt_parts: DisclosurePromptParts,
        generation_config: GenerationConfig | None,
    ) -> GenerationConfig | None:
        if not bool(self._llm.capabilities.progressive_prefix_kv_cache):
            return generation_config
        get_handle = getattr(self._llm, "get_prompt_cache_handle", None)
        if not callable(get_handle):
            raise PrefixCacheUnavailable("LLM client declares prefix KV cache support but exposes no handle lookup")
        handle = get_handle(prompt_parts.cache_id)
        if handle is None:
            raise PrefixCacheUnavailable(f"prefix cache handle is missing: {prompt_parts.cache_id}")
        resolved = generation_config or self._llm.default_generation_config()
        return replace(
            resolved,
            prompt_cache=PromptCacheHint(
                handle=handle,
                suffix_text=prompt_parts.suffix_text,
                expected_prefix_len=getattr(handle, "prefix_len", None),
            ),
        )

    def _continue_from_resolutions(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        selected: Sequence[SelectableResolution],
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
    ) -> List[RetrieverCandidate]:
        trace.record(
            "fragment_continue",
            node_id=node.node_id,
            depth=depth,
            detail={
                "selected_codes": [item.code for item in selected],
                "selected_terminal_ids": [
                    item.item.item_id
                    for item in selected
                    if item.is_terminal and item.item is not None
                ],
                "selected_branch_ids": [
                    item.node.node_id
                    for item in selected
                    if not item.is_terminal and item.node is not None
                ],
            },
        )
        branch_top_k = self._resolve_branch_top_k(top_k=top_k, branch_count=max(1, len(selected)))
        grouped_results: List[List[RetrieverCandidate] | None] = [None] * len(selected)
        branch_indexes = [
            index
            for index, item in enumerate(selected)
            if not item.is_terminal and item.node is not None
        ]
        if branch_indexes and len(branch_indexes) > 1 and self._parallel_branches_enabled():
            max_workers = min(len(branch_indexes), max(1, int(self._config.max_parallel_branches)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(
                        self._search_node,
                        model=model,
                        query_messages=query_messages,
                        node=selected[index].node,
                        depth=depth + 1,
                        top_k=branch_top_k,
                        trace=trace,
                        branch_path=selected[index].branch_path,
                    ): index
                    for index in branch_indexes
                }
                for future, index in future_to_index.items():
                    grouped_results[index] = future.result()
        for index, resolution in enumerate(selected):
            if resolution.is_terminal and resolution.item is not None:
                grouped_results[index] = [
                    RetrieverCandidate(
                        rank=1,
                        item_id=resolution.item.item_id,
                        payload=resolution.item.payload,
                        branch_path=resolution.branch_path,
                        label=resolution.item.label,
                        description=resolution.item.description,
                    )
                ]
            elif resolution.node is not None and grouped_results[index] is None:
                grouped_results[index] = self._search_node(
                    model=model,
                    query_messages=query_messages,
                    node=resolution.node,
                    depth=depth + 1,
                    top_k=branch_top_k,
                    trace=trace,
                    branch_path=resolution.branch_path,
                )
        reduced = self._merge_branch_candidates(branch_results=grouped_results, top_k=top_k)
        trace.record(
            "reduce_complete",
            node_id=node.node_id,
            depth=depth,
            detail={
                "input_candidates": sum(len(group or []) for group in grouped_results),
                "output_candidates": len(reduced),
                "mode": "round_robin" if self._config.round_robin_branch_reduce else "sequential",
            },
        )
        return reduced

    def _search_children(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        selected_children: List[RetrieverNode],
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
        branch_path: tuple[str, ...],
    ) -> List[RetrieverCandidate]:
        child_ids = [child.node_id for child in selected_children]
        trace.record(
            "branch_fork",
            node_id=node.node_id,
            depth=depth,
            detail={
                "selected_child_ids": child_ids,
                "parallel": len(selected_children) > 1 and self._config.enable_parallel_branches,
                "branch_top_k": self._resolve_branch_top_k(top_k=top_k, branch_count=len(selected_children)),
            },
        )
        branch_top_k = self._resolve_branch_top_k(top_k=top_k, branch_count=len(selected_children))
        if len(selected_children) <= 1 or not self._config.enable_parallel_branches:
            branch_results = [
                self._search_node(
                    model=model,
                    query_messages=query_messages,
                    node=child,
                    depth=depth + 1,
                    top_k=branch_top_k,
                    trace=trace,
                    branch_path=branch_path + (child.node_id,),
                )
                for child in selected_children
            ]
        else:
            max_workers = min(len(selected_children), max(1, int(self._config.max_parallel_branches)))
            branch_results = [None] * len(selected_children)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(
                        self._search_node,
                        model=model,
                        query_messages=query_messages,
                        node=child,
                        depth=depth + 1,
                        top_k=branch_top_k,
                        trace=trace,
                        branch_path=branch_path + (child.node_id,),
                    ): index
                    for index, child in enumerate(selected_children)
                }
                for future, index in future_to_index.items():
                    branch_results[index] = future.result()
        merged: List[RetrieverCandidate] = []
        for branch_candidates in branch_results:
            if branch_candidates:
                merged.extend(branch_candidates)
        reduced = self._merge_branch_candidates(branch_results=branch_results, top_k=top_k)
        trace.record(
            "reduce_complete",
            node_id=node.node_id,
            depth=depth,
            detail={
                "input_candidates": len(merged),
                "output_candidates": len(reduced),
                "mode": "round_robin" if self._config.round_robin_branch_reduce else "sequential",
            },
        )
        return reduced

    def _select_children(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        children: Sequence[RetrieverNode],
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
    ) -> List[RetrieverNode]:
        if len(children) == 1:
            only_child = children[0]
            trace.record(
                "node_selection",
                node_id=node.node_id,
                depth=depth,
                detail={"selected_node_ids": [only_child.node_id], "raw_output": "", "mode": "single_child_shortcut"},
            )
            return [only_child]
        prompt_node = RetrieverNode(
            node_id=node.node_id,
            label=node.label,
            description=node.description,
            children=tuple(children),
        )
        options = _build_visible_options(
            branches=children,
            compact_codes_enabled=bool(self._config.compact_boundary_codes_enabled),
        )
        option_by_name = {option.display_name: option for option in options}
        child_by_id = {child.node_id: child for child in children}
        system_prompt = _build_visible_subtree_prompt(
            node=prompt_node,
            options=options,
            top_k=top_k,
            compact_codes_enabled=bool(self._config.compact_boundary_codes_enabled),
        )
        generation_config = self._build_generation_config(
            choice_id_to_payload={option.display_name: option.canonical_id for option in options},
            excluded_choice_ids=[],
            top_k=top_k,
        )
        output = self._complete(
            model=model,
            system_prompt=system_prompt,
            query_messages=query_messages,
            max_tokens=self._generate_selection_max_tokens(top_k=top_k),
            trace=trace,
            node_id=node.node_id,
            depth=depth,
            stage="select_children",
            generation_config=generation_config,
            before_llm_call_hook=None,
        )
        selected_ids = self._parse_ids(output)
        selected_children: List[RetrieverNode] = []
        seen: set[str] = set()
        for display_name in selected_ids:
            option = self._resolve_option(option_by_name=option_by_name, raw_value=display_name)
            if option is None:
                continue
            node_id = option.canonical_id
            child = child_by_id.get(node_id)
            if child is None or node_id in seen:
                continue
            selected_children.append(child)
            seen.add(node_id)
            if len(selected_children) >= top_k:
                break
        trace.record(
            "node_selection",
            node_id=node.node_id,
            depth=depth,
            detail={
                "selected_node_ids": [child.node_id for child in selected_children],
                "selected_display_names": [
                    option.display_name
                    for option in options
                    if option.canonical_id in {child.node_id for child in selected_children}
                ],
                "raw_output": output,
            },
        )
        return selected_children

    def _select_items(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
        branch_path: tuple[str, ...],
        items: Sequence[RetrieverItem] | None = None,
        item_paths: Dict[str, tuple[str, ...]] | None = None,
        allowed_payloads: Dict[str, str] | None = None,
        resolve_candidate: Callable[[str, Dict[str, str]], str] | None = None,
        system_prompt_override: str | None = None,
            before_llm_call_hook: Callable[[], None] | None = None,
    ) -> ProgressiveRetrieverResult | List[RetrieverCandidate]:
        candidate_items = list(items if items is not None else node.items)
        resolved_item_paths = dict(item_paths or {})
        visible_options = _build_visible_options(
            items=candidate_items,
            compact_codes_enabled=bool(self._config.compact_boundary_codes_enabled),
        )
        option_by_name = {option.display_name: option for option in visible_options}
        display_name_by_payload = {option.canonical_id: option.display_name for option in visible_options}
        subtree_prompt = _build_visible_subtree_prompt(
            node=node,
            options=visible_options,
            top_k=top_k,
            compact_codes_enabled=bool(self._config.compact_boundary_codes_enabled),
        )
        if not candidate_items:
            return ProgressiveRetrieverResult(candidates=[], trace=trace) if allowed_payloads is not None else []
        if len(candidate_items) == 1:
            item = candidate_items[0]
            item_branch_path = resolved_item_paths.get(item.item_id, branch_path)
            display_name = display_name_by_payload.get(
                item.payload or item.item_id,
                item.payload or item.item_id,
            )
            trace.record(
                "terminal_selection",
                node_id=node.node_id,
                depth=depth,
                detail={
                    "mode": "single_item_shortcut",
                    "selected_item_ids": [item.item_id],
                    "selected_display_names": [display_name],
                },
            )
            single = RetrieverCandidate(
                rank=1,
                item_id=item.item_id,
                payload=item.payload,
                branch_path=item_branch_path,
                label=item.label,
                description=item.description,
            )
            if allowed_payloads is not None:
                return ProgressiveRetrieverResult(
                    candidates=[single],
                    trace=trace,
                    candidate_records=[
                        {
                            "rank": 1,
                            "raw_output": display_name,
                            "resolved_payload": item.payload,
                            "valid": True,
                            "selected": True,
                            "choice_id": item.item_id,
                        }
                    ],
                    summary_lines=[f"1. {display_name} -> {item.payload} (ok, shortcut)"],
                    selected_payload=item.payload,
                    selected_rank=1,
                    raw_outputs=[],
                    request_messages=[],
                )
            return [single]

        if allowed_payloads is not None and resolve_candidate is not None:
            return self._select_items_flat(
                model=model,
                query_messages=query_messages,
                node=node,
                depth=depth,
                top_k=top_k,
                trace=trace,
                branch_path=branch_path,
                choice_id_to_payload=allowed_payloads,
                resolve_candidate=resolve_candidate,
                system_prompt_prefix=str(system_prompt_override or "").strip(),
                before_llm_call_hook=before_llm_call_hook,
                option_by_name=option_by_name,
                item_by_payload={item.payload: item for item in candidate_items},
            )

        system_prompt = subtree_prompt
        generation_config = self._build_generation_config(
            choice_id_to_payload={option.display_name: option.canonical_id for option in visible_options},
            excluded_choice_ids=[],
            top_k=top_k,
        )
        output = self._complete(
            model=model,
            system_prompt=system_prompt,
            query_messages=query_messages,
            max_tokens=self._generate_selection_max_tokens(top_k=top_k),
            trace=trace,
            node_id=node.node_id,
            depth=depth,
            stage="select_items",
            generation_config=generation_config,
            before_llm_call_hook=None,
        )
        selected_ids = self._parse_ids(output)
        selected: List[RetrieverCandidate] = []
        seen: set[str] = set()
        item_by_payload = {item.payload: item for item in candidate_items}
        for display_name in selected_ids:
            option = self._resolve_option(option_by_name=option_by_name, raw_value=display_name)
            if option is None:
                continue
            payload = option.canonical_id
            item = item_by_payload.get(payload)
            if item is None or item.item_id in seen:
                continue
            seen.add(item.item_id)
            selected.append(
                RetrieverCandidate(
                    rank=len(selected) + 1,
                    item_id=item.item_id,
                    payload=item.payload,
                    branch_path=resolved_item_paths.get(item.item_id, branch_path),
                    label=item.label,
                    description=item.description,
                )
            )
            if len(selected) >= top_k:
                break
        trace.record(
            "terminal_selection",
            node_id=node.node_id,
            depth=depth,
            detail={
                "selected_item_ids": [item.item_id for item in selected],
                "selected_display_names": [
                    display_name_by_payload.get(item.payload, item.item_id)
                    for item in selected
                ],
                "raw_output": output,
            },
        )
        if not selected and _is_abstain_output(output):
            trace.record(
                "terminal_selection_fallback",
                node_id=node.node_id,
                depth=depth,
                detail={"selected_item_ids": [], "strategy": "abstain_no_backfill"},
            )
        return selected

    def _select_items_flat(
        self,
        *,
        model: str,
        query_messages: List[Dict[str, str]],
        node: RetrieverNode,
        depth: int,
        top_k: int,
        trace: RetrieverTrace,
        branch_path: tuple[str, ...],
        choice_id_to_payload: Dict[str, str],
        resolve_candidate: Callable[[str, Dict[str, str]], str],
        system_prompt_prefix: str,
        before_llm_call_hook: Callable[[], None] | None,
        option_by_name: Dict[str, _VisibleOption],
        item_by_payload: Dict[str, RetrieverItem],
    ) -> ProgressiveRetrieverResult:
        candidate_records: List[Dict[str, object]] = []
        summary_lines: List[str] = []
        raw_outputs: List[str] = []
        selected_payloads: set[str] = set()
        excluded_choice_ids: List[str] = []
        selected_payload: str | None = None
        selected_rank = -1
        global_rank = 0
        max_rounds = 1
        messages: List[Dict[str, str]] = []
        for round_index in range(1, max_rounds + 1):
            remaining = max(0, top_k - len(selected_payloads))
            if remaining <= 0:
                break
            remaining_options = [
                option
                for option in option_by_name.values()
                if option.canonical_id not in selected_payloads
            ]
            if not remaining_options:
                break
            request_k = remaining
            round_choice_id_to_payload = {
                option.display_name: option.canonical_id
                for option in remaining_options
            }
            generation_config = self._build_generation_config(
                choice_id_to_payload=round_choice_id_to_payload,
                excluded_choice_ids=excluded_choice_ids,
                top_k=request_k,
            )
            round_subtree_prompt = _build_visible_subtree_prompt(
                node=node,
                options=remaining_options,
                top_k=request_k,
                compact_codes_enabled=bool(self._config.compact_boundary_codes_enabled),
            )
            if system_prompt_prefix:
                system_prompt = f"{system_prompt_prefix}\n\n{round_subtree_prompt}".strip()
            else:
                system_prompt = round_subtree_prompt
            if not messages:
                messages = [{"role": "system", "content": system_prompt}] + list(query_messages)
            output = self._complete(
                model=model,
                system_prompt=system_prompt,
                query_messages=query_messages,
                max_tokens=self._generate_selection_max_tokens(top_k=request_k),
                trace=trace,
                node_id=node.node_id,
                depth=depth,
                stage="select_items",
                generation_config=generation_config,
                before_llm_call_hook=before_llm_call_hook,
                io_event_type="retriever_io",
            )
            if output:
                raw_outputs.append(output)
            parsed_items = self._parse_multi_output(
                output,
                limit=request_k,
                option_by_name=option_by_name,
                item_by_payload=item_by_payload,
            )
            if not output:
                self._record_debug_event(
                    {
                        "type": "retriever_iteration",
                        "model": model,
                        "round": round_index,
                        "request_k": request_k,
                        "remaining": remaining,
                        "excluded_choice_ids": list(excluded_choice_ids),
                        "outputs": [],
                        "new_valid_payloads": [],
                        "new_excluded_choice_ids": [],
                        "status": "empty",
                    }
                )
                break
            round_new_valid = 0
            round_new_valid_payloads: List[str] = []
            round_new_excluded_choice_ids: List[str] = []
            for raw_output, resolved_payload, matched_choice_id in parsed_items:
                global_rank += 1
                valid = False
                if resolved_payload and resolved_payload not in selected_payloads:
                    valid = True
                    selected_payloads.add(resolved_payload)
                    if matched_choice_id:
                        excluded_choice_ids.append(matched_choice_id)
                        round_new_excluded_choice_ids.append(matched_choice_id)
                    round_new_valid += 1
                    round_new_valid_payloads.append(resolved_payload)
                    if selected_payload is None:
                        selected_payload = resolved_payload
                        selected_rank = global_rank
                candidate_records.append(
                    {
                        "rank": global_rank,
                        "raw_output": raw_output,
                        "resolved_payload": resolved_payload,
                        "valid": valid,
                        "selected": False,
                        "choice_id": matched_choice_id,
                    }
                )
                label = resolved_payload or "-"
                status = "ok" if valid else "invalid"
                summary_lines.append(f"{global_rank}. {raw_output} -> {label} ({status}, round={round_index})")
            self._record_debug_event(
                {
                    "type": "retriever_iteration",
                    "model": model,
                    "round": round_index,
                    "request_k": request_k,
                    "remaining": remaining,
                    "excluded_choice_ids": list(excluded_choice_ids),
                    "outputs": [item[0] for item in parsed_items],
                    "raw_output": output,
                    "new_valid_payloads": round_new_valid_payloads,
                    "new_excluded_choice_ids": round_new_excluded_choice_ids,
                    "status": "ok" if round_new_valid > 0 else "stalled",
                }
            )
            if round_new_valid <= 0:
                break
        if selected_rank > 0 and 0 <= selected_rank - 1 < len(candidate_records):
            candidate_records[selected_rank - 1]["selected"] = True
        candidates = [
            RetrieverCandidate(
                rank=int(item["rank"]),
                item_id=str(item.get("choice_id") or item.get("raw_output") or ""),
                payload=str(item.get("resolved_payload") or ""),
                branch_path=branch_path,
                label=str(item.get("choice_id") or ""),
                description="",
            )
            for item in candidate_records
            if item.get("valid")
        ]
        return ProgressiveRetrieverResult(
            candidates=candidates,
            trace=trace,
            candidate_records=candidate_records,
            summary_lines=summary_lines,
            selected_payload=selected_payload,
            selected_rank=selected_rank,
            raw_outputs=raw_outputs,
            request_messages=messages,
            elapsed_ms=0.0,
        )

    def _generate_selection_max_tokens(self, *, top_k: int) -> int:
        if (
            bool(self._config.compact_boundary_codes_enabled)
            and str(self._config.selection_mode or "generate").strip().lower() == "generate"
        ):
            return _compact_code_generation_max_tokens(
                top_k,
                generated_decimal_codes=not bool(self._config.compact_boundary_codebook),
            )
        return max(1, int(self._config.max_tokens))

    def _complete(
        self,
        *,
        model: str,
        system_prompt: str,
        query_messages: List[Dict[str, str]],
        max_tokens: int,
        trace: RetrieverTrace,
        node_id: str,
        depth: int,
        stage: str,
        generation_config: GenerationConfig | None,
        before_llm_call_hook: Callable[[], None] | None,
        io_event_type: str = "finder_io",
    ) -> str:
        resolved_generation_config = generation_config or self._llm.default_generation_config()
        messages = [{"role": "system", "content": system_prompt}] + list(query_messages)
        request_detail = {
            "stage": stage,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "generation_config": generation_config_to_debug_dict(resolved_generation_config),
        }
        trace.record("llm_request", node_id=node_id, depth=depth, detail=request_detail)
        if self._debug_event_hook is not None:
            self._debug_event_hook({"type": io_event_type, "phase": "request", **request_detail})
        if before_llm_call_hook is not None:
            before_llm_call_hook()
        llm_started = perf_counter()
        ttft_ms: float | None = None
        use_stream = bool(self._llm.capabilities.streaming)
        usage: Dict[str, Any] = {}
        if use_stream:
            chunks: list[str] = []
            for chunk in self._llm.stream_complete(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                stop_sequences=None,
                generation_config=resolved_generation_config,
                request_timeout=self._config.request_timeout,
            ):
                chunk_usage = getattr(chunk, "usage", None)
                if isinstance(chunk_usage, dict) and chunk_usage:
                    usage = dict(chunk_usage)
                text = str(chunk or "")
                if not text:
                    continue
                if ttft_ms is None:
                    ttft_ms = (perf_counter() - llm_started) * 1000.0
                chunks.append(text)
            outputs = ["".join(chunks)]
        else:
            outputs = self._llm.complete(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                stop_sequences=None,
                generation_config=resolved_generation_config,
                n=1,
                request_timeout=self._config.request_timeout,
            )
        elapsed_ms = (perf_counter() - llm_started) * 1000.0
        output = outputs[0] if outputs else ""
        response_detail = {
            "stage": stage,
            "model": model,
            "outputs": list(outputs),
            "usage": dict(usage),
            "latency": {
                "elapsed_ms": round(elapsed_ms, 3),
                "ttft_ms": None if ttft_ms is None else round(ttft_ms, 3),
                "decode_ms": None if ttft_ms is None else round(max(0.0, elapsed_ms - ttft_ms), 3),
                "stream": use_stream,
            },
        }
        trace.record("llm_response", node_id=node_id, depth=depth, detail=response_detail)
        if self._debug_event_hook is not None:
            self._debug_event_hook({"type": io_event_type, "phase": "response", **response_detail})
        return output

    def _build_generation_config(
        self,
        *,
        choice_id_to_payload: Dict[str, str],
        excluded_choice_ids: List[str] | None,
        top_k: int,
    ) -> GenerationConfig | None:
        if not self._config.trie_constrained_decoding_enabled:
            return None
        if not bool(self._llm.capabilities.trie_constrained_decoding):
            return None
        choice_ids = list(choice_id_to_payload.keys())
        excluded = [str(item).strip() for item in (excluded_choice_ids or []) if str(item).strip()]
        digest = hashlib.sha256(json.dumps(choice_ids, ensure_ascii=False).encode("utf-8")).hexdigest()
        return GenerationConfig(
            constraints=GenerationConstraints(
                trie=TrieConstraint(
                    allowed_output_ids=tuple(choice_ids),
                    excluded_output_ids=tuple(excluded),
                    top_k=max(1, int(top_k)),
                    version=digest,
                )
            )
        )

    @staticmethod
    def _collapse_unique_chain(node: RetrieverNode) -> tuple[RetrieverNode, List[str]]:
        current = node
        collapsed: List[str] = []
        while len(current.children) == 1 and not current.items:
            current = current.children[0]
            collapsed.append(current.node_id)
        return current, collapsed

    @staticmethod
    def _parse_ids(output: str) -> List[str]:
        values: List[str] = []
        for line in str(output or "").splitlines():
            cleaned = re.sub(r"^\s*(?:\d+[\).\s:-]+|[-*]\s+)", "", line.strip())
            if not cleaned:
                continue
            values.append(cleaned.split("|", 1)[0].strip())
        if values:
            return values
        return re.findall(r"[A-Za-z][A-Za-z0-9_./-]*", str(output or ""))

    def _parse_multi_output(
        self,
        content: str,
        *,
        limit: int,
        option_by_name: Dict[str, _VisibleOption],
        item_by_payload: Dict[str, RetrieverItem],
    ) -> List[tuple[str, str, str]]:
        raw_candidates = self._parse_ids((content or "").strip())
        parsed: List[tuple[str, str, str]] = []
        for raw in raw_candidates:
            option = self._resolve_option(option_by_name=option_by_name, raw_value=raw)
            if option is None:
                continue
            resolved_payload = option.canonical_id
            item = item_by_payload.get(resolved_payload)
            if item is None:
                continue
            parsed.append((raw, resolved_payload, item.item_id))
            if len(parsed) >= max(1, int(limit)):
                break
        return parsed

    @staticmethod
    def _resolve_choice_id(raw_candidate: str, choice_id_to_payload: Dict[str, str], resolved_payload: str) -> str:
        raw = str(raw_candidate or "").strip().strip("`").strip("<>").strip()
        if raw in choice_id_to_payload and choice_id_to_payload[raw] == resolved_payload:
            return raw
        for choice_id, payload in choice_id_to_payload.items():
            if payload == resolved_payload:
                return choice_id
        return ""

    @staticmethod
    def _resolve_option(*, option_by_name: Dict[str, _VisibleOption], raw_value: str) -> _VisibleOption | None:
        option = option_by_name.get(str(raw_value or "").strip())
        if option is not None:
            return option
        text = str(raw_value or "").strip()
        if not text.isdigit():
            return None
        index = int(text) - 1
        if index < 0:
            return None
        ordered = list(option_by_name.values())
        if index >= len(ordered):
            return None
        return ordered[index]

    def _record_debug_event(self, event: Dict[str, object]) -> None:
        if self._debug_event_hook is None:
            return
        self._debug_event_hook(dict(event))

    def _resolve_branch_limit(self, *, child_count: int, top_k: int) -> int:
        limit_with_slack = int(top_k) + max(0, int(self._config.branch_choice_slack))
        return min(
            max(1, int(child_count)),
            max(1, min(int(self._config.max_branch_choices), limit_with_slack)),
        )

    def _resolve_branch_top_k(self, *, top_k: int, branch_count: int) -> int:
        if branch_count <= 0:
            return max(1, int(top_k))
        slack = max(0, int(self._config.branch_candidate_slack))
        budget = math.ceil(max(1, int(top_k)) / branch_count) + slack
        return min(max(1, int(top_k)), max(1, budget))

    def _analyze_node(self, node: RetrieverNode) -> _RetrieverNodeStats:
        cache_key = id(node)
        cached = self._node_stats_cache.get(cache_key)
        if cached is not None:
            return cached
        subtree_item_count = len(node.items)
        subtree_depth = 0
        for child in node.children:
            child_stats = self._analyze_node(child)
            subtree_item_count += child_stats.subtree_item_count
            subtree_depth = max(subtree_depth, child_stats.subtree_depth + 1)
        stats = _RetrieverNodeStats(subtree_item_count=subtree_item_count, subtree_depth=subtree_depth)
        with self._node_stats_lock:
            self._node_stats_cache.setdefault(cache_key, stats)
        return stats

    def _collect_subtree_items(
        self,
        node: RetrieverNode,
        branch_path: tuple[str, ...],
    ) -> List[tuple[RetrieverItem, tuple[str, ...]]]:
        collected: List[tuple[RetrieverItem, tuple[str, ...]]] = [(item, branch_path) for item in node.items]
        for child in node.children:
            collected.extend(self._collect_subtree_items(child, branch_path + (child.node_id,)))
        return collected

    def _merge_branch_candidates(
        self,
        *,
        branch_results: Sequence[List[RetrieverCandidate] | None],
        top_k: int,
    ) -> List[RetrieverCandidate]:
        if not self._config.round_robin_branch_reduce:
            merged: List[RetrieverCandidate] = []
            for branch_candidates in branch_results:
                if branch_candidates:
                    merged.extend(branch_candidates)
            return self._dedupe_candidates(merged)[:top_k]
        reduced: List[RetrieverCandidate] = []
        seen: set[str] = set()
        index = 0
        while len(reduced) < top_k:
            added = False
            for branch_candidates in branch_results:
                if not branch_candidates or index >= len(branch_candidates):
                    continue
                candidate = branch_candidates[index]
                dedupe_key = candidate.payload or candidate.item_id
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                reduced.append(candidate)
                added = True
                if len(reduced) >= top_k:
                    break
            if not added:
                break
            index += 1
        if len(reduced) >= top_k:
            return reduced[:top_k]
        merged: List[RetrieverCandidate] = []
        for branch_candidates in branch_results:
            if branch_candidates:
                merged.extend(branch_candidates)
        for candidate in self._dedupe_candidates(merged):
            dedupe_key = candidate.payload or candidate.item_id
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            reduced.append(candidate)
            if len(reduced) >= top_k:
                break
        return reduced[:top_k]

    @staticmethod
    def _dedupe_candidates(candidates: List[RetrieverCandidate]) -> List[RetrieverCandidate]:
        reduced: List[RetrieverCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            dedupe_key = candidate.payload or candidate.item_id
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            reduced.append(candidate)
        return reduced


def _iter_nodes(
    root: RetrieverNode,
    branch_path: tuple[str, ...],
) -> Sequence[tuple[RetrieverNode, tuple[str, ...]]]:
    nodes: list[tuple[RetrieverNode, tuple[str, ...]]] = [(root, branch_path)]
    for child in root.children:
        nodes.extend(_iter_nodes(child, branch_path + (child.node_id,)))
    return nodes


__all__ = [
    "RetrieverCandidate",
    "RetrieverItem",
    "RetrieverNode",
    "RetrieverTrace",
    "RetrieverTraceEvent",
    "RetrieverChoice",
    "ProgressiveRetriever",
    "ProgressiveRetrieverConfig",
    "ProgressiveRetrieverResult",
]

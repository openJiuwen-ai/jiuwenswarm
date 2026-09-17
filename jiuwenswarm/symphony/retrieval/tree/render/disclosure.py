from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import yaml

from models.retrieval import RetrieverItem, RetrieverNode
from ...protocols.display_name import to_pascal_case

_PROMPT_FILE = Path(__file__).with_name("prompts.yaml")
_LEGACY_FLAT_NON_COMPACT_SYSTEM_PROMPT = (
    "\n# 背景与目标：\n"
    "- 系统的最终目标是解决用户的问题。\n"
    "- 系统通过调用 Candidate skill 来执行任务。\n"
    "- 你的职责是：组建一个最优 skill 小组，以端到端闭环用户的任务。\n"
    "\n"
    "注意：\n"
    "- 你只做“候选筛选”，不做规划、不做执行。\n"
    "\n\n"
    "# 输出规则：\n"
    "\n"
    "1. 总共输出至多 5 个 Candidate skill 节点 id\n"
    "\n"
    "2. 该结果必须是：\n"
    "   - 最优 Candidate skill 组合\n"
    "   - 该组合能够完整闭环用户任务\n"
    "\n"
    "3. 至多输出 5 行\n"
    "\n\n"
    "# 选择规则：\n"
    "\n"
    "- 必须严格遵守用户的显式约束（如时间、范围、条件等）\n"
    "- 要选出**能够完整闭环用户意图的 skill 组合**，包括用户的显式意图和隐式意图。\n"
    "  - 例如，用户有出行意图时，不仅考虑出行规划，也要选择查询天气相关skill\n"
    "  - 用户有饮食、购物等意图时，不仅直接选择对应的出行助手，也要选择美团红包优惠\n"
    "  - 用户要搭建网站时，不仅直接选择html生成与前端开发，也可以考虑艺术创作\n"
    "  - 务必确认用户的真实隐含意图，例如“生成一张地图”实质上对应于基于AI的图像生成，而非查询地图。\n"
    "  - 不要附带任何无关的skill，例如，用户不需要ppt时不要选择ppt相关技能。用户需要生成地图而非查询地点时，不要选择地图相关技能。\n"
    "  - 用户明确不要某类skill时，不要选择对应的skill。\n"
    "\n\n"
    "{candidate_block}\n"
    "\n\n"
    "# 输出 id 说明：\n"
    "\n"
    "- 仅输出驼峰格式的节点 id, 不要附带任何其他内容\n"
)


@lru_cache(maxsize=1)
def _load_progressive_prompt_bank() -> dict[str, str]:
    raw = yaml.safe_load(_PROMPT_FILE.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid prompt yaml: {_PROMPT_FILE}")
    bank = raw.get("progressive_system_prompt")
    if not isinstance(bank, dict):
        raise ValueError(f"missing progressive_system_prompt in prompt yaml: {_PROMPT_FILE}")
    required_keys = {
        "shared",
        "shared_top2",
        "structure_codes",
        "structure_names",
        "structure_codes_flat",
        "structure_names_flat",
        "output_codes",
        "output_codes_flat",
        "output_names_tree",
        "output_names_flat",
    }
    missing = sorted(required_keys.difference(bank))
    if missing:
        raise ValueError(f"missing progressive prompt keys in {_PROMPT_FILE}: {missing}")
    return {key: str(value).rstrip("\n") for key, value in bank.items()}


def _build_system_prompt(
    *,
    compact_codes_enabled: bool,
    flat_list_mode: bool,
    top_k: int,
    candidate_block: str = "",
) -> str:
    resolved_top_k = max(1, int(top_k))
    if not compact_codes_enabled and flat_list_mode and resolved_top_k != 2:
        return _LEGACY_FLAT_NON_COMPACT_SYSTEM_PROMPT.format(
            candidate_block=str(candidate_block or "").rstrip()
        )
    bank = _load_progressive_prompt_bank()
    candidate_region = "<CANDIDATE_LIST>" if flat_list_mode else "<CANDIDATE_TREE>"
    if compact_codes_enabled and flat_list_mode:
        structure_block = bank["structure_codes_flat"]
        output_block = _compact_flat_output_block()
    elif compact_codes_enabled:
        structure_block = bank["structure_codes"]
        output_block = bank["output_codes"]
    elif flat_list_mode:
        structure_block = bank["structure_names_flat"]
        output_block = bank["output_names_flat"]
    else:
        structure_block = bank["structure_names"]
        output_block = bank["output_names_tree"]
    shared_key = "shared_top2" if resolved_top_k == 2 else "shared"
    return bank[shared_key].format(
        top_k=resolved_top_k,
        candidate_region=candidate_region,
        structure_block=structure_block,
        candidate_block=str(candidate_block or "").rstrip(),
        output_block=output_block.format(top_k=resolved_top_k),
    )


_SELECTION_LINE_RE = re.compile(r"^\s*(?:\d+[\).:-]+\s*|[-*]\s+)?(.+?)\s*$")
_COMPACT_ID_HANDLE_RE = re.compile(r"\[\s*id\s*:\s*([^\]\s|,:;]+)\s*\]", re.IGNORECASE)
_COMPACT_JSON_ID_FIELD_RE = re.compile(r"""["'](?:id|c|code)["']\s*:\s*["']([^"'\]\s|,:;{}]+)["']""", re.IGNORECASE)
_COMPACT_BRACKET_PREFIX_RE = re.compile(r"^\[\s*([^\]\s|,:;]+)\s*\]\s*\.")
_QUERY_FROM_PREFIX_RE = re.compile(r"^\s*From\s+[^:]+:\s*", re.IGNORECASE)
_REPRESENTATIVE_DESCENDANTS_RE = re.compile(
    r"(?:\n\s*|\s+)Representative descendants:\s*.+$",
    re.IGNORECASE | re.DOTALL,
)
_FLAT_COMPACT_DESCRIPTION_MAX_CHARS_DEFAULT = 150
_FLAT_COMPACT_FIELD_ORDER_DEFAULT = ("category", "name", "raw_name", "description", "id")


@dataclass(frozen=True)
class DisclosureConfig:
    max_exposure_depth_per_call: int = 2
    exposure_threshold: int = 12
    compact_boundary_codes_enabled: bool = False
    compact_boundary_codebook: tuple[str, ...] = ()
    flatten_full_tree_in_prompt: bool = False


@dataclass(frozen=True)
class SelectableResolution:
    code: str
    canonical_id: str
    display_name: str
    label: str
    description: str
    is_terminal: bool
    branch_path: tuple[str, ...]
    score_key: str = ""
    token_id: int | None = None
    node: RetrieverNode | None = None
    item: RetrieverItem | None = None


@dataclass(frozen=True)
class ExposedNode:
    canonical_id: str
    label: str
    description: str
    is_selectable: bool
    selectable_canonical_id: str | None = None
    children: tuple["ExposedNode", ...] = ()


_SelectableEntry = tuple[
    str,
    str,
    str,
    str,
    bool,
    tuple[str, ...],
    RetrieverNode | None,
    RetrieverItem | None,
]


@dataclass(frozen=True)
class ExposedFragment:
    root: ExposedNode
    rendered_tree: str
    code_to_resolution: Dict[str, SelectableResolution]
    selectable_nodes: tuple[ExposedNode, ...] = ()
    candidate_codes: tuple[str, ...] = ()
    fragment_fingerprint: str = ""
    code_width: int = 1
    compact_codes_enabled: bool = False
    flat_list_mode: bool = False

    def build_system_prompt(self, *, top_k: int | None = None) -> str:
        resolved_top_k = max(1, int(top_k if top_k is not None else (len(self.code_to_resolution) or 1)))
        return _build_system_prompt(
            compact_codes_enabled=bool(self.compact_codes_enabled),
            flat_list_mode=bool(self.flat_list_mode),
            top_k=resolved_top_k,
        )

    @property
    def system_prompt(self) -> str:
        return self.build_system_prompt()

    @property
    def user_prefix(self) -> str:
        if self.flat_list_mode:
            return f"Available options:\n{self.rendered_tree}\n\nUser request:\n"
        return f"<CANDIDATE_TREE>\n{self.rendered_tree}\n</CANDIDATE_TREE>\n\n<USER_REQUEST>\n"


@dataclass(frozen=True)
class DisclosurePromptParts:
    full_messages: tuple[Dict[str, str], ...]
    prefix_messages: tuple[Dict[str, str], ...]
    suffix_text: str
    cache_id: str
    prefix_token_hash: str


def build_exposed_fragment(
    *,
    root: RetrieverNode,
    branch_path: tuple[str, ...],
    config: DisclosureConfig,
    subtree_item_count: callable,
) -> ExposedFragment:
    selectable_entries: List[_SelectableEntry] = []
    root_node = _expand_node(
        node=root,
        current_path=branch_path,
        remaining_depth=max(0, int(config.max_exposure_depth_per_call)),
        is_root=True,
        config=config,
        subtree_item_count=subtree_item_count,
        selectable_entries=selectable_entries,
    )
    if bool(config.flatten_full_tree_in_prompt):
        return _build_flat_fragment_from_exposed_subtree(
            root_node=root_node,
            selectable_entries=selectable_entries,
            config=config,
        )
    codes = _build_codes(
        selectable_entries,
        compact_codes_enabled=bool(config.compact_boundary_codes_enabled),
        compact_codebook=config.compact_boundary_codebook,
    )
    display_names = _build_boundary_names(selectable_entries)
    resolution_by_code: Dict[str, SelectableResolution] = {}
    resolution_by_canonical_id: Dict[str, SelectableResolution] = {}
    for code, display_name, entry in zip(codes, display_names, selectable_entries):
        (
            canonical_id,
            label,
            description,
            selectable_canonical_id,
            is_terminal,
            selectable_path,
            node_ref,
            item_ref,
        ) = entry
        resolution = SelectableResolution(
            code=code,
            canonical_id=selectable_canonical_id,
            display_name=display_name,
            label=label,
            description=description,
            is_terminal=is_terminal,
            branch_path=selectable_path,
            score_key=code,
            node=node_ref,
            item=item_ref,
        )
        resolution_by_code[code] = resolution
        resolution_by_canonical_id[canonical_id] = resolution
    rendered_tree = _render_tree(
        root_node,
        resolution_by_canonical_id=resolution_by_canonical_id,
        compact_codes_enabled=bool(config.compact_boundary_codes_enabled),
    )
    candidate_codes = tuple(codes)
    return ExposedFragment(
        root=root_node,
        rendered_tree=rendered_tree,
        code_to_resolution=resolution_by_code,
        selectable_nodes=tuple(_iter_selectable_nodes(root_node)),
        candidate_codes=candidate_codes,
        fragment_fingerprint=_build_fragment_fingerprint(root_node=root_node, candidate_codes=candidate_codes),
        code_width=_resolve_code_width(codes),
        compact_codes_enabled=bool(config.compact_boundary_codes_enabled),
        flat_list_mode=False,
    )


def build_disclosure_messages(
    *,
    fragment: ExposedFragment,
    query_messages: Sequence[Dict[str, str]],
    top_k: int | None = None,
) -> List[Dict[str, str]]:
    return list(
        build_disclosure_prompt_parts(
            fragment=fragment,
            query_messages=query_messages,
            top_k=top_k,
        ).full_messages
    )


def build_disclosure_prompt_parts(
    *,
    fragment: ExposedFragment,
    query_messages: Sequence[Dict[str, str]],
    top_k: int | None = None,
) -> DisclosurePromptParts:
    query_text = "\n".join(
        _normalize_query_content(str(message.get("content") or "").strip())
        for message in query_messages
        if str(message.get("content") or "").strip()
    ).strip()
    resolved_top_k = None if top_k is None else max(1, int(top_k))
    system_prompt, user_prefix, prefix_token_hash, cache_id = _build_disclosure_prompt_static(
        rendered_tree=fragment.rendered_tree,
        fragment_fingerprint=fragment.fragment_fingerprint,
        candidate_codes=tuple(str(code) for code in fragment.candidate_codes),
        compact_codes_enabled=bool(fragment.compact_codes_enabled),
        flat_list_mode=bool(fragment.flat_list_mode),
        top_k=resolved_top_k,
    )
    if fragment.flat_list_mode:
        suffix_text = query_text
    else:
        suffix_text = f"{query_text}\n</USER_REQUEST>".rstrip()
    user_content = f"{user_prefix}{suffix_text}".rstrip()
    prefix_messages = (
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prefix},
    )
    full_messages = (
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    )
    return DisclosurePromptParts(
        full_messages=full_messages,
        prefix_messages=prefix_messages,
        suffix_text=suffix_text,
        cache_id=cache_id,
        prefix_token_hash=prefix_token_hash,
    )


@lru_cache(maxsize=4096)
def _build_disclosure_prompt_static(
    *,
    rendered_tree: str,
    fragment_fingerprint: str,
    candidate_codes: tuple[str, ...],
    compact_codes_enabled: bool,
    flat_list_mode: bool,
    top_k: int | None,
) -> tuple[str, str, str, str]:
    candidate_text = (
        f"Available options:\n{rendered_tree}"
        if flat_list_mode
        else f"<CANDIDATE_TREE>\n{rendered_tree}\n</CANDIDATE_TREE>"
    )
    candidate_block = f"# 候选列表\n{candidate_text}"
    system_prompt = _build_system_prompt(
        compact_codes_enabled=bool(compact_codes_enabled),
        flat_list_mode=bool(flat_list_mode),
        top_k=max(1, int(top_k if top_k is not None else (len(candidate_codes) or 1))),
        candidate_block=candidate_block,
    )
    if flat_list_mode:
        user_prefix = "User request:\n"
    else:
        user_prefix = "<USER_REQUEST>\n"
    prefix_payload = (
        f"{system_prompt}\n"
        f"{user_prefix}\n"
        f"{fragment_fingerprint}\n"
        f"{top_k or ''}\n"
        f"{int(compact_codes_enabled)}\n"
        f"{int(flat_list_mode)}"
    )
    prefix_token_hash = hashlib.sha256(prefix_payload.encode("utf-8")).hexdigest()
    cache_id = hashlib.sha256(
        (
            "progressive-disclosure-v1\n"
            f"{prefix_token_hash}\n"
            f"{'/'.join(str(code) for code in candidate_codes)}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    return system_prompt, user_prefix, prefix_token_hash, cache_id


def _normalize_query_content(text: str) -> str:
    lines = []
    for raw in str(text or "").splitlines():
        cleaned = _QUERY_FROM_PREFIX_RE.sub("", raw.strip())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _compact_flat_output_block() -> str:
    return (
        "- 只输出候选 JSON 对象中的两个大写字母 id，每行一个 id。\n"
        "- 不要输出 JSON、字段名、候选名称、解释、编号、冒号、标点或整个候选对象。\n"
        "- 如果想复制某个候选 JSON 对象，必须改为只输出该对象的两个大写字母 id。\n"
        "- id 是无语义句柄，只能根据候选名称和能力说明判断相关性。"
    )


def parse_selected_codes(*, fragment: ExposedFragment, output: str) -> List[SelectableResolution]:
    text = str(output or "").strip()
    if not text or text == "0":
        return []
    lines = _parse_selection_lines(text)
    if fragment.compact_codes_enabled:
        parsed_codes = _parse_compact_codes(fragment=fragment, lines=lines)
    else:
        parsed_codes = _parse_non_compact_codes(fragment=fragment, lines=lines)
    if _can_parse_joined_compact_codes(fragment=fragment, parsed_codes=parsed_codes, text=text):
        compact = text.strip()
        if compact != "0" and len(compact) % fragment.code_width == 0:
            compact_codes = [
                compact[index: index + fragment.code_width]
                for index in range(0, len(compact), fragment.code_width)
            ]
            if all(code in fragment.code_to_resolution for code in compact_codes):
                parsed_codes = compact_codes
    selected: List[SelectableResolution] = []
    seen: set[str] = set()
    for code in parsed_codes:
        resolution = fragment.code_to_resolution.get(code)
        if resolution is None and not fragment.compact_codes_enabled:
            resolution = _match_numeric_code(fragment=fragment, code=code)
        if resolution is None and not fragment.compact_codes_enabled:
            resolution = _match_label_code(fragment=fragment, code=code)
        if resolution is None:
            continue
        dedupe_key = resolution.canonical_id
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        selected.append(resolution)
    return selected


def _parse_selection_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        match = _SELECTION_LINE_RE.match(raw)
        if match:
            lines.append(match.group(1).strip())
    return lines


def _can_parse_joined_compact_codes(
    *,
    fragment: ExposedFragment,
    parsed_codes: Sequence[str],
    text: str,
) -> bool:
    return (
        not parsed_codes
        and fragment.compact_codes_enabled
        and fragment.code_width > 0
        and "\n" not in text
        and " " not in text
    )


def _parse_non_compact_codes(*, fragment: ExposedFragment, lines: Sequence[str]) -> List[str]:
    parsed: List[str] = []
    for line in lines:
        normalized = str(line or "").strip().strip("`").strip().strip("\"'")
        if not normalized:
            continue
        candidates = [normalized]
        if normalized.lower().startswith("candidate "):
            candidates.append(normalized[len("candidate "):].strip())
        if normalized.lower().startswith("name:"):
            candidates.append(normalized.split(":", 1)[1].split("|", 1)[0].strip())
        if ":" in normalized:
            candidates.append(normalized.split(":", 1)[0].strip())
        for candidate in candidates:
            matched_code = _match_non_compact_candidate_code(fragment=fragment, candidate=candidate)
            if matched_code:
                parsed.append(matched_code)
                break
        else:
            if re.fullmatch(r"\d+", normalized):
                parsed.append(normalized)
                continue
            listed_codes = _parse_non_compact_code_list(normalized, fragment)
            if listed_codes:
                parsed.extend(listed_codes)
    return parsed


def _match_non_compact_candidate_code(*, fragment: ExposedFragment, candidate: str) -> str:
    text = str(candidate or "").strip().strip("`").strip().strip("\"'")
    if not text:
        return ""
    if text in fragment.code_to_resolution:
        return text
    folded = text.casefold()
    code_matches = [code for code in fragment.code_to_resolution if str(code).casefold() == folded]
    if len(code_matches) == 1:
        return code_matches[0]
    label_matches = [
        resolution.code
        for resolution in fragment.code_to_resolution.values()
        if str(resolution.label or "").strip().casefold() == folded
    ]
    if len(label_matches) == 1:
        return label_matches[0]

    # Fallback: fuzzy match LLM output against display_name/label tokens
    hits = [r.code for r in fragment.code_to_resolution.values() if _tokens_overlap(r, folded)]
    if len(hits) == 1:
        return hits[0]

    return ""


def _tokens_overlap(resolution: SelectableResolution, folded: str) -> bool:
    for s in (resolution.display_name or "", resolution.label or ""):
        if any(t in folded for t in _tokenize_camel(s) if len(t) > 2):
            return True
    return False


def _tokenize_camel(s: str) -> List[str]:
    """Split a camelCase string into its constituent tokens."""
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", s)
    return [p.lower() for p in parts if p]


def _parse_non_compact_code_list(line: str, fragment: ExposedFragment) -> List[str]:
    text = str(line or "").strip()
    if not text or ":" in text:
        return []
    tokens = [
        token.strip().strip("`").strip().strip("\"'")
        for token in re.split(r"[\s,;|]+", text.strip("[](){}"))
        if token.strip()
    ]
    meaningful_tokens = [
        token
        for token in tokens
        if not re.fullmatch(r"\d+[\).]?", token)
    ]
    if not meaningful_tokens or len(meaningful_tokens) <= 1:
        return []
    resolved_codes = [
        _match_non_compact_candidate_code(fragment=fragment, candidate=token)
        for token in meaningful_tokens
    ]
    if all(resolved_codes):
        return resolved_codes
    return []


def _parse_compact_codes(*, fragment: ExposedFragment, lines: Sequence[str]) -> List[str]:
    parsed: List[str] = []
    codes = sorted((str(code) for code in fragment.code_to_resolution), key=len, reverse=True)
    for line in lines:
        raw_line = str(line or "").strip()
        json_codes = _parse_compact_json_id_fields(raw_line, fragment.code_to_resolution)
        if json_codes:
            parsed.extend(json_codes)
            continue
        normalized = raw_line.strip("`").strip().strip("\"'")
        if normalized.lower().startswith("candidate "):
            normalized = normalized[len("candidate "):].strip()
        if normalized.lower().startswith(("id:", "c:", "code:")):
            normalized = normalized.split(":", 1)[1].strip()
        if normalized in fragment.code_to_resolution:
            parsed.append(normalized)
            continue
        bracketed_prefix = _parse_compact_bracket_prefix(normalized, fragment.code_to_resolution)
        if bracketed_prefix:
            parsed.append(bracketed_prefix)
            continue
        handled_codes = _parse_compact_id_handles(normalized, fragment.code_to_resolution)
        if handled_codes:
            parsed.extend(handled_codes)
            continue
        listed_codes = _parse_compact_code_list(normalized, fragment.code_to_resolution)
        if listed_codes:
            parsed.extend(listed_codes)
            continue
        matched_prefix = _match_compact_code_prefix(normalized, codes)
        if matched_prefix:
            parsed.append(matched_prefix)
            continue
    return parsed


def _parse_compact_json_id_fields(line: str, code_to_resolution: Dict[str, SelectableResolution]) -> List[str]:
    parsed: List[str] = []
    for match in _COMPACT_JSON_ID_FIELD_RE.finditer(str(line or "")):
        code = str(match.group(1) or "").strip().strip("`").strip().strip("\"'")
        if code in code_to_resolution:
            parsed.append(code)
    return parsed


def _parse_compact_bracket_prefix(line: str, code_to_resolution: Dict[str, SelectableResolution]) -> str:
    match = _COMPACT_BRACKET_PREFIX_RE.match(str(line or ""))
    if not match:
        return ""
    code = str(match.group(1) or "").strip().strip("`").strip().strip("\"'")
    if code in code_to_resolution:
        return code
    return ""


def _parse_compact_id_handles(line: str, code_to_resolution: Dict[str, SelectableResolution]) -> List[str]:
    parsed: List[str] = []
    for match in _COMPACT_ID_HANDLE_RE.finditer(str(line or "")):
        code = str(match.group(1) or "").strip().strip("`").strip().strip("\"'")
        if code in code_to_resolution:
            parsed.append(code)
    return parsed


def _match_compact_code_prefix(line: str, codes: Sequence[str]) -> str:
    for code in codes:
        if not line.startswith(code):
            continue
        if len(line) == len(code):
            return code
        if line[len(code)] in {" ", "\t", "|", ":", "-", ",", ";", "]", "}", ")", "\"", "'"}:
            return code
    return ""


def _parse_compact_code_list(line: str, code_to_resolution: Dict[str, SelectableResolution]) -> List[str]:
    text = str(line or "").strip()
    if not text or "|" in text or ":" in text:
        return []
    tokens = [token for token in re.split(r"[\s,;]+", text.strip("[](){}")) if token]
    if not tokens:
        return []
    if all(token in code_to_resolution for token in tokens):
        return tokens
    return []


def _match_numeric_code(*, fragment: ExposedFragment, code: str) -> SelectableResolution | None:
    text = str(code or "").strip()
    if not text.isdigit():
        return None
    numeric_value = str(int(text))
    for candidate, resolution in fragment.code_to_resolution.items():
        candidate_text = str(candidate or "").strip()
        if not candidate_text.isdigit():
            continue
        if str(int(candidate_text)) == numeric_value:
            return resolution
    index = int(numeric_value) - 1
    ordered = list(fragment.code_to_resolution.values())
    if 0 <= index < len(ordered):
        return ordered[index]
    return None


def _match_label_code(*, fragment: ExposedFragment, code: str) -> SelectableResolution | None:
    text = str(code or "").strip()
    if not text:
        return None
    matches = [
        resolution
        for resolution in fragment.code_to_resolution.values()
        if str(resolution.label or "").strip() == text
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _expand_node(
    *,
    node: RetrieverNode,
    current_path: tuple[str, ...],
    remaining_depth: int,
    is_root: bool,
    config: DisclosureConfig,
    subtree_item_count: callable,
    selectable_entries: List[_SelectableEntry],
) -> ExposedNode:
    children_nodes: List[ExposedNode] = []
    for item in node.items:
        canonical_id = f"item::{item.payload or item.item_id}"
        selectable_entries.append(
            (
                canonical_id,
                item.label or item.item_id,
                item.description or "",
                item.payload or item.item_id,
                True,
                current_path,
                None,
                item,
            )
        )
        children_nodes.append(
            ExposedNode(
                canonical_id=canonical_id,
                label=item.label or item.item_id,
                description=item.description or "",
                is_selectable=True,
                selectable_canonical_id=item.payload or item.item_id,
            )
        )

    child_nodes = list(node.children)
    if _should_auto_expand_single_child(
        is_root=is_root,
        remaining_depth=remaining_depth,
        node=node,
        child_nodes=child_nodes,
    ):
        only_child = child_nodes[0]
        children_nodes.append(
            _expand_node(
                node=only_child,
                current_path=current_path + (only_child.node_id,),
                remaining_depth=remaining_depth - 1,
                is_root=False,
                config=config,
                subtree_item_count=subtree_item_count,
                selectable_entries=selectable_entries,
            )
        )
        return ExposedNode(
            canonical_id=f"node::{node.node_id}",
            label=node.label or node.node_id,
            description=node.description or "",
            is_selectable=False,
            children=tuple(children_nodes),
        )

    for child in child_nodes:
        child_path = current_path + (child.node_id,)
        if remaining_depth <= 0:
            children_nodes.append(
                _register_selectable_branch(
                    child=child,
                    child_path=child_path,
                    selectable_entries=selectable_entries,
                )
            )
            continue
        child_item_count = int(subtree_item_count(child))
        if child_item_count <= max(0, int(config.exposure_threshold)):
            children_nodes.append(
                _expand_node(
                    node=child,
                    current_path=child_path,
                    remaining_depth=remaining_depth - 1,
                    is_root=False,
                    config=config,
                    subtree_item_count=subtree_item_count,
                    selectable_entries=selectable_entries,
                )
            )
        else:
            children_nodes.append(
                _register_selectable_branch(
                    child=child,
                    child_path=child_path,
                    selectable_entries=selectable_entries,
                )
            )
    return ExposedNode(
        canonical_id=f"node::{node.node_id}",
        label=node.label or node.node_id,
        description=node.description or "",
        is_selectable=False,
        children=tuple(children_nodes),
    )


def _should_auto_expand_single_child(
    *,
    is_root: bool,
    remaining_depth: int,
    node: RetrieverNode,
    child_nodes: Sequence[RetrieverNode],
) -> bool:
    if is_root or node.items:
        return False
    return remaining_depth > 0 and len(child_nodes) == 1


def _build_flat_fragment_from_exposed_subtree(
    *,
    root_node: ExposedNode,
    selectable_entries: Sequence[_SelectableEntry],
    config: DisclosureConfig,
) -> ExposedFragment:
    selectable_entries = _sort_flat_entries(
        selectable_entries,
        stable_by_content=bool(config.compact_boundary_codes_enabled),
    )
    display_names = _build_boundary_names(selectable_entries)
    codes = _build_flat_compact_codes(
        selectable_entries=selectable_entries,
        config=config,
    )
    resolution_by_code: Dict[str, SelectableResolution] = {}
    resolution_by_canonical_id: Dict[str, SelectableResolution] = {}
    selectable_nodes: List[ExposedNode] = []
    for code, display_name, entry in zip(codes, display_names, selectable_entries):
        (
            canonical_id,
            label,
            description,
            selectable_canonical_id,
            is_terminal,
            selectable_path,
            node_ref,
            item_ref,
        ) = entry
        resolution = SelectableResolution(
            code=code,
            canonical_id=selectable_canonical_id,
            display_name=display_name,
            label=label,
            description=description,
            is_terminal=is_terminal,
            branch_path=selectable_path,
            score_key=code,
            node=node_ref,
            item=item_ref,
        )
        resolution_by_code[code] = resolution
        resolution_by_canonical_id[canonical_id] = resolution
        selectable_nodes.append(
            ExposedNode(
                canonical_id=canonical_id,
                label=label,
                description=description,
                is_selectable=True,
                selectable_canonical_id=selectable_canonical_id,
            )
        )
    flat_root_node = ExposedNode(
        canonical_id=root_node.canonical_id,
        label=root_node.label,
        description=root_node.description,
        is_selectable=False,
        children=tuple(selectable_nodes),
    )
    rendered_tree = _render_flat_list(
        selectable_nodes,
        resolution_by_canonical_id=resolution_by_canonical_id,
        compact_codes_enabled=bool(config.compact_boundary_codes_enabled),
    )
    candidate_codes = tuple(codes)
    return ExposedFragment(
        root=flat_root_node,
        rendered_tree=rendered_tree,
        code_to_resolution=resolution_by_code,
        selectable_nodes=tuple(selectable_nodes),
        candidate_codes=candidate_codes,
        fragment_fingerprint=_build_fragment_fingerprint(root_node=flat_root_node, candidate_codes=candidate_codes),
        code_width=_resolve_code_width(codes),
        compact_codes_enabled=bool(config.compact_boundary_codes_enabled),
        flat_list_mode=True,
    )


def _build_flat_compact_codes(
    *,
    selectable_entries: Sequence[_SelectableEntry],
    config: DisclosureConfig,
) -> List[str]:
    return _build_codes(
        selectable_entries,
        compact_codes_enabled=bool(config.compact_boundary_codes_enabled),
        compact_codebook=config.compact_boundary_codebook if config.compact_boundary_codes_enabled else (),
    )


def _sort_flat_entries(
    selectable_entries: Sequence[_SelectableEntry],
    *,
    stable_by_content: bool = False,
) -> List[_SelectableEntry]:
    del stable_by_content
    return sorted(
        selectable_entries,
        key=lambda entry: (
            str(entry[3] or entry[0] or entry[1] or "").casefold(),
            str(entry[1] or "").casefold(),
        ),
    )


def _register_selectable_branch(
    *,
    child: RetrieverNode,
    child_path: tuple[str, ...],
    selectable_entries: List[_SelectableEntry],
) -> ExposedNode:
    canonical_id = f"node::{child.node_id}"
    selectable_entries.append(
        (
            canonical_id,
            child.label or child.node_id,
            child.description or "",
            child.node_id,
            False,
            child_path,
            child,
            None,
        )
    )
    return ExposedNode(
        canonical_id=canonical_id,
        label=child.label or child.node_id,
        description=child.description or "",
        is_selectable=True,
        selectable_canonical_id=child.node_id,
    )


def _render_tree(
    node: ExposedNode,
    *,
    resolution_by_canonical_id: Dict[str, SelectableResolution],
    compact_codes_enabled: bool,
    depth: int = 0,
) -> str:
    lines: List[str] = []
    indent = "  " * depth
    if node.is_selectable:
        resolution = resolution_by_canonical_id[node.canonical_id]
        if compact_codes_enabled:
            identifier = f"{resolution.display_name} [id: {resolution.code}]"
        else:
            identifier = f"Candidate {resolution.display_name}"
    else:
        identifier = f"Category {node.label}"
    description = str(node.description or "")
    if node.is_selectable:
        description = _sanitize_candidate_description(description)
    if description:
        lines.append(f"{indent}- {identifier}: {description}")
    else:
        lines.append(f"{indent}- {identifier}")
    for child in node.children:
        lines.append(
            _render_tree(
                child,
                resolution_by_canonical_id=resolution_by_canonical_id,
                compact_codes_enabled=compact_codes_enabled,
                depth=depth + 1,
            )
        )
    return "\n".join(lines)


def _render_flat_list(
    selectable_nodes: Sequence[ExposedNode],
    *,
    resolution_by_canonical_id: Dict[str, SelectableResolution],
    compact_codes_enabled: bool,
) -> str:
    if compact_codes_enabled:
        candidates: List[Dict[str, str]] = []
        for node in selectable_nodes:
            resolution = resolution_by_canonical_id[node.canonical_id]
            description = _sanitize_candidate_description(
                str(node.description or ""),
                collapse_newlines=True,
            )
            description = _maybe_shorten_flat_compact_description(description)
            candidates.append(
                _ordered_flat_compact_item(
                    {
                        "category": _format_branch_category(resolution.branch_path),
                        "name": _format_flat_compact_name(resolution),
                        "raw_name": _format_flat_compact_raw_name(resolution),
                        "description": description,
                        "id": str(resolution.code),
                    }
                )
            )
        return json.dumps(candidates, ensure_ascii=False, indent=2)

    lines: List[str] = []
    for node in selectable_nodes:
        resolution = resolution_by_canonical_id[node.canonical_id]
        description = _sanitize_candidate_description(
            str(node.description or ""),
            collapse_newlines=False,
        )
        line = f"- {resolution.display_name}"
        if description:
            line = f"{line}: {description}"
        lines.append(line)
    return "\n".join(lines)


def _ordered_flat_compact_item(fields: Dict[str, str]) -> Dict[str, str]:
    order = _flat_compact_field_order()
    return {field: fields[field] for field in order}


def _flat_compact_field_order() -> tuple[str, ...]:
    return _FLAT_COMPACT_FIELD_ORDER_DEFAULT


def _format_flat_compact_name(resolution: SelectableResolution) -> str:
    if not resolution.is_terminal:
        return str(resolution.display_name or "").strip()
    label = re.sub(r"\s+", " ", str(resolution.label or "").strip())
    if label:
        return label
    return str(resolution.display_name or "").strip()


def _format_flat_compact_raw_name(resolution: SelectableResolution) -> str:
    if resolution.is_terminal and resolution.item is not None:
        worker_id = str(getattr(resolution.item, "worker_id", "") or "").strip()
        if worker_id:
            return worker_id
    return str(resolution.display_name or "").strip()


def _sanitize_candidate_description(description: str, *, collapse_newlines: bool = False) -> str:
    text = str(description or "").strip()
    if not text:
        return ""
    text = _REPRESENTATIVE_DESCENDANTS_RE.sub("", text).strip()
    if collapse_newlines:
        text = re.sub(r"\s*\n+\s*", " ", text).strip()
    return text


def _maybe_shorten_flat_compact_description(text: str) -> str:
    max_chars = _FLAT_COMPACT_DESCRIPTION_MAX_CHARS_DEFAULT
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    boundary = max(
        cleaned.rfind("。", 0, max_chars),
        cleaned.rfind(".", 0, max_chars),
        cleaned.rfind("；", 0, max_chars),
        cleaned.rfind(";", 0, max_chars),
    )
    if boundary >= max(40, max_chars // 2):
        return cleaned[:boundary + 1].strip()
    return cleaned[:max_chars].rstrip(" ,，;；。.") + "..."


def _format_branch_category(branch_path: Sequence[str]) -> str:
    category_labels = _fixed_category_labels()
    labels: List[str] = []
    for raw_part in branch_path:
        part = str(raw_part or "").strip()
        if not part:
            continue
        label = part.rsplit(".", 1)[-1]
        if label.upper() == "ROOT":
            continue
        label = category_labels.get(label, label)
        if label and (not labels or labels[-1] != label):
            labels.append(label)
    if not labels:
        return ""
    return " > ".join(labels[-3:])


@lru_cache(maxsize=1)
def _fixed_category_labels() -> Dict[str, str]:
    try:
        from indexing.tree.schema import FIXED_ROOT_CATEGORIES
    except Exception:
        return {}

    labels: Dict[str, str] = {}

    def visit(category_id: str, spec: object) -> None:
        if not isinstance(spec, dict):
            return
        name = str(spec.get("name") or "").strip()
        if name:
            labels[str(category_id)] = name
            pascal = to_pascal_case(str(category_id))
            if pascal:
                labels[pascal] = name
        children = spec.get("children")
        if isinstance(children, dict):
            for child_id, child_spec in children.items():
                visit(str(child_id), child_spec)

    for root_id, root_spec in FIXED_ROOT_CATEGORIES.items():
        visit(str(root_id), root_spec)
    return labels


def _iter_selectable_nodes(node: ExposedNode) -> Iterable[ExposedNode]:
    if node.is_selectable:
        yield node
    for child in node.children:
        yield from _iter_selectable_nodes(child)


def _build_codes(
    selectable_entries: Sequence[_SelectableEntry],
    *,
    compact_codes_enabled: bool,
    compact_codebook: Sequence[str] = (),
) -> List[str]:
    count = len(selectable_entries)
    if count <= 0:
        return []
    if not compact_codes_enabled:
        return _build_boundary_names(selectable_entries)
    normalized_codebook = _normalize_codebook(compact_codebook)
    if normalized_codebook:
        if len(normalized_codebook) < count:
            raise ValueError(
                "compact codebook provides "
                f"{len(normalized_codebook)} codes, but {count} selectable nodes were exposed"
            )
        return list(normalized_codebook[:count])
    return [_encode_boundary_code(index, width=0) for index in range(count)]


def _build_boundary_names(
    selectable_entries: Sequence[_SelectableEntry],
) -> List[str]:
    base_names: List[str] = []
    for entry in selectable_entries:
        selectable_canonical_id = str(entry[3] or "").strip()
        terminal = selectable_canonical_id.split(".")[-1] if selectable_canonical_id else ""
        base_name = (
            to_pascal_case(terminal)
            or to_pascal_case(selectable_canonical_id.replace(".", "-"))
            or selectable_canonical_id
            or str(entry[1] or "").strip()
        )
        base_names.append(base_name)
    grouped: Dict[str, List[int]] = {}
    for index, name in enumerate(base_names):
        grouped.setdefault(name, []).append(index)

    identifiers = list(base_names)
    for name, indices in grouped.items():
        if len(indices) <= 1:
            continue
        for index in indices:
            selectable_path = tuple(
                str(part or "").strip()
                for part in selectable_entries[index][5]
                if str(part or "").strip()
            )
            path_parts = [to_pascal_case(part) or part for part in selectable_path[1:] if part]
            if path_parts:
                identifiers[index] = "/".join([*path_parts, name])
            else:
                identifiers[index] = f"{name}__{index + 1}"
    return identifiers


def _normalize_codebook(codebook: Sequence[str]) -> tuple[str, ...]:
    normalized: List[str] = []
    seen: set[str] = set()
    for raw_code in codebook:
        code = str(raw_code or "").strip()
        if not code:
            raise ValueError("compact codebook entries must be non-empty strings")
        if code == "0":
            raise ValueError("compact codebook cannot include reserved abstain code '0'")
        if any(character.isspace() for character in code):
            raise ValueError(f"compact codebook entry {code!r} cannot contain whitespace")
        if code in seen:
            raise ValueError(f"compact codebook entry {code!r} is duplicated")
        seen.add(code)
        normalized.append(code)
    return tuple(normalized)


def _resolve_code_width(codes: Sequence[str]) -> int:
    if not codes:
        return 1
    widths = {len(str(code)) for code in codes}
    if len(widths) == 1:
        return next(iter(widths))
    return 0


def _build_fragment_fingerprint(*, root_node: ExposedNode, candidate_codes: Sequence[str]) -> str:
    payload = f"{_serialize_exposed_node(root_node)}|{'/'.join(str(code) for code in candidate_codes)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _serialize_exposed_node(node: ExposedNode) -> str:
    children = ",".join(_serialize_exposed_node(child) for child in node.children)
    return f"{node.canonical_id}|{node.label}|{int(node.is_selectable)}|{children}"


def _encode_boundary_code(index: int, *, width: int) -> str:
    del width
    return str(max(0, int(index)) + 1)


__all__ = [
    "DisclosureConfig",
    "DisclosurePromptParts",
    "ExposedFragment",
    "ExposedNode",
    "SelectableResolution",
    "build_disclosure_messages",
    "build_disclosure_prompt_parts",
    "build_exposed_fragment",
    "parse_selected_codes",
]

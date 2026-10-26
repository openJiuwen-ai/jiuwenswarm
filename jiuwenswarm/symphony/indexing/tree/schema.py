"""Core schema and config objects for Demo's capability-tree indexing flow."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Union


class SkillStatus(str, Enum):
    ACTIVE = "active"
    DORMANT = "dormant"
    PINNED = "pinned"

    @classmethod
    def default(cls) -> "SkillStatus":
        return cls.ACTIVE


SKILL_DESCRIPTION_MAX_LENGTH = 150

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "data" / "skills"
DEFAULT_TREE_OUTPUT_PATH = PROJECT_ROOT / "data" / "capability_trees" / "tree.yaml"

BRANCHING_FACTOR = 8
MAX_DEPTH = 6

TREE_BUILD_MAX_WORKERS = 1
TREE_BUILD_NUM_RETRIES = 2
TREE_BUILD_TIMEOUT = 180.0
TREE_BUILD_POSTPROCESS_ENABLED = True
TREE_BUILD_POSTPROCESS_MAX_PASSES = 1
TREE_BUILD_POSTPROCESS_MIN_SKILLS = 6
TREE_BUILD_EQUIV_GROUPING_ENABLED = True

MAX_SKILLS_PER_NODE_MULTIPLIER = 1.5
EXPAND_THRESHOLD_MULTIPLIER = 0.7
EARLY_STOP_MULTIPLIER = 1.7
LAZY_SPLIT_MULTIPLIER = 1.3
CLASSIFICATION_BATCH_MULTIPLIER = 6
STRUCTURE_SAMPLE_MULTIPLIER = 12
FALLBACK_CATEGORY_ID_HASH_LENGTH = 12

FIXED_ROOT_CATEGORIES = {
    "office-docs": {
        "name": "办公文档",
        "description": "办公文件的生成、编辑、转换、提取、排版和结构化处理。",
        "select_when": "用户要处理 Word、PDF、PPT、Excel、Markdown、邮件、会议纪要或办公流程产物。",
        "dont_select_when": "用户只是写普通文章/营销文案、管理个人知识库/笔记/任务、联网查资料、开发网页、生成图片视频或查询金融行情。",
    },
    "writing-content": {
        "name": "写作内容",
        "description": "文章、故事、新闻、营销文案、内容策划、文本润色和写作风格处理。",
        "select_when": "用户要写、改写、润色、策划或生成面向读者的文字内容，而不是操作具体办公文件。",
        "dont_select_when": "用户明确要输出 Word/PDF/PPT/Excel 文件、生成图片视频、联网调研、管理知识库或构建智能体/skill。",
    },
    "search-research": {
        "name": "搜索研究",
        "description": "网页搜索、内容抓取、新闻、论文、深度调研和知识库检索。",
        "select_when": "用户要查找已有信息、联网搜索、读取网页、研究主题、检索论文、查询新闻或使用知识库/笔记/记忆。",
        "dont_select_when": "用户已经给定资料且只要求生成具体文件、图片、视频、表格或创作文案。",
    },
    "media-creative": {
        "name": "图片音视频",
        "description": "图片理解、图像生成、视觉设计、视频动画、语音音乐、漫画和绘本视觉产物。",
        "select_when": "用户输入或输出涉及图片、截图、照片、OCR、音频、语音、音乐、视频、动画、漫画或视觉设计。",
        "dont_select_when": "用户主要是在写文本、做办公文件、网页开发、旅行查询或金融分析。",
    },
    "life-services": {
        "name": "生活服务",
        "description": "出行旅行、地图天气、健康教育、本地优惠、餐饮和日常生活服务。",
        "select_when": "用户请求与真实生活行动、位置、旅行、天气、健康、学习、优惠券、餐饮或本地服务有关。",
        "dont_select_when": "请求主要是内容创作、办公文件、网页开发、系统配置或金融投资。",
    },
    "product-dev": {
        "name": "开发产品",
        "description": "网页应用、产品需求、UX/UI、部署数据库、代码测试和工程质量。",
        "select_when": "用户要设计产品、写 PRD、做网页/应用、部署网站、接数据库、优化前端或测试代码。",
        "dont_select_when": "用户只是写营销文案、生成办公文件、做图片视频、查询资料或配置智能体。",
    },
    "finance-business": {
        "name": "金融商业",
        "description": "股票行情、投资分析、金融新闻、量化交易、模拟交易和企业信息查询。",
        "select_when": "用户询问股票、基金、指数、黄金、行情、投资、金融新闻、企业工商或商业数据。",
        "dont_select_when": "用户只是做普通市场调研、表格分析、营销文案或产品竞品分析。",
    },
    "system-tools": {
        "name": "系统工具",
        "description": "手机设备、文件云盘、智能体/技能管理、任务自动化、安全合规和连接配置。",
        "select_when": "用户请求操作设备系统、管理文件云盘、配置渠道、创建技能/智能体、切换人格、管理任务或做安全校验。",
        "dont_select_when": "用户主要要完成一个具体业务任务，如写文档、查资料、做图、出行或金融分析。",
    },
}


def _slug_term(value: str, fallback: str = "category") -> str:
    source = str(value or "")
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", source).replace("_", "-").lower()
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-") or fallback
    if normalized == fallback and source.strip():
        # Non-ASCII names may not produce a readable slug; append a stable short hash.
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:FALLBACK_CATEGORY_ID_HASH_LENGTH]
        normalized = f"{fallback}-{digest}"
    return normalized if normalized[0].isalpha() else f"n-{normalized}"


def normalize_root_categories(raw_categories) -> Optional[dict]:
    if raw_categories in (None, [], ()):
        return None
    if not isinstance(raw_categories, list):
        raise ValueError("TREE_BUILDER_ROOT_CATEGORIES must be a list.")

    def normalize_entries(entries: list, *, label_path: str) -> dict:
        items: list[tuple[str, dict]] = []
        for entry in entries:
            if isinstance(entry, str):
                label = entry.strip()
                if not label:
                    continue
                category_id = _slug_term(label)
                payload: dict[str, object] = {
                    "name": label,
                    "description": f"Skills related to {label.lower()}.",
                }
            elif isinstance(entry, dict):
                label = str(entry.get("name") or entry.get("id") or "").strip()
                if not label:
                    raise ValueError(f"Each {label_path} item must include 'name' or 'id'.")
                category_id = _slug_term(str(entry.get("id") or label))
                payload = {
                    "name": label,
                    "description": str(entry.get("description") or f"Skills related to {label.lower()}.").strip(),
                }
                select_when = str(entry.get("select_when") or "").strip()
                dont_select_when = str(entry.get("dont_select_when") or "").strip()
                if select_when:
                    payload["select_when"] = select_when
                if dont_select_when:
                    payload["dont_select_when"] = dont_select_when
                raw_children = entry.get("children")
                if raw_children in (None, [], ()):
                    raw_children = None
                if raw_children is not None:
                    if isinstance(raw_children, dict):
                        child_entries = [
                            {"id": child_id, **dict(child_payload)}
                            for child_id, child_payload in raw_children.items()
                            if isinstance(child_payload, dict)
                        ]
                        if len(child_entries) != len(raw_children):
                            raise ValueError(f"{label_path} item '{category_id}' children dict values must be objects.")
                    elif isinstance(raw_children, list):
                        child_entries = raw_children
                    else:
                        raise ValueError(f"{label_path} item '{category_id}' children must be a list or dict.")
                    children = normalize_entries(child_entries, label_path=f"{label_path}.{category_id}.children")
                    if children:
                        payload["children"] = children
            else:
                raise ValueError(f"{label_path} items must be strings or dicts.")
            items.append((category_id, payload))

        normalized: dict[str, dict] = {}
        for category_id, payload in items:
            if category_id in normalized:
                raise ValueError(f"Duplicate {label_path} id: {category_id}")
            normalized[category_id] = payload
        return normalized

    normalized = normalize_entries(raw_categories, label_path="TREE_BUILDER_ROOT_CATEGORIES")
    return normalized or None


@dataclass(frozen=True)
class TreeBuildConfig:
    max_workers: int = TREE_BUILD_MAX_WORKERS
    num_retries: int = TREE_BUILD_NUM_RETRIES
    timeout: float = TREE_BUILD_TIMEOUT
    postprocess_enabled: bool = TREE_BUILD_POSTPROCESS_ENABLED
    postprocess_max_passes: int = TREE_BUILD_POSTPROCESS_MAX_PASSES
    postprocess_min_skills: int = TREE_BUILD_POSTPROCESS_MIN_SKILLS
    equiv_grouping_enabled: bool = TREE_BUILD_EQUIV_GROUPING_ENABLED
    discovery_seed: int = 42
    classify_batch_cap: int = 20


@dataclass(frozen=True)
class TreeManagerConfig:
    branching_factor: int = BRANCHING_FACTOR
    max_depth: int = MAX_DEPTH
    root_categories: Optional[dict] = None
    build: TreeBuildConfig = field(default_factory=TreeBuildConfig)


@dataclass
class DynamicTreeConfig:
    branching_factor: int = BRANCHING_FACTOR
    max_depth: int = MAX_DEPTH
    root_categories: Optional[dict] = None
    rebalance_interval: int = 50

    def _scaled(self, multiplier: float, seed: Optional[int] = None) -> int:
        anchor = self.branching_factor if seed is None else seed
        return int(anchor * multiplier)

    def _derived_value(self, key: str) -> int:
        if key == "max_skills_per_node":
            return self._scaled(MAX_SKILLS_PER_NODE_MULTIPLIER)
        if key == "expand_threshold":
            return self._scaled(EXPAND_THRESHOLD_MULTIPLIER)
        if key == "early_stop_skill_count":
            return self._scaled(EARLY_STOP_MULTIPLIER)
        if key == "lazy_split_threshold":
            return self._scaled(LAZY_SPLIT_MULTIPLIER, self.max_skills_per_node)
        if key == "classification_batch_size":
            return self._scaled(CLASSIFICATION_BATCH_MULTIPLIER)
        if key == "structure_sample_size":
            return self._scaled(STRUCTURE_SAMPLE_MULTIPLIER)
        raise KeyError(key)

    @property
    def max_skills_per_node(self) -> int:
        return self._derived_value("max_skills_per_node")

    @property
    def expand_threshold(self) -> int:
        return self._derived_value("expand_threshold")

    @property
    def early_stop_skill_count(self) -> int:
        return self._derived_value("early_stop_skill_count")

    @property
    def lazy_split_threshold(self) -> int:
        return self._derived_value("lazy_split_threshold")

    @property
    def classification_batch_size(self) -> int:
        return self._derived_value("classification_batch_size")

    @property
    def structure_sample_size(self) -> int:
        return self._derived_value("structure_sample_size")


class Skill:
    def __init__(
        self,
        *,
        item_id: str = "",
        name: str,
        description: str = "",
        path: str = "",
        skill_path: str = "",
        content: str = "",
        selection_reason: str = "",
        select_when: str = "",
        dont_select_when: str = "",
        source_description: str = "",
        github_url: str = "",
        stars: int = 0,
        is_official: bool = False,
        author: str = "",
        status: SkillStatus = SkillStatus.ACTIVE,
        installs_count: int = 0,
        pinned_at: Optional[str] = None,
        last_used: Optional[str] = None,
        **extra: object,
    ) -> None:
        legacy_id = str(extra.pop("id", "") or "").strip()
        if extra:
            unknown = ", ".join(sorted(str(key) for key in extra))
            raise TypeError(f"Unexpected Skill arguments: {unknown}")
        self.id = item_id or legacy_id
        self.name = name
        self.description = description
        self.path = path
        self.skill_path = skill_path
        self.content = content
        self.selection_reason = selection_reason
        self.select_when = select_when
        self.dont_select_when = dont_select_when
        self.source_description = source_description
        self.github_url = github_url
        self.stars = stars
        self.is_official = is_official
        self.author = author
        self.status = status
        self.installs_count = installs_count
        self.pinned_at = pinned_at
        self.last_used = last_used

    def to_dict(self, include_content: bool = True) -> dict:
        keys = (
            "id",
            "name",
            "description",
            "skill_path",
            "select_when",
            "dont_select_when",
            "source_description",
            "github_url",
            "stars",
            "is_official",
            "author",
        )
        values = (
            self.id,
            self.name,
            self.description,
            self.skill_path,
            self.select_when,
            self.dont_select_when,
            self.source_description,
            self.github_url,
            self.stars,
            self.is_official,
            self.author,
        )
        payload = dict(zip(keys, values))
        if include_content:
            payload["content"] = self.content
        return payload


class TreeNode:
    def __init__(
        self,
        *,
        node_id: str = "",
        name: str,
        description: str = "",
        select_when: str = "",
        dont_select_when: str = "",
        children: Optional[list["TreeNode"]] = None,
        skills: Optional[list[Skill]] = None,
        depth: int = 0,
        parent_id: Optional[str] = None,
        pending_split: bool = False,
        **extra: object,
    ) -> None:
        legacy_id = str(extra.pop("id", "") or "").strip()
        if extra:
            unknown = ", ".join(sorted(str(key) for key in extra))
            raise TypeError(f"Unexpected TreeNode arguments: {unknown}")
        self.id = node_id or legacy_id
        self.name = name
        self.description = description
        self.select_when = select_when
        self.dont_select_when = dont_select_when
        self.children = list(children or [])
        self.skills = list(skills or [])
        self.depth = depth
        self.parent_id = parent_id
        self.pending_split = pending_split

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def is_intermediate(self) -> bool:
        return not self.is_leaf

    def count_all_skills(self) -> int:
        total = 0
        stack = [self]
        while stack:
            current = stack.pop()
            if current.children:
                stack.extend(current.children)
            else:
                total += len(current.skills)
        return total

    def collect_all_skills(self) -> list[Skill]:
        gathered: list[Skill] = []
        agenda = [self]
        while agenda:
            current = agenda.pop()
            if current.children:
                agenda.extend(current.children)
                continue
            if current.skills:
                gathered.extend(current.skills)
        return gathered

    def get_leaf_nodes(self) -> list["TreeNode"]:
        result: list[TreeNode] = []
        frontier = [self]
        while frontier:
            current = frontier.pop()
            if current.children:
                frontier.extend(current.children)
            else:
                result.append(current)
        return result

    def get_pending_split_nodes(self) -> list["TreeNode"]:
        flagged: list[TreeNode] = []
        queue = [self]
        while queue:
            current = queue.pop()
            if current.pending_split:
                flagged.append(current)
            queue.extend(current.children)
        return flagged

    def clear_pending_splits(self) -> None:
        for current in [self, *self._walk_descendants()]:
            current.pending_split = False

    def get_path(self) -> str:
        return self.id

    def to_dict(self) -> dict:
        payload: dict = {}
        payload.update(id=self.id, name=self.name, description=self.description)
        if self.select_when:
            payload["select_when"] = self.select_when
        if self.dont_select_when:
            payload["dont_select_when"] = self.dont_select_when
        child_items = list(self.children)
        skill_items = list(self.skills)
        if child_items:
            serialized_children: list[dict] = []
            for child in child_items:
                serialized_children.append(child.to_dict())
            payload["children"] = serialized_children
        if skill_items:
            payload["skills"] = [item.to_dict() for item in skill_items]
        return payload

    def _walk_descendants(self):
        stack = list(self.children)
        while stack:
            current = stack.pop()
            yield current
            stack.extend(current.children)

    @classmethod
    def from_recursive_tree(
        cls,
        tree_dict: dict,
        depth: int = 0,
        parent_id: Optional[str] = None,
    ) -> "TreeNode":
        node = cls(
            id=tree_dict.get("id", "unknown"),
            name=tree_dict.get("name", ""),
            description=tree_dict.get("description", ""),
            select_when=tree_dict.get("select_when", ""),
            dont_select_when=tree_dict.get("dont_select_when", ""),
            depth=depth,
            parent_id=parent_id,
        )
        for child_payload in list(tree_dict.get("children", []) or []):
            node.children.append(cls.from_recursive_tree(child_payload, depth + 1, node.id))
        for skill_payload in list(tree_dict.get("skills", []) or []):
            node.skills.append(
                Skill(
                    id=skill_payload.get("id", ""),
                    name=skill_payload.get("name", ""),
                    description=skill_payload.get("description", ""),
                    path=node.id,
                    skill_path=skill_payload.get("skill_path", ""),
                    content=skill_payload.get("content", ""),
                    select_when=skill_payload.get("select_when", ""),
                    dont_select_when=skill_payload.get("dont_select_when", ""),
                    source_description=skill_payload.get("source_description", ""),
                    github_url=skill_payload.get("github_url", ""),
                    stars=skill_payload.get("stars", 0),
                    is_official=skill_payload.get("is_official", False),
                    author=skill_payload.get("author", ""),
                )
            )
        return node

    @classmethod
    def from_capability_tree(cls, tree_dict: dict) -> "TreeNode":
        root = cls(id="root", name="Root", description="Skill Tree Root")
        domains = tree_dict.get("domains", {}) or {}
        for domain_id, domain_payload in domains.items():
            domain_node = cls(
                id=domain_id,
                name=domain_payload.get("name", domain_id),
                description=domain_payload.get("description", ""),
                select_when=domain_payload.get("select_when", ""),
                dont_select_when=domain_payload.get("dont_select_when", ""),
                depth=1,
                parent_id=root.id,
            )
            for type_id, type_payload in (domain_payload.get("types", {}) or {}).items():
                type_node = cls(
                    id=type_id,
                    name=type_payload.get("name", type_id),
                    description=type_payload.get("description", ""),
                    select_when=type_payload.get("select_when", ""),
                    dont_select_when=type_payload.get("dont_select_when", ""),
                    depth=2,
                    parent_id=domain_id,
                )
                for skill_payload in list(type_payload.get("skills", []) or []):
                    type_node.skills.append(
                        Skill(
                            id=skill_payload.get("id", ""),
                            name=skill_payload.get("name", ""),
                            description=skill_payload.get("description", ""),
                            path="/".join([domain_id, type_id]),
                            select_when=skill_payload.get("select_when", ""),
                            dont_select_when=skill_payload.get("dont_select_when", ""),
                            source_description=skill_payload.get("source_description", ""),
                            github_url=skill_payload.get("github_url", ""),
                            stars=skill_payload.get("stars", 0),
                            is_official=skill_payload.get("is_official", False),
                            author=skill_payload.get("author", ""),
                        )
                    )
                domain_node.children.append(type_node)
            root.children.append(domain_node)
        return root


class SearchStep:
    def __init__(
        self,
        *,
        level: int,
        node_id: str,
        options: list[str],
        selected: list[str],
        is_parallel: bool = False,
    ) -> None:
        self.level = level
        self.node_id = node_id
        self.options = list(options)
        self.selected = list(selected)
        self.is_parallel = is_parallel


class MultiLevelSearchResult:
    def __init__(
        self,
        *,
        query: str,
        selected_skills: list[dict],
        steps: Optional[list[SearchStep]] = None,
        llm_calls: int = 0,
        parallel_rounds: int = 0,
        early_stops: int = 0,
    ) -> None:
        self.query = query
        self.selected_skills = list(selected_skills)
        self.steps = list(steps or [])
        self.llm_calls = llm_calls
        self.parallel_rounds = parallel_rounds
        self.early_stops = early_stops


def parse_json_from_response(response: str, default: Union[dict, list, None] = None) -> Union[dict, list]:
    fallback = {} if default is None else default
    if not isinstance(response, str):
        return fallback

    for candidate in _json_candidates(response):
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, (dict, list)):
            return decoded
    return fallback


def _json_candidates(response: str) -> list[str]:
    raw = response.strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    fenced = _strip_wrapping_fence(raw)
    if fenced and fenced != raw:
        candidates.insert(0, fenced)
    candidates.extend(_extract_balanced_fragments(response))
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique.append(cleaned)
    return unique


def _strip_wrapping_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    body = text.splitlines()
    if body and body[0].startswith("```"):
        body = body[1:]
    if body and body[-1].strip() == "```":
        body = body[:-1]
    return "\n".join(body).strip()


def _extract_balanced_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    for opening, closing in (("{", "}"), ("[", "]")):
        index = text.find(opening)
        while index >= 0:
            fragment = _slice_balanced(text, index, opening, closing)
            if fragment:
                fragments.append(fragment)
                break
            index = text.find(opening, index + 1)
    return fragments


def _slice_balanced(text: str, start: int, opening: str, closing: str) -> Optional[str]:
    level = 0
    inside_string = False
    escaped = False
    for cursor, char in enumerate(text[start:], start=start):
        if escaped:
            escaped = False
            continue
        if char == "\\" and inside_string:
            escaped = True
            continue
        if char == '"':
            inside_string = not inside_string
            continue
        if inside_string:
            continue
        if char == opening:
            level += 1
        elif char == closing:
            level -= 1
            if level == 0:
                return text[start:cursor + 1]
    return None

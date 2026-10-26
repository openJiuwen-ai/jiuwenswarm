# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""swarm-design-docs 共享库：扫描 / 解析 design.md 的 frontmatter，并集中定义 Schema 常量。

被 validate_frontmatter.py 与 generate_index.py 复用。无副作用、仅依赖标准库 + PyYAML。
All document paths are relative to the design asset root, overridable with --root.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------- #
# Schema 常量（与 plan / CONTRIBUTING.md 的 Frontmatter Schema 一一对应）
# --------------------------------------------------------------------------- #

STAGES: tuple[str, ...] = ("proposals", "active", "archive")

# status 枚举 ↔ stage 映射（顺序即索引内的展示顺序）
STATUS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "proposals": ("draft", "in-review", "changes-requested", "approved", "rejected"),
    "active": ("in-development", "on-hold"),
    "archive": ("shipped", "superseded", "abandoned"),
}

ALL_STATUSES: tuple[str, ...] = tuple(
    s for stage in STAGES for s in STATUS_BY_STAGE[stage]
)

# 顶部必填字段（implementation / *-date 等为「条件必填」，在校验器内按规则单独判断）
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "slug",
    "status",
    "stage",
    "owner",
    "created",
    "updated",
    "domain",
)

# draft 之外的所有状态都已进入评审，要求 reviewers 非空
REVIEWERS_REQUIRED_STATUSES: frozenset[str] = frozenset(
    s for s in ALL_STATUSES if s != "draft"
)

DOMAINS: tuple[str, ...] = (
    "memory",
    "scheduler",
    "messaging",
    "protocol",
    "harness",
    "security",
    "observability",
    "core",
    "tooling",
)

IMPLEMENTATION_STATUSES: tuple[str, ...] = (
    "not-started",
    "in-progress",
    "shipped",
    "partially-shipped",
    "abandoned",
)

ID_RE = re.compile(r"^(?:SDD-\d{4}|D-\d{8}-\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*)$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FOLDER_RE = re.compile(r"^(SDD-\d{4}|D-\d{8}-\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)$")

REGISTRY_FILE = "REGISTRY.md"
INDEX_FILE = "INDEX.md"
DESIGN_FILE = "design.md"

# 各 stage 目录对应的文档文件名
DOC_FILE_BY_STAGE: dict[str, str] = {
    "proposals": DESIGN_FILE,
    "active": DESIGN_FILE,
    "archive": DESIGN_FILE,
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class DocRecord:
    """Store the scan result for a single design document."""

    path: Path  # 文档绝对路径
    rel_path: str  # 相对仓库根（用于报告 / 索引链接），始终用 "/" 分隔
    stage_dir: str  # 所在顶层目录名（proposals/active/archive）
    folder: str  # Stable legacy or timestamp-based design folder name.
    folder_id: str | None  # New IDs include the topic to distinguish same-minute designs.
    folder_slug: str | None  # 由文件夹名解析出的 slug；不合规则为 None
    doc_file: str  # Design document filename.
    body: str = ""  # frontmatter 之后的正文
    fm: dict[str, Any] = field(default_factory=dict)  # 解析出的 frontmatter
    parse_error: str | None = None  # 解析失败原因；非 None 时 fm 不可信


# --------------------------------------------------------------------------- #
# 解析 / 扫描
# --------------------------------------------------------------------------- #


def repo_root() -> Path:
    """Return the integrated design asset root independently of the working directory."""
    return Path(__file__).resolve().parents[3] / "agent-docs" / "designs"


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """拆出 (frontmatter 原文, 正文)。无 frontmatter 时返回 (None, 全文)。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    """解析 frontmatter 文本。

    返回 (fm_dict, body, error)。error 非 None 时 fm_dict 为空。
    """
    raw, body = split_frontmatter(text)
    if raw is None:
        return {}, body, "缺少 YAML frontmatter（文件需以 '---' 开头）"
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:  # noqa: BLE001 - 转成可读消息
        return {}, body, f"frontmatter YAML 解析失败：{exc}"
    if data is None:
        return {}, body, "frontmatter 为空"
    if not isinstance(data, dict):
        return {}, body, "frontmatter 顶层必须是键值映射"
    return data, body, None


def iter_docs(root: Path | None = None, stage: str | None = None) -> list[DocRecord]:
    """Read legacy and D-prefixed designs from the supported lifecycle stages.

    A supplied stage restricts discovery to that stage. Each folder contains
    design.md, and results are ordered by stage and folder name.
    """
    root = root or repo_root()
    stages = (stage,) if stage else STAGES
    records: list[DocRecord] = []
    for stage_dir in stages:
        base = root / stage_dir
        if not base.is_dir():
            continue
        doc_file = DOC_FILE_BY_STAGE.get(stage_dir, DESIGN_FILE)
        for folder_path in sorted(p for p in base.iterdir() if p.is_dir()):
            doc_path = folder_path / doc_file
            if not doc_path.is_file():
                # 没有对应文档文件的文件夹由校验器单独报告，这里跳过非候选目录
                if not folder_path.name.startswith(("SDD-", "D-")):
                    continue
            folder = folder_path.name
            fm_match = FOLDER_RE.match(folder)
            folder_id = None
            if fm_match:
                folder_id = folder if folder.startswith("D-") else fm_match.group(1)
            folder_slug = fm_match.group(2) if fm_match else None
            rec = DocRecord(
                path=doc_path,
                rel_path=str(doc_path.relative_to(root)).replace("\\", "/"),
                stage_dir=stage_dir,
                folder=folder,
                folder_id=folder_id,
                folder_slug=folder_slug,
                doc_file=doc_file,
            )
            if not doc_path.is_file():
                rec.parse_error = f"文件夹缺少 {doc_file}"
            else:
                text = doc_path.read_text(encoding="utf-8")
                fm, body, err = parse_frontmatter(text)
                rec.fm, rec.body, rec.parse_error = fm, body, err
            records.append(rec)
    records.sort(key=lambda r: (STAGES.index(r.stage_dir), r.folder))
    return records


def load_registry(root: Path | None = None) -> tuple[list[str], list[str]]:
    """解析 REGISTRY.md 的 id 列。

    返回 (ids_in_file_order, warnings)。文件缺失返回 ([], [warning])。
    仅提取每行第一列中匹配 ^SDD-\\d{4}$ 的单元格，对表格写法宽容。
    """
    root = root or repo_root()
    path = root / REGISTRY_FILE
    if not path.is_file():
        return [], [f"未找到 {REGISTRY_FILE}（历史台账缺失；新文档使用分钟时间标识）"]
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|", 1)[0].strip().strip("`")
        if ID_RE.match(first):
            ids.append(first)
    return ids, []


def as_date(value: Any) -> _dt.date | None:
    """把 frontmatter 中的日期（PyYAML 可能已解析为 date，或仍是字符串）转为 date。

    非法返回 None。
    """
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def coerce_str(value: Any) -> str:
    """把标量安全转为字符串用于正则比较（日期等会被还原为 ISO 文本）。"""
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)

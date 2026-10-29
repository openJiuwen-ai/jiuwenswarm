#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Maintain document metadata and regenerate navigation from frontmatter."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import os
from pathlib import Path
import re
import sys

import yaml

import generate_index
import lib_frontmatter
from document_identity import GitIdentity, git_identity

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)
_SECTIONS = ("designs", "features", "bugfix", "specs")
_NAMES = {"designs": "设计 D", "features": "实施与决策 F", "bugfix": "故障修复 B", "specs": "当前规约 S"}
_TYPES = {"designs": "D", "features": "F", "bugfix": "B", "specs": "S"}
_MODULE_PREFIX = ""


@dataclass
class Document:
    """Store one source document and its parsed metadata."""

    path: Path
    text: str
    metadata: dict
    body: str
    section: str


def read_document(path: Path, root: Path) -> Document:
    """Parse frontmatter while retaining the original body and header text."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    data = yaml.safe_load(match.group(1)) if match else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: frontmatter must be a mapping")
    body = text[match.end():] if match else text
    parts = path.relative_to(root).parts
    section = "testing" if "testing" in parts else (parts[0] if parts[0] in _SECTIONS else "reference")
    return Document(path, text, data, body, section)


def list_value(value: object, path: Path, field: str) -> list[str]:
    """Read a declared string list without silently coercing invalid values."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{path}: {field} must be a list of strings or null")
    return value


def updated_text(doc: Document, metadata: dict) -> str:
    """Add missing fields and remove the obsolete scalar owner without rewriting prose."""
    match = _FRONTMATTER.match(doc.text)
    if not match:
        header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
        return f"---\n{header}\n---\n\n{doc.body}"
    header = match.group(1)
    if "owner" in doc.metadata:
        if isinstance(doc.metadata["owner"], (dict, list)):
            raise ValueError(f"{doc.path}: non-scalar legacy owner needs explicit migration")
        header = re.sub(r"^owner:.*(?:\n|$)", "", header, flags=re.M).rstrip()
    for key in ("author", "author_email", "author_note"):
        if key in doc.metadata and metadata.get(key) != doc.metadata[key]:
            replacement = yaml.safe_dump({key: metadata[key]}, allow_unicode=True, sort_keys=False).rstrip()
            header = re.sub(rf"^{key}:.*$", lambda match: replacement, header, flags=re.M)
    additions = {key: value for key, value in metadata.items() if key not in doc.metadata}
    if additions:
        header += "\n" + yaml.safe_dump(additions, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{header}\n---\n\n{doc.body}"


def prepare_metadata(doc: Document, identity: GitIdentity, current: Path | None, adopt: bool) -> dict:
    """Migrate historical owner values to author without losing attribution."""
    data = dict(doc.metadata)
    legacy_author = data.pop("owner", None)
    if legacy_author:
        if not isinstance(legacy_author, str):
            raise ValueError(f"{doc.path}: legacy owner must be an author string")
        if data.get("author") not in (None, legacy_author):
            raise ValueError(f"{doc.path}: author conflicts with historical owner; reconcile explicitly before migration")
        if data.get("author") is None:
            data["author"] = legacy_author
            data["author_email"] = None
            data["author_note"] = "由历史 owner 字段原值迁移"
    title = re.search(r"^#\s+(.+)$", doc.body, re.M)
    data.setdefault("title", title.group(1).strip() if title else doc.path.stem)
    if "author" not in data or (data["author"] is None and ((doc.path == current and not data.get("author_note")) or adopt)):
        if adopt or doc.path == current:
            data["author"] = identity.name
            data["author_email"] = identity.email
        else:
            data["author"] = None
            data["author_note"] = "历史作者待确认；缺少原 owner 和可靠创建署名"
    if data.get("author") is not None and not isinstance(data["author"], str):
        raise ValueError(f"{doc.path}: author must be a Git account name or null for unconfirmed history")
    if doc.section in _SECTIONS:
        data.setdefault("id", doc.path.stem)
        data.setdefault("type", _TYPES[doc.section])
        if "modules" not in data:
            parent = doc.path.parent.name
            data["modules"] = [f"openjiuwen/{parent}"] if parent in ("harness", "agent_teams") else []
        data.setdefault("files", [])
        list_value(data["modules"], doc.path, "modules")
        list_value(data["files"], doc.path, "files")
    if doc.section in ("features", "bugfix"):
        if "designs" not in data:
            data["designs"] = None
            data["design_note"] = "历史设计关联待核实，不等同于没有关联设计"
        list_value(data["designs"], doc.path, "designs")
        if data["designs"] == [] and not str(data.get("design_note", "")).strip():
            raise ValueError(f"{doc.path}: an empty designs list requires design_note explaining why")
        if doc.path == current and data["designs"] is None:
            raise ValueError(f"{doc.path}: declare designs explicitly; use [] with design_note when no design applies")
    return data


def cell(value: object) -> str:
    """Escape table cells and keep multiline metadata on one row."""
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def relative_link(path: Path, parent: Path) -> str:
    """Render portable Markdown links, including paths containing spaces."""
    target = Path(os.path.relpath(path, parent)).as_posix()
    return f"<{target}>" if " " in target else target


def design_links(doc: Document, parent: Path, designs: dict[str, Document]) -> str:
    """Show explicit local, cross-repository or unresolved design relationships."""
    declared = doc.metadata.get("designs")
    if declared is None:
        return "待核实"
    if declared == []:
        return "无独立 D：" + cell(doc.metadata.get("design_note"))
    result = []
    for identifier in declared:
        target = designs.get(identifier)
        result.append(f"[{cell(identifier)}]({relative_link(target.path, parent)})" if target else cell(identifier) + "（非本仓或待定位）")
    return "<br>".join(result)


def document_modules(doc: Document) -> list[str]:
    """Group declared modules consistently with the repository design index."""
    names: list[str] = []
    for declared in list_value(doc.metadata.get("modules"), doc.path, "modules"):
        name = declared.strip().rstrip("/")
        if _MODULE_PREFIX:
            name = name.removeprefix(_MODULE_PREFIX)
        if name and name not in names:
            names.append(name)
    return names or ["未分类"]


def index_row(doc: Document, parent: Path, designs: dict[str, Document]) -> str:
    """Render one source document without altering author or design metadata."""
    data = doc.metadata
    label = cell(data.get("id", doc.path.stem))
    author = cell(data.get("author")) if data.get("author") else "待确认"
    relation = design_links(doc, parent, designs) if doc.section in ("features", "bugfix") else "—"
    fields = [
        f"[{label}]({relative_link(doc.path, parent)})",
        cell(data.get("type", "说明")),
        cell(data.get("title")),
        author,
        cell(data.get("modules")),
        cell(data.get("files")),
        relation,
    ]
    return "| " + " | ".join(fields) + " |"


def render_index(path: Path, title: str, docs: list[Document], identity: GitIdentity, designs: dict[str, Document]) -> str:
    """Generate module navigation with globally deduplicated document counts."""
    metadata = {"title": title, "author": identity.name, "author_email": identity.email, "generated": True}
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    unique_docs = list({doc.path: doc for doc in docs}.values())
    grouped: dict[str, list[Document]] = {}
    for doc in unique_docs:
        for name in document_modules(doc):
            grouped.setdefault(name, []).append(doc)
    names = sorted(grouped, key=lambda name: (name == "未分类", name))
    lines = [
        f"---\n{header}\n---", "", f"# {title}", "",
        "由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。", "",
        f"共 {len(unique_docs)} 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。", "",
        "## 模块导航", "", "| 模块 | 文档数 |", "|---|---|",
    ]
    for number, name in enumerate(names, start=1):
        lines.append(f"| [{cell(name)}](#module-{number}) | {len(grouped[name])} |")
    if not names:
        lines.extend(["| — | 0 |", "", "暂无文档。"])
    else:
        lines.append("")
    type_order = {"D": 0, "F": 1, "B": 2, "S": 3}
    for number, name in enumerate(names, start=1):
        lines.extend([
            f'<a id="module-{number}"></a>', "", f"## {cell(name)}", "",
            "| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |",
            "|---|---|---|---|---|---|---|",
        ])
        ordered = sorted(grouped[name], key=lambda doc: (
            type_order.get(str(doc.metadata.get("type")), 4),
            str(doc.metadata.get("id", doc.path.stem)),
            doc.path.as_posix(),
        ))
        lines.extend(index_row(doc, path.parent, designs) for doc in ordered)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def synchronize(root: Path, identity: GitIdentity, current_document: Path | None = None, adopt_legacy_author: bool = False) -> int:
    """Prepare all changes before writing metadata or replacing generated indexes."""
    root = root.resolve()
    current = current_document.resolve() if current_document else None
    if current and (not current.is_relative_to(root) or not current.is_file()):
        raise ValueError("The selected document must exist inside the managed document root")
    paths = [path for path in sorted(root.rglob("*.md")) if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root) and path.name != "INDEX.md"]
    docs = [read_document(path, root) for path in paths]
    writes: dict[Path, str] = {}
    for doc in docs:
        metadata = prepare_metadata(doc, identity, current, adopt_legacy_author)
        writes[doc.path] = updated_text(doc, metadata)
        doc.metadata = metadata
    designs: dict[str, Document] = {}
    for doc in docs:
        if doc.section == "designs" and doc.path.name == "design.md":
            identifier = str(doc.metadata["id"])
            if identifier in designs:
                raise ValueError(f"Duplicate design id: {identifier}")
            designs[identifier] = doc
    for section in ("features", "bugfix", "specs"):
        path = root / section / "INDEX.md"
        writes[path] = render_index(path, _NAMES[section] + "索引", [doc for doc in docs if doc.section == section], identity, designs)
    writes[root / "INDEX.md"] = render_index(root / "INDEX.md", "D/F/B/S 文档总索引", [doc for doc in docs if doc.section != "testing"], identity, designs)
    design_root = root / "designs"
    if design_root.is_dir():
        records = lib_frontmatter.iter_docs(design_root)
        by_path = {doc.path: doc for doc in docs}
        for record in records:
            if record.path in by_path:
                record.fm = by_path[record.path].metadata
                record.parse_error = None
        timestamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="minutes")
        index_body = generate_index.build_index(records, timestamp)
        index_metadata = {"title": "设计文档索引", "author": identity.name, "author_email": identity.email, "generated": True}
        header = yaml.safe_dump(index_metadata, allow_unicode=True, sort_keys=False).rstrip()
        writes[design_root / "INDEX.md"] = f"---\n{header}\n---\n\n{index_body}"
    for path, content in writes.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return len(docs)


def main(argv: list[str] | None = None) -> int:
    """Synchronize metadata and all managed indexes from the repository identity."""
    repo = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=repo / ".agents/agent-docs")
    parser.add_argument("--document", type=Path, help="New document whose author is the current Git account")
    parser.add_argument("--adopt-legacy-author", action="store_true", help="Explicitly attribute existing unattributed documents to the current Git account")
    args = parser.parse_args(argv)
    try:
        identity_root = args.root.resolve()
        while not identity_root.is_dir() and identity_root.parent != identity_root:
            identity_root = identity_root.parent
        identity = git_identity(identity_root)
        count = synchronize(args.root, identity, args.document, args.adopt_legacy_author)
        print(f"Updated document metadata and indexes: {count} source documents")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Document synchronization failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

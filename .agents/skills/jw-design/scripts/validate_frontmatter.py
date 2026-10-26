#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""校验所有 design.md 的 frontmatter 与命名 / 位置不变量（只读，绝不写文件）。

用法：
    python .agents/skills/jw-design/scripts/validate_frontmatter.py            # 校验全仓
    python .agents/skills/jw-design/scripts/validate_frontmatter.py --stage active
    python .agents/skills/jw-design/scripts/validate_frontmatter.py --json     # 机器可读输出（供 AI / 钩子）

退出码：0 = 无 ERROR（可能有 WARN）；1 = 存在 ERROR；2 = 内部失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import lib_frontmatter as lib


class Finding:
    __slots__ = ("rel_path", "level", "code", "message")

    def __init__(self, rel_path: str, level: str, code: str, message: str):
        self.rel_path = rel_path
        self.level = level  # "ERROR" | "WARN"
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {
            "file": self.rel_path,
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }


def _check_doc(rec: lib.DocRecord, all_ids: set[str], findings: list[Finding]) -> None:
    """对单个文档累加 findings。"""

    def err(code: str, msg: str) -> None:
        findings.append(Finding(rec.rel_path, "ERROR", code, msg))

    def warn(code: str, msg: str) -> None:
        findings.append(Finding(rec.rel_path, "WARN", code, msg))

    # 文件夹命名
    if rec.folder_id is None:
        err("folder-name", f"文件夹名 '{rec.folder}' 不符合 历史 SDD 或 D-YYYYMMDD-HHMM-<slug> 规范")

    # 解析失败则无法继续逐字段校验
    if rec.parse_error:
        err("frontmatter", rec.parse_error)
        return

    fm = rec.fm

    # 必填字段
    for key in lib.REQUIRED_FIELDS:
        if key not in fm or fm[key] in (None, "", []):
            err("missing-field", f"缺少必填字段 '{key}'")

    # id
    doc_id = lib.coerce_str(fm.get("id", "")) if "id" in fm else ""
    if doc_id and not lib.ID_RE.match(doc_id):
        err("id-format", f"id '{doc_id}' 不符合历史 SDD 或分钟时间标识规范")
    if doc_id and rec.folder_id and doc_id != rec.folder_id:
        err("id-folder", f"frontmatter id '{doc_id}' 与文件夹号 '{rec.folder_id}' 不一致")

    # slug
    slug = lib.coerce_str(fm.get("slug", "")) if "slug" in fm else ""
    if slug and not lib.SLUG_RE.match(slug):
        err("slug-format", f"slug '{slug}' 不符合 kebab-case 规范")
    if slug and rec.folder_slug and slug != rec.folder_slug:
        err("slug-folder", f"frontmatter slug '{slug}' 与文件夹 slug '{rec.folder_slug}' 不一致")

    # stage（必须等于所在目录）
    stage = lib.coerce_str(fm.get("stage", "")) if "stage" in fm else ""
    if stage and stage not in lib.STAGES:
        err("stage-value", f"stage '{stage}' 非法（应为 {'/'.join(lib.STAGES)}）")
    if stage and stage != rec.stage_dir:
        err("stage-dir", f"stage '{stage}' 与所在目录 '{rec.stage_dir}' 不一致")

    # status（必须属于该 stage）
    status = lib.coerce_str(fm.get("status", "")) if "status" in fm else ""
    if status and status not in lib.ALL_STATUSES:
        err("status-value", f"status '{status}' 不是已知状态")
    elif status and rec.stage_dir in lib.STATUS_BY_STAGE:
        allowed = lib.STATUS_BY_STAGE[rec.stage_dir]
        if status not in allowed:
            err(
                "status-stage",
                f"status '{status}' 不允许出现在 {rec.stage_dir}/（允许：{', '.join(allowed)}）",
            )

    # 日期
    created = lib.as_date(fm.get("created")) if "created" in fm else None
    updated = lib.as_date(fm.get("updated")) if "updated" in fm else None
    if "created" in fm and created is None:
        err("date-format", "created 不是合法的 YYYY-MM-DD 日期")
    if "updated" in fm and updated is None:
        err("date-format", "updated 不是合法的 YYYY-MM-DD 日期")
    if created and updated and updated < created:
        err("date-order", f"updated ({updated}) 早于 created ({created})")

    # Preserve optional version fields in legacy documents; new reviews use dates.
    if "version" in fm:
        version = fm["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            err("version", f"version 必须为 ≥1 的整数，当前为 {version!r}")

    # domain
    domain = lib.coerce_str(fm.get("domain", "")) if "domain" in fm else ""
    if domain and domain not in lib.DOMAINS:
        err("domain", f"domain '{domain}' 不在受控词表内（{', '.join(lib.DOMAINS)}）")

    # owner 形态（轻量约束 → WARN）
    owner = fm.get("owner")
    if owner and not (isinstance(owner, str) and owner.startswith("@")):
        warn("owner-format", f"owner 建议为单个 @handle，当前为 {owner!r}")

    # reviewers：draft 之外必须非空列表
    if status in lib.REVIEWERS_REQUIRED_STATUSES:
        reviewers = fm.get("reviewers")
        if not isinstance(reviewers, list) or not reviewers:
            err("reviewers", f"status '{status}' 要求 reviewers 为非空列表")

    # approved-date：已批准，或已进入 active/archive，必须有合法 approved-date
    needs_approved = status == "approved" or rec.stage_dir in ("active", "archive")
    if needs_approved:
        ad = lib.as_date(fm.get("approved-date")) if "approved-date" in fm else None
        if "approved-date" not in fm:
            err("approved-date", "已批准 / 进入开发的设计必须填写 approved-date")
        elif ad is None:
            err("date-format", "approved-date 不是合法的 YYYY-MM-DD 日期")

    # Archival metadata is shared; implementation facts belong to feature records.
    if rec.stage_dir == "archive":
        archd = lib.as_date(fm.get("archived-date")) if "archived-date" in fm else None
        if "archived-date" not in fm:
            err("archived-date", "归档设计必须填写 archived-date")
        elif archd is None:
            err("date-format", "archived-date 不是合法的 YYYY-MM-DD 日期")

        impl = fm.get("implementation")
        if isinstance(impl, dict):
            impl_status = lib.coerce_str(impl.get("status", "")) if "status" in impl else ""
            if not impl_status:
                err("implementation", "implementation.status 必填")
            elif impl_status not in lib.IMPLEMENTATION_STATUSES:
                err(
                    "implementation",
                    f"implementation.status '{impl_status}' 非法（{', '.join(lib.IMPLEMENTATION_STATUSES)}）",
                )


    # 关联引用：目标存在性 + 互惠
    for key in ("supersedes", "superseded-by"):
        val = fm.get(key)
        if val:
            target = lib.coerce_str(val)
            if not lib.ID_RE.match(target):
                err("ref-format", f"{key} '{target}' 不是合法 id")
            elif target not in all_ids:
                err("ref-missing", f"{key} 指向的 {target} 在仓库中不存在")
    related = fm.get("related")
    if related:
        if not isinstance(related, list):
            err("ref-format", "related 必须为 id 列表")
        else:
            for target in (lib.coerce_str(t) for t in related):
                if not lib.ID_RE.match(target):
                    err("ref-format", f"related 含非法 id '{target}'")
                elif target not in all_ids:
                    err("ref-missing", f"related 指向的 {target} 在仓库中不存在")


def _section_is_empty(body: str, heading_keyword: str) -> bool:
    """判断正文中含 heading_keyword 的章节到下一个 ## 之间是否为空 / 仅占位。"""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and heading_keyword in line:
            start = i + 1
            break
    if start is None:
        return True  # 连章节都没有
    content: list[str] = []
    for line in lines[start:]:
        if line.lstrip().startswith("## "):
            break
        stripped = line.strip()
        # 跳过空行、子标题、占位符（<...> / TODO / 待填）
        if not stripped or stripped.startswith("#"):
            continue
        if re.fullmatch(r"[<（(].*[>）)]", stripped):
            continue
        if re.search(r"TODO|待填|待补充|TBD", stripped, re.IGNORECASE):
            continue
        content.append(stripped)
    return not content


def _check_repo(records: list[lib.DocRecord], findings: list[Finding], root: Path | None) -> None:
    """跨文档校验：id 唯一性 + REGISTRY 对账。"""
    # id 唯一性（按 frontmatter id；解析失败的文档跳过）
    seen: dict[str, str] = {}
    for rec in records:
        if rec.parse_error:
            continue
        doc_id = lib.coerce_str(rec.fm.get("id", "")) if "id" in rec.fm else ""
        if not doc_id:
            continue
        if doc_id in seen:
            prev = seen[doc_id]
            findings.append(
                Finding(
                    rec.rel_path,
                    "ERROR",
                    "id-duplicate",
                    f"id '{doc_id}' 与 {prev} 重复",
                )
            )
        else:
            seen[doc_id] = rec.rel_path

    # REGISTRY 对账
    registry_ids, reg_warns = lib.load_registry(root)
    for w in reg_warns:
        findings.append(Finding(lib.REGISTRY_FILE, "WARN", "registry", w))
    if registry_ids:
        # 台账内部：无重复 + 单调递增
        if len(set(registry_ids)) != len(registry_ids):
            findings.append(
                Finding(lib.REGISTRY_FILE, "ERROR", "registry-dup", "台账存在重复 id")
            )
        nums = [int(i.split("-")[1]) for i in registry_ids]
        if nums != sorted(nums):
            findings.append(
                Finding(lib.REGISTRY_FILE, "ERROR", "registry-order", "台账 id 未严格升序排列")
            )
        # 每个文件夹 id 必须已登记
        reg_set = set(registry_ids)
        for rec in records:
            fid = rec.folder_id
            if fid and fid.startswith("SDD-") and fid not in reg_set:
                findings.append(
                    Finding(rec.rel_path, "ERROR", "registry-missing", f"{fid} 未登记在 {lib.REGISTRY_FILE}")
                )


def run(root: Path | None, stage: str | None):
    # 始终基于全仓扫描计算 id 集合与跨仓校验，避免 --stage 过滤导致误判
    all_records = lib.iter_docs(root, None)
    all_ids = {
        lib.coerce_str(r.fm.get("id"))
        for r in all_records
        if not r.parse_error and r.fm.get("id")
    }
    records = all_records if stage is None else [r for r in all_records if r.stage_dir == stage]
    findings: list[Finding] = []
    for rec in records:
        _check_doc(rec, all_ids, findings)
    _check_repo(all_records, findings, root)
    return records, findings


def _print_report(records, findings: list[Finding]) -> None:
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.rel_path, []).append(f)

    errors = sum(1 for f in findings if f.level == "ERROR")
    warns = sum(1 for f in findings if f.level == "WARN")

    if not findings:
        print(f"✓ 校验通过：扫描 {len(records)} 篇设计文档，无问题。")
        return

    for rel_path in sorted(by_file):
        print(f"\n{rel_path}")
        for f in sorted(by_file[rel_path], key=lambda x: (x.level != "ERROR", x.code)):
            marker = "✗ ERROR" if f.level == "ERROR" else "! WARN "
            print(f"  {marker} [{f.code}] {f.message}")

    print(f"\n汇总：{len(records)} 篇文档，{errors} 个 ERROR，{warns} 个 WARN。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 design.md frontmatter 与命名/位置不变量")
    parser.add_argument("--root", type=Path, default=None, help="设计资产根目录（默认 .agents/agent-docs/designs）")
    parser.add_argument("--stage", choices=lib.STAGES, default=None, help="仅校验某阶段")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    try:
        records, findings = run(args.root, args.stage)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底
        print(f"内部错误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        _print_report(records, findings)

    return 1 if any(f.level == "ERROR" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())

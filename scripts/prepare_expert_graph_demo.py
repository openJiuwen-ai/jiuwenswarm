# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prepare local beta3 experts with explicit collaboration contracts.

This is a generic data-preparation CLI for the Xiaoyi Work expert-graph demo.
Product graph code never knows any demo expert IDs; the checked-in JSON file is
only an example catalogue that can be replaced with another reviewed batch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from jiuwenswarm.server.runtime.expert.expert_store import validate_expert_package
from jiuwenswarm.server.runtime.expert.package_normalizer import (
    normalize_standalone_expert,
)


def _read_contracts(path: Path) -> dict[str, dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("contracts file must contain an object keyed by expert id")
    contracts: dict[str, dict[str, Any]] = {}
    for expert_id, contract in value.items():
        if not isinstance(expert_id, str) or not isinstance(contract, dict):
            raise ValueError("every collaboration contract must be an object")
        contracts[expert_id] = contract
    return contracts


def _source_packages(roots: list[Path]) -> dict[str, Path]:
    packages: dict[str, Path] = {}
    for root in roots:
        resolved = root.expanduser().resolve(strict=True)
        for child in sorted(resolved.iterdir()):
            if not child.is_dir() or not (child / "manifest.json").is_file():
                continue
            if child.name in packages:
                raise ValueError(f"duplicate source expert id: {child.name}")
            packages[child.name] = child
    return packages


def _port_lines(ports: Any, *, verb: str) -> list[str]:
    if not isinstance(ports, list):
        return []
    lines: list[str] = []
    for port in ports:
        if not isinstance(port, Mapping):
            continue
        port_id = str(port.get("id") or "").strip()
        schema = str(port.get("schema") or "").strip()
        visibility = str(port.get("visibility") or "internal").strip()
        description = str(port.get("description") or "").strip()
        if port_id:
            lines.append(
                f"- {verb} `{port_id}`（schema: `{schema or '未声明'}`，"
                f"{visibility}）：{description or '按契约完成交接'}"
            )
    return lines


def _collaboration_persona(expert_id: str, contract: Mapping[str, Any]) -> str:
    inputs = _port_lines(contract.get("inputs"), verb="读取")
    outputs = _port_lines(contract.get("outputs"), verb="生成")
    input_section = "\n".join(inputs) or "- 本专家没有强制的上游交接输入。"
    output_section = "\n".join(outputs) or "- 按用户要求交付最终成品。"
    return (
        "# 专家协作契约\n\n"
        f"当前专家 ID：`{expert_id}`。单专家使用时照常直接理解用户需求；被专家团调度时，"
        "还必须遵守以下机器可读交接约定。\n\n"
        "## 上游输入\n\n"
        f"{input_section}\n\n"
        "若共享工作区 `.expert-handoffs/` 中存在上述输入文件，先读取并以其中事实为准；"
        "不得要求用户重新粘贴同一信息。缺失非必要字段时可做保守假设并清楚标注。\n\n"
        "## 本阶段输出\n\n"
        f"{output_section}\n\n"
        "所有 `internal` JSON 必须写入共享工作区的 `.expert-handoffs/`，文件名与 id 完全一致，"
        "使用 UTF-8，内容为合法 JSON，并带 `schemaVersion` 字段。它们只用于成员交接，不作为"
        "用户最终主产物。`public` 且 `primary=true` 的文件才是本阶段可展示成品。\n\n"
        "完成后通过 `send_message` 向 `team-leader` 回报：完成情况、交接文件真实路径、"
        "关键假设和风险。不得虚构不存在的文件或调用结果。\n"
    )


def prepare_demo(
    *,
    source_roots: list[Path],
    destination_root: Path,
    contracts_path: Path,
) -> dict[str, Any]:
    contracts = _read_contracts(contracts_path)
    sources = _source_packages(source_roots)
    missing = sorted(set(contracts) - set(sources))
    if missing:
        raise ValueError(f"missing source packages: {', '.join(missing)}")

    prepared: list[dict[str, Any]] = []
    for expert_id in sorted(contracts):
        destination = normalize_standalone_expert(
            sources[expert_id],
            destination_root=destination_root,
            collaboration=contracts[expert_id],
        )
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        persona_dir = destination / str(manifest["persona"]["dir"])
        (persona_dir / "99-collaboration-contract.md").write_text(
            _collaboration_persona(expert_id, contracts[expert_id]),
            encoding="utf-8",
        )
        warnings = validate_expert_package(destination)
        prepared.append(
            {
                "id": expert_id,
                "path": str(destination),
                "skillNames": [
                    Path(item["dir"]).name for item in manifest.get("skills") or []
                ],
                "warnings": warnings,
            }
        )
    return {"success": True, "experts": prepared}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        action="append",
        required=True,
        type=Path,
        help="Directory whose immediate children are standalone expert packages",
    )
    parser.add_argument("--destination-root", required=True, type=Path)
    parser.add_argument("--contracts", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = prepare_demo(
        source_roots=args.source_root,
        destination_root=args.destination_root,
        contracts_path=args.contracts,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

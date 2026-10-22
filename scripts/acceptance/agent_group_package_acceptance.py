#!/usr/bin/env python3
"""Validate AgentGroup directories and smoke-test the public local-import route.

The script intentionally delegates package parsing, archive extraction, installation,
catalog listing, and detail rendering to JiuwenSwarm production code.  It only
orchestrates those calls and emits a machine-readable acceptance report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence


REPORT_SCHEMA_VERSION = "workswarm.agent-group-acceptance.v1"


def _absolute_existing_paths(raw_paths: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_paths:
        path = Path(raw).expanduser().absolute()
        if not path.exists():
            raise ValueError(f"package path does not exist: {path}")
        paths.append(path)
    return paths


def _base_report(mode: str, packages: Sequence[Path]) -> dict[str, Any]:
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "passed": False,
        "python": sys.version.split()[0],
        "operations": [],
        "packageCount": len(packages),
        "packagePaths": [str(package) for package in packages],
        "cases": [],
        "errors": [],
    }


def _assert_expected_count(report: dict[str, Any], expected_count: int | None) -> None:
    if expected_count is None:
        return
    actual = report["packageCount"]
    if actual != expected_count:
        report["errors"].append(
            f"expected {expected_count} package(s), received {actual}"
        )


def validate_only(
    packages: Sequence[Path], *, expected_count: int | None = None
) -> dict[str, Any]:
    """Load package directories with the production AgentGroup loader."""
    from jiuwenswarm.agents.swarm.agent_group import (
        load_agent_group_package_bundle,
    )

    report = _base_report("validate-only", packages)
    report["operations"] = ["load_agent_group_package_bundle"]
    _assert_expected_count(report, expected_count)
    seen_ids: set[str] = set()

    for package in packages:
        case: dict[str, Any] = {
            "source": str(package),
            "passed": False,
        }
        try:
            if not package.is_dir():
                raise ValueError(
                    "validate-only accepts unpacked package directories; "
                    "use import-local for zip/tar archives"
                )
            bundle = load_agent_group_package_bundle(package)
            package_id = bundle.package_dir.name
            if package_id in seen_ids:
                raise ValueError(f"duplicate package id in input set: {package_id}")
            seen_ids.add(package_id)
            case.update(
                {
                    "packageId": package_id,
                    "memberCount": len(bundle.templates),
                    "members": list(bundle.templates),
                    "passed": True,
                }
            )
        except Exception as exc:  # noqa: BLE001 - aggregate every package result
            case["error"] = str(exc)
        report["cases"].append(case)

    report["passed"] = not report["errors"] and all(
        case["passed"] for case in report["cases"]
    )
    return report


def _prepare_isolated_data_dir(data_dir: Path) -> Path:
    data_dir = data_dir.expanduser()
    if not data_dir.is_absolute():
        raise ValueError("--data-dir must be an absolute path")
    data_dir = data_dir.absolute()
    if data_dir.exists() and not data_dir.is_dir():
        raise ValueError(f"--data-dir is not a directory: {data_dir}")
    if data_dir.exists() and any(data_dir.iterdir()):
        raise ValueError(f"--data-dir must not contain existing state: {data_dir}")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["JIUWENSWARM_DATA_DIR"] = str(data_dir)
    return data_dir


def _avatar_evidence(value: Any) -> dict[str, Any]:
    avatar = value.strip() if isinstance(value, str) else ""
    if not avatar:
        return {"present": False, "kind": "none", "length": 0}
    if avatar.lower().startswith("data:image/"):
        kind = "data-image"
    elif avatar.lower().startswith(("http://", "https://")):
        kind = "url"
    else:
        kind = "package-reference"
    evidence: dict[str, Any] = {
        "present": True,
        "kind": kind,
        "length": len(avatar),
    }
    if kind != "data-image":
        evidence["reference"] = avatar
    return evidence


def _catalog_card_evidence(card: dict[str, Any]) -> dict[str, Any]:
    members = card.get("members") if isinstance(card.get("members"), list) else []
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    return {
        "id": card.get("id"),
        "source": card.get("source"),
        "installed": card.get("installed"),
        "canUse": (card.get("capabilities") or {}).get("canUse"),
        "memberCount": card.get("memberCount"),
        "avatar": _avatar_evidence(card.get("avatar")),
        "members": [
            {
                "id": member.get("id"),
                "role": member.get("role"),
                "avatar": _avatar_evidence(member.get("avatar")),
            }
            for member in members
            if isinstance(member, dict)
        ],
        "skillIds": [
            str(skill.get("id"))
            for skill in skills
            if isinstance(skill, dict) and skill.get("id")
        ],
    }


def _assert_catalog_card_contract(
    card: dict[str, Any],
    *,
    package_id: str,
    expected_member_count: int,
    surface: str,
) -> None:
    """Assert the user-visible contract shared by list and show cards."""
    failures: list[str] = []
    if card.get("id") != package_id:
        failures.append(f"id={card.get('id')!r}, expected {package_id!r}")
    if card.get("source") != "local":
        failures.append(f"source={card.get('source')!r}, expected 'local'")
    if card.get("installed") is not True:
        failures.append(f"installed={card.get('installed')!r}, expected true")
    can_use = (card.get("capabilities") or {}).get("canUse")
    if can_use is not True:
        failures.append(f"canUse={can_use!r}, expected true")
    if card.get("memberCount") != expected_member_count:
        failures.append(
            f"memberCount={card.get('memberCount')!r}, expected {expected_member_count}"
        )
    if failures:
        raise ValueError(f"{surface} contract mismatch: " + "; ".join(failures))


async def _request(
    facade: Any,
    method: Any,
    params: dict[str, Any],
    *,
    suffix: str,
) -> dict[str, Any]:
    from jiuwenswarm.common.schema.agent import AgentRequest

    response = await facade.process_message(
        AgentRequest(
            request_id=f"agent-group-acceptance-{suffix}-{uuid.uuid4().hex}",
            channel_id="acceptance",
            req_method=method,
            params=params,
        )
    )
    payload = response.payload if isinstance(response.payload, dict) else {}
    if not response.ok:
        code = str(payload.get("code") or "AGENT_GROUP_REQUEST_FAILED")
        detail = str(payload.get("error") or "unknown request failure")
        raise ValueError(f"{method.value} failed [{code}]: {detail}")
    return payload


async def import_local_smoke(
    packages: Sequence[Path],
    *,
    data_dir: Path,
    expected_count: int | None = None,
) -> dict[str, Any]:
    """Import packages through the public AgentServer facade and inspect catalog state."""
    data_dir = _prepare_isolated_data_dir(data_dir)

    # Imports stay below this point so JIUWENSWARM_DATA_DIR is fixed before any
    # workspace path is cached by product modules.
    from jiuwenswarm.agents.swarm.agent_group import (
        load_agent_group_package_bundle,
    )
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime import extension_package_manager
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    report = _base_report("import-local", packages)
    report["dataDir"] = str(data_dir)
    report["operations"] = [
        ReqMethod.AGENT_GROUPS_IMPORT_LOCAL.value,
        ReqMethod.AGENT_GROUPS_LIST.value,
        ReqMethod.AGENT_GROUPS_SHOW.value,
        "resolve_agent_group_dir",
        "load_agent_group_package_bundle",
    ]
    _assert_expected_count(report, expected_count)
    facade = JiuWenSwarm()
    imported_ids: list[str] = []

    for package in packages:
        case: dict[str, Any] = {
            "source": str(package),
            "passed": False,
        }
        try:
            payload = await _request(
                facade,
                ReqMethod.AGENT_GROUPS_IMPORT_LOCAL,
                {"path": str(package)},
                suffix="import",
            )
            package_id = str(payload.get("id") or "").strip()
            if not package_id:
                raise ValueError("agent_groups.import_local returned no package id")

            show_payload = await _request(
                facade,
                ReqMethod.AGENT_GROUPS_SHOW,
                {"id": package_id},
                suffix="show",
            )
            group = show_payload.get("group")
            if not isinstance(group, dict):
                raise ValueError("agent_groups.show returned no group card")

            installed_dir = extension_package_manager.resolve_agent_group_dir(
                package_id
            )
            bundle = load_agent_group_package_bundle(installed_dir)
            member_count = len(bundle.templates)
            _assert_catalog_card_contract(
                group,
                package_id=package_id,
                expected_member_count=member_count,
                surface="agent_groups.show",
            )
            imported_ids.append(package_id)
            case.update(
                {
                    "packageId": package_id,
                    "installedPath": str(installed_dir),
                    "sourceType": "archive" if package.is_file() else "directory",
                    "memberCount": member_count,
                    "members": list(bundle.templates),
                    "memberSkillIds": {
                        member_id: [Path(skill.dir).name for skill in template.skills]
                        for member_id, template in bundle.templates.items()
                    },
                    "show": _catalog_card_evidence(group),
                    "passed": True,
                }
            )
        except Exception as exc:  # noqa: BLE001 - aggregate every package result
            case["error"] = str(exc)
        report["cases"].append(case)

    try:
        list_payload = await _request(
            facade,
            ReqMethod.AGENT_GROUPS_LIST,
            {"filter": "local"},
            suffix="list",
        )
        cards = list_payload.get("agentGroups")
        if not isinstance(cards, list):
            raise ValueError("agent_groups.list returned no agentGroups array")
        catalog_cards = {
            str(card.get("id")): card
            for card in cards
            if isinstance(card, dict) and card.get("id")
        }
        missing = sorted(set(imported_ids) - set(catalog_cards))
        for case in report["cases"]:
            package_id = case.get("packageId")
            card = catalog_cards.get(package_id)
            if isinstance(card, dict):
                case["list"] = _catalog_card_evidence(card)
                try:
                    expected_member_count = int(case["memberCount"])
                    _assert_catalog_card_contract(
                        card,
                        package_id=str(package_id),
                        expected_member_count=expected_member_count,
                        surface="agent_groups.list",
                    )
                    show_member_count = (case.get("show") or {}).get("memberCount")
                    if card.get("memberCount") != show_member_count:
                        raise ValueError(
                            "agent_groups.list contract mismatch: memberCount "
                            "differs from agent_groups.show"
                        )
                except (KeyError, TypeError, ValueError) as exc:
                    case["passed"] = False
                    case["error"] = str(exc)
                    report["errors"].append(f"{package_id}: {exc}")
        report["catalog"] = {
            "installedIds": sorted(
                package_id
                for package_id, card in catalog_cards.items()
                if card.get("installed") is True
            ),
            "missingImportedIds": missing,
        }
        if missing:
            report["errors"].append(
                f"imported package(s) missing from agent_groups.list: {missing}"
            )
    except Exception as exc:  # noqa: BLE001 - report list failure with case results
        report["errors"].append(str(exc))

    if expected_count is not None and len(imported_ids) != expected_count:
        report["errors"].append(
            f"expected {expected_count} successful import(s), got {len(imported_ids)}"
        )
    report["passed"] = not report["errors"] and all(
        case["passed"] for case in report["cases"]
    )
    return report


def _write_report(report: dict[str, Any], json_out: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if json_out is not None:
        target = json_out.expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    # Keep the final stdout line parseable even when imported dependencies log.
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-only",
        help="Validate unpacked package directories with the production loader",
    )
    validate.add_argument("packages", nargs="+")
    validate.add_argument("--expected-count", type=int)
    validate.add_argument("--json-out", type=Path)

    import_local = subparsers.add_parser(
        "import-local",
        help="Import directory/archive packages through agent_groups.import_local",
    )
    import_local.add_argument("packages", nargs="+")
    import_local.add_argument("--data-dir", type=Path, required=True)
    import_local.add_argument("--expected-count", type=int)
    import_local.add_argument("--json-out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    submitted_packages = [Path(raw).expanduser().absolute() for raw in args.packages]
    try:
        packages = _absolute_existing_paths(args.packages)
        if args.command == "validate-only":
            # Some imported runtime dependencies initialize their log paths from
            # JIUWENSWARM_DATA_DIR even though the loader itself is read-only.
            # A disposable home keeps validation from touching the user's real
            # workspace while preserving the production import graph.
            with tempfile.TemporaryDirectory(
                prefix="workswarm_agent_group_validate_"
            ) as temp_data_dir:
                os.environ["JIUWENSWARM_DATA_DIR"] = temp_data_dir
                report = validate_only(packages, expected_count=args.expected_count)
        else:
            report = asyncio.run(
                import_local_smoke(
                    packages,
                    data_dir=args.data_dir,
                    expected_count=args.expected_count,
                )
            )
    except Exception as exc:  # noqa: BLE001 - emit a report for CLI/setup failures
        report = _base_report(args.command, submitted_packages)
        report["errors"] = [str(exc)]
    _write_report(report, args.json_out)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

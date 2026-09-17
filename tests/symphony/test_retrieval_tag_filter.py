from __future__ import annotations

import json
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.symphony.skill_retrieval.dispatch_imports import dispatch_import_path


def test_offline_build_records_skill_tags(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "phone-control"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Phone Control
description: Controls a phone application.
tags: [automation, Mobile, ALL]
---

Use the phone.
""",
        encoding="utf-8",
    )

    with dispatch_import_path():
        from indexing.io.manifest import write_manifest
        from indexing.scanners.skill import SkillScanner
        from indexing.workflows.artifacts import build_catalog_records_from_nodes, write_catalog
        from retrieval.io.loading import load_catalog_records

        scanned = SkillScanner(tmp_path / "skills").to_dict_list()
        assert scanned[0]["tags"] == ["automation", "mobile", "all"]

        records = build_catalog_records_from_nodes(
            nodes=[
                {
                    "cid": "automation.phone.phone_control",
                    "type": "leaf",
                    "worker_id": "phone-control",
                }
            ],
            scanned_skills={"phone-control": scanned[0]},
        )
        catalog_path = tmp_path / "catalog.jsonl"
        write_catalog(records, catalog_path)
        write_manifest(tmp_path, [skill_dir], records, mode="full", item_type="skill")
        assert load_catalog_records(catalog_path)[0].tags == ("automation", "mobile", "all")

    catalog_row = json.loads(catalog_path.read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert catalog_row["tags"] == ["automation", "mobile", "all"]
    assert catalog_row["metadata"]["tags"] == ["automation", "mobile", "all"]
    assert manifest["tag_counts"] == {"all": 1, "automation": 1, "mobile": 1}


def test_jsonl_tags_are_read_from_content_extend_param() -> None:
    payload = {
        "tags": ["top-level-tag"],
        "contentExtendParam": {
            "skillId": "sms",
            "skillName": "SMS",
            "skillDesc": "Read messages.",
            "tags": ["Messaging", "Mobile"],
        }
    }

    with dispatch_import_path():
        from indexing.io.items_jsonl import parse_jsonl_scanned_items

        scanned, _paths = parse_jsonl_scanned_items(json.dumps(payload))

    assert scanned["sms"]["tags"] == ["messaging", "mobile"]


def test_catalog_build_uses_canonical_scanned_tags_once(monkeypatch) -> None:
    with dispatch_import_path():
        from indexing.workflows import artifacts

        calls = []
        original_normalize_tags = artifacts.normalize_tags

        def capture_normalize_tags(*values):
            calls.append(values)
            return original_normalize_tags(*values)

        monkeypatch.setattr(artifacts, "normalize_tags", capture_normalize_tags)
        records = artifacts.build_catalog_records_from_nodes(
            nodes=[{"cid": "messaging.sms", "type": "leaf", "worker_id": "sms"}],
            scanned_skills={
                "sms": {
                    "name": "SMS",
                    "description": "Read messages.",
                    "path": "jsonl://skill/sms",
                    "tags": ["messaging", "mobile"],
                    "content_extend_param": {
                        "tags": ["messaging"],
                    },
                }
            },
        )

    assert records[0].tags == ("messaging", "mobile")
    assert calls == [(["messaging", "mobile"],)]


def test_request_config_filters_tree_before_retrieval(monkeypatch, tmp_path: Path) -> None:
    with dispatch_import_path():
        from models.retrieval import RetrieverItem, RetrieverNode
        from retrieval.io.loading import CatalogRecord, LoadedRetrieverIndex
        from retrieval.service import RequestConfig, Retriever, RetrieverConfig, SearchResult

        records = (
            CatalogRecord(
                choice_id="Phone Control",
                payload="automation.phone",
                worker_id="phone-control",
                tags=("mobile", "automation"),
            ),
            CatalogRecord(
                choice_id="Desktop Control",
                payload="automation.desktop",
                worker_id="desktop-control",
                tags=("pc", "automation"),
            ),
            CatalogRecord(
                choice_id="Cloud Storage",
                payload="storage.cloud",
                worker_id="cloud-storage",
                tags=("all",),
            ),
            CatalogRecord(
                choice_id="Unknown Compatibility",
                payload="misc.unknown",
                worker_id="unknown",
            ),
        )
        root = RetrieverNode(
            node_id="ROOT",
            label="ROOT",
            children=(
                RetrieverNode(
                    node_id="capabilities",
                    label="Capabilities",
                    items=tuple(
                        RetrieverItem(item_id=record.choice_id, payload=record.payload)
                        for record in records
                    ),
                ),
            ),
        )
        loaded = LoadedRetrieverIndex(
            index_dir=tmp_path,
            tree_root=root,
            choices=(),
            catalog_records=records,
        )
        retriever = Retriever(loaded_index=loaded, config=RetrieverConfig(top_k=10))
        calls: list[list[str]] = []

        def fake_search_progressive(*, query, top_k, runtime_config, root):
            del query, top_k, runtime_config

            def payloads(node):
                return [item.payload for item in node.items] + [
                    payload
                    for child in node.children
                    for payload in payloads(child)
                ]

            visible = payloads(root)
            calls.append(visible)
            by_payload = {record.payload: record for record in records}
            return SearchResult(
                method="progressive",
                payloads=visible,
                candidate_records=[
                    {
                        "rank": index,
                        "raw_output": by_payload[payload].choice_id,
                        "resolved_payload": payload,
                        "choice_id": by_payload[payload].choice_id,
                        "valid": True,
                        "selected": index == 1,
                        "source": "test",
                    }
                    for index, payload in enumerate(visible, start=1)
                ],
                summary_lines=[],
                selected_payload=visible[0] if visible else None,
                selected_rank=1 if visible else -1,
            )

        monkeypatch.setattr(retriever, "_search_progressive", fake_search_progressive)

        result = retriever.search_details(
            "control my device",
            search_config=RequestConfig(tags=("mobile",)),
        )
        assert calls == [["automation.phone", "storage.cloud"]]
        assert result.payloads == ["phone-control", "cloud-storage"]
        assert result.candidate_records[0]["tags"] == ["mobile", "automation"]
        assert result.trace_events[0]["event_type"] == "tag_filter"
        assert result.trace_events[0]["detail"]["retained_count"] == 2
        assert set(result.trace_events[0]["detail"]) == {
            "requested_tags",
            "catalog_count",
            "retained_count",
        }
        assert retriever.available_tags == ("all", "automation", "mobile", "pc")

        unfiltered = retriever.search_details("anything")
        assert len(unfiltered.payloads) == 4
        assert calls[-1] == [
            "automation.phone",
            "automation.desktop",
            "storage.cloud",
            "misc.unknown",
        ]

        non_wildcard_records = records[:2]
        non_wildcard_root = RetrieverNode(
            node_id="ROOT",
            label="ROOT",
            items=tuple(
                RetrieverItem(item_id=record.choice_id, payload=record.payload)
                for record in non_wildcard_records
            ),
        )
        no_match_retriever = Retriever(
            loaded_index=LoadedRetrieverIndex(
                index_dir=tmp_path,
                tree_root=non_wildcard_root,
                choices=(),
                catalog_records=non_wildcard_records,
            ),
            config=RetrieverConfig(top_k=10),
        )

        def fail_search_progressive(**kwargs):
            del kwargs
            raise AssertionError("empty filtered trees must not invoke retrieval")

        monkeypatch.setattr(no_match_retriever, "_search_progressive", fail_search_progressive)
        no_match = no_match_retriever.search_details(
            "use a watch",
            search_config=RequestConfig(tags=("watch",)),
        )
        assert no_match.payloads == []


def test_tag_filter_requires_all_tags_and_supports_reserved_wildcard() -> None:
    with dispatch_import_path():
        from retrieval.tree.filtering import candidate_tags_match

        assert candidate_tags_match(("mobile", "automation"), ("mobile", "automation"))
        assert not candidate_tags_match(("mobile",), ("mobile", "automation"))
        assert candidate_tags_match(("all",), ("mobile", "automation"))
        assert not candidate_tags_match((), ("mobile",))


def test_request_config_exposes_only_tags_for_filtering() -> None:
    with dispatch_import_path():
        from retrieval.service import RequestConfig

        assert tuple(RequestConfig.__dataclass_fields__) == ("top_k", "tags")


def test_normalize_tags_handles_boundary_values_deterministically() -> None:
    with dispatch_import_path():
        from shared.tags import normalize_tags

        cyclic = ["cycle"]
        cyclic.append(cyclic)

        assert normalize_tags(None, "", "[]", [], UserDict({"ignored": "mobile"})) == ()
        assert normalize_tags(b"Mobile,PC", bytearray(b"ALL"), memoryview(b"Watch")) == (
            "mobile",
            "pc",
            "all",
            "watch",
        )
        assert normalize_tags({"pc", "mobile"}) == ("mobile", "pc")
        assert normalize_tags(["outer", ["inner"]], cyclic) == ("outer", "inner", "cycle")
        assert normalize_tags(0, False) == ("0", "false")


def test_available_tags_includes_choice_only_catalog_records(tmp_path: Path) -> None:
    with dispatch_import_path():
        from models.retrieval import RetrieverNode
        from retrieval.io.loading import LoadedRetrieverIndex
        from retrieval.service import Retriever

        retriever = Retriever(
            loaded_index=LoadedRetrieverIndex(
                index_dir=tmp_path,
                tree_root=RetrieverNode(node_id="ROOT", label="ROOT"),
                choices=(),
                catalog_records=(
                    SimpleNamespace(choice_id="choice-only", tags=("choice-tag",)),
                ),
            )
        )

        assert retriever.available_tags == ("choice-tag",)

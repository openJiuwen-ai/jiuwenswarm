import base64
import hashlib
import json
import os
import stat

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin import (
    report_bundle as bundle_module,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.report_bundle import (
    build_report_bundle,
    serialize_final_result_snapshot,
)


def _encoded(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _payload(
    *,
    response_content: str = "Report body",
    infer_messages: list[dict] | None = None,
    chart_messages: list[dict] | None = None,
    citations: list[dict] | None = None,
) -> dict:
    return {
        "response_content": response_content,
        "infer_messages": list(infer_messages or []),
        "chart_messages": list(chart_messages or []),
        "citation_messages": {"data": list(citations or [])},
    }


def _infer(resource_id: str, payload: bytes = b"<html>safe</html>") -> dict:
    return {"id": resource_id, "html_base64": _encoded(payload)}


def _chart(resource_id: str, payload: bytes = b"png-bytes") -> dict:
    return {
        "chart_id": resource_id,
        "chart_title": f"Chart {resource_id}",
        "base64": _encoded(payload),
    }


@pytest.mark.parametrize(
    "occupied_kind",
    ["directory", "directory_symlink", "file_symlink", "file_hardlink"],
)
def test_build_report_bundle_rejects_occupied_resource_paths_without_mutation(
    tmp_path, occupied_kind
):
    report_base = tmp_path / "report-v1"
    chart_dir = tmp_path / "report-v1_charts"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside-original")

    if occupied_kind == "directory":
        chart_dir.mkdir()
    elif occupied_kind == "directory_symlink":
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        try:
            chart_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            # Creating symlinks needs privilege on Windows; the rejection
            # semantics are covered where symlinks are creatable.
            pytest.skip("symlink creation requires privilege on this platform")
    else:
        chart_dir.mkdir()
        target = chart_dir / "chart-a.png"
        if occupied_kind == "file_symlink":
            try:
                target.symlink_to(outside)
            except OSError:
                pytest.skip(
                    "symlink creation requires privilege on this platform"
                )
        else:
            os.link(outside, target)

    with pytest.raises((OSError, ValueError)):
        build_report_bundle(
            _payload(chart_messages=[_chart("chart-a", b"replacement")]),
            report_base,
        )

    assert outside.read_bytes() == b"outside-original"
    if occupied_kind == "directory":
        assert list(chart_dir.iterdir()) == []


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits are not materialized on Windows"
)
def test_build_report_bundle_publishes_private_files_and_direct_manifest(tmp_path, monkeypatch):
    report_base = tmp_path / "report-v1"
    real_fsync = bundle_module.os.fsync
    injected = False

    def inject_concurrent_file(descriptor):
        nonlocal injected
        real_fsync(descriptor)
        if not injected and (tmp_path / "report-v1_charts").is_dir():
            injected = True
            (tmp_path / "report-v1_charts" / "concurrent.png").write_bytes(
                b"concurrent"
            )

    monkeypatch.setattr(bundle_module.os, "fsync", inject_concurrent_file)
    bundle = build_report_bundle(
        _payload(
            response_content="See (#insertChart:chart-a)",
            infer_messages=[_infer("infer-a")],
            chart_messages=[_chart("chart-a")],
        ),
        report_base,
    )

    infer_path = tmp_path / "report-v1_infer" / "inference_infer-a.html"
    chart_path = tmp_path / "report-v1_charts" / "chart-a.png"
    assert stat.S_IMODE(infer_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(chart_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(infer_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(chart_path.stat().st_mode) == 0o600
    assert bundle.inference_manifest == [
        {
            "id": "infer-a",
            "path": "report-v1_infer/inference_infer-a.html",
            "sha256": hashlib.sha256(b"<html>safe</html>").hexdigest(),
        }
    ]
    assert bundle.chart_manifest == [
        {
            "id": "chart-a",
            "path": "report-v1_charts/chart-a.png",
            "sha256": hashlib.sha256(b"png-bytes").hexdigest(),
        }
    ]
    assert "concurrent.png" not in json.dumps(bundle.final_result_snapshot)
    assert bundle.final_result_snapshot["infer_messages"][0]["artifact_path"] == (
        "report-v1_infer/inference_infer-a.html"
    )
    assert bundle.final_result_snapshot["chart_messages"][0]["artifact_path"] == (
        "report-v1_charts/chart-a.png"
    )


def test_build_report_bundle_validates_every_resource_before_creating_directories(tmp_path):
    with pytest.raises(ValueError, match="invalid base64"):
        build_report_bundle(
            _payload(
                chart_messages=[
                    _chart("first"),
                    {"chart_id": "second", "base64": "not base64!"},
                ]
            ),
            tmp_path / "report-v1",
        )

    assert not (tmp_path / "report-v1_charts").exists()


@pytest.mark.parametrize(
    ("field", "messages"),
    [
        ("infer_messages", [_infer("duplicate"), _infer("duplicate")]),
        ("chart_messages", [_chart("duplicate"), _chart("duplicate")]),
        ("infer_messages", [{"id": "../escape", "html_base64": _encoded(b"x")}]),
        ("chart_messages", [{"chart_id": "", "base64": _encoded(b"x")}]),
    ],
)
def test_build_report_bundle_rejects_duplicate_or_invalid_resource_ids(
    tmp_path, field, messages
):
    with pytest.raises(ValueError):
        build_report_bundle(_payload(**{field: messages}), tmp_path / "report-v1")

    assert not (tmp_path / "report-v1_infer").exists()
    assert not (tmp_path / "report-v1_charts").exists()


def test_build_report_bundle_cleans_only_owned_resources_after_publication_failure(
    tmp_path, monkeypatch
):
    real_fsync = bundle_module.os.fsync
    calls = 0

    def fail_second_file(descriptor):
        nonlocal calls
        calls += 1
        real_fsync(descriptor)
        if calls == 2:
            chart_dir = tmp_path / "report-v1_charts"
            (chart_dir / "foreign.png").write_bytes(b"foreign")
            raise OSError("injected fsync failure")

    monkeypatch.setattr(bundle_module.os, "fsync", fail_second_file)
    with pytest.raises(OSError, match="injected fsync failure"):
        build_report_bundle(
            _payload(chart_messages=[_chart("first"), _chart("second")]),
            tmp_path / "report-v1",
        )

    chart_dir = tmp_path / "report-v1_charts"
    assert (chart_dir / "foreign.png").read_bytes() == b"foreign"
    assert not (chart_dir / "first.png").exists()
    assert not (chart_dir / "second.png").exists()


def test_build_report_bundle_cleans_owned_resources_after_rewrite_failure(tmp_path):
    report_base = tmp_path / "report-v1"

    with pytest.raises(ValueError):
        build_report_bundle(
            _payload(
                response_content="(#insertChart:../escape)",
                chart_messages=[_chart("chart-a")],
            ),
            report_base,
        )

    assert not (tmp_path / "report-v1_charts").exists()


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX directory descriptor contract"
)
def test_build_report_bundle_rejects_directory_descriptor_swap_without_outside_write(
    tmp_path, monkeypatch
):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    real_open_directory = bundle_module._open_directory_no_follow

    def open_outside_instead(directory):
        descriptor = real_open_directory(outside_dir)
        assert descriptor is not None
        return descriptor

    monkeypatch.setattr(
        bundle_module, "_open_directory_no_follow", open_outside_instead
    )

    with pytest.raises(OSError):
        build_report_bundle(
            _payload(chart_messages=[_chart("chart-a")]),
            tmp_path / "report-v1",
        )

    assert list(outside_dir.iterdir()) == []


@pytest.mark.parametrize(
    "case",
    [
        "response_content",
        "infer_count",
        "chart_count",
        "single_base64",
        "decoded_total",
        "citation_count",
    ],
)
def test_build_report_bundle_enforces_hard_bounds_before_writing(tmp_path, monkeypatch, case):
    monkeypatch.setattr(bundle_module, "RESPONSE_CONTENT_MAX_BYTES", 8)
    monkeypatch.setattr(bundle_module, "INFER_MESSAGE_MAX", 1)
    monkeypatch.setattr(bundle_module, "CHART_MESSAGE_MAX", 1)
    monkeypatch.setattr(bundle_module, "BASE64_FIELD_MAX_BYTES", 8)
    monkeypatch.setattr(bundle_module, "DECODED_BINARY_TOTAL_MAX_BYTES", 3)
    monkeypatch.setattr(bundle_module, "CITATION_COUNT_MAX", 1)
    payload = _payload()
    if case == "response_content":
        payload["response_content"] = "九" * 3
    elif case == "infer_count":
        payload["infer_messages"] = [_infer("one", b"x"), _infer("two", b"x")]
    elif case == "chart_count":
        payload["chart_messages"] = [_chart("one", b"x"), _chart("two", b"x")]
    elif case == "single_base64":
        payload["chart_messages"] = [_chart("one", b"1234567")]
    elif case == "decoded_total":
        payload["infer_messages"] = [_infer("one", b"12")]
        payload["chart_messages"] = [_chart("one", b"34")]
    else:
        payload["citation_messages"]["data"] = [{}, {}]

    with pytest.raises(ValueError):
        build_report_bundle(
            payload,
            tmp_path / "report-v1",
            max_infer_messages=1,
            max_single_html_base64_bytes=8,
        )

    assert not (tmp_path / "report-v1_infer").exists()
    assert not (tmp_path / "report-v1_charts").exists()


def test_build_report_bundle_accepts_exact_hard_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle_module, "RESPONSE_CONTENT_MAX_BYTES", 8)
    monkeypatch.setattr(bundle_module, "INFER_MESSAGE_MAX", 1)
    monkeypatch.setattr(bundle_module, "CHART_MESSAGE_MAX", 1)
    monkeypatch.setattr(bundle_module, "BASE64_FIELD_MAX_BYTES", 8)
    monkeypatch.setattr(bundle_module, "DECODED_BINARY_TOTAL_MAX_BYTES", 3)
    monkeypatch.setattr(bundle_module, "CITATION_COUNT_MAX", 1)

    bundle = build_report_bundle(
        _payload(
            response_content="12345678",
            infer_messages=[_infer("one", b"1")],
            chart_messages=[_chart("one", b"23")],
            citations=[{}],
        ),
        tmp_path / "report-v1",
        max_infer_messages=1,
        max_single_html_base64_bytes=8,
    )

    assert len(bundle.inference_manifest) == 1
    assert len(bundle.chart_manifest) == 1


def test_build_report_bundle_preserves_numeric_resource_id_compatibility(tmp_path):
    bundle = build_report_bundle(
        _payload(
            response_content="[details](#inference:7) (#insertChart:8)",
            infer_messages=[{"id": 7, "html_base64": _encoded(b"html")}],
            chart_messages=[
                {"chart_id": 8, "chart_title": "Chart", "base64": _encoded(b"png")}
            ],
        ),
        tmp_path / "report-v1",
    )

    assert [item["id"] for item in bundle.inference_manifest] == ["7"]
    assert [item["id"] for item in bundle.chart_manifest] == ["8"]
    assert "report-v1_infer/inference_7.html" in bundle.markdown_text
    assert "report-v1_charts/8.png" in bundle.markdown_text


@pytest.mark.parametrize(
    ("max_infer_messages", "max_single_html_base64_bytes"),
    [(-1, 8), (1, -1), (10**100, 8), (1, 10**100), (True, 8), (1, 1.5)],
)
def test_build_report_bundle_rejects_invalid_or_unbounded_compatibility_limits(
    tmp_path, max_infer_messages, max_single_html_base64_bytes
):
    with pytest.raises(ValueError):
        build_report_bundle(
            _payload(infer_messages=[_infer("one", b"x")]),
            tmp_path / "report-v1",
            max_infer_messages=max_infer_messages,
            max_single_html_base64_bytes=max_single_html_base64_bytes,
        )


@pytest.mark.parametrize(
    "citation_field",
    [
        "nine-byte",
        {"nested": "nine-byte"},
        b"not-json-compatible",
    ],
)
def test_build_report_bundle_rejects_oversized_or_invalid_citation_fields_before_writes(
    tmp_path, monkeypatch, citation_field
):
    monkeypatch.setattr(bundle_module, "CITATION_FIELD_MAX_BYTES", 8)
    report_base = tmp_path / "report-v1"
    payload = _payload(
        infer_messages=[_infer("infer-a", b"x")],
        chart_messages=[_chart("chart-a", b"y")],
        citations=[{"content": citation_field}],
    )

    with pytest.raises(ValueError):
        build_report_bundle(payload, report_base)

    assert not (tmp_path / "report-v1_infer").exists()
    assert not (tmp_path / "report-v1_charts").exists()


def test_build_report_bundle_rejects_oversized_unknown_snapshot_field_before_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bundle_module, "FINAL_RESULT_MAX_BYTES", 128)
    report_base = tmp_path / "report-v1"
    payload = _payload(
        infer_messages=[_infer("infer-a", b"x")],
        chart_messages=[_chart("chart-a", b"y")],
    )
    payload["unknown"] = {"nested": "x" * 256}

    with pytest.raises(ValueError):
        build_report_bundle(payload, report_base)

    assert not (tmp_path / "report-v1_infer").exists()
    assert not (tmp_path / "report-v1_charts").exists()


def test_build_report_bundle_rejects_excessive_nesting_without_recursion_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bundle_module, "MAX_NESTING_DEPTH", 4)
    payload = _payload(
        infer_messages=[_infer("infer-a", b"x")],
        chart_messages=[_chart("chart-a", b"y")],
    )
    nested = {}
    payload["unknown"] = nested
    for _ in range(6):
        child = {}
        nested["child"] = child
        nested = child

    with pytest.raises(ValueError, match="nesting"):
        build_report_bundle(payload, tmp_path / "report-v1")

    assert not (tmp_path / "report-v1_infer").exists()
    assert not (tmp_path / "report-v1_charts").exists()


def test_build_report_bundle_rejects_excessive_container_items_before_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bundle_module, "MAX_CONTAINER_ITEMS", 2)
    payload = _payload(
        infer_messages=[_infer("infer-a", b"x")],
        chart_messages=[_chart("chart-a", b"y")],
    )
    payload["unknown"] = [1, 2, 3]

    with pytest.raises(ValueError, match="container"):
        build_report_bundle(payload, tmp_path / "report-v1")

    assert not (tmp_path / "report-v1_infer").exists()
    assert not (tmp_path / "report-v1_charts").exists()


def test_serialize_final_result_snapshot_enforces_exact_utf8_bound(monkeypatch):
    snapshot = {"value": "é"}
    exact = len(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    monkeypatch.setattr(bundle_module, "FINAL_RESULT_MAX_BYTES", exact)

    assert json.loads(serialize_final_result_snapshot(snapshot)) == snapshot

    monkeypatch.setattr(bundle_module, "FINAL_RESULT_MAX_BYTES", exact - 1)
    with pytest.raises(ValueError, match="snapshot"):
        serialize_final_result_snapshot(snapshot)

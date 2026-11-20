"""Wire markers for list / depth truncation in restored history records.

Before these markers, ``_sanitize_history_wire_value`` trimmed lists to
``_HISTORY_WIRE_LIST_LIMIT`` and replaced anything past
``_HISTORY_WIRE_DEPTH_LIMIT`` with ``"<truncated>"`` without telling anyone, so
a reopened session showed structured fields that no longer matched the record
on disk. The tests below pin the markers and the shapes the frontend parses
(arrays stay arrays, so its ``Array.isArray`` guards keep working).
"""

import json

from jiuwenswarm.server import wire_truncate


def _record(**fields):
    record = {
        "id": "rec-1",
        "role": "assistant",
        "event_type": "chat.tool_result",
        "content": "ok",
    }
    record.update(fields)
    return record


def _marker_keys(value):
    """Every ``truncated`` / ``*_truncated`` key anywhere in ``value``."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "truncated" or key.endswith("_truncated"):
                found.append(key)
            found.extend(_marker_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_marker_keys(item))
    return found


def _contains_depth_sentinel(value):
    if value == "<truncated>":
        return True
    if isinstance(value, dict):
        return any(_contains_depth_sentinel(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_depth_sentinel(item) for item in value)
    return False


def test_over_limit_list_keeps_array_shape_and_marks_parent():
    record = _record(raw_output={"items": [{"idx": index} for index in range(150)]})

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    items = wire["raw_output"]["items"]
    # Array.isArray-guarded frontend readers keep rendering the prefix.
    assert isinstance(items, list)
    assert len(items) == 100
    assert items[0] == {"idx": 0}
    assert wire["raw_output"]["items_truncated"] is True
    assert wire["raw_output_truncated"] is True
    assert wire["truncated"] is True


def test_list_at_limit_is_not_marked():
    record = _record(raw_output={"items": list(range(100))})

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert len(wire["raw_output"]["items"]) == 100
    assert "items_truncated" not in wire["raw_output"]
    assert "raw_output_truncated" not in wire
    assert "truncated" not in wire


def test_depth_cut_keeps_sentinel_and_marks_the_path():
    nested = {"leaf": [1, 2, 3]}
    for _ in range(12):
        nested = {"child": nested}
    record = _record(raw_output=nested)

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert _contains_depth_sentinel(wire["raw_output"])
    assert wire["raw_output_truncated"] is True
    assert wire["truncated"] is True


def test_chat_final_split_chunks_carry_metadata_markers():
    record = {
        "id": "final-1",
        "event_type": "chat.final",
        "content": "x" * (200 * 1024),
        "files": {"uploaded_images": [{"path": f"/tmp/{index}.png"} for index in range(150)]},
    }

    chunks = wire_truncate.split_history_record_for_stream(record)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk["truncated"] is True
        assert chunk["files_truncated"] is True
        assert isinstance(chunk["files"]["uploaded_images"], list)
        assert len(chunk["files"]["uploaded_images"]) == 100
        assert chunk["_part"]["record_id"] == "final-1"


def test_chat_final_narrow_band_stays_single_frame():
    # Unmarked fits the 64 KiB budget exactly, the 79 marker bytes overflow it.
    # Markers must not decide the frame count: the record is served whole and
    # unmarked instead of being sliced into N frames that repeat its metadata.
    limit = wire_truncate._HISTORY_WIRE_RECORD_MAX_BYTES
    record = {
        "id": "final-1",
        "role": "assistant",
        "event_type": "chat.final",
        "content": "",
        "files": {"uploaded_images": [{"path": f"/tmp/{index}.png"} for index in range(150)]},
    }
    fields = {key: value for key, value in record.items() if key != "content"}
    unmarked_metadata = wire_truncate._sanitize_mapping_items(fields, child_depth=0)
    # "x" is one byte per character, so padding by hand lands exactly on budget.
    record["content"] = "x" * (
        limit - wire_truncate._json_wire_size({**unmarked_metadata, "content": ""})
    )
    assert wire_truncate._json_wire_size(
        {**unmarked_metadata, "content": record["content"]}
    ) == limit
    assert wire_truncate._json_wire_size(
        {**wire_truncate._sanitize_record_fields(fields, child_depth=0), "content": record["content"]}
    ) > limit

    chunks = wire_truncate.split_history_record_for_stream(record)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert "_part" not in chunk
    assert chunk["content"] == record["content"]
    assert _marker_keys(chunk) == []
    # The list is still trimmed to 100 items; only the markers that would have
    # pushed the frame over the budget are dropped.
    assert len(chunk["files"]["uploaded_images"]) == 100
    assert wire_truncate._json_wire_size(chunk) == limit


def test_preset_true_marker_is_preserved():
    # The live rail already emits raw_output_truncated when it trims the
    # payload on the way out (stream_event_rail.py).
    record = _record(raw_output={"items": list(range(150))}, raw_output_truncated=True)

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert wire["raw_output_truncated"] is True
    assert wire["raw_output"]["items_truncated"] is True


def test_preset_false_marker_is_upgraded():
    # Tool payloads ship falsy flags like global_file_ops_truncated: false; a
    # real cut must not hide behind them.
    record = _record(
        raw_output={"items": list(range(150)), "items_truncated": False},
        raw_output_truncated=False,
    )

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert wire["raw_output"]["items_truncated"] is True
    assert wire["raw_output_truncated"] is True


def test_mcp_app_only_overflow_leaves_no_markers():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 24.5}}}}}}}
    record = {
        "event_type": "chat.tool_result",
        "tool_call_id": "call_1",
        "raw_output": {
            "result": "ok",
            "mcp_app": {
                "server": "s",
                "tool": "t",
                "toolResult": {"structuredContent": deep, "items": list(range(150))},
            },
        },
    }

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    tool_result = wire["raw_output"]["mcp_app"]["toolResult"]
    assert tool_result["structuredContent"] == deep
    assert tool_result["items"] == list(range(150))
    assert _marker_keys(wire) == []


def test_markers_never_force_collapse_of_a_record_that_fits():
    # The unmarked record is 65463 bytes; the 74 bytes of markers would push it
    # over the 64 KiB budget and collapse it, dropping the MCP App block. The
    # unmarked form is served instead, so the block survives at the cost of the
    # markers in this narrow band.
    record = {
        "event_type": "chat.tool_result",
        "id": "r",
        "raw_output": {"items": [1] * 150, "mcp_app": {"blob": "z" * 65066}},
    }

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert wire["raw_output"]["mcp_app"]["blob"] == "z" * 65066
    assert len(wire["raw_output"]["items"]) == 100
    assert "truncated" not in wire
    assert "raw_output_truncated" not in wire
    assert wire_truncate._json_wire_size(wire) <= wire_truncate._HISTORY_WIRE_RECORD_MAX_BYTES


def test_markers_never_force_collapse_without_mcp_app():
    limit = wire_truncate._HISTORY_WIRE_RECORD_MAX_BYTES
    probe = {
        "event_type": "chat.tool_result",
        "id": "r",
        "raw_output": {"items": [1] * 150, "pad": [], "note": ""},
    }
    # Bulk padding stays under the 100-item cap; the note tunes the last bytes.
    probe["raw_output"]["pad"] = ["x"] * 100
    one_char = wire_truncate._json_wire_size(wire_truncate._sanitize_history_wire_value(probe))
    length = 1 + (limit - one_char) // 100  # 100 items grow 100 bytes per char
    probe["raw_output"]["pad"] = ["x" * length] * 100
    current = wire_truncate._json_wire_size(wire_truncate._sanitize_history_wire_value(probe))
    probe["raw_output"]["note"] = "y" * (limit - current)
    # Unmarked lands exactly on the budget; the markers would overflow it.
    assert wire_truncate._json_wire_size(
        wire_truncate._sanitize_history_wire_value(probe)
    ) == limit
    assert wire_truncate._json_wire_size(
        wire_truncate._sanitize_record_fields(probe, child_depth=1)
    ) > limit

    wire = wire_truncate._sanitize_history_record_for_wire(probe)

    assert "raw_output" in wire
    assert wire["raw_output"]["pad"] == probe["raw_output"]["pad"]
    assert wire["raw_output"]["note"] == probe["raw_output"]["note"]
    assert "truncated" not in wire
    assert wire_truncate._json_wire_size(wire) == limit


def test_record_over_budget_without_markers_still_collapses():
    record = {
        "event_type": "chat.tool_result",
        "id": "r",
        "raw_output": {"items": [1] * 150, "mcp_app": {"blob": "z" * 65145}},
    }

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert wire["truncated"] is True
    assert "raw_output" not in wire


def test_oversized_marked_record_still_collapses_within_budget():
    record = _record(raw_output={"items": ["x" * 1024 for _ in range(150)]})

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert wire["truncated"] is True
    assert "raw_output" not in wire
    size = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
    assert size <= wire_truncate._HISTORY_WIRE_RECORD_MAX_BYTES


def test_string_truncation_keeps_suffix_without_record_flag():
    record = _record(content="y" * 40_000)

    wire = wire_truncate._sanitize_history_record_for_wire(record)

    assert wire["content"].endswith("[truncated]")
    assert "truncated" not in wire

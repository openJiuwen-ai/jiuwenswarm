# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict NDJSON codec for one one-shot Process CLI execution."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TypeAlias

from jiuwenswarm.channels.process_cli.protocol.model import (
    OneShotEvent,
    OneShotRunResult,
)


OneShotRecord: TypeAlias = OneShotEvent | OneShotRunResult


def _materialize_records(
    records: Iterable[OneShotRecord],
) -> tuple[OneShotRecord, ...]:
    if isinstance(records, (str, bytes)):
        raise TypeError("records must be an iterable of one-shot records")
    try:
        materialized = tuple(records)
    except TypeError as exc:
        raise TypeError("records must be an iterable of one-shot records") from exc
    if not materialized:
        raise ValueError("record stream must contain a terminal result")
    for index, record in enumerate(materialized):
        if not isinstance(record, (OneShotEvent, OneShotRunResult)):
            raise TypeError(f"records[{index}] has an unsupported record type")
    return materialized


def _validate_record_identity(records: tuple[OneShotRecord, ...]) -> None:
    first = records[0]
    observed_session_id: str | None = None
    for index, record in enumerate(records):
        if record.sequence != index:
            raise ValueError("record sequence must start at zero and be contiguous")
        if record.request_id != first.request_id:
            raise ValueError("all records must use the same request_id")
        if record.schema_version != first.schema_version:
            raise ValueError("all records must use the same schema_version")
        if record.session_id is None:
            continue
        if observed_session_id is None:
            observed_session_id = record.session_id
        elif record.session_id != observed_session_id:
            raise ValueError("all non-null session_id values must match")

    result = records[-1]
    if observed_session_id is not None and result.session_id != observed_session_id:
        raise ValueError("terminal result must carry the resolved session_id")


def validate_one_shot_records(
    records: Iterable[OneShotRecord],
) -> tuple[OneShotRecord, ...]:
    """Validate ordered events followed by exactly one terminal result."""

    materialized = _materialize_records(records)

    result_indexes = [
        index
        for index, record in enumerate(materialized)
        if isinstance(record, OneShotRunResult)
    ]
    if result_indexes != [len(materialized) - 1]:
        raise ValueError("record stream must end with exactly one result")
    _validate_record_identity(materialized)
    return materialized


def encode_jsonl(records: Iterable[OneShotRecord]) -> str:
    """Encode one complete, validated NDJSON document.

    The command output path uses :func:`encode_jsonl_record` for immediate
    event delivery. This aggregate helper is for complete-document validation,
    tests, and SDK consumers that already hold all records.
    """

    validated = validate_one_shot_records(records)
    return "".join(encode_jsonl_record(record) for record in validated)


def encode_jsonl_record(record: OneShotRecord) -> str:
    """Encode one record for immediate write-and-flush by a one-shot process."""

    if not isinstance(record, (OneShotEvent, OneShotRunResult)):
        raise TypeError("record has an unsupported record type")
    return (
        json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def decode_jsonl(document: str) -> tuple[OneShotRecord, ...]:
    """Decode and validate one complete NDJSON document."""

    if not isinstance(document, str):
        raise TypeError("document must be a string")
    lines = document.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError("document must contain non-empty JSON records")

    records: list[OneShotRecord] = []
    for index, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"record {index} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise TypeError(f"record {index} must be a JSON object")
        record_type = raw.get("type")
        if record_type == "event":
            records.append(OneShotEvent.from_dict(raw))
        elif record_type == "result":
            records.append(OneShotRunResult.from_dict(raw))
        else:
            raise ValueError(f"record {index} has unsupported type: {record_type}")
    return validate_one_shot_records(records)


__all__ = [
    "OneShotRecord",
    "decode_jsonl",
    "encode_jsonl",
    "encode_jsonl_record",
    "validate_one_shot_records",
]

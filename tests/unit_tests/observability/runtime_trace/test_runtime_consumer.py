# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the on_end consumer."""

from __future__ import annotations

from dataclasses import dataclass

from jiuwenswarm.runtime_trace.consumer import RuntimeRecordSink


@dataclass
class _Record:
    span_id: str

    @property
    def raw_json(self) -> bytes:
        raise AssertionError("consumer must not encode the payload")

    @property
    def sequences(self):
        raise AssertionError("consumer must not inspect sequences")


class _Kinds:
    def __init__(self, value):
        self.value = value
        self.taken = []

    def take(self, span_id):
        self.taken.append(span_id)
        return self.value


class _Writer:
    def __init__(self):
        self.offers = []
        self.accepting = True

    def offer(self, item, *, level):
        if not self.accepting:
            return
        self.offers.append((item, level))


def test_consumer_only_reads_scalar_fields() -> None:
    writer = _Writer()
    sink = RuntimeRecordSink(writer, _Kinds(("terminal", "worker.exited", "success")))
    sink.consume(_Record("abc"))
    item, level = writer.offers[0]
    assert level == "terminal"
    assert item.name == "worker.exited"
    assert item.outcome == "success"
    assert item.record.span_id == "abc"


def test_consumer_swallows_failures() -> None:
    class _Broken:
        def take(self, span_id):
            raise RuntimeError("boom")

    sink = RuntimeRecordSink(_Writer(), _Broken())
    sink.consume(_Record("abc"))  # no exception

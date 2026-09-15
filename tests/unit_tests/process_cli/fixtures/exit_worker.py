# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess fault injector at the public client boundary, not a real Agent."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import types


def note(stage: str) -> None:
    print(f"EXIT_TEST:{stage}", file=sys.stderr, flush=True)


class FakeClient:
    def __init__(self) -> None:
        self.scenario = os.environ.get("EXIT_TEST_SCENARIO", "normal")

    async def start(self) -> None:
        note("start")
        if self.scenario == "startup_failure":
            raise SystemExit(0)
        if self.scenario == "shutdown_failure":
            asyncio.create_task(self.background())
            await asyncio.sleep(0)

    async def background(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            note("asyncio-shutdown")
            raise RuntimeError("injected shutdown failure")

    def resolve_mode_capability(self, requested: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(mode=requested, work_mode="code")

    async def create_or_resume_session(self, *, channel_id, session_id):
        assert channel_id == "process_cli"
        assert session_id is None
        note("session")
        return "exit-test-session"

    async def stream(self, request):
        from jiuwenswarm.runtime.events import RuntimeEvent

        def event(kind: str, **payload):
            return RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={"event_type": kind, **payload},
                is_complete=kind == "chat.final",
            )

        try:
            note("stream")
            if self.scenario == "runtime_failure":
                yield event("chat.error", code="MODEL_FAILED", message="failed")
                await asyncio.Event().wait()
            elif self.scenario == "stream_exit":
                raise SystemExit(0)
            elif self.scenario in {"timeout", "signal", "broken_output"}:
                yield event("chat.delta", delta="active")
                while True:
                    await asyncio.sleep(0.02)
                    yield event("chat.delta", delta="active")
            elif self.scenario == "interaction_eof":
                yield event(
                    "chat.ask_user_question",
                    request_id="card",
                    source="ask_user_interrupt",
                    questions=[{"question": "Approve?", "options": ["yes", "no"]}],
                )
                await asyncio.Event().wait()
            else:
                yield event("chat.final", content="exit fixture completed")
        finally:
            note("stream-closed")

    async def cancel(self, request) -> None:
        assert request.channel_id == "process_cli"
        assert request.session_id == "exit-test-session"
        note("cancel")

    async def cleanup_session(self, *, channel_id, session_id):
        assert channel_id == "process_cli"
        assert session_id == "exit-test-session"
        note("session-cleaned")
        return True

    async def close(self) -> None:
        note("closing")
        if self.scenario in {"signal", "signal_cleanup"}:
            await asyncio.sleep(0.2)
        if self.scenario == "cleanup_failure":
            raise SystemExit(0)
        note("runtime-closed")


def main() -> None:
    module = types.ModuleType("jiuwenswarm.channels.process_cli.client")
    module.InProcessRuntimeClient = FakeClient
    sys.modules[module.__name__] = module
    from jiuwenswarm.channels.process_cli import machine_entry
    from jiuwenswarm.channels.process_cli.duplex_input import DuplexLineReader

    original_line = DuplexLineReader.read_line
    original_document = DuplexLineReader.read_document

    async def read_line(self):
        note("reading")
        return await original_line(self)

    async def read_document(self):
        note("reading")
        return await original_document(self)

    DuplexLineReader.read_line = read_line
    DuplexLineReader.read_document = read_document
    if os.environ.get("EXIT_TEST_SELF_SIGNAL"):
        from jiuwenswarm.channels.process_cli.machine_signals import CommandSignals

        original_run = CommandSignals.run

        async def self_signal_run(self, operation):
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, signal.raise_signal, signal.SIGTERM)
            return await original_run(self, operation)

        CommandSignals.run = self_signal_run
    source = "-"
    json_lines = "--run-jsonl" in sys.argv
    raise SystemExit(machine_entry.execute_source(source, json_lines=json_lines))


if __name__ == "__main__":
    main()

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime Worker entrypoint. Front reaches this process only through IPC."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal


def _activate_managed_external_cli_paths() -> None:
    """Expose managed external CLI SDK paths before runtime modules load.

    The frozen app installs the optional external CLI SDKs (``claude_agent_sdk``,
    ``openai_codex``) outside the bundle, and every process importing them must
    append their site-packages directories to ``sys.path`` first. Agent turns,
    including team members backed by those SDKs, execute in this process. The
    call never raises, so an optional SDK problem cannot block worker startup.
    """
    try:
        from jiuwenswarm.common.external_cli_runtime import (
            activate_external_cli_runtime_paths,
        )

        activate_external_cli_runtime_paths()
    except Exception:  # noqa: BLE001 - optional capability must not block startup
        logging.getLogger(__name__).warning(
            "Managed external CLI runtime activation failed at worker startup",
            exc_info=True,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from jiuwenswarm.server.worker.diagnostics import enable_worker_diagnostics

    enable_worker_diagnostics()
    parser = argparse.ArgumentParser(prog="jiuwenswarm-runtime-worker")
    parser.add_argument(
        "--socket", required=True, help="Unix socket or named-pipe path."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18092)
    parser.add_argument(
        "--stub", action="store_true", help="Speak IPC without loading Runtime."
    )
    parser.add_argument("--stub-ready-delay", type=float, default=0.0)
    parser.add_argument("--stub-exit-on-request", action="store_true")
    args = parser.parse_args()
    if args.stub:
        from jiuwenswarm.server.worker.stub import StubExit, serve_stub

        try:
            asyncio.run(
                serve_stub(
                    args.socket,
                    ready_delay=args.stub_ready_delay,
                    exit_on_request=args.stub_exit_on_request,
                )
            )
        except StubExit:
            raise SystemExit(1) from None
        return

    _activate_managed_external_cli_paths()
    from jiuwenswarm.server.worker.service import WorkerForceExit, serve_worker

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(serve_worker(args.socket, host=args.host, port=args.port))

    def _stop() -> None:
        task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, OSError):
        pass
    try:
        loop.run_until_complete(task)
    except WorkerForceExit:
        logging.getLogger(__name__).exception(
            "[Worker] fatal exit: Runtime dispatch did not stop"
        )
        raise SystemExit(1) from None
    finally:
        loop.close()


if __name__ == "__main__":
    main()

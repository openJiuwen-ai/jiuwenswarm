# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime Worker entrypoint. Front reaches this process only through IPC."""

from __future__ import annotations

import argparse
import asyncio
import logging
from os import _exit as exit_process
import signal


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
    parser.add_argument("--identity-from-parent", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    code_identity_value = None
    if args.identity_from_parent:
        from jiuwenswarm.server.supervisor.identity import read_launch_identity

        code_identity_value = read_launch_identity()
    if args.stub:
        from jiuwenswarm.server.worker.stub import StubExit, serve_stub

        try:
            asyncio.run(
                serve_stub(
                    args.socket,
                    ready_delay=args.stub_ready_delay,
                    exit_on_request=args.stub_exit_on_request,
                    code_identity_value=code_identity_value,
                )
            )
        except StubExit:
            raise SystemExit(1) from None
        return

    from jiuwenswarm.server.worker.service import WorkerForceExit, serve_worker

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(serve_worker(args.socket, host=args.host, port=args.port,
                                       code_identity_value=code_identity_value))

    def _stop() -> None:
        task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, OSError):
        pass
    try:
        loop.run_until_complete(task)
    except WorkerForceExit as exc:
        logging.getLogger(__name__).exception("[Worker] fatal exit with unfinished shutdown steps: %s", exc)
        logging.shutdown()
        # A cancelled to_thread load may still own non-daemon executor threads.
        # SystemExit waits for those threads and can exceed the shared deadline.
        exit_process(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()

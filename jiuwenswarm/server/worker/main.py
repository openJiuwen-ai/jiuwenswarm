# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime Worker entrypoint. Front reaches this process only through IPC."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
import logging
from os import _exit as exit_process
import signal
import sys


def _install_stop_handlers(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> Callable[[], None]:
    """Turn console signals into one cancellation while keeping cleanup alive."""
    previous = {}
    loop_signals = set()
    stopping = False
    console_interrupted = False

    def stop() -> None:
        nonlocal stopping
        if not stopping and not task.done():
            stopping = True
            task.cancel()

    def console_signal(_signum: int, _frame: object) -> None:
        nonlocal console_interrupted
        console_interrupted = True
        if not loop.is_closed():
            loop.call_soon_threadsafe(stop)

    def restore() -> None:
        for signum, handler in previous.items():
            if signum in loop_signals and not loop.is_closed():
                loop.remove_signal_handler(signum)
            if console_interrupted and sys.platform == "win32" and signum == signal.SIGINT:
                # This process is exiting. Keep repeated Ctrl+C ignored during
                # interpreter teardown, after the event loop no longer exists.
                handler = signal.SIG_IGN
            signal.signal(signum, handler)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            try:
                loop.add_signal_handler(signum, stop)
            except (NotImplementedError, OSError):
                # Windows event loops have no add_signal_handler support.
                signal.signal(signum, console_signal)
            else:
                loop_signals.add(signum)
    except BaseException:
        restore()
        raise
    return restore


async def _wait_for_worker(task: asyncio.Task) -> None:
    await task


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
    from jiuwenswarm.common.background_logging import (
        enable_background_logging,
        stop_background_logging,
    )

    enable_background_logging()
    from jiuwenswarm.server.worker.diagnostics import enable_worker_diagnostics

    enable_worker_diagnostics()
    parser = argparse.ArgumentParser(prog="jiuwenswarm-runtime-worker")
    parser.add_argument(
        "--socket", help="Unix socket or named-pipe path (resident mode)."
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
    from jiuwenswarm.server.ipc.session import FRONT_LOOPBACK, runtime_ipc_mode

    mode = runtime_ipc_mode()
    if mode == FRONT_LOOPBACK and not args.identity_from_parent:
        parser.error("front_loopback requires a private parent launch payload")
    if mode != FRONT_LOOPBACK and not args.socket:
        parser.error("resident mode requires --socket")
    code_identity_value = None
    session_source = None
    if args.identity_from_parent:
        from jiuwenswarm.server.supervisor.identity import data_scope, read_launch_payload

        payload = read_launch_payload()
        code_identity_value = payload["code_identity"]
        if mode == FRONT_LOOPBACK:
            from jiuwenswarm.server.ipc.loopback import FrontConnectSessionSource, LoopbackParameters

            session_source = FrontConnectSessionSource(
                LoopbackParameters.from_payload(
                    payload.pop("runtime_ipc", None), code_identity=code_identity_value, data_scope=data_scope(),
                ),
            )
        elif payload.get("runtime_ipc") is not None:
            parser.error("runtime IPC mode does not match the parent launch payload")
    if args.stub:
        from jiuwenswarm.server.worker.stub import StubExit, serve_stub

        exit_code = 1
        try:
            asyncio.run(
                serve_stub(
                    args.socket or "",
                    ready_delay=args.stub_ready_delay,
                    exit_on_request=args.stub_exit_on_request,
                    code_identity_value=code_identity_value,
                    session_source=session_source,
                )
            )
            exit_code = 0
        except StubExit:
            raise SystemExit(1) from None
        finally:
            if not stop_background_logging():
                exit_process(exit_code)
        return

    _activate_managed_external_cli_paths()
    from jiuwenswarm.server.worker.service import WorkerForceExit, serve_worker

    runner = asyncio.Runner()
    loop = runner.get_loop()
    task = loop.create_task(serve_worker(args.socket or "", host=args.host, port=args.port,
                                       code_identity_value=code_identity_value,
                                       session_source=session_source))
    restore_handlers: Callable[[], None] | None = None
    exit_code = 1
    try:
        restore_handlers = _install_stop_handlers(loop, task)
        runner.run(_wait_for_worker(task))
        exit_code = 0
    except asyncio.CancelledError:
        # A signal may arrive before serve_worker enters its cancellation guard.
        exit_code = 0
    except WorkerForceExit as exc:
        logging.getLogger(__name__).exception("[Worker] fatal exit with unfinished shutdown steps: %s", exc)
        if stop_background_logging():
            logging.shutdown()
        # A cancelled to_thread load may still own non-daemon executor threads.
        # SystemExit waits for those threads and can exceed the shared deadline.
        exit_process(1)
    finally:
        try:
            # Also drain SDK-owned tasks, async generators and the executor.
            # Closing the loop directly abandons their asynchronous cleanup.
            runner.close()
        finally:
            try:
                # logging.shutdown acquires every sink's lock at interpreter exit.
                # A stuck disk must not make an otherwise drained Worker immortal.
                if not stop_background_logging():
                    exit_process(exit_code)
            finally:
                if restore_handlers is not None:
                    restore_handlers()


if __name__ == "__main__":
    main()

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A bounded child owns the real logging lock and forces the retiring race."""

import io
import faulthandler
import logging
import logging.config
import os
from pathlib import Path
import sys
import threading

from jiuwenswarm.common.background_logging import BackgroundLogWriter


def main():
    directory = Path(sys.argv[1])
    configuration = sys.argv[2]
    waiting = threading.Event()
    cleanup_finished = threading.Event()
    original_lock = logging._lock

    class ObservedLock:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "worker-log-writer":
                waiting.set()
            return original_lock.acquire(*args, **kwargs)

        def release(self):
            original_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()

        def __getattr__(self, name):
            return getattr(original_lock, name)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    old_sink = logging.FileHandler(directory / "before.log", encoding="utf-8")
    old_sink.set_name("active")
    root.handlers = [old_sink]
    class ObservedWriter(BackgroundLogWriter):
        def _close_retired(self):
            with self._lock:
                closes_old_sink = old_sink in self._retired
            super()._close_retired()
            if closes_old_sink:
                cleanup_finished.set()

    writer = ObservedWriter()
    writer.wrap(root)
    root.info("before configuration")
    writer.queue.join()
    proxy = root.handlers[0]
    logging._lock = ObservedLock()
    try:
        # dictConfig/fileConfig use this same module lock. The event observes
        # the writer's actual lock request, without sleeps or replacing close().
        with logging._lock:
            proxy.close()
            assert waiting.wait(3), "background cleanup never reached the logging lock"
            if configuration == "dict":
                logging.config.dictConfig({
                    "version": 1, "disable_existing_loggers": False,
                    "handlers": {"active": {"class": "logging.FileHandler",
                                              "filename": str(directory / "after.log"), "encoding": "utf-8"}},
                    "root": {"level": "INFO", "handlers": ["active"]},
                })
            elif configuration == "file":
                logging.config.fileConfig(io.StringIO(
                    "[loggers]\nkeys=root\n[handlers]\nkeys=active\n[formatters]\nkeys=\n"
                    "[logger_root]\nlevel=INFO\nhandlers=active\n"
                    "[handler_active]\nclass=FileHandler\nlevel=INFO\n"
                    f"args=({str(directory / 'after.log')!r}, 'a', 'utf-8')\n"
                ), disable_existing_loggers=False)
            else:
                if configuration == "shutdown":
                    logging.shutdown()
                logging.basicConfig(filename=directory / "after.log", encoding="utf-8", force=True,
                                    level=logging.INFO, format="%(message)s")
                root.handlers[0].set_name("active")
            new_sink = root.handlers[0]
        # The old handler's final close must not unregister the replacement
        # which reused its name. Keep both old/new sinks strongly referenced.
        assert cleanup_finished.wait(3), "old sink retirement did not finish"
        assert logging._handlers.get("active") is new_sink
        writer.wrap(root)
        root.info("after configuration")
        assert writer.stop(timeout=3), "retired sink cleanup did not finish"
        assert logging._handlers.get("active") is None  # final stop closes the new sink too
        assert old_sink.stream is None and new_sink.stream is None
        print("reconfiguration completed", flush=True)
    finally:
        logging._lock = original_lock


if __name__ == "__main__":
    def expired():
        faulthandler.dump_traceback()
        os._exit(74)

    watchdog = threading.Timer(8, expired)
    watchdog.daemon = True
    watchdog.start()
    main()
    watchdog.cancel()

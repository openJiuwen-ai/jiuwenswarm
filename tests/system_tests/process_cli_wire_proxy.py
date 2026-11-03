# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Record real CLI stdio byte streams without emulating any SDK/Runtime behavior."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid


def main() -> int:
    trace = Path(sys.argv[1]) / uuid.uuid4().hex
    trace.mkdir(parents=True)
    child = subprocess.Popen(
        sys.argv[2:],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    (trace / "process.json").write_text(
        json.dumps({"argv": sys.argv[2:], "pid": child.pid}), encoding="utf-8"
    )

    def relay(source, target, name, close=False):
        try:
            with (trace / name).open("wb") as log:
                while True:
                    chunk = os.read(source.fileno(), 8192)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                    target.write(chunk)
                    target.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            if close:
                try:
                    target.close()
                except OSError:
                    pass

    threads = [
        threading.Thread(
            target=relay,
            args=(sys.stdin.buffer, child.stdin, "stdin.jsonl", True),
            daemon=True,
        ),
        threading.Thread(
            target=relay, args=(child.stdout, sys.stdout.buffer, "stdout.jsonl")
        ),
        threading.Thread(
            target=relay, args=(child.stderr, sys.stderr.buffer, "stderr.txt")
        ),
    ]
    for thread in threads:
        thread.start()
    code = child.wait()
    for thread in threads[1:]:
        thread.join()
    (trace / "exit.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

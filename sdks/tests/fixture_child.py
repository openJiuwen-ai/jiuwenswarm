# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real pipe/process adversary; NOT a substitute for real Runtime E2E."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = os.environ.get("SDK_FIXTURE", "success")
query = "--query-json" in sys.argv
request = json.load(sys.stdin) if query else json.loads(sys.stdin.buffer.readline())
request_id = request["request_id"]
sequence = 0
session_id = None if query else "owned-session"


def emit(kind, **payload):
    global sequence
    record = {
        "schema_version": "0.1",
        "type": kind,
        "sequence": sequence,
        "request_id": request_id,
        "session_id": session_id,
        **payload,
    }
    if mode == "bad_id":
        record["request_id"] = "wrong"
    if mode == "bad_version":
        record["schema_version"] = "2.0"
    if mode == "bad_sequence":
        record["sequence"] = True
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    if mode == "partial":
        data = data[:-1]
    if mode == "bad_utf8":
        data = data.replace(b"hello", b"\xff")
    if mode == "duplicate":
        data = data.replace(
            b'"schema_version": "0.1"', b'"schema_version":"0.1","schema_version":"0.1"'
        )
    if mode == "overflow_number":
        data = data.replace(b'"usage": {}', b'"usage":{"tokens":1e999}')
    if mode == "split_utf8":
        for value in data:
            os.write(1, bytes([value]))
    else:
        os.write(1, data)
    sequence += 1


def mark():
    target = os.environ.get("SDK_MARKER")
    if target:
        Path(target).write_text(str(os.getpid()), encoding="utf-8")


def finish(code=0):
    status = {
        0: "completed",
        1: "failed",
        2: "failed",
        124: "timed_out",
        130: "cancelled",
    }[code]
    common = {
        "status": status,
        "exit_code": code,
        "error": None if code == 0 else {"code": "TEST_ERROR", "message": "safe"},
    }
    if query:
        emit(
            "query_result",
            operation=request["operation"],
            data={"fixture": True, "pid": os.getpid()},
            **common,
        )
    else:
        emit("result", output="hello 中文 😀", usage={}, **common)


def main():
    if mode == "empty":
        return 0
    if mode == "stubborn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(os.environ["SDK_MARKER"]).write_text(
            json.dumps([os.getpid(), child.pid]), encoding="utf-8"
        )
        time.sleep(120)
        return 0
    if mode == "oversize":
        os.write(1, b"x" * 5000 + b"\n")
        return 1
    if mode == "stderr":
        for _ in range(256):
            os.write(2, b"diagnostics" * 1024)
    if mode in ("ask", "two_questions", "wait", "callback_wait"):
        count = 2 if mode == "two_questions" else 1
        for index in range(count):
            emit(
                "event",
                event_type="interaction.requested",
                payload={
                    "interaction_id": f"opaque-{index}",
                    "interaction": {"question": "answer"},
                },
            )
            control = json.loads(sys.stdin.buffer.readline())
            if control["request_id"] != request_id:
                raise ValueError("request identity mismatch")
            if control["type"] == "cancel":
                mark()
                finish(130)
                return 130
            if control["interaction_id"] != f"opaque-{index}":
                raise ValueError("interaction identity mismatch")
            if control["session_id"] != session_id:
                raise ValueError("Session identity mismatch")
            if control["answers"][0]["selected_options"] != ["yes"]:
                raise ValueError("unexpected interaction answer")
    if mode == "error":
        finish(1)
        return 1
    if mode == "delayed_exit":
        finish()
        sys.stdout.close()
        time.sleep(0.2)
        mark()
        return 0
    mark()
    finish()
    if mode == "extra":
        finish()
    if mode == "wrong_exit":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

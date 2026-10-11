# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run concurrent, independent metadata writers against the deployed file lock."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


_WRITER = '''
import json, os, sys, time
from jiuwenswarm.server.runtime.session import session_metadata as sm
role = sys.argv[1]
sm.get_session_metadata('same-session')
print('ready', flush=True)
sys.stdin.readline()
started = time.perf_counter_ns()
for index in range(64):
    if role == 'worker':
        sm.update_session_metadata(session_id='same-session', increment_message_count=True, sync=True)
    else:
        sm.patch_session_metadata('same-session', {'accent_color': 'blue' if index % 2 else 'red',
            'title': 'front-' + str(index), 'pinned': bool(index % 2), 'pin_order': index})
    time.sleep(.001)
assert sm.flush_pending_writes(10)
print(json.dumps({'role': role, 'pid': os.getpid(), 'started_at_ns': started,
                  'finished_at_ns': time.perf_counter_ns()}), flush=True)
'''


def run(output: Path):
    code = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-metadata-") as root:
        data = Path(root) / "data"
        session = data / "agent" / "sessions" / "same-session"
        session.mkdir(parents=True)
        metadata = session / "metadata.json"
        metadata.write_text(json.dumps({"session_id": "same-session", "title": "initial",
                                       "mode": "agent", "message_count": 0}), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(code), "JIUWENSWARM_DATA_DIR": str(data),
               "JIUWENSWARM_HOME": str(Path(root) / "home")}
        processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", _WRITER, role],
                     cwd=code, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, encoding="utf-8") for role in ("worker", "front")]
        try:
            for process in processes:
                assert process.stdout.readline().strip() == "ready"
            for process in processes:
                process.stdin.write("start\n")
                process.stdin.flush()
            writers = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=25)
                assert process.returncode == 0, stderr
                writers.append(json.loads(stdout.strip().splitlines()[-1]))
            actual = json.loads(metadata.read_text(encoding="utf-8"))
            expected = {"message_count": 64, "accent_color": "blue", "title": "front-63",
                        "pinned": True, "pin_order": 63}
            assert {key: actual.get(key) for key in expected} == expected, actual
            overlap = min(w["finished_at_ns"] for w in writers) - max(w["started_at_ns"] for w in writers)
            assert overlap > 0, "writers did not overlap"
            result = {"writers": writers, "overlap_ms": overlap / 1e6, "final_fields": expected,
                      "all_fields_preserved": True, "fixture": "two-native-Python-writers"}
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().output)))

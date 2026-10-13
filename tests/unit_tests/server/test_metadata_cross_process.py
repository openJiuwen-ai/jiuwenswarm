# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real independent writers must preserve fields and their queued revisions."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_front_fields_survive_worker_cache_and_queued_updates(tmp_path):
    env = os.environ.copy()
    env["JIUWENSWARM_HOME"] = str(tmp_path / "home")
    env["JIUWENSWARM_DATA_DIR"] = str(tmp_path / "data")
    worker_code = """
import json, sys
from jiuwenswarm.server.runtime.session import session_metadata as sm
sm.init_session_metadata(session_id='same-session', title='initial')
sm.update_session_metadata(session_id='same-session', accent_color='default', sync=True)
sm.get_session_metadata('same-session')
print('ready', flush=True)
sys.stdin.readline()
for _ in range(32):
    sm.update_session_metadata(session_id='same-session', increment_message_count=True)
assert sm.flush_pending_writes(10)
value=sm.get_session_metadata('same-session', cache_bust=True)
print(json.dumps({key:value.get(key) for key in ('accent_color','message_count','title')}), flush=True)
"""
    worker = subprocess.Popen([sys.executable, "-X", "utf8", "-c", worker_code], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        assert worker.stdout.readline().strip() == "ready"
        front_code = """
from jiuwenswarm.server.runtime.session.session_metadata import patch_session_metadata
patch_session_metadata('same-session', {'accent_color':'blue', 'title':'renamed by front'})
"""
        front = subprocess.run([sys.executable, "-X", "utf8", "-c", front_code], env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert front.returncode == 0, front.stderr
        out, err = worker.communicate("continue\n", timeout=20)
        assert worker.returncode == 0, err
        result = json.loads(out.strip().splitlines()[-1])
        assert result == {"accent_color": "blue", "message_count": 32, "title": "renamed by front"}
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate()

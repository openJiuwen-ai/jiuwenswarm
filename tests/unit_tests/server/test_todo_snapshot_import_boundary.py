# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front history restore must remain independent of SDK schema imports."""

import os
from pathlib import Path
import subprocess
import sys


def test_front_todo_restore_works_when_sdk_imports_are_forbidden(tmp_path):
    code = """
import importlib.abc
import sys
from enum import Enum
class NoSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openjiuwen' or fullname.startswith('openjiuwen.'):
            raise AssertionError('Front imported SDK: ' + fullname)
sys.meta_path.insert(0, NoSdk())
from jiuwenswarm.common.todo_snapshot import format_todos_for_frontend, load_todo_snapshot_for_frontend
class Status(Enum):
    RUNNING = 'running'
from types import SimpleNamespace
items = [dict(id='a', content='one', status='waiting'),
         SimpleNamespace(id='b', content='two', status=Status.RUNNING),
         dict(id='c', status='deleted'), dict(id='d', status='canceled')]
assert [i['status'] for i in format_todos_for_frontend(items)] == ['pending','in_progress','cancelled']
assert load_todo_snapshot_for_frontend('missing-session') == []
assert not any(name.startswith('openjiuwen') for name in sys.modules)
"""
    environment = dict(os.environ, JIUWENSWARM_HOME=str(tmp_path),
                       PYTHONPATH=str(Path(__file__).parents[3]), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

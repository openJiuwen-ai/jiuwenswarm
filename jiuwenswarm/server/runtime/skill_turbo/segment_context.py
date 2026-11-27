"""单次段执行的控制状态与原始内容快照；不存放进程级业务状态。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec


def data_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def execution_snapshot(skill_root: str) -> dict:
    root = Path(skill_root).resolve(strict=True)
    sources = []
    excluded = {'.git', '__pycache__', '.pytest_cache', '.venv', 'node_modules', 'skill_runs'}
    def unreadable(error):
        raise error

    for directory, directories, files in os.walk(root, onerror=unreadable):
        directories[:] = [name for name in directories if name not in excluded]
        if any((Path(directory) / name).is_symlink() for name in directories):
            raise ValueError('execution_source_symlink_directory')
        for filename in files:
            path = Path(directory) / filename
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError('execution_source_outside_skill')
            # SDK把字典键中的点解析为路径；文件名必须放在值内，不能作状态键。
            sources.append({'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    sources.sort(key=lambda item: item['path'])
    if not sources:
        raise ValueError('execution_sources_missing')
    return {'skill_root': str(root), 'files': sources, 'fingerprint': data_fingerprint(sources)}


def public_segment_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: public_segment_output(child) for key, child in value.items()
                if key not in {'__segment_handoff__', '__artifact__'}}
    if isinstance(value, list):
        return [public_segment_output(child) for child in value]
    return value


@dataclass
class SegmentContext:
    skill: str
    accelerator: str
    task_key: str
    spec: AcceleratorSpec
    namespace: str
    control: dict
    initial_inputs: dict
    executed: bool = False
    completed: bool = False
    root_output: dict = field(default_factory=dict)
    handoff: Any = None
    child_statuses: dict[str, str] = field(default_factory=dict)
    answered_tools: list[dict] = field(default_factory=list)
    error: str = ''
    outputs: dict = field(default_factory=dict)
    fallback_count: int = 0
    send_calls: list[dict] = field(default_factory=list)

    def capture_root(self, output: Any) -> Any:
        if not isinstance(output, dict):
            return output
        handoff = output.pop('__segment_handoff__', None)
        if handoff is not None:
            self.handoff = copy.deepcopy(handoff)
        if output.get('status') in {'ok', 'partial', 'failed', 'error', 'blocked', 'not_applicable'} or (
            any(key in output for key in ('facts', 'success', 'error')) or handoff is not None
        ):
            self.root_output = copy.deepcopy(output)
        return output

    def capture_child(self, node, output: Any) -> None:
        if isinstance(output, dict):
            output.pop('__segment_handoff__', None)
            if output.get('resume_skip') or output.get('event_type'):
                return
            status = output.get('status', 'ok')
            if output.get('success') is False or output.get('error') or status == 'error':
                status = 'failed'
            self.child_statuses[node.plan_name] = status
        elif isinstance(output, BaseException):
            self.child_statuses[node.plan_name] = 'failed'

    def checkpoint(self, inputs: dict, *, task_states: list, pending_tool_call_id: str) -> dict:
        value = copy.deepcopy(self.control)
        value['initial_inputs'] = copy.deepcopy(self.initial_inputs)
        value['inputs_fingerprint'] = data_fingerprint(inputs)
        value['task_states_fingerprint'] = data_fingerprint(task_states)
        value['pending_tool_call_id'] = pending_tool_call_id
        value['child_statuses'] = [{'node': node, 'status': status} for node, status in self.child_statuses.items()]
        value['answered_tools'] = copy.deepcopy(self.answered_tools)
        value['control_fingerprint'] = data_fingerprint(value)
        return value

    def capture_repair(self, node, result: Any) -> None:
        if not isinstance(result, dict) or result.get('status') not in {'ok', 'partial', 'completed'} or result.get('success') is False:
            return
        result = copy.deepcopy(result)
        if result['status'] == 'completed':
            result['status'] = 'ok'
        pending = list(node.sub_plans)
        while pending:
            child = pending.pop()
            self.child_statuses.pop(child.plan_name, None)
            pending.extend(child.sub_plans)
        self.child_statuses[node.plan_name] = result['status']
        if node.depth == 0:
            self.error = ''
            self.handoff = None
            self.capture_root(result)

    def outcome(self) -> dict:
        value = copy.deepcopy(self.root_output)
        status = value.get('status')
        if status == 'error' or value.get('success') is False or value.get('error') or self.error:
            status = 'failed'
        elif isinstance(self.handoff, dict) and self.handoff.get('status') in {'failed', 'blocked', 'not_applicable', 'partial'}:
            if status not in {'failed', 'blocked', 'not_applicable'}:
                status = self.handoff['status']
        if 'failed' in self.child_statuses.values():
            status = 'failed'
        elif status not in {'failed', 'blocked', 'not_applicable'} and 'partial' in self.child_statuses.values():
            status = 'partial'
        if not status:
            status = 'ok' if self.completed and not self.error else 'failed'
        value['status'] = status
        return value

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Front/Gateway/Worker and locked SDK with a local model protocol stub.

Only the external model and a required-startup gate are scripted. The test
retains readiness events, frames and process logs in pytest's temporary folder.
It is a correctness test; injected waits are not performance measurements.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

import psutil
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidMessage
import yaml


OBSERVER = r'''
import asyncio, importlib.abc, importlib.machinery, json, os, sys, time
from pathlib import Path
root = Path(os.environ["WORKER_STUB_EVIDENCE"])
def record(event, **fields):
    with (root / "stages.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"event":event,"pid":os.getpid(),"ns":time.perf_counter_ns(),**fields})+"\n")
class Observe(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name not in {"jiuwenswarm.runtime.service", "jiuwenswarm.server.worker.execution_host",
                        "jiuwenswarm.server.worker.signals", "openjiuwen.harness.deep_agent"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec): return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                if name == "jiuwenswarm.server.worker.execution_host":
                    start = module.WorkerExecutionHost.start
                    async def gated(instance, *args, **kwargs):
                        record("start_enter")
                        if os.environ["WORKER_STUB_FAULT"] == "slow":
                            deadline = time.monotonic() + 60
                            while not (root / "release").exists():
                                if time.monotonic() > deadline: raise TimeoutError("test gate not released")
                                await asyncio.sleep(.02)
                        elif not (root / "failed-once").exists():
                            (root / "failed-once").write_text("failed", encoding="utf-8")
                            record("injected_failure")
                            raise RuntimeError("required startup stub failure")
                        result = await start(instance, *args, **kwargs)
                        record("start_return")
                        return result
                    module.WorkerExecutionHost.start = gated
                elif name == "jiuwenswarm.runtime.service":
                    close = module.AgentRuntime.close
                    async def closed(instance, *args, **kwargs):
                        result = await close(instance, *args, **kwargs)
                        record("runtime_closed")
                        return result
                    module.AgentRuntime.close = closed
                elif name.endswith("signals"):
                    emit = module.RuntimeSignalSink._emit
                    def emitted(instance, state, reason):
                        record("readiness", state=state, reason=reason,
                               ready=instance._agent_ready, minimum=instance._execution_minimum_ready)
                        return emit(instance, state, reason)
                    module.RuntimeSignalSink._emit = emitted
                else:
                    send = module.DeepAgent.send_input
                    async def entered(instance, *args, **kwargs):
                        record("sdk_entry", legacy_server_loaded="jiuwenswarm.server.agent_ws_server" in sys.modules,
                               sdk=module.__file__, python=sys.executable)
                        return await send(instance, *args, **kwargs)
                    module.DeepAgent.send_input = entered
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Observe())
'''


class ModelStub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[{"id":"startup-stub","object":"model"}]}')

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        markers = re.findall(r'STARTUP_STUB_(first|follow)', json.dumps(request.get('messages', [])))
        reply = 'STUB_REPLY_' + (markers[-1] if markers else 'probe')
        self.server.calls.append({'reply': reply, 'at_ns': time.perf_counter_ns()})
        streaming = bool(request.get('stream'))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
        self.end_headers()
        try:
            if streaming:
                for delta, finish in (({'role': 'assistant'}, None), ({'content': reply}, None), ({}, 'stop')):
                    payload = {'id': 'stub', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                               'model': 'startup-stub', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())
                    self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.wfile.write(json.dumps({'id': 'stub', 'object': 'chat.completion',
                    'model': 'startup-stub', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': reply}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}}).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class WorkerChain:
    def __init__(self, root, temporary, mode, fault, model_port):
        self.root, self.directory = root, temporary
        self.mode = mode
        self.processes, self.identities, self.frames = [], [], []
        self.ws = self.log = None
        self.attempt = 0
        config_dir = temporary / 'data/config'
        config_dir.mkdir(parents=True)
        config = yaml.safe_load((root / 'jiuwenswarm/resources/config.yaml').read_text(encoding='utf-8'))
        config['models']['defaults'] = [{'model_id': 'mdl_fixture', 'is_default': True,
            'model_client_config': {'client_provider': 'OpenAI', 'api_base': f'http://127.0.0.1:{model_port}/v1',
                                    'api_key': 'local-stub', 'model_name': 'startup-stub'},
            'model_config_obj': {'temperature': 0, 'max_tokens': 128}}]
        (config_dir / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
        (config_dir / '.env').write_text('# isolated startup stub\n', encoding='utf-8')
        session = temporary / 'data/agent/sessions/stub-session'
        session.mkdir(parents=True)
        (session / 'metadata.json').write_text(json.dumps({'session_id': 'stub-session', 'mode': mode,
            'channel_id': 'web', 'user_id': 'stub-owner', 'work_mode': 'code' if mode.startswith('code') else 'work',
            'project_dir': str(temporary / 'project')}), encoding='utf-8')
        (temporary / 'project').mkdir()
        subprocess.run(['git', 'init', '--quiet', str(temporary / 'project')], check=True, capture_output=True)
        observer = temporary / 'observer'
        observer.mkdir()
        (observer / 'sitecustomize.py').write_text(OBSERVER, encoding='utf-8')
        self.env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(observer), str(root))), PYTHONUTF8='1',
                        JIUWENSWARM_DATA_DIR=str(temporary / 'data'), JIUWENSWARM_HOME=str(temporary / 'home'),
                        NO_PROXY='127.0.0.1,localhost', JIUWENSWARM_RUNTIME_PROFILE='desktop',
                        WORKER_STUB_EVIDENCE=str(temporary), WORKER_STUB_FAULT=fault)
        for key in ('JIUWENSWARM_RUNTIME_SOCKET', 'JIUWENSWARM_RUNTIME_STUB', 'JIUWENSWARM_RUNTIME_WORKSPACE_READY'):
            self.env.pop(key, None)

    def stages(self):
        path = self.directory / 'stages.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    async def stage(self, event):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            found = [row for row in self.stages() if row['event'] == event]
            if found:
                return found[-1]
            await asyncio.sleep(.05)
        raise AssertionError(f'{event} not observed; see {self.directory}')

    async def start(self):
        self.attempt += 1
        front, gateway = free_port(), free_port()
        self.env.update(WEB_PORT=str(gateway), GATEWAY_PORT=str(free_port()))
        self.log = (self.directory / f'process-{self.attempt}.log').open('wb')
        commands = [
            ['-m', 'jiuwenswarm.server.app_agentserver', '--port', str(front)],
            ['-m', 'gateway.app_gateway', '--host', '127.0.0.1', '--port', str(gateway),
             '--agent-server-url', f'ws://127.0.0.1:{front}', '--web-path', '/ws'],
        ]
        for args in commands:
            self.processes.append(subprocess.Popen([sys.executable, *args], cwd=self.directory, env=self.env,
                                                   stdout=self.log, stderr=self.log))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                self.ws = await connect(f'ws://127.0.0.1:{gateway}/ws?user_id=stub-owner',
                                        max_size=32 * 1024 * 1024, proxy=None)
                return
            except (OSError, InvalidMessage):
                await asyncio.sleep(.1)
        raise AssertionError('Gateway did not become reachable')

    async def send(self, identifier, method, params):
        await self.ws.send(json.dumps({'type': 'req', 'id': identifier, 'method': method,
            'params': {'session_id': 'stub-session', **params}, 'is_stream': method == 'chat.send'}))

    async def receive_until(self, predicate):
        async with asyncio.timeout(90):
            while True:
                wire = json.loads(await self.ws.recv())
                self.frames.append(wire)
                if predicate(wire):
                    return wire

    async def chat(self, identifier):
        await self.send(identifier, 'chat.send', {'query': f'STARTUP_STUB_{identifier} Reply without tools.',
            'mode': self.mode, 'mcp': []})
        return await self.receive_until(lambda wire: wire.get('event') == 'chat.final'
            and (wire.get('payload') or {}).get('content') == 'STUB_REPLY_' + identifier)

    async def stop(self):
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        for process in self.processes:
            try:
                self.identities.extend((p.pid, p.create_time()) for p in psutil.Process(process.pid).children(recursive=True))
            except psutil.NoSuchProcess:
                pass
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 10)
        self.processes.clear()
        helper = await asyncio.create_subprocess_exec(sys.executable, '-m', 'jiuwenswarm.server.supervisor.shutdown',
            '--shutdown-worker', '--timeout', '30', cwd=self.directory, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(helper.communicate(), 35)
            assert helper.returncode == 0, stderr.decode(errors='replace')[-2000:]
            receipt = json.loads(stdout.splitlines()[-1])
            assert receipt['ok'], receipt
            (self.directory / f'shutdown-{self.attempt}.json').write_text(json.dumps(receipt), encoding='utf-8')
        finally:
            if helper.returncode is None:
                helper.kill()
                await helper.wait()
            for pid, created in self.identities:
                try:
                    process = psutil.Process(pid)
                    if process.create_time() == created:
                        process.terminate()
                        await asyncio.to_thread(process.wait, 5)
                except psutil.NoSuchProcess:
                    pass
            self.identities.clear()
            if self.log is not None:
                self.log.close()
                self.log = None
            (self.directory / 'frames.json').write_text(json.dumps(self.frames), encoding='utf-8')


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'fault'), [('agent', 'slow'), ('code.normal', 'slow'), ('agent', 'fail-once')])
async def test_required_startup_gate_and_retry_through_gateway(tmp_path, mode, fault):
    model = ThreadingHTTPServer(('127.0.0.1', 0), ModelStub)
    model.calls = []
    threading.Thread(target=model.serve_forever, daemon=True).start()
    chain = WorkerChain(Path(__file__).resolve().parents[2], tmp_path, mode, fault, model.server_port)
    try:
        await chain.start()
        entering = await chain.stage('start_enter')
        if fault == 'slow':
            assert not any(row.get('ready') or row.get('minimum') for row in chain.stages())
            await chain.send('control', 'config.get', {})
            response = await chain.receive_until(lambda wire: wire.get('type') == 'res' and wire.get('id') == 'control')
            assert response.get('ok') is True
            assert not any(call['reply'] != 'STUB_REPLY_probe' for call in model.calls)
            (tmp_path / 'release').write_text('release required preparation', encoding='utf-8')
        else:
            await chain.stage('injected_failure')
            deadline = time.monotonic() + 30
            while not any(row.get('state') == 'FAILED' for row in chain.stages()):
                assert time.monotonic() < deadline
                await asyncio.sleep(.05)
            assert not any(row.get('ready') or row.get('minimum') for row in chain.stages())
            assert not any(row['event'] == 'sdk_entry' for row in chain.stages())
            await chain.stop()
            assert any(row['event'] == 'runtime_closed' and row['pid'] == entering['pid'] for row in chain.stages())
            await chain.start()
        first = await chain.chat('first')
        follow = await chain.chat('follow')
        first_runtime = first['payload']['runtime_event']['worker_instance']
        assert first_runtime == follow['payload']['runtime_event']['worker_instance']
        entries = [row for row in chain.stages() if row['event'] == 'sdk_entry']
        assert len(entries) >= 2
        assert len({row['pid'] for row in entries}) == 1
        assert not any(row['legacy_server_loaded'] for row in entries)
        assert {call['reply'] for call in model.calls} >= {'STUB_REPLY_first', 'STUB_REPLY_follow'}
        assert any(row.get('minimum') is True for row in chain.stages())
    finally:
        (tmp_path / 'release').touch()
        await chain.stop()
        model.shutdown()
        model.server_close()
        (tmp_path / 'model-calls.json').write_text(json.dumps(model.calls), encoding='utf-8')

"""Execute pinned InterruptBench's runner with a Native model-call adapter.

The upstream code owns browser reset, replay, prompts, action parsing and scoring.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from jiuwenswarm.common.duplex_public_benchmark import read_json, verify_checkout

POLICIES = ("official", "serial", "steer", "abort_restart", "always_interrupt", "model")


def instrument_runner(source: str, filename: str):
    tree = ast.parse(source, filename=filename)
    matched = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # This exact upstream branch runs only after successful replay at K.
        if ast.unparse(node.test) == "update_intent and (not replay_ended)":
            node.body.append(ast.parse(
                "__duplex__.on_interrupt(agent, task_id, intent_before_interrupt, intent, "
                "update_intent, k, update_mode)"
            ).body[0])
            matched += 1
    if matched != 1:
        raise ValueError(f"expected one official injection branch, found {matched}")
    return compile(ast.fix_missing_locations(tree), filename, "exec")


class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="interruptbench-native", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


class ProcessWorker:
    """Keep the pinned WebArena and modern Native SDK dependencies separate."""
    def __init__(self, python, models, policy, metrics):
        metrics.parent.mkdir(parents=True, exist_ok=True)
        self.log = metrics.with_suffix(".log").open("w", encoding="utf-8")
        # Managed CPython launchers can export their stdlib location. Passing
        # a 3.11 PYTHONHOME into a 3.12 executable causes SRE/stdlib mismatches.
        env = {key: value for key, value in os.environ.items() if key not in
               ("PYTHONHOME", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV")}
        self.process = subprocess.Popen([str(python), "-u", "-m",
            "jiuwenswarm.benchmarks.interruptbench_worker", "--models", str(models),
            "--policy", policy, "--metrics", str(metrics)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8", env=env)

    def predict(self, config, prompt, current):
        payload = {"model": config.model, "gen_config": config.gen_config,
                   "prompt": prompt, "current": current}
        response = self.request(payload)
        if current and current["pending"]:
            current["pending"]["delivered"] = True
        return response["output"]

    def reset(self, task=None, environment=None):
        self.request({"operation": "reset", "task": task, "environment": environment})

    def request(self, payload):
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError("Native worker exited; inspect its log") from error
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Native worker exited; inspect its .native.log")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
        finally:
            self.log.close()


class NativeBrowserBridge:
    def __init__(self, *, models, policy, events, official_records, timeout=300,
                 native_python=None, models_file=None):
        self.models, self.policy, self.events = models, policy, events
        self.official_records = {str(item["task_id"]): item for item in official_records}
        self.timeout = timeout
        self.pending = {}
        self.current = None
        self.worker = None
        self.original_call = None
        self.native_python, self.models_file = native_python, models_file
        self._peer = None
        self._active_prompt = None
        self._environment = uuid.uuid4().hex
        self._active_goal = ""
        self._pending_goal = None

    async def reset_native(self, task=None, environment=None):
        await self.close_native()

    async def close_native(self):
        if self._peer is not None:
            peer, self._peer = self._peer, None
            await peer.close()

    def on_interrupt(self, agent, task_id, before, after, update, boundary, mode):
        record = self.official_records[str(task_id)]
        if update not in record["updates"]:
            raise ValueError("runner attempted to inject a non-official update")
        self.pending[id(agent)] = {"before": before, "after": after, "update": update,
                                  "task_id": str(task_id), "boundary": boundary, "mode": mode,
                                  "delivered": False, "message_id": uuid.uuid5(uuid.NAMESPACE_URL,
                                      f"{self.events.path.resolve()}:{task_id}:{boundary}:{mode}:{update}").hex}
        self.events.add("official_update_boundary", task_id=str(task_id), boundary=boundary, mode=mode)

    def install(self, agent_module):
        bridge = self
        original_next = agent_module.PromptAgent.next_action
        original_reset = agent_module.PromptAgent.reset
        self.original_call = agent_module.call_llm
        if self.policy != "official":
            if self.native_python:
                self.worker = ProcessWorker(self.native_python, self.models_file, self.policy,
                                            self.events.path.with_suffix(".native.jsonl"))
            else:
                self.worker = LoopThread()
                from openjiuwen.core.runner import Runner
                self.worker.call(Runner.start())

        def reset(agent, config_file):
            bridge.pending.pop(id(agent), None)
            bridge._environment = uuid.uuid4().hex
            if isinstance(bridge.worker, ProcessWorker):
                bridge.worker.reset(str(Path(config_file).resolve()), bridge._environment)
            elif isinstance(bridge.worker, LoopThread):
                bridge.worker.call(bridge.reset_native(str(Path(config_file).resolve()), bridge._environment))
            return original_reset(agent, config_file)

        def next_action(agent, trajectory, intent, meta_data, images=None, output_response=False):
            pending = bridge.pending.pop(id(agent), None)
            constructor = agent.prompt_constructor
            original_construct = constructor.construct
            bridge.current = {"pending": pending, "after_prompt": None, "intent": intent}

            def construct(*args, **kwargs):
                prompt = original_construct(*args, **kwargs)
                if pending is not None:
                    updated_args = list(args)
                    updated_args[1] = pending["after"]
                    bridge.current["after_prompt"] = original_construct(*updated_args, **kwargs)
                return prompt

            constructor.construct = construct
            try:
                return original_next(agent, trajectory,
                    pending["before"] if pending and bridge.policy != "official" else intent,
                    meta_data=meta_data, images=images, output_response=output_response)
            finally:
                constructor.construct = original_construct
                bridge.current = None

        def call_llm(lm_config, prompt, **kwargs):
            if bridge.policy == "official":
                started = time.monotonic()
                call_id, status = uuid.uuid4().hex, "complete"
                bridge.events.add("model_start", lane="slow", call_id=call_id)
                try:
                    return bridge.original_call(lm_config, prompt, **kwargs)
                except Exception:
                    status = "error"
                    raise
                finally:
                    bridge.events.add("model_end", lane="slow", call_id=call_id, status=status,
                                      seconds=time.monotonic() - started, usage=None)
            if not isinstance(prompt, list) or any(not isinstance(m, dict) for m in prompt):
                raise ValueError("Native bridge requires upstream chat-message prompts")
            if isinstance(bridge.worker, ProcessWorker):
                return bridge.worker.predict(lm_config, prompt, bridge.current)
            return bridge.worker.call(bridge._predict(lm_config, prompt, bridge.current))

        agent_module.PromptAgent.next_action = next_action
        agent_module.PromptAgent.reset = reset
        agent_module.call_llm = call_llm

        def restore():
            agent_module.PromptAgent.next_action = original_next
            agent_module.PromptAgent.reset = original_reset
            agent_module.call_llm = bridge.original_call
            if isinstance(bridge.worker, LoopThread):
                try:
                    bridge.worker.call(bridge.close_native())
                    bridge.worker.call(Runner.stop())
                finally:
                    bridge.worker.close()
            elif bridge.worker is not None:
                bridge.worker.close()

        return restore

    async def _predict(self, lm_config, prompt, current):
        from jiuwenswarm.benchmarks.duplex_runtime import UserInputPeer
        config = self.models["slow"]
        if config.model_request_config.model_name != lm_config.model:
            raise ValueError("official --model and slow model configuration must match")
        # Keep upstream sampling settings in the paired experiment.
        request = config.model_request_config.model_copy(update={key: lm_config.gen_config[key]
            for key in ("temperature", "top_p", "max_tokens") if key in lm_config.gen_config})
        models = {**self.models, "slow": config.model_copy(update={"model_request_config": request})}
        pending = current["pending"] if current else None
        changing = pending is not None and not pending["delivered"]
        self._active_goal = pending["before"] if changing else (current or {}).get("intent", "")
        self._pending_goal = pending if changing else None
        self._active_prompt = copy.deepcopy(current["after_prompt"] if pending and pending["delivered"] else prompt)
        def goal():
            update = self._pending_goal
            harness = self._peer.harness
            accepted = getattr(harness, "_duplex_received", set()) | self._peer._received
            return update["after"] if update and update["message_id"] in accepted else self._active_goal
        if self._peer is None:
            self._peer = UserInputPeer(name="interruptbench", models=models, policy=self.policy,
                system_prompt="", tools=[], events=self.events, prompt=lambda: self._active_prompt,
                goal=goal)
            self._peer.harness._duplex_goal_provider = goal
            await self._peer.start()
        peer = self._peer
        peer.harness._duplex_goal_provider = goal
        peer.model.entered.clear()
        try:
            await peer.send(pending["before"] if changing else (current or {}).get("intent", "Continue the official browser task."))
            if changing:
                # Still at the same K-action boundary: no browser action can run
                # while next_action is blocked here. Start the old model request
                # and deliver the official event concurrently with that request.
                await asyncio.wait_for(peer.model.entered.wait(), self.timeout)
                self._active_prompt = copy.deepcopy(current["after_prompt"])
                await peer.receive_user(pending["update"], message_id=pending["message_id"])
                pending["delivered"] = True
            result = await peer.wait(timeout=self.timeout)
            output = result.get("output")
            if not isinstance(output, str):
                raise ValueError("Native model returned no textual browser action")
            return output
        except BaseException:
            await self.close_native()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--native-python", type=Path, required=True,
                        help="Python in the separate environment containing the locked Native SDK")
    parser.add_argument("--suite", required=True,
                        choices=["1update", "2update", "2modification", "1retraction", "2retraction", "3mixed"])
    parser.add_argument("--policy", choices=POLICIES, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("upstream_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.repo.resolve()
    verify_checkout(root, "interruptbench")
    runner = root / "Eval/run.py"
    code = instrument_runner(runner.read_text(encoding="utf-8"), str(runner))
    from jiuwenswarm.benchmarks.duplex_metrics import Events

    events = Events(args.metrics.resolve())
    bridge = NativeBrowserBridge(models=None, policy=args.policy, events=events,
        native_python=args.native_python.resolve(strict=True), models_file=args.models.resolve(strict=True),
        official_records=read_json(root / f"Eval/interrupt_config/raw/{args.suite}.json"))
    argv, cwd = sys.argv[:], Path.cwd()
    sys.path.insert(0, str(root / "Eval"))
    os.chdir(root / "Eval")
    import agent.agent as upstream_agent

    restore = bridge.install(upstream_agent)
    try:
        options = args.upstream_args[1:] if args.upstream_args[:1] == ["--"] else args.upstream_args
        sys.argv = [str(runner), *options]
        events.add("run_start", benchmark="interruptbench", policy=args.policy,
                   injection="official_action_boundary_model_dispatch", suite=args.suite)
        exec(code, {"__name__": "__main__", "__file__": str(runner), "__duplex__": bridge})
        events.add("run_end", status="runner_returned_check_official_scores")
    finally:
        restore()
        sys.argv = argv
        sys.path.pop(0)
        os.chdir(cwd)


if __name__ == "__main__":
    main()

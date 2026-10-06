"""Run one real Native peer launched by the original Coral message server."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import signal
import time
import uuid
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.agent_teams.harness.state import HarnessState

from jiuwenswarm.benchmarks.duplex_runtime import Events, load_models


class BashTool(Tool):
    def __init__(self, cwd, events, *, shell="bash"):
        super().__init__(ToolCard(name="Bash", description="Execute a shell command. Background tasks notify you with their complete output.",
            input_params={"type": "object", "properties": {
                "command": {"type": "string"}, "run_in_background": {"type": "boolean"},
                "timeout": {"type": "integer", "description": "Milliseconds; maximum 1200000"}},
                "required": ["command"]}))
        self.cwd, self.events, self.shell = cwd, events, shell
        self.peer = None
        self.jobs = {}
        self.processes = set()
        self.watcher = None

    async def invoke(self, inputs, **kwargs):
        command = inputs["command"]
        background = bool(inputs.get("run_in_background", False))
        watcher = background and "wait_for_mention.sh" in command
        if watcher and self.watcher is not None and not self.watcher.done():
            return "A watcher is already running; its output will be delivered."
        job_id = uuid.uuid4().hex
        timeout = max(1, min(int(inputs.get("timeout", 1200000)), 1200000)) / 1000
        if background:
            task = asyncio.create_task(self._background(command, job_id, timeout))
            self.jobs[job_id] = task
            if watcher:
                self.watcher = task
            return f"Background task {job_id} started. Output will arrive automatically."
        return await self._execute(command, job_id, timeout)

    async def _background(self, command, job_id, timeout):
        output = await self._execute(command, job_id, timeout)
        if self.peer is None:
            raise RuntimeError("background Bash tool has no delivery target")
        # Release the watcher slot before delivering: the successor is allowed
        # to relaunch it while this task is still waiting for routing to finish.
        if self.watcher is asyncio.current_task():
            self.watcher = None
        message_id = hashlib.sha256((job_id + output).encode()).hexdigest()
        await self.peer.receive(output, message_id=message_id, sender="coral-watcher")

    async def _execute(self, command, job_id, timeout):
        self.events.add("tool_start", tool="Bash", job_id=job_id)
        process = await asyncio.create_subprocess_exec(self.shell, "-c", command, cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt")
        self.processes.add(process)
        status = "complete"
        try:
            async with asyncio.timeout(timeout):
                output, _ = await process.communicate()
            return f"exit_code={process.returncode}\n" + output.decode("utf-8", errors="replace")
        except TimeoutError:
            status = "timeout"
            return "Command timed out; external effects may already have occurred."
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if process.returncode is None:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            self.processes.discard(process)
            self.events.add("tool_end", tool="Bash", job_id=job_id, status=status,
                            external_effects="unknown")

    async def stream(self, inputs, **kwargs):
        raise NotImplementedError

    async def close(self):
        for task in self.jobs.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)


async def run(args):
    events = Events(Path(args.metrics))
    models = load_models(Path(args.models))
    events.add("run_start", benchmark="agentradio", policy=args.policy,
               model=models["slow"].model_request_config.model_name,
               fast_model=models["fast"].model_request_config.model_name)
    tool = BashTool(Path.cwd(), events)
    from jiuwenswarm.benchmarks.duplex_database_peer import DatabasePeer
    peer = DatabasePeer(database=Path(args.database), team=os.environ.get("CORAL_SESSION_ID", "agentradio"),
        name=os.environ.get("CORAL_AGENT_ID", "peer"), models=models,
        policy=args.policy, system_prompt=Path("CLAUDE.md").read_text(), tools=[tool], events=events)
    tool.peer = peer
    resume_prompt = Path(args.resume_prompt).read_text(encoding="utf-8")
    await Runner.start()
    try:
        await peer.start()
        await peer.send(args.prompt)
        idle_since = None
        async with asyncio.timeout(args.timeout):
            # The official protocol decides completion by the assembler's file.
            # Intermediate idle rounds do not terminate a peer's listener.
            while not Path(args.answer).is_file():
                if peer.harness.state is HarnessState.TERMINATED:
                    raise RuntimeError("Native supervisor terminated before team submission")
                for task in tool.jobs.values():
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        raise task.exception()
                if peer.harness.state is HarnessState.IDLE:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= 15:
                        events.add("resume_guard", member=peer.name)
                        await peer.send(resume_prompt)
                        idle_since = None
                else:
                    idle_since = None
                await asyncio.sleep(0.1)
        events.add("run_end", status="answer_submitted")
    except BaseException:
        events.add("run_end", status="incomplete")
        raise
    finally:
        await tool.close()
        await peer.close()
        await Runner.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True)
    parser.add_argument("--policy", choices=["serial", "steer", "abort_restart", "model", "always_interrupt"], required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--database", default="/logs/agent/jiuwen-messages.sqlite3")
    parser.add_argument("--answer", default="/logs/agent/answer.txt")
    parser.add_argument("--resume-prompt", default="/tmp/jiuwen-bench-resume.txt")
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("prompt")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

"""Harbor 0.6.4 adapter: keep Coral's orchestration; replace only peer execution.

Add both the Jiuwen and pinned AgentRadio roots to PYTHONPATH before Harbor.
"""
from __future__ import annotations

import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

from multi_agent.coral_multi_agent_passive import CoralMultiAgentPassive, MULTI_AGENT_DIR
from multi_agent.resume_guard import GUARD_BLOCK

from jiuwenswarm.common.duplex_public_benchmark import verify_checkout


def native_startup(source: str, policy: str) -> str:
    line = 'exec claude --verbose --output-format=stream-json --permission-mode=bypassPermissions --effort high --print -- "$PROMPT" 2>&1 </dev/null | tee "$LOG_FILE"'
    if source.count(line) != 1:
        raise ValueError("pinned AgentRadio startup entry changed")
    if policy not in ("serial", "steer", "abort_restart", "model", "always_interrupt"):
        raise ValueError("invalid policy")
    replacement = ('exec /tmp/jiuwen-bench-venv/bin/python -m jiuwenswarm.benchmarks.agentradio_peer '
        '--models /tmp/jiuwen-bench-models.json --policy ' + shlex.quote(policy) +
        ' --metrics "/logs/agent/${CORAL_AGENT_ID}-duplex.jsonl" "$PROMPT" 2>&1 </dev/null | tee "$LOG_FILE"')
    return source.replace(line, replacement)


class JiuwenAgentRadio(CoralMultiAgentPassive):
    def __init__(self, *args, models_file: str, duplex_policy: str = "model", **kwargs):
        super().__init__(*args, **kwargs)
        self.models_file = Path(models_file).resolve(strict=True)
        self.duplex_policy = duplex_policy
        native_startup((MULTI_AGENT_DIR / "startup_passive.sh").read_text(), duplex_policy)

    @staticmethod
    def name():
        return "jiuwen-agentradio"

    async def install(self, environment):
        verify_checkout(MULTI_AGENT_DIR.parent, "agentradio")
        await super().install(environment)
        package = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="duplex-harbor-") as temp:
            archive = Path(temp) / "jiuwenswarm.tar.gz"
            tracked = subprocess.check_output(["git", "-C", str(package.parent),
                "ls-files", "--", "jiuwenswarm"], text=True).splitlines()
            paths = {package.parent / item for item in tracked}
            # Include this adapter while testing an uncommitted implementation;
            # never sweep unrelated untracked files or local credentials.
            paths.update((package / "benchmarks").glob("*.py"))
            with tarfile.open(archive, "w:gz") as bundle:
                for path in sorted(paths):
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                        bundle.add(path, arcname=str(Path("jiuwenswarm") / path.relative_to(package)))
            await environment.upload_file(archive, "/tmp/jiuwenswarm-bench.tar.gz")
            startup = Path(temp) / "startup_passive.sh"
            startup.write_text(native_startup((MULTI_AGENT_DIR / "startup_passive.sh").read_text(),
                                             self.duplex_policy), encoding="utf-8", newline="\n")
            await environment.upload_file(startup, "/tmp/coral-workspace/swe-atlas-agent/startup_passive.sh")
        await environment.upload_file(self.models_file, "/tmp/jiuwen-bench-models.json")
        with tempfile.TemporaryDirectory(prefix="duplex-resume-") as temp:
            prompt = next(line.split("=", 1)[1] for line in GUARD_BLOCK.splitlines()
                          if line.startswith("RESUME_GUARD_PROMPT="))
            resume_file = Path(temp) / "resume.txt"
            resume_file.write_text(shlex.split(prompt)[0], encoding="utf-8")
            await environment.upload_file(resume_file, "/tmp/jiuwen-bench-resume.txt")
        await self.exec_as_agent(environment, command=(
            "set -eu\n"
            "curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            '"$HOME/.local/bin/uv" venv --python 3.12 /tmp/jiuwen-bench-venv\n'
            '"$HOME/.local/bin/uv" pip install --python /tmp/jiuwen-bench-venv/bin/python '
            "'openjiuwen[sqlite] @ git+https://gitcode.com/openJiuwen/agent-core.git@94e10cb6102c36fe78a64547957c0def97299273' ruamel.yaml\n"
            "tar -xzf /tmp/jiuwenswarm-bench.tar.gz -C /tmp/jiuwen-bench-venv/lib/python3.12/site-packages\n"
            "chmod 600 /tmp/jiuwen-bench-models.json\n"
            "chmod +x /tmp/coral-workspace/swe-atlas-agent/startup_passive.sh\n"
            "/tmp/jiuwen-bench-venv/bin/python -m jiuwenswarm.benchmarks.agentradio_peer --help"
        ), timeout_sec=1800)

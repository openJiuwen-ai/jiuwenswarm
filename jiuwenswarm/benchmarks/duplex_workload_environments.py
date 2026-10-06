"""Task containers and original graders; evaluator inputs stay outside agent tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path


SWE_REVISION = "3f01bd622c0a22c00406139f69a234ef08225f22"  # v3.0.17


def command(argv, *, timeout=1800, cwd=None, env=None, input=None, check=True):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, input=input,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{Path(str(argv[0])).name} failed ({result.returncode}): "
                           + result.stderr.decode(errors="replace")[-2000:])
    return result


class DockerEnvironment:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.prefix = "jiuwen-workload-" + uuid.uuid4().hex[:12]
        self.containers, self.volumes, self.locks = {}, [], {}
        self.images = {}

    def image(self, tag):
        check = command(["docker", "image", "inspect", tag], check=False)
        if check.returncode:
            command(["docker", "pull", tag], timeout=3600)
        info = json.loads(command(["docker", "image", "inspect", tag]).stdout)[0]
        self.images[tag] = {"id": info["Id"], "digests": info.get("RepoDigests", [])}
        return info["Id"]

    def start_container(self, role, image, *options):
        name = f"{self.prefix}-{role}"
        command(["docker", "run", "-d", "--name", name, *options, image, "sleep", "infinity"])
        self.containers[role] = name
        self.locks[role] = asyncio.Lock()
        return name

    def shell(self, role, script, timeout=120):
        args = ["docker", "exec", "-w", self.workdir, self.containers[role],
                "timeout", "--kill-after=5", str(timeout), "bash", "-lc", self.preamble + script]
        result = command(args, timeout=timeout + 20, check=False)
        return f"exit_code={result.returncode}\n" + (result.stdout + result.stderr).decode(errors="replace")

    async def execute(self, role, script, timeout):
        async with self.locks[role]:
            return await asyncio.to_thread(self.shell, role, script, timeout)

    def close(self):
        errors = []
        # Only resources created by this environment are eligible for removal.
        for name in reversed(list(self.containers.values())):
            result = command(["docker", "rm", "-f", name], check=False)
            if result.returncode:
                errors.append(result.stderr.decode(errors="replace"))
        for volume in self.volumes:
            result = command(["docker", "volume", "rm", volume], check=False)
            if result.returncode:
                errors.append(result.stderr.decode(errors="replace"))
        if errors:
            (self.output / "cleanup-errors.txt").write_text("\n".join(errors), encoding="utf-8")


class DevelopmentEnvironment(DockerEnvironment):
    workdir = "/testbed"
    preamble = "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && "
    instructions = "Each role has a separate /testbed checkout and the task's original dependency environment. " \
                   "Bash activates conda environment testbed. Network access is disabled. " \
                   "Only implementer's final git diff is submitted. No official hidden tests are available to you."

    def __init__(self, row, output, *, swe_python, swe_root):
        super().__init__(output)
        self.row, self.swe_python, self.swe_root = row, str(swe_python), Path(swe_root)
        self.base = {}

    def prepare(self):
        identity = self.row["instance_id"]
        tag = "swebench/sweb.eval.x86_64." + identity.lower().replace("__", "_1776_") + ":latest"
        image = self.image(tag)
        for role in ("implementer", "diagnoser", "reviewer"):
            container = self.start_container(role, image, "--network", "none")
            head = command(["docker", "exec", "-w", "/testbed", container, "git", "rev-parse", "HEAD"]).stdout.decode().strip()
            if head != self.row["base_commit"]:
                raise ValueError(f"{identity}: image is not at the frozen base commit")
            # A new git history prevents looking up later commits/branches.
            # This is inside a newly created disposable container, never the host repo.
            script = "rm -rf /testbed/.git && cd /testbed && git init -q && " \
                     "git add . && git -c user.name=benchmark -c user.email=benchmark@local commit -qm base && git rev-parse HEAD"
            self.base[role] = command(["docker", "exec", container, "bash", "-lc", script]).stdout.decode().strip()

    def patch(self):
        container = self.containers["implementer"]
        command(["docker", "exec", "-w", "/testbed", container, "git", "add", "-N", "."])
        patch = command(["docker", "exec", "-w", "/testbed", container,
                         "git", "diff", "--binary", self.base["implementer"]]).stdout
        if len(patch) > 2_000_000:
            raise ValueError("Generated patch exceeds 2 MB; inspect the recorded workspace")
        return patch

    async def review_snapshot(self):
        # Hold the writer lock while exporting; reviewer tests use that exact
        # immutable patch, even if implementation continues immediately after.
        async with self.locks["implementer"]:
            patch = await asyncio.to_thread(self.patch)
        digest = hashlib.sha256(patch).hexdigest()
        async with self.locks["reviewer"]:
            await asyncio.to_thread(self._apply_review_patch, patch)
        return {"patch_sha256": digest, "bytes": len(patch), "member": "reviewer"}

    def _apply_review_patch(self, patch):
        container = self.containers["reviewer"]
        command(["docker", "exec", "-w", "/testbed", container, "git", "reset", "--hard", self.base["reviewer"]])
        command(["docker", "exec", "-w", "/testbed", container, "git", "clean", "-fd"])
        if patch:
            command(["docker", "exec", "-i", "-w", "/testbed", container, "git", "apply", "--binary", "-"], input=patch)

    def grade(self, evaluator_row, trajectory):
        patch = self.patch()
        (self.output / "model.patch").write_bytes(patch)
        identity = self.row["instance_id"]
        data = self.output / "evaluator-input.json"
        predictions = self.output / "predictions.jsonl"
        data.write_text(json.dumps([evaluator_row]), encoding="utf-8")
        predictions.write_text(json.dumps({"instance_id": identity, "model_patch": patch.decode(),
            "model_name_or_path": "jiuwen"}) + "\n", encoding="utf-8")
        run_id = self.prefix
        args = [self.swe_python, "-m", "swebench.harness.run_evaluation", "--dataset_name", data,
                "--predictions_path", predictions, "--run_id", run_id, "--instance_ids", identity,
                "--max_workers", "1", "--timeout", "1200", "--namespace", "swebench", "--cache_level", "instance"]
        env = {**os.environ, "PYTHONPATH": str(self.swe_root)}
        result = command(args, cwd=self.output, env=env, timeout=1800, check=False)
        (self.output / "grader.log").write_bytes(result.stdout + result.stderr)
        report = self.output / "logs/run_evaluation" / run_id / "jiuwen" / identity / "report.json"
        if report.exists():
            official = json.loads(report.read_text())[identity]
            return {"score": float(official["resolved"]), "success": bool(official["resolved"]),
                    "status": "scored", "official": official}
        if not patch and result.returncode == 0:
            return {"score": 0.0, "success": False, "status": "official_empty_patch"}
        return {"score": None, "success": None, "status": "grader_error", "returncode": result.returncode}


class OfficeEnvironment(DockerEnvironment):
    workdir, preamble = "/workspace", ""
    instructions = "Public task files are in /instruction (read only), outputs in /workspace. " \
        "Use Python requests or Playwright Chromium through Bash to access the original services. " \
        "Public benchmark accounts: RocketChat and OwnCloud username/password theagentcompany; " \
        "GitLab root@local/theagentcompany; Plane agent@company.com/theagentcompany. " \
        "Only coordinator has network access and communicates with the original NPCs. " \
        "Coordinator downloads relevant files into /workspace for the other two agents. NPC responses may take time; " \
        "coordinator should poll for replies as needed. Researcher/reviewer send findings internally."

    def __init__(self, task_id, output, *, server_host, env_model, workstation_image, instruction):
        super().__init__(output)
        self.task_id, self.server_host = task_id, server_host
        self.env_model, self.workstation_image = env_model, workstation_image
        self.instruction = instruction

    def model_env(self):
        client = self.env_model.model_client_config
        return {**os.environ, "SERVER_HOSTNAME": self.server_host,
                "LITELLM_API_KEY": client.api_key, "LITELLM_BASE_URL": client.api_base,
                "LITELLM_MODEL": "openai/" + self.env_model.model_request_config.model_name}

    def prepare(self):
        image = self.image(f"ghcr.io/theagentcompany/{self.task_id}-image:1.0.0")
        for directory in ("workspace", "instruction"):
            volume = self.prefix + "-" + directory
            command(["docker", "volume", "create", volume])
            self.volumes.append(volume)
        task = self.start_container("official", image, "--network", "host",
            "-v", f"{self.volumes[0]}:/workspace", "-v", f"{self.volumes[1]}:/instruction")
        actual = command(["docker", "exec", task, "cat", "/instruction/task.md"]).stdout.decode()
        if actual.replace("\r\n", "\n").rstrip("\n") != self.instruction.replace("\r\n", "\n").rstrip("\n"):
            raise ValueError("Published office image instruction differs from the frozen dataset input")
        # Original init resets the service state and starts original NPCs.
        result = command(["docker", "exec", "-e", "SERVER_HOSTNAME", "-e", "LITELLM_API_KEY",
            "-e", "LITELLM_BASE_URL", "-e", "LITELLM_MODEL", task, "bash", "/utils/init.sh"],
            env=self.model_env(), timeout=1200, check=False)
        secret = str(self.env_model.model_client_config.api_key).encode()
        (self.output / "init.log").write_bytes((result.stdout + result.stderr).replace(secret, b"[REDACTED]"))
        if result.returncode:
            raise RuntimeError("Official office initialization failed; see init.log")
        workstation = self.image(self.workstation_image)
        hosts = command(["docker", "exec", task, "cat", "/etc/hosts"]).stdout.decode()
        ip = next(line.split()[0] for line in reversed(hosts.splitlines()) if "the-agent-company.com" in line.split()[1:])
        for role in ("coordinator", "researcher", "reviewer"):
            self.start_container(role, workstation, "--network", "host" if role == "coordinator" else "none",
                "--add-host", f"the-agent-company.com:{ip}",
                "-v", f"{self.volumes[0]}:/workspace", "-v", f"{self.volumes[1]}:/instruction:ro")
        # Agents only see public volumes. /npc, /utils and model keys exist
        # exclusively in the separate official container, without a shared PID namespace.

    def grade(self, evaluator_row, trajectory):
        task = self.containers["official"]
        command(["docker", "cp", str(trajectory), f"{task}:/tmp/jiuwen-trajectory.jsonl"])
        env = {**self.model_env(), "DECRYPTION_KEY": "theagentcompany is all you need"}
        # The pinned source uses --result_path (the README says --output_path).
        result = command(["docker", "exec", "-e", "LITELLM_API_KEY", "-e", "LITELLM_BASE_URL",
            "-e", "LITELLM_MODEL", "-e", "DECRYPTION_KEY", task, "python_default", "/utils/eval.py",
            "--trajectory_path", "/tmp/jiuwen-trajectory.jsonl", "--result_path", "/tmp/jiuwen-result.json"],
            env=env, timeout=900, check=False)
        secret = str(self.env_model.model_client_config.api_key).encode()
        (self.output / "grader.log").write_bytes((result.stdout + result.stderr).replace(secret, b"[REDACTED]"))
        if result.returncode:
            return {"score": None, "success": None, "status": "grader_error", "returncode": result.returncode}
        command(["docker", "cp", f"{task}:/tmp/jiuwen-result.json", self.output / "official.json"])
        official = json.loads((self.output / "official.json").read_text())
        final = official["final_score"]
        if not 0 <= final["result"] <= final["total"] or final["total"] <= 0:
            raise ValueError("Invalid original office score")
        return {"score": final["result"] / final["total"], "success": final["result"] == final["total"],
                "status": "scored", "official": official}

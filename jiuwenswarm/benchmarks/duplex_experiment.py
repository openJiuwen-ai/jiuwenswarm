"""Run paired official benchmark workflows from explicit local configuration."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from jiuwenswarm.benchmarks.duplex_metrics import official_scores, summarize_events, paired_success
from jiuwenswarm.common.duplex_public_benchmark import read_json, verify_checkout


class Experiment:
    def __init__(self, config, output: Path):
        self.config = config
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.records = []
        self.env = os.environ.copy()
        if config.get("environment_file"):
            values = read_json(Path(config["environment_file"]))
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
                raise ValueError("environment_file must map names to string values")
            self.env.update(values)
        package_root = str(Path(__file__).resolve().parents[2])
        self.env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, self.env.get("PYTHONPATH", "")]))

    def command(self, args, *, cwd, label):
        path = self.output / f"{label}.log"
        # No shell interpolation, no credentials in arguments or summary files.
        with path.open("w", encoding="utf-8") as log:
            result = subprocess.run([str(a) for a in args], cwd=cwd, env=self.env,
                                    stdout=log, stderr=subprocess.STDOUT)
        self.records.append({"label": label, "returncode": result.returncode, "log": str(path)})
        self.save()
        if result.returncode:
            raise RuntimeError(f"{label} failed; see {path}")

    def save(self):
        (self.output / "execution.json").write_text(json.dumps(self.records, indent=2), encoding="utf-8")

    def interruptbench(self):
        c = self.config
        root = Path(c["repo"]).resolve()
        verify_checkout(root, "interruptbench")
        cwd = root / "Eval"
        python = Path(c["python"]).resolve()
        models = Path(c["models"]).resolve()
        native = Path(c["native_python"]).resolve()
        suite = c["suite"]
        raw = cwd / f"interrupt_config/raw/{suite}.json"
        data = read_json(raw)
        task_ids = [str(item["task_id"]) for item in data]
        if c.get("task_ids") is not None:
            selected = [str(item) for item in c["task_ids"]]
            if not selected or not set(selected) <= set(task_ids):
                raise ValueError("unknown or empty official task selection")
            task_ids = selected
        stages = max(len(item["updates"]) for item in data if str(item["task_id"]) in task_ids)
        if "--reset_server_url" not in c["runner_args"] or "--reset_before_each_task" not in c["runner_args"]:
            raise ValueError("paired WebArena runs require --reset_server_url and --reset_before_each_task")
        config0 = self.output / "configs-stage0"
        self.command([python, "interrupt_config/make_transformed_intent_configs.py",
            "--base_config_dir", cwd / "config_files/wa/test_webarena_lite",
            "--raw_interrupt_file", raw, "--out_dir", config0], cwd=cwd, label="prepare-initial")
        base = [python, "-m", "jiuwenswarm.benchmarks.interruptbench_runner", "--repo", root,
                "--models", models, "--native-python", native, "--suite", suite]
        reports = []
        for repeat in range(int(c.get("repeats", 3))):
            seed = int(c.get("seed", 42)) + repeat
            self.env["SEED"] = str(seed)
            baseline = self.output / f"r{repeat}-baseline"
            self.command([*base, "--policy", "official", "--metrics", self.output / f"r{repeat}-baseline.jsonl", "--",
                *c["runner_args"], "--test_config_base_dir", config0, "--test_indices", ",".join(task_ids),
                "--result_dir", baseline, "--save_trajectory"], cwd=cwd, label=f"r{repeat}-baseline")
            for policy in c.get("policies", ["official", "steer", "always_interrupt", "abort_restart", "model"]):
                previous = baseline
                for stage in range(1, stages + 1):
                    label = f"r{repeat}-{policy}-stage{stage}"
                    configs, spec = self.output / f"{label}-configs", self.output / f"{label}-spec.json"
                    self.command([python, "interrupt_config/make_multi_interrupt_stage.py",
                        "--raw_interrupt_file", raw, "--base_config_dir", config0, "--result_dirs", previous,
                        "--stage", stage, "--out_config_dir", configs, "--out_interrupt_spec", spec],
                        cwd=cwd, label=f"{label}-prepare")
                    available = read_json(spec)["tasks"]
                    missing = [task for task in task_ids if task not in available]
                    if missing:
                        raise RuntimeError(f"official stage generator could not prepare {missing}; do not omit failed tasks")
                    result = self.output / label
                    metrics = self.output / f"{label}.jsonl"
                    self.command([*base, "--policy", policy, "--metrics", metrics, "--", *c["runner_args"],
                        "--test_config_base_dir", configs, "--test_indices", ",".join(task_ids),
                        "--result_dir", result, "--save_trajectory", "--interrupt_spec", spec,
                        "--replay_trajectory_dir", previous / "trajectories"], cwd=cwd, label=label)
                    reports.append({"repeat": repeat, "seed": seed, "policy": policy, "stage": stage,
                        "official": official_scores(result, task_ids, benchmark="interruptbench"),
                        "metrics": summarize_events(list(self.output.glob(f"{label}*.jsonl")))})
                    self._report(reports)
                    previous = result

    def agentradio(self):
        c = self.config
        root = Path(c["repo"]).resolve()
        verify_checkout(root, "agentradio")
        self.env["PYTHONPATH"] = str(root) + os.pathsep + self.env["PYTHONPATH"]
        tasks = sorted(path.name for path in (root / "data/qa").glob("task-*"))
        if c.get("task_ids") is not None:
            selected = c["task_ids"]
            if not selected or not set(selected) <= set(tasks):
                raise ValueError("unknown or empty official task selection")
            tasks = selected
        imports = {"L2": "multi_agent.coral_multi_agent:CoralMultiAgent",
                   "L3": "multi_agent.coral_multi_agent_passive:CoralMultiAgentPassive"}
        reports = []
        for repeat in range(int(c.get("repeats", 3))):
            for policy in c.get("policies", ["L2", "L3", "steer", "abort_restart", "model"]):
                job = f"r{repeat}-{policy}"
                args = [c["harbor"], "run", "-p", root / "data/qa", "--agent-import-path",
                    imports.get(policy, "jiuwenswarm.benchmarks.agentradio_harbor:JiuwenAgentRadio"),
                    "-m", c["model"], "-e", c.get("environment", "docker"), "-k", "1", "-n", "1",
                    "-o", self.output, "--job-name", job, "-y"]
                for task in tasks:
                    args.extend(["-i", task])
                if policy not in imports:
                    args.extend(["--ak", f"models_file={Path(c['models']).resolve()}",
                                 "--ak", f"duplex_policy={policy}"])
                self.command(args, cwd=root, label=job)
                result = self.output / job
                for task in tasks:
                    trials = list(result.glob(f"{task}__*/"))
                    if len(trials) > 1:
                        raise ValueError("ambiguous Harbor trial directory")
                    if trials:
                        self.command([c["python"], root / "verify_local.py", task, trials[0]],
                                     cwd=root, label=f"{job}-{task}-verify")
                reports.append({"repeat": repeat, "policy": policy,
                    "official": official_scores(result, tasks, benchmark="agentradio"),
                    "metrics": summarize_events(list(result.rglob("*-duplex.jsonl")))})
                self._report(reports)

    def _report(self, reports):
        (self.output / "report.json").write_text(json.dumps({"benchmark": self.config["benchmark"],
            "runs": reports, "paired_success": paired_success(reports,
                self.config.get("comparison_baseline",
                    "official" if self.config["benchmark"] == "interruptbench" else "steer")),
            "notes": ["Missing official scores count in the denominator.",
            "Absent provider usage and GPU metrics are null, never estimated as zero.",
            "Later InterruptBench stages follow each policy's own official trajectory."]}, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = read_json(args.config)
    if config.get("benchmark") not in ("agentradio", "interruptbench"):
        parser.error("benchmark must be agentradio or interruptbench")
    experiment = Experiment(config, args.output)
    getattr(experiment, config["benchmark"])()


if __name__ == "__main__":
    main()

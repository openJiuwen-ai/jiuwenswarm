"""Run the generated experiments against a different model than the pipeline agents.

The pipeline agents (manager, design, code, writing) are prompt-cache friendly — 90%+ of their
input tokens hit the provider cache — while the experiment subprocesses send a fresh question on
every call and almost never hit it. On our runs the experiments were the most expensive part per
token. Their answering model is the *object of study*, not a pipeline component: any model works as
long as every variant and every item uses the same one. So it can be a cheaper model on another
provider.

agent-core passes the process environment to every experiment subprocess (host smoke test in
``code_implementation`` and full run in ``experiment_execution``) through
``experiment_execution.agent._variant_env``, and generated code reads ``API_KEY`` / ``API_BASE`` /
``MODEL_NAME``. Wrapping that one function swaps the credentials for the subprocesses only.
"""

from __future__ import annotations

from pathlib import Path

OVERRIDE_KEYS = ("API_KEY", "API_BASE", "MODEL_NAME", "MODEL_PROVIDER")


def read_env(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines of a credentials file (comments and blank lines skipped, quotes stripped)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_experiment_env(env_file: Path, model: str | None = None) -> dict[str, str]:
    values = read_env(Path(env_file))
    override = {k: values[k] for k in OVERRIDE_KEYS if values.get(k)}
    if model:
        override["MODEL_NAME"] = model
    missing = [k for k in ("API_KEY", "API_BASE", "MODEL_NAME") if k not in override]
    if missing:
        raise ValueError(f"{env_file}: missing {missing}")
    return override


def experiment_model_note(override: dict[str, str]) -> str:
    return (
        "\n\n## Experiment model (host setting)\n\n"
        f"Experiment subprocesses (smoke test and full run) receive MODEL_NAME={override['MODEL_NAME']} "
        "through the usual API_KEY / API_BASE / MODEL_NAME environment variables. This is the "
        "answering model of the study: every variant uses it, read it from the environment (never "
        "hard-code a model name) and echo it in metrics.json `config.model`. The paper must name it "
        "as the model under test.\n"
    )


def install_experiment_model(override: dict[str, str], env_file: Path | None = None) -> str:
    """Patch ``_variant_env`` (idempotent) and tell the design / code agents which model answers.

    With ``env_file``, the credentials are re-read on every subprocess launch, so a key can be
    rotated (e.g. an exhausted account) by editing the file, without restarting the run. The model
    name stays the one fixed at install time: the model under study must not change mid-run.
    """
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as execution

    from jiuwenswarm.agents.harness.common.paper_pipeline import research_protocol

    model = override["MODEL_NAME"]
    current = getattr(execution, "_variant_env")  # agent-core's private per-variant environment builder
    if not hasattr(current, "__wrapped__"):
        def _variant_env(*args, **kwargs):
            env = current(*args, **kwargs)
            fresh = override
            if env_file is not None:
                try:
                    fresh = {**load_experiment_env(env_file), "MODEL_NAME": model}
                except (OSError, ValueError, KeyError):
                    fresh = override  # a half-edited file must not break the launch
            env.update(fresh)
            return env

        _variant_env.__wrapped__ = current  # type: ignore[attr-defined]
        setattr(execution, "_variant_env", _variant_env)
    note = experiment_model_note(override)
    if note not in research_protocol.CODE_PROTOCOL:
        research_protocol.CODE_PROTOCOL += note
        research_protocol.DESIGN_PROTOCOL += note
    return override["MODEL_NAME"]

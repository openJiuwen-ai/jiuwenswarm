# Experiment Integrity and Artifact Lineage

`ExperimentIntegrityRail` records how an experiment was executed and links a
reported metric back to the command, environment, output artifact, and artifact
hash that produced it. The feature is opt-in, observation-only, and fail-safe:
if provenance recording fails, it logs a warning without changing the agent's
execution result.

## Enable the rail

The rail is disabled by default. Enable it in `config.yaml`:

```yaml
research_integrity:
  enabled: true
  # Empty means <project_root>/.jiuwen/research_integrity
  manifest_dir: ""
  # Empty lists use the built-in defaults.
  tracked_tools: []
  tracked_stages: []
```

Relative `manifest_dir` values are resolved from `project_root`; absolute paths
are used unchanged.

The default tracked tools are `bash`, `code`, `python`, and
`run_research_experiment`. A non-empty `tracked_stages` list limits recording to
the named workflow stages.

## Run a verified experiment

The standalone API executes a declared command, hashes its outputs, extracts
metrics deterministically, and validates the complete lineage:

```python
from jiuwenswarm.research_integrity import (
    ExperimentSpec,
    MetricSpec,
    run_research_experiment,
)

spec = ExperimentSpec(
    experiment_id="evaluation_seed42",
    name="evaluation",
    command="python evaluate.py --seed 42 --output results.json",
    cwd=".",
    seed=42,
    expected_artifacts=["results.json"],
    metric_specs=[
        MetricSpec(
            name="accuracy",
            source="results.json",
            locator="$.accuracy",
        )
    ],
)

outcome = run_research_experiment(spec, project_root=".")
if not outcome.passed:
    raise RuntimeError(outcome.report.model_dump_json(indent=2))
print(outcome.run.run_id)
```

Supported metric locators are:

- JSON: `$.accuracy` or `$.results[0].score`
- CSV: `row=method_a,column=accuracy`
- JSONL: `jsonl[line=14].score`

Metrics are parsed from artifacts, never inferred from model-generated text.
A failed process, missing output, changed artifact hash, non-finite metric, or
invalid seed relationship causes validation to fail.

## Stored records

The default manifest root is:

```text
<project_root>/.jiuwen/research_integrity/
  specs/
  runs/
  artifacts/
  metrics/
  reports/
  fingerprints/
  sessions/
```

Each artifact stores a SHA-256 digest. Environment fingerprints include Python
and platform information, dependency/config/dataset/code hashes, and optional
Git state. Environment variables are recorded only when explicitly allowlisted.

## Security and operational boundaries

- The feature is disabled by default and writes only to the configured local
  manifest directory.
- Common credential fields, bearer values, environment assignments, and CLI
  secret flags are redacted before tool payloads are persisted.
- Redaction is defense in depth, not a credential transport mechanism. Do not
  place secrets directly in command-line arguments or experiment artifacts.
- Model response content is not recorded; only model identity and token usage
  metadata are retained.
- The rail observes execution. Enforcement comes from the deterministic
  experiment tools and validator.

## Verification

Run the package and JiuwenSwarm wiring tests with:

```shell
python -m pytest tests/unit_tests/research_integrity --no-cov
python -m pytest tests/agents/swarm/test_swarm_assembly.py \
  -k experiment_integrity --no-cov
```

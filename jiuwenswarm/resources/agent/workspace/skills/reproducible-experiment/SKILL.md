---
name: reproducible-experiment
description: >-
  Run a user-approved local experiment with a predeclared command, seed, working directory, timeout, and expected artifacts; then create or verify a SHA-256 provenance manifest. Use for reproducible evaluations, benchmarks, ablations, and artifact-tampering checks. Do not use for arbitrary shell tasks or commands that contain credentials.
metadata:
  version: 1.0.0
allowed-tools: [mcp_exec_command]
---

# Reproducible experiment

Use this skill when an experiment result must remain traceable to the exact
command and files that produced it. The bundled runner uses only the Python
standard library, executes an argument vector without a shell, captures logs,
hashes every declared artifact, and can verify the result later.

## Safety boundary

- Treat the experiment command as an external side effect. Show the command,
  working directory, timeout, and expected artifacts, and obtain the user's
  approval immediately before running it.
- Do not use this workflow for a general shell request.
- Never place API keys, tokens, passwords, cookies, or private keys in the
  command or `env_allowlist`. The runner rejects conventional secret flags and
  environment names. Pass unavoidable credentials through the process
  environment without allowlisting them, and state which provider receives
  data before any networked experiment.
- Keep `cwd` and every artifact path relative to `--project-root`. Absolute
  paths and `..` traversal are rejected.
- Keep `--output-dir` under `--project-root`; paths outside it are rejected.
- The runner refuses to overwrite an existing output manifest or log.

## 1. Declare before execution

Create a JSON specification before running the experiment:

```json
{
  "schema_version": 1,
  "experiment_id": "eval-seed-42",
  "command": ["python", "evaluate.py", "--seed", "42", "--output", "results.json"],
  "cwd": ".",
  "seed": 42,
  "timeout_seconds": 3600,
  "expected_artifacts": ["results.json"],
  "env_allowlist": ["CUDA_VISIBLE_DEVICES"]
}
```

`command` must be a JSON array, not a shell string. Each expected artifact is
resolved from the experiment `cwd`. A pre-existing artifact must be rewritten
or replaced by the command; an unchanged stale file does not pass.

## 2. Run after approval

Installed skill path:

```text
~/.jiuwenswarm/agent/workspace/skills/reproducible-experiment/
```

Unix/macOS:

```bash
python ~/.jiuwenswarm/agent/workspace/skills/reproducible-experiment/scripts/reproducible_experiment.py \
  run --spec experiment.json --project-root . --output-dir .runs/eval-seed-42
```

Windows `cmd` (`~` is not expanded):

```bat
python %USERPROFILE%\.jiuwenswarm\agent\workspace\skills\reproducible-experiment\scripts\reproducible_experiment.py run --spec experiment.json --project-root . --output-dir .runs\eval-seed-42
```

The output directory receives `spec.json`, `stdout.bin`, `stderr.bin`, and
`manifest.json`.
Exit code `0` means the process succeeded and every expected artifact exists and
was freshly produced. Exit code `1` means the run was retained but failed its
execution or artifact gate. Exit code `2` means the specification or invocation
was invalid and no experiment was started.

## 3. Verify before reporting a result

```bash
python ~/.jiuwenswarm/agent/workspace/skills/reproducible-experiment/scripts/reproducible_experiment.py \
  verify --manifest .runs/eval-seed-42/manifest.json --project-root .
```

On Windows, use the `%USERPROFILE%` form shown above. Verification recomputes
the spec, log, and artifact hashes and checks file sizes. Never quote a result
as verified when this command exits nonzero.

## Output contract

The manifest records:

- schema version, experiment id, seed, command vector, and relative `cwd`;
- UTC start/finish, duration, exit code, and pass/fail status;
- Python/platform plus Git commit and dirty state when available;
- explicitly allowlisted non-secret environment values;
- SHA-256 and size for the declaration, stdout, stderr, and artifacts;
- missing or stale expected artifacts.

In the final response, report the manifest path, experiment status, artifact
paths, and verification result. Keep failed manifests as evidence; do not delete
or relabel them as successful.

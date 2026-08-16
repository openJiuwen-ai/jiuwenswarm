# Staged Task Lifecycle Rail

StagedTaskLifecycleRail is an optional, generic DeepAgentRail for long-running work expressed as ordered stages. It records a JSON-safe lifecycle snapshot through the existing session state API.

## Enablement

The rail is disabled by default. Enable it with a strict boolean in the runtime react configuration:

~~yaml
react:
  staged_task_lifecycle: true
~~

The per-instance adapter override is also supported. Accepted forms are true/false and {"enabled": true}/{"enabled": false}. Strings, numbers, lists, and other values do not enable the feature.

The normal configuration path is:

~~text
config/config.yaml
  -> JiuwenSwarm get_config()
  -> config["react"]
  -> JiuWenSwarmDeepAdapter.create_instance()
  -> _build_agent_rails(config, config_base)
  -> optional StagedTaskLifecycleRail
~~

When disabled, existing rail construction and behavior are unchanged. No model call, database, checkpoint service, or new dependency is introduced.

## Staged-task namespace

Callers should provide a staged_task mapping through callback extra, a plain-dict run_context, or structured RunContext.extra. The mapping may contain:

- task_id, stage_id, stage_name
- artifact_refs
- checkpoint_ref
- JSON-compatible metadata

Ordinary metadata without the explicit staged_task namespace is ignored unless it contains a staged-specific key (task_id, stage_id, stage_name, artifact_refs, or checkpoint_ref).

If one session runs multiple independent staged tasks, callers must provide different explicit task_id values. Changing the explicit task ID resets the prior task's stages, iterations, current stage, and failure state. Without an explicit task ID, the existing session-based continuation behavior is preserved.

## Lifecycle

The rail observes before_invoke, before_task_iteration, after_task_iteration, and after_invoke:

1. before_invoke marks the task RUNNING.
2. before_task_iteration marks the current stage RUNNING.
3. Successful after_task_iteration marks only the stage and iteration COMPLETED; the overall task remains RUNNING.
4. Failed after_task_iteration marks the stage, iteration, and task FAILED, including the sanitized task failure.
5. Successful after_invoke marks the overall task COMPLETED.
6. Failed after_invoke marks the overall task FAILED.

A completed stage is not equivalent to a completed task. Historical failed stages may remain in the snapshot while a later recovered invoke ultimately completes the task.

The supported stage statuses are PENDING, RUNNING, COMPLETED, and FAILED. get_snapshot(session) returns a detached JSON-safe mapping containing task, stages, current_stage, iterations, and overall status.

## Artifact and persistence boundaries

Artifact handling is reference-only. The artifact reference count is bounded to 256. The rail does not read artifact paths, calculate hashes, normalize paths, or create an artifact database. Metadata depth and individual payload size are not bounded by this rail.

checkpoint_ref is recorded only as a caller-supplied reference. No checkpoint backend is added. State is stored under jiuwenswarm.staged_task_lifecycle through the existing session state methods, so deployments retain ownership of persistence and cleanup policy.

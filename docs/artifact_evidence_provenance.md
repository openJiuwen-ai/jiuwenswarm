# Artifact / Evidence Provenance

## Purpose

This contract gives agent workflows stable, traceable identities for explicit
artifact references. It is domain-neutral and applies to data analysis, coding,
office, research, multi-agent, and long-running workflows. It does not create a
database or inspect artifact contents.

## Data contract

The canonical type is `ArtifactProvenance`. Its optional fields are:

- `artifact_id`: stable artifact identity.
- `evidence_id`: optional evidence identity.
- `uri`, `name`, `mime_type`: caller-supplied reference information.
- `content_hash`: caller-supplied structured hash such as `sha256:<hex>` or
  another namespaced hash scheme.
- `source`: open mapping with optional `type`, `uri`, `identifier`, and
  `metadata`; source types are not closed by an enum.
- `producer`: caller-supplied fields such as `agent_id`, `tool_name`,
  `tool_call_id`, `session_id`, `task_id`, and `stage_id`. Missing producer data
  is omitted rather than replaced by a fabricated value.
- `task_id`, `stage_id`, `created_at`, and JSON-safe `metadata`.

Nested `artifact_provenance` input is accepted, then normalized to one flat
canonical artifact mapping. The legacy `hash` input is accepted as an alias for
`content_hash`; the normalized output uses `content_hash`.
The hash algorithm namespace is lowercased after surrounding whitespace is
trimmed; the digest/value is otherwise preserved and not format-validated.

## Identity rules

An explicit `artifact_id` wins. Otherwise, a caller-supplied `content_hash`
produces a deterministic ID. Otherwise, a canonical JSON serialization of
normalized stable reference scalar fields is hashed. The fallback basis uses
artifact reference fields and `source.type`, `source.uri`, and
`source.identifier`; workflow context and arbitrary metadata are excluded.
No UUID, random value, or timestamp is used in fallback identity generation.

When `evidence_id` is omitted, it defaults to the normalized `artifact_id`. This
keeps the first contract simple for later claim binding while allowing callers to
provide a distinct evidence identity when needed. Evidence ID uniqueness and
referential integrity are not enforced by this stateless contract.

## Normalization and safety

`normalize_artifact_ref` accepts a URI string, a mapping, or the typed contract.
Malformed values are skipped or reduced to an empty metadata mapping. Values are
JSON-safe with `allow_nan=False` semantics. Credential-like keys and strings are
recursively sanitized with the existing masking helper.

`normalize_artifact_refs` preserves first occurrence order, deduplicates by
`artifact_id`, and bounds the collection at 256 references. Metadata payload size is
not claimed to be fully bounded by this contract.

The helper never opens a URI or path, reads artifact content, calculates a file
hash, canonicalizes a server path, or creates persistence. `content_hash` is only
recorded when supplied in a structured string form.

## StagedTaskLifecycleRail integration

Task lifecycle snapshots continue to expose `stage["artifact_refs"]`. Existing
URI strings and legacy mappings remain accepted. Each accepted reference now
passes through the shared provenance normalizer, so snapshots can carry stable
`artifact_id`, default or explicit `evidence_id`, source, producer, hash, task,
stage, and metadata fields. The rail does not duplicate provenance logic.

## Stream and E2A propagation

When a tool result or callback context explicitly contains an
`artifact_provenance` namespace, `JiuSwarmStreamEventRail` adds a sanitized
`tool_result.artifact_provenance` list. The extension is additive. With no explicit
provenance input, the existing tool result fields are unchanged. Tool output text
is never parsed to guess artifacts.

E2A public types were not changed. Existing `E2AResponse.metadata`,
`E2AProvenance.details`, and `E2AFileRef._meta` can carry this contract where
an integration already uses those slots, but automatic E2A projection is not
implemented by Contribution 002.

## Security and path handling

Authorization values, cookies, API keys, passwords, bearer tokens, request
headers, environment-variable mappings, and supported free-text credential
forms are masked or replaced. Caller-supplied paths may remain in an internal
reference when explicitly provided. The external projection omits `path` and
local absolute `uri` values while retaining ordinary network URIs. The contract
does not add the current working directory, HOME, workspace root, or any other
server path.

## Backward compatibility

The contract is passive and optional. Old `artifact_refs` URI strings remain valid;
stream payloads without explicit provenance retain their prior shape; E2A public
constructors are unchanged; and no new dependency is introduced.

## Non-goals

This is not a Citation Retriever, Claim Verifier, Evidence Store, vector or SQL
database, file hashing service, paper reader, reviewer, model integration, or
resource ledger.

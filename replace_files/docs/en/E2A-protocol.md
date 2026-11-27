# E2A (Everything-to-Agent) Protocol

> **Implementation**: `jiuwenclaw/e2a/` (`models.py`, `adapters.py`, `constants.py`, `__init__.py`). **Version**: `E2A_PROTOCOL_VERSION` (default `1.0`). **On conflict**: treat the dataclass fields in `models.py` as the source of truth and update this document accordingly. Requests: `E2AEnvelope`; responses: `E2AResponse`.  
> **中文版**：[../zh/E2A-protocol.md](../zh/E2A-protocol.md)

---

## 0. Document layout and single source of truth

| Location | Role |
|----------|------|
| **docs/zh/E2A-protocol.md** | Same specification in Chinese |
| **docs/en/E2A-protocol.md** (this file) | Normative description, pitfalls, examples (English) |
| `jiuwenclaw/e2a/models.py` | Request `E2AEnvelope`, response `E2AResponse`, nested types; `from_dict` / `to_dict` |
| `jiuwenclaw/e2a/constants.py` | **`E2A_SOURCE_PROTOCOL_*`**, **`E2A_RESPONSE_KINDS`**, **`E2A_RESPONSE_STATUS_*`**, ACP method names and SessionUpdate strings (runtime: use tuples in code) |
| `jiuwenclaw/e2a/adapters.py` | ACP / A2A → E2A; E2A → ACP JSON-RPC; **`E2AResponse` → ACP / A2A projections** (§8) |
| `jiuwenclaw/e2a/__init__.py` | Public exports |
| `jiuwenclaw/e2a/ACP-reference.md` / `A2A-reference.md` | External protocol notes; **not** normative for E2A |

---

## 1. Scope and boundaries

- Payloads sent to AgentServer **after Gateway normalization** are expressed as E2A. Raw channel fields are **mapped at the Gateway edge** into normalized fields (`user_id`, `message_id`, `params`, etc.); the full channel JSON is **not** required to be the E2A primary path.
- **`channel_context`**: optional; transitional or residual keys that do not fit normalized fields; `from_dict` may migrate legacy `metadata` for compatibility.
- E2A **does not** specify the transport (WebSocket, HTTP, etc.).

---

## 2. Pitfall: two meanings of `method` (required reading)

The same field **`method`** means different things by origin. It is **not** two different fields; do not confuse it with `request_id` / `message_id`.

| Origin | Typical `method` values | Notes |
|--------|-------------------------|-------|
| **Gateway → AgentServer** | `chat.send`, `history.get`, `chat.interrupt`, … | In-project RPC names; **not** enumerated in `constants.ACP_CLIENT_TO_AGENT_METHODS` |
| **ACP via `envelope_from_acp_jsonrpc`** | `session/prompt`, `initialize`, … | Same as JSON-RPC `method`; full list: **`constants.ACP_CLIENT_TO_AGENT_METHODS`** |
| **Internal special** | `null` | e.g. heartbeat with no RPC-style operation name |

**Export to ACP**: `envelope_to_acp_jsonrpc_call` only uses `merge_params_to_acp_prompt` to fill `prompt` from **`params`** when `method == "session/prompt"`. If `method` is `chat.send`, you must **map explicitly** in an adapter to ACP’s `session/prompt` and `params`; otherwise the JSON-RPC is invalid for a pure ACP peer.

---

## 3. Relationship to ACP and A2A (summary)

| Aspect | ACP | A2A | E2A (internal) |
|--------|-----|-----|----------------|
| Role | JSON-RPC session | Task / Message / Card | Unified envelope; externals **adapt in** |
| `method` | JSON-RPC method | Abstract op (binding maps) | See **§2** |
| Extension | `_meta` | `metadata` | Gateway uses normalized fields; `a2a_metadata` / `acp_meta` for interop only |

---

## 4. `E2AEnvelope` field reference

### 4.1 Core and correlation

| Field | Type | Description |
|-------|------|-------------|
| `protocol_version` | string | Default `1.0` |
| `request_id` | string / null | Primary Gateway↔AgentServer id (stream chunks align on this) |
| `jsonrpc_id` | string / number / null | JSON-RPC `id` when ingested from ACP |
| `correlation_id` | string / null | Tracing; distinct from `request_id` |
| `task_id` / `context_id` | string / null | A2A alignment |
| `session_id` | string / null | Session |
| `message_id` | string / null | Platform or A2A message id; do not conflate with `request_id` |
| `is_stream` | boolean | Streamed response or not |

### 4.2 `provenance` (`E2AProvenance`)

| Field | Description |
|-------|-------------|
| `source_protocol` | `e2a` / `acp` / `a2a` (see `E2A_SOURCE_PROTOCOL_*`) |
| `converter` / `converted_at` / `details` | Converter id, time, hints (e.g. `jsonrpc_method`) |

Hand-built defaults use `source_protocol` = `e2a`. Legacy `binding` migrates to `provenance.details.migrated_from_binding`.

### 4.3 Time, identity, ingress

| Field | Description |
|-------|-------------|
| `timestamp` | **Normative**: RFC 3339 UTC string; `from_dict` may accept float/int epoch seconds |
| `identity_origin` | `system` / `user` / `agent` / `service` |
| `channel` | Ingress name; **use `channel` only**. `from_dict` may read `channel_id` |
| `user_id` / `source_agent_id` | User or upstream agent |

`timestamp` (business time) ≠ `provenance.converted_at` (E2A conversion time).

### 4.4 `method`, `params`, bridging

| Field | Description |
|-------|-------------|
| `method` | See **§2**; `from_dict` may read `req_method` |
| **`params`** | **Single business-parameter dict** (aligned with JSON-RPC `params`): options, user text, multimodal blocks, attachments **all** go here; **do not** use a top-level `payload` (see table below) |
| `ext_method` | Real name when `method == "ext"` |
| `session_update_kind` | For ACP `session/update` bridge; values match `constants.ACP_SESSION_UPDATE_KINDS` |
| `expected_output_modes` | A2A `acceptedOutputModes` |

**Common `params` keys** (extend as needed; names below cooperate with ACP bridging):

| Key | Purpose |
|-----|---------|
| `prompt` | Use as-is when already an ACP ContentBlock array (`merge_params_to_acp_prompt` does not overwrite) |
| `content_blocks` | JSON array isomorphic to ACP `Vec<ContentBlock>`; when `method==session/prompt` and no `prompt`, can map to `prompt` |
| `text` / `content` / `query` | Plain user text; if no `prompt`/`content_blocks`, first non-empty string becomes a single `text` block |
| `files` / `attachments` | Attachment lists; elements follow **`E2AFileRef`** (`uri` required) |
| Others | e.g. `mode`, `page_idx`, heartbeat text—extend per Gateway RPC |

### 4.5 `auth` and extension slots

| Field | Description |
|-------|-------------|
| `auth` | `E2AAuth`: credential references, etc. |
| `channel_context` | Optional overflow / legacy `metadata`; see **§1** |
| `a2a_metadata` | **A2A interop only** (adapter arg `metadata` maps here—not Gateway `metadata`) |
| `acp_meta` | Merged into `params._meta` by `merge_params_to_acp_prompt` (§7) |

---

## 5. Nested types (summary)

- **`E2AFileRef`**: recommended attachment element (`uri` required; `name`, `mime_type`, `size`, `_meta` optional); usually in **`params.files`** or **`params.attachments`**.
- **`E2AAuth`**: `method_id`, `bearer_token`, `api_key_ref`, `credential_ref`, `extra_headers`, `_meta`

---

## 6. ACP strings in `constants.py` (bridge reference)

For ACP bridging and doc cross-check only; **at runtime** use tuples in `jiuwenclaw.e2a.constants`:

- **`ACP_CLIENT_TO_AGENT_METHODS`**: client → Agent JSON-RPC method names
- **`ACP_AGENT_TO_CLIENT_METHODS`**, **`ACP_NOTIFICATION_NAMES`**: downstream / notifications
- **`ACP_SESSION_UPDATE_KINDS`**: values for `session_update_kind`
- **`E2A_RESPONSE_KINDS`**, **`E2A_RESPONSE_STATUS_*`**, **`E2A_A2A_STREAM_BRANCHES`**: response `response_kind` / `status` / `a2a.stream_event.branch` (see **§12**)

**Gateway RPC** names (`chat.send`, etc.) are project-defined and **not** in those tuples.

---

## 7. `merge_params_to_acp_prompt`

Python: `jiuwenclaw.e2a.merge_params_to_acp_prompt(envelope)`.

If and only if **`method == "session/prompt"`**:

1. If `params` already has `prompt`, do not overwrite.
2. Else if `params.content_blocks` is a non-empty array, use as `prompt`.
3. Else if any of `params.text`, `params.content`, `params.query` is a non-empty string, build `{ "type": "text", "text": "..." }` as `prompt`.
4. If the envelope has `session_id` and `params` lacks `session_id`, set it.
5. Merge `envelope.acp_meta` into `params._meta`.

For any other `method`, return a shallow copy of `params`.

---

## 8. Adapters (`adapters.py`)

| Function | Role |
|----------|------|
| `envelope_from_acp_jsonrpc` | ACP JSON-RPC → E2A; `provenance.source_protocol=acp`; `method` is ACP method |
| `envelope_from_a2a_send_message` | A2A SendMessage semantics → E2A; adapter **`metadata`** → **`a2a_metadata`** (A2A-only, not Gateway `metadata`) |
| `envelope_to_acp_jsonrpc_call` | E2A → JSON-RPC call shape; does not change `provenance` |
| `e2a_response_to_acp_jsonrpc_response` | **`E2AResponse`** → one JSON-RPC **response** object (`result` or `error`); returns `None` if not applicable |
| `e2a_response_to_a2a_stream_payload` | **`E2AResponse`** (`a2a.stream_event`) → A2A **`StreamResponse`**-shaped JSON (exactly one branch); returns `None` if not applicable |

---

## 9. Serialization and compatibility keys

- `E2AEnvelope.to_dict()` / `from_dict(data)`
- **Read compatibility**: `channel_id`→`channel`, `req_method`→`method`, `metadata`→`channel_context` (only if `channel_context` empty), `binding`→`provenance` (above)
- **Optional**: if top-level legacy **`payload`** (object) exists, keys are **merged into `params`** without overwriting existing `params` keys; **new protocol must not** send top-level `payload`.

---

## 10. Mapping from `AgentRequest`

| AgentRequest | E2AEnvelope |
|--------------|-------------|
| `request_id` | `request_id` |
| `channel_id` | `channel` |
| `session_id` | `session_id` |
| `req_method` | `method` |
| `params` | `params` |
| `is_stream` | `is_stream` |
| `timestamp` (float) | `timestamp` (string, auto-normalized) |
| `metadata` | Target: map to normalized fields; compat: `from_dict`→`channel_context` |

---

## 11. Examples (JSON shapes)

### 11.1 After Gateway normalization → AgentServer (chat, streaming)

```json
{
  "protocol_version": "1.0",
  "request_id": "req_abc_01",
  "session_id": "sess_xyz",
  "channel": "web",
  "method": "chat.send",
  "is_stream": true,
  "timestamp": "2026-03-28T12:00:00+00:00",
  "identity_origin": "user",
  "user_id": "u_001",
  "params": {
    "content": "Hello",
    "mode": "plan",
    "query": "Hello"
  },
  "provenance": {
    "source_protocol": "e2a"
  }
}
```

`channel_context` may be omitted (or `{}` when serialized empty).

### 11.2 Legacy `AgentRequest` shape (readable by `from_dict`)

```json
{
  "request_id": "req_legacy_1",
  "channel_id": "feishu",
  "session_id": "sess_feishu_1",
  "req_method": "chat.send",
  "is_stream": true,
  "timestamp": 1774524781.15,
  "params": {
    "content": "List desktop files",
    "mode": "plan",
    "query": "List desktop files"
  },
  "metadata": {
    "feishu_open_id": "ou_xxx",
    "message_id": "om_xxx"
  }
}
```

After parse: `channel`=`feishu`, `method`=`chat.send`, `timestamp` normalized to RFC 3339; if not mapped elsewhere, `metadata` lands in `channel_context` (migration). **Target state**: Gateway should map `feishu_open_id` into `user_id` / `message_id`, etc., and minimize `channel_context`.

### 11.3 ACP ingress (illustrative; built by `envelope_from_acp_jsonrpc`)

- `method` = `"session/prompt"`
- `jsonrpc_id` = `42`
- `params` = `{ "session_id": "s1", "prompt": [...] }`
- `provenance.source_protocol` = `"acp"`, `details` includes `jsonrpc_method`, etc.

### 11.4 Exported ACP JSON-RPC (`envelope_to_acp_jsonrpc_call` shape)

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "session/prompt",
  "params": {
    "session_id": "s1",
    "prompt": [{"type": "text", "text": "hi"}]
  }
}
```

`merge_params_to_acp_prompt` only fills `prompt` from `content_blocks` or `text`/`content`/`query` when envelope `method == "session/prompt"` and `params` has no `prompt` yet.

### 11.5 `session/prompt` + multimodal (all in `params`)

```json
{
  "method": "session/prompt",
  "session_id": "s1",
  "jsonrpc_id": 1,
  "params": {
    "content_blocks": [
      {"type": "text", "text": "Describe this image"},
      {"type": "image", "mime_type": "image/png", "data": "<base64 omitted>"}
    ]
  }
}
```

After `merge_params_to_acp_prompt`, `params` gains a `prompt` equivalent to `content_blocks` if there was no `prompt` before.

---

## 12. E2A response protocol (`E2AResponse`)

Every outbound record from Agent → Gateway → client (including each streaming frame) is expressed as **`E2AResponse`**, symmetric with **`E2AEnvelope`** (`snake_case`, RFC 3339 `timestamp`, reuse **`E2AProvenance`**).

### 12.1 Three layers

| Layer | Name | Role |
|-------|------|------|
| **L1** | Response envelope | Correlation ids, ordering, stream end, status, provenance |
| **L2** | Discriminated payload | **`response_kind`** + **`body`** (shape constrained by kind) |
| **L3** | `projections` (optional) | Under `acp` / `a2a`, pre-built wire JSON for interop—**auxiliary only**; consumers **must not** treat L3 as the sole source of truth |

### 12.2 `is_final` and `status`

- **`is_final`**: `true` means **no further** `E2AResponse` records for the same **`request_id`**. This is **not** “success”; a failed final frame may still have `is_final: true`.
- **`status`**: one of **`constants.E2A_RESPONSE_STATUS_*`** (`succeeded` / `failed` / `in_progress`). When **`status == failed`**, **`response_kind`** should be **`e2a.error`** (see **§12.6** if `acp.jsonrpc_error` is also used).

### 12.3 Invariants

- For one **`request_id`**, **`sequence`** starts at **0** and strictly increases (0, 1, 2, …).
- Normal completion: **exactly one** record has **`is_final: true`**.
- Abnormal cases (disconnect, crash): an `is_final` frame may be missing; use timeouts and connection state—this spec does not require a synthetic final frame.

### 12.4 L1: envelope fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `protocol_version` | string | yes | Same as request; default `1.0` |
| `response_id` | string | yes | Unique id for this record (UUID recommended) |
| `request_id` | string | yes | Echo request; stream chunk alignment |
| `sequence` | integer | yes | Per-`request_id`, starting at 0 |
| `is_final` | boolean | yes | `true` on the last frame |
| `status` | string | yes | `succeeded` / `failed` / `in_progress` |
| `response_kind` | string | yes | See **§12.5**; runtime: **`constants.E2A_RESPONSE_KINDS`** |
| `timestamp` | string | yes | RFC 3339 UTC; when this record was produced |
| `provenance` | object | yes | **`E2AProvenance`** (same shape as requests) |
| `body` | object | yes | L2 payload |
| `jsonrpc_id` | string / number / null | no | Echo ACP JSON-RPC `id` |
| `correlation_id` | string | no | Tracing |
| `task_id` / `context_id` / `session_id` / `message_id` | string | no | Same meaning as on the request envelope |
| `is_stream` | boolean | no | Whether the originating request was streaming |
| `identity_origin` | string | no | Often **`agent`** for agent-originated responses |
| `channel` / `user_id` / `source_agent_id` | same as request | no | Echo |
| `method` | string / null | no | Echo request `method` (including heartbeat `null`) |
| `metadata` | object | no | **Channel/business** key-value map; aligns with legacy **`AgentResponse.metadata`**; on **protocol conversion failure**, may hold fallback data. **AgentServer→Gateway WebSocket** uses **`constants.E2A_WIRE_LEGACY_AGENT_RESPONSE_KEY`** / **`E2A_WIRE_LEGACY_AGENT_CHUNK_KEY`** for full legacy JSON blobs; use channel-prefixed keys for app metadata |
| `projections` | object | no | L3: optional `acp`, `a2a` keys |
| `channel_context` / `a2a_metadata` / `acp_meta` | object | no | Same spillover slots as request; **distinct from `metadata`**—`a2a_metadata` / `acp_meta` are ACP/A2A interop only; `metadata` is general business and fallback |

### 12.5 L2: `response_kind` and `body`

Runtime list: **`constants.E2A_RESPONSE_KINDS`**; **`body`** is a **dict**. Normative summary:

| `response_kind` | `body` highlights |
|-----------------|-------------------|
| `e2a.complete` | **`result`**: business outcome (mirror the “single `params` dict” philosophy for requests) |
| `e2a.chunk` | **`delta_kind`**: `text` / `reasoning` / `tool` / `custom`; **`delta`**: string or structured object; optional **`mime_type`** |
| `e2a.error` | **`code`**, **`message`**, **`details`**; optional **`external`**: `{ "protocol": "acp"|"a2a", "type": string, "payload": object }` |
| `acp.session_update` | ACP `session/update` notification payload; **`sessionUpdate`** aligns with **`ACP_SESSION_UPDATE_KINDS`** |
| `acp.prompt_result` | JSON-RPC **`result`** for `session/prompt` (e.g. `stop_reason`, optional `usage`, `_meta`) |
| `acp.jsonrpc_error` | JSON-RPC **`error`** semantics: `code`, `message`, optional `data` (prefer **`e2a.error`** as the canonical failure kind; use this for ACP-edge projection if needed) |
| `a2a.task` | A2A **`Task`** JSON (**snake_case** keys aligned with `spec/a2a.proto`) |
| `a2a.message` | A2A **`Message`** JSON (same) |
| `a2a.stream_event` | **`branch`**: `task` / `message` / `status_update` / `artifact_update` (**`E2A_A2A_STREAM_BRANCHES`**); **`payload`**: branch object |
| `ext` | **`ext_method`** + **`params`** (symmetric to request `ext`) |

**Recommendation**: internally prefer **`e2a.complete`** / **`e2a.chunk`** / **`e2a.error`**; build **`acp.*`** / **`a2a.*`** or **`projections`** at the edge.

### 12.6 Mapping to ACP / A2A (summary)

- **ACP**: JSON-RPC response from **`jsonrpc_id`** + **`acp.prompt_result`** (`result`) or **`e2a.error`** / **`acp.jsonrpc_error`** (`error`); **`acp.session_update`** maps to **`session/update`** **`params`**. See **`e2a_response_to_acp_jsonrpc_response`** (§8).
- **A2A**: **`a2a.stream_event`** matches **`StreamResponse`** branches; **`e2a_response_to_a2a_stream_payload`** emits one event. **`a2a.task`** / **`a2a.message`** match SendMessage outcomes.

### 12.7 Logging (e.g. `E:\logs`)

- One line per **`E2AResponse`** JSON; sort/replay by **`sequence`**.
- Minimum keys: **`request_id`**, **`response_id`**, **`sequence`**, **`is_final`**, **`response_kind`**, **`timestamp`**.
- Do not log secrets (`bearer_token`, sensitive model output). Large **`body`** may later use **`body_ref`** (not defined in this version).

### 12.8 Example: two chunks + final

```json
{"protocol_version":"1.0","response_id":"r1","request_id":"req_1","sequence":0,"is_final":false,"status":"in_progress","response_kind":"e2a.chunk","timestamp":"2026-03-29T12:00:00+00:00","provenance":{"source_protocol":"e2a"},"body":{"delta_kind":"text","delta":"Hello"}}
```

```json
{"protocol_version":"1.0","response_id":"r2","request_id":"req_1","sequence":1,"is_final":false,"status":"in_progress","response_kind":"e2a.chunk","timestamp":"2026-03-29T12:00:01+00:00","provenance":{"source_protocol":"e2a"},"body":{"delta_kind":"text","delta":" world"}}
```

```json
{"protocol_version":"1.0","response_id":"r3","request_id":"req_1","sequence":2,"is_final":true,"status":"succeeded","response_kind":"e2a.complete","timestamp":"2026-03-29T12:00:02+00:00","provenance":{"source_protocol":"e2a"},"body":{"result":{"content":"Hello world"}}}
```

Cross-reference: request log normalization in **`E2A-AgentRequest-log-migration.md`**; response logging can adopt the same single-line JSON convention later.

---

## 13. Security notes

- Prefer `credential_ref` / `api_key_ref` and ticket exchange at the Gateway; do not log full `bearer_token`.
- Use TLS and least privilege across trust boundaries.

---

*Maintained in sync with `jiuwenclaw/e2a/models.py` (`E2AEnvelope`, `E2AResponse`).*

# Debug Trace

JiuwenSwarm's debug tracing provides unified observability across **Agent and Code modes**, helping you reconstruct the full behaviour of a conversational run:

- **Local debug dump**: a human-readable plain-text record of the run's **model output, reasoning, tool calls and results, and token usage**, written per run — works out of the box with no backend.
- **Structured OTel trace** (optional): standard `gen_ai.*` spans exported via OTLP to backends such as Langfuse / Jaeger / Grafana for latency analysis and cross-run aggregation.
- **Subagent data flow**: when the main agent delegates to a subagent (builtin or custom), the subagent's internal reasoning and tool calls also land in the same dump — no longer a "black box".

The `/debug` directive and its parsing primitive are shared across both modes. The local-dump and OTel paths are independent yet cross-referenceable via `trace_id`.

---

## 1. Overview

### 1.1 What problem it solves

When debugging a misbehaving agent, the most common questions are:

- What did the model actually output this round? Did it drift off-topic?
- Which tools did it call? With what arguments? What did they return?
- How many tokens did this round cost? How long did it take?
- When it dispatched a subagent, what happened inside that subagent?

Routine application logs (see [Logging System](Logs.md)) target system operation status, are scattered by component, and rarely tell "the full story of one run". Debug tracing is built for exactly this: **organised per run, in time order, focused on model and tool behaviour**, written to a single file you can read directly.

### 1.2 Core capabilities

| Capability | Description |
|------------|-------------|
| Request-level trigger | Type `/debug <your question>` to enable the dump for just this round — no config change |
| Config-level always-on | Turn the dump on globally or per-mode in `config.yaml` |
| Unified across modes | Agent / Code both support `/debug`, sharing one directive primitive |
| Subagent data flow | Records the full flow of builtin subagents (TaskTool) and custom subagents (AgentTool), tagged by source |
| Structured OTel (optional) | Emits standard `gen_ai.*` spans, backend-agnostic, cross-referenceable with the local dump |
| Local offline upload | With the OTel `file` exporter, spans can be batch-uploaded to Langfuse later |
| Safe | Secret-like fields are always masked; large payloads are truncated; write failures never affect the model run |

### 1.3 Mode support

Both modes support debug tracing; they differ only in the dump directory and the config block used:

| Mode | `/debug` | Local dump file | OTel config block |
|------|----------|-----------------|-------------------|
| `agent.plan` / `agent.fast` | ✅ | `~/.jiuwenswarm/.agent/traces/dump-agent-<session>.txt` | `agent_observability` |
| `code.normal` / `code.plan` | ✅ | `~/.jiuwenswarm/.code/traces/dump-code-<session>.txt` | `agent_observability` |

> Both modes share the same OTel backend stack (OTel Collector + Langfuse). See [Section 5](#5-local-file-mode-offline-upload-to-langfuse) and `deploy/observability/`.

---

## 2. Prerequisites

### 2.1 Base environment

| Item | Description |
|------|-------------|
| Config file | `~/.jiuwenswarm/config/config.yaml` (default template: `jiuwenswarm/resources/config.yaml`) |
| Data directory | `~/.jiuwenswarm/`; override with the `JIUWENSWARM_DATA_DIR` env var |
| Entry point | `/debug` is a TUI / Web frontend slash command, passed through to the backend for parsing |

---

## 3. Quick Start

In any mode, type:

```
/debug help me fix the failing tests in tests/test_login.py
```

After the round finishes, open the dump for the current mode to see the complete record (model output, every tool call and result, token usage, elapsed time — and if a subagent was dispatched, the subagent's full behaviour):

```
~/.jiuwenswarm/.agent/traces/dump-agent-<session_id>.txt        # Agent mode
~/.jiuwenswarm/.code/traces/dump-code-<session_id>.txt           # Code mode
```

**No config change required.**

---

## 4. Usage

> The sections below are organised by *what you want to do*; all of them apply to Agent / Code alike — there is no per-mode split.

### 4.1 Ad-hoc debugging of one request (request-level)

For "it occasionally breaks and I don't want to change config". Prefix any message with `/debug`:

```
/debug <your question>
```

- Effective for **this round only**; subsequent rounds return to normal.
- A bare `/debug` (no question) is rejected, so an empty request never reaches the model.
- In Plan mode, even if a `<system-reminder>` block is injected before `/debug`, it is still recognised.

### 4.2 Always-on dump (config-level)

For "I always want a record for a certain mode". Edit `~/.jiuwenswarm/config/config.yaml`:

```yaml
debug_trace:
  enabled: true          # always-on globally (all Agent / Code sub-modes)
  # or only for a specific mode:
  # agent:
  #   enabled: true
```

Takes effect on the **next request** (each run reads the latest config — hot-reloadable).

### 4.3 Tracing subagent data flow

When the main agent delegates work to a subagent (the builtin `task` tool, or a custom agent created via `/agents`), the subagent's full behaviour appears automatically in the dump, distinguished by a `source=` tag:

| Source tag | Meaning |
|------------|---------|
| `source=main` | The main agent itself |
| `source=subagent:builtin:<type>` | A builtin subagent (e.g. explore / plan / code / browser) |
| `source=subagent:custom:<name>` | A custom subagent created via `/agents` |

Enabled by default. To stop recording subagent flow (Agent / Code only), turn it off in config:

```yaml
debug_trace:
  agent:
    include_subagent_flow: false
```

> When off, the subagent still executes normally — it just is not written to the dump (behaviour identical to having debug disabled).

### 4.4 Reporting structured OTel traces

OTel supports four exporter modes — pick by whether you are always online:

#### 4.4.1 OTLP gRPC → Collector → Langfuse (recommended, always-online)

The app sends spans to the local Collector; Langfuse credential auth is handled by the Collector. Start the stack first:

```bash
cd deploy/observability
docker-compose up -d
```

Then configure (`agent_observability` for Agent/Code):

```yaml
agent_observability:
  enabled: true
  exporter: otlp_grpc
  endpoint: http://localhost:4317
  sample_rate: 1.0
```

#### 4.4.2 OTLP HTTP → Langfuse direct (always-online, bypasses Collector)

```yaml
agent_observability:
  enabled: true
  exporter: otlp_http
  endpoint: https://cloud.langfuse.com/api/public/otel/v1/traces
  langfuse_public_key: "pk-lf-xxx"
  langfuse_secret_key: "sk-lf-xxx"
```

#### 4.4.3 Local `file` mode + offline upload (offline / two-phase)

For when you cannot stay connected to a backend, or want to accumulate a batch of traces before replaying. See [Section 5](#5-local-file-mode-offline-upload-to-langfuse).

#### 4.4.4 console (quick verification)

```yaml
agent_observability:
  enabled: true
  exporter: console
```

Spans are printed as JSON to the console — handy for a quick local wiring check.

---

## 5. Local file mode: offline upload to Langfuse

When you cannot (or don't want to) stay connected to a Collector, use the `file` exporter to write spans to local disk, then batch-upload them later with `deploy/observability/upload_traces_to_langfuse.py`. This is the "two-phase" approach offered by the deploy stack and **works for Agent / Code alike**.

### 5.1 How it works

```
 App (file exporter) ──writes──► ~/.jiuwenswarm/.trace/traces-<YYYY-MM-DD>.jsonl
                                                                     │
                                          upload_traces_to_langfuse.py
                                                                     ▼
                            Local OTel Collector (:4318) ──OTLP HTTP + Basic Auth──► Langfuse (:3000)
```

- The `file` exporter writes spans into **one per-day** `traces-<YYYY-MM-DD>.jsonl`, stored flat under `traces_dir` (default `~/.jiuwenswarm/.trace`). **Each line is a standalone single-span OTLP JSON.**
- Spans from different traces are **interleaved** in the same daily file — on upload the Collector re-groups them by the `traceId` carried on each span, so interleaving does not affect replay.
- Disk writes are flushed asynchronously by the `BatchSpanProcessor` (default every 5s / 512 spans, and on shutdown), **never blocking the business thread**.
- `session.id` is read by Langfuse from span attributes, not from the filename.

### 5.2 Step 1 — configure the file exporter

Edit `~/.jiuwenswarm/config/config.yaml` (`agent_observability` covers both Agent and Code):

```yaml
agent_observability:
  enabled: true
  exporter: file
  traces_dir: ""               # empty → default ~/.jiuwenswarm/.trace
  file_retention_days: 7       # stale .jsonl files are lazily pruned
  sample_rate: 1.0
```

### 5.3 Step 2 — start the Collector + Langfuse stack

```bash
cd deploy/observability
docker-compose up -d            # ~30-60s to become healthy on first start
docker-compose ps               # check status
```

Langfuse UI:

- URL: `http://localhost:3000`
- Login: `jiuwenswarm@jiuwen.local` / `jiuwenswarm`
- Project keys: `pk-lf-jiuwen` / `sk-lf-jiuwen` (auth is handled by the Collector's `otel-collector-config.yaml`; **the upload script itself needs no credentials**)

### 5.4 Step 3 — run the upload script

```bash
cd deploy/observability

# When traces_dir is not configured, trace files land in ~/.jiuwenswarm/.trace/ by default;
# running with no arguments uploads that default directory:
python upload_traces_to_langfuse.py

# Or pass the default directory explicitly (equivalent to the line above):
python upload_traces_to_langfuse.py ~/.jiuwenswarm/.trace
python upload_traces_to_langfuse.py --dir ~/.jiuwenswarm/.trace

# Upload a single day's file from the default directory:
python upload_traces_to_langfuse.py --file ~/.jiuwenswarm/.trace/traces-2026-07-24.jsonl

# Upload a custom directory / pass a custom Collector endpoint:
python upload_traces_to_langfuse.py ./traces_run_001 --endpoint http://localhost:4318/v1/traces
```

The script POSTs each `.jsonl` file line-by-line to the Collector's OTLP HTTP endpoint (default `http://127.0.0.1:4318/v1/traces`, no auth), which forwards to Langfuse. After the run it prints the trace IDs that were successfully replayed:

```
[upload] source=~/.jiuwenswarm/.trace  files=3  endpoint=http://127.0.0.1:4318/v1/traces
[upload] total_lines=128 ok=128 fail=0 elapsed=0.6s
[upload] trace_ids (3):
  4f3c1a8b9d2e4f6081a3c5b7d9e1f2a3
  8e1c0b2d4f6a8c9e1d3b5a7c9e1f0a2b
  1a2b3c4d5e6f70819203a4b5c6d7e8f9
```

Exit codes: `0` all succeeded; `1` some lines failed; `2` path not found or no `.jsonl` files.

### 5.5 Notes

- **Use `127.0.0.1`, not `localhost`**: on macOS, `getaddrinfo("localhost")` returns IPv6 `::1` first; Docker Desktop's IPv6 port-forward returns `502 Bad Gateway`, failing every upload. The script defaults to `127.0.0.1`; if you pass `--endpoint`, keep it that way.
- **Data directory**: the script defaults to `~/.jiuwenswarm/.trace`; if `JIUWENSWARM_DATA_DIR` is set it reads `<that dir>/.trace`, matching the file exporter's output path.
- **Cleanup**: `*.jsonl` older than `file_retention_days` is lazily deleted by the exporter (a sweep runs at most every 64 exports) — no manual cleanup needed.
- **Stop the stack**: `docker-compose down` (keep data) / `docker-compose down -v` (remove data volumes too).

---

## 6. Output

### 6.1 Dump file locations

| Mode | Path |
|------|------|
| Agent | `~/.jiuwenswarm/.agent/traces/dump-agent-<session_id>.txt` |
| Code | `~/.jiuwenswarm/.code/traces/dump-code-<session_id>.txt` |
| OTel file mode | `~/.jiuwenswarm/.trace/traces-<YYYY-MM-DD>.jsonl` |

- A local dump is **one file per session**, opened in append mode: rounds of one session append chronologically, separated by `run start` / `run end` boundaries.
- The `session_id` is sanitised to safe characters and cannot escape its directory.

### 6.2 Dump file format

```text
========== run start ==========
timestamp=2026-07-24 10:12:31.123
mode=agent.plan
session_id=sess_a1b2
request_id=req_c3d4
otel_trace_id=4f2e...e1          ← empty when OTel is not enabled
otel_span_id=8a3f...b2
input=help me fix the failing tests   ← user input preview

[INFO] mode=agent.plan source=main category=text
  | Let me look at the test file and the error first.

[DEBUG] mode=agent.plan source=main category=tool_call
  | tool_name=bash tool_call_id=call_123
  | arguments={"command":"pytest -q tests/test_login.py"}

[DEBUG] mode=agent.plan source=main category=tool_result
  | tool_name=bash tool_call_id=call_123
  | result: FAILED tests/test_login.py::test_login - AssertionError ...

[DEBUG] mode=agent.plan source=main category=context_usage
  | input_tokens=1234 output_tokens=567 total_tokens=1801 model_name=...

========== subagent start ==========
timestamp=2026-07-24 10:12:40.002
source=subagent:builtin:explore_agent
prompt=find every call site of login() in the repo
... (the subagent's model output, tool calls, etc., same format) ...
========== subagent end ==========
timestamp=2026-07-24 10:12:52.880
source=subagent:builtin:explore_agent
status=ok

========== run end ==========
timestamp=2026-07-24 10:12:58.456
status=ok
elapsed_ms=27333
chunks=42
```

On error or cancellation, `run end` writes `status=error` (with `error_type` / `error`) or `status=cancelled`.

### 6.3 Recorded categories

| category | Meaning | Default |
|----------|---------|---------|
| `text` | Model natural-language output | yes (`include_model_output`) |
| `reasoning` | Reasoning fragments | yes (`include_reasoning`) |
| `tool_call` | Tool call (name, ID, arguments) | yes (`include_tool_args`) |
| `tool_result` | Tool result / error | yes (`include_tool_result`) |
| `tool_update` | Tool progress update | yes |
| `context_usage` | Token usage, model name | yes |
| other | Controller output / messages / todos, etc. | yes |

### 6.4 Cross-referencing the OTel trace

The dump's `run start` contains `otel_trace_id` / `otel_span_id` (empty when OTel is off). Paste it into the Langfuse / Jaeger search box to locate the full span tree for this run; conversely, a Langfuse trace can lead you back to the local dump.

---

## 7. Configuration Reference

### 7.1 `debug_trace` — Agent / Code local text dump

```yaml
debug_trace:
  enabled: false                    # global switch (true = always-on for Agent/Code)
  agent:                            # agent mode (agent.plan / agent.fast …)
    enabled: false                  # agent-mode-only switch
    dump_enabled: true              # write local dump when debug is on
    otel_enabled: false             # also force-enable OTel during debug
    include_model_output: true
    include_reasoning: true
    include_tool_args: true
    include_tool_result: true
    include_subagent_flow: true     # record subagent data flow
  code:                             # code mode (code.normal / code.plan …)
    enabled: false
    dump_enabled: true
    otel_enabled: false
    include_model_output: true
    include_reasoning: true
    include_tool_args: true
    include_tool_result: true
    include_subagent_flow: true
  limits:
    tool_args_max_chars: 2000       # tool-args truncation threshold
    tool_result_max_chars: 8000     # tool-result truncation threshold
    generic_payload_max_chars: 4000 # generic payload / run input threshold
    max_model_output_chars:         # empty = never cap model output; a number caps it
  redaction:
    redact_prompts: false           # secret-key masking is always on regardless of these
    redact_completions: false
```

### 7.2 `agent_observability` — Agent / Code structured OTel

```yaml
agent_observability:
  enabled: false                    # single-agent observability master switch
  exporter: file                    # otlp_grpc / otlp_http / file / console
  endpoint: http://localhost:4317   # OTLP endpoint (not needed for file/console)
  service_name: jiuwenswarm-agent   # OTel resource service.name
  sample_rate: 1.0                  # sampling rate (0.0-1.0)
  redact_prompts: false             # redact prompt content
  redact_completions: false         # redact completion content
  attribute_value_max_length: 10240 # max OTel attribute value length
  langfuse_public_key: ""           # Langfuse OTLP auth public key (needed for otlp_http direct)
  langfuse_secret_key: ""           # Langfuse OTLP auth secret key
  traces_dir: ""                    # file exporter output dir, default ~/.jiuwenswarm/.trace
  file_retention_days: 7            # file exporter retention in days
```

> **How the two blocks relate**: `debug_trace` governs Agent/Code local text dumps; `agent_observability` governs Agent/Code OTel spans. Both share the same `deploy/observability` backend stack. The single coupling point is `debug_trace.<mode>.otel_enabled` — it lets a `/debug` run temporarily pull up Agent/Code OTel.

### 7.3 Resolution rules (debug_trace)

```
debug_enabled = request has /debug  OR  debug_trace.<mode>.enabled
dump_enabled  = debug_enabled  AND  debug_trace.<mode>.dump_enabled != false
otel_enabled  = debug_enabled  AND  debug_trace.<mode>.otel_enabled
```

- `<mode>` collapsing: a mode name starting with `code` uses the `code` section, otherwise `agent`.
- Config reading is best-effort: any failure falls back to "request-level-only" behaviour.

---

## 8. FAQ

**Q: I typed `/debug` but no dump file appeared?**
Check: ① you looked in the right path for the current mode (see 6.1); ② data-directory permissions; ③ whether the app logs contain a warning like `[DebugTrace] disabled ... open failed`.

**Q: Where is the dump file?**
By mode, under `~/.jiuwenswarm/.agent/traces/` or `.code/traces/`. If `JIUWENSWARM_DATA_DIR` is set, the corresponding subdirectory under `<that dir>/`.

**Q: Why is the subagent section empty?**
Make sure `include_subagent_flow` is not `false`, and that this round is actually a debug state (`/debug` or config always-on). In a non-debug state the subagent takes the original path and is not recorded. (Subagent data flow currently applies to Agent / Code only.)

**Q: I enabled OTel but there is no trace in Langfuse / the collector?**
First verify spans are produced with `exporter: console` or `exporter: file`; in `file` mode check that `~/.jiuwenswarm/.trace/` contains `*.jsonl`. For always-online mode, confirm the `endpoint` is correct and the Collector is up (`docker-compose up`). See [Logging System](Logs.md) to check whether the service started normally.

**Q: Offline file-mode upload fails with 502 for every line?**
Almost always because the endpoint was written as `localhost`. On macOS use `127.0.0.1:4318` instead (see 5.5).

**Q: Will the dump leak sensitive information?**
Secret-like fields (`password` / `token` / `api_key` / `authorization` / `cookie`, etc.) are **always masked** as `***`, independent of the `redaction` switches; large payloads are truncated per `limits`. For fuller redaction, enable `redact_prompts` / `redact_completions`.

**Q: Does enabling debug affect model-run performance or cause errors?**
No. The whole mechanism is best-effort: any write failure is swallowed and warned about, never affecting the model run; in a non-debug state the subagent path is identical to having it disabled.

---

## 9. Related Documentation

- [Logging System](Logs.md) — system-level operation logs (complementary to this doc)
- [Modes](Modes.md) — `agent.*` / `code.*` mode descriptions and switching
- [Configuration](Configuration.md) — full `config.yaml` field reference
- [Slash Commands Reference](SlashCommands.md) — list of slash commands including `/debug`
- `deploy/observability/` — one-command OTel Collector + Langfuse stack and the offline upload script (`README.md`)

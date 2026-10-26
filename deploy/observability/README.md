# Jiuwenswarm Team Observability Stack

Local docker-compose deployment for OpenTelemetry observability with Langfuse backend.

## Architecture

```
Application --(OTLP gRPC)--> OTel Collector --(OTLP HTTP)--> Langfuse
           localhost:4317                        localhost:3000
```

- **Application → Collector**: gRPC (port 4317) or HTTP (port 4318), no auth required
- **Collector → Langfuse**: HTTP with Basic Auth (configured in `otel-collector-config.yaml`)

## Prerequisites

- Docker & Docker Compose

## Quick Start

```bash
cd deploy/observability

# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View collector logs
docker-compose logs -f otel-collector
```

Wait for all services to become healthy (~30-60s on first start).

### Access Langfuse UI

- URL: http://localhost:3000
- Login email: `jiuwenswarm@jiuwen.local`
- Password: `jiuwenswarm`
- Project keys: `pk-lf-jiuwen` / `sk-lf-jiuwen`

## File Exporter (offline / two-phase)

The `file` exporter writes traces as standard OTLP JSON files on disk
instead of streaming to the collector in real time. Files can be uploaded
later with `upload_traces_to_langfuse.py`.

### Phase 1 — configure the exporter

编辑 `~/.jiuwenswarm/config/config.yaml`:

```yaml
team_observability:
  enabled: true
  exporter: "file"
  # traces_dir 留空则默认写入 ~/.jiuwenswarm/.trace
  traces_dir: ""
  file_retention_days: 7          # optional, default 7
  sample_rate: 1.0
```

- Flat layout — all trace files are written directly under `traces_dir`,
  no per-session sub-folders.
- One append-only file per calendar day, named `traces-<YYYY-MM-DD>.jsonl`.
  Spans from all traces are interleaved in it; each line is a standalone
  single-span OTLP JSON (`resourceSpans` → `scopeSpans` → `spans`),
  directly ingestible by the collector at `/v1/traces` — replaying is
  just POSTing each line in turn. The collector splits traces by the
  `traceId` carried on every span, so interleaving is irrelevant for
  ingestion.
- `export()` appends straight to disk with no in-memory buffer; paired
  with `BatchSpanProcessor` (the default for the `file` exporter) so
  span-end does not block the business thread — spans land on disk when
  the processor flushes (default every 5s / 512 spans) and on shutdown.
- `session.id` (if present) is read by Langfuse from span attributes,
  not the filename — the filename carries only the date.

### Phase 2 — upload to Langfuse

The upload script accepts either a single trace file or a directory.
With no arguments it defaults to `~/.jiuwenswarm/.trace`
(overridable via `JIUWENSWARM_DATA_DIR`), matching the file exporter's
output path. When given a directory, it uploads every `*.jsonl` directly
under it (flat — no sub-folder walking). It POSTs each line of each file
as a standalone OTLP request to the collector. After the run it prints
the unique trace IDs that were ingested, parsed from each uploaded line's
`resourceSpans[].scopeSpans[].spans[].traceId`.

```bash
# Start the collector stack
docker-compose up -d

# Upload the default traces directory (~/.jiuwenswarm/.trace):
python upload_traces_to_langfuse.py

# Upload a whole directory of trace files:
python upload_traces_to_langfuse.py ./traces_run_001
# or, equivalently:
python upload_traces_to_langfuse.py --dir ./traces_run_001

# Upload a single trace file:
python upload_traces_to_langfuse.py --file ./traces_run_001/traces-2026-06-29.jsonl

# Use a non-default collector endpoint:
python upload_traces_to_langfuse.py ./traces_run_001 --endpoint http://localhost:4318/v1/traces
```

The script POSTs each `.jsonl` line to the collector's OTLP HTTP
endpoint (`localhost:4318/v1/traces` by default, no auth), which forwards
to Langfuse. Exit code is `0` on full success, `1` if any line failed,
`2` if the input path was not found or contained no `.jsonl` files.

Sample output:

```
[upload] source=./traces_run_001  files=3  endpoint=http://localhost:4318/v1/traces
[upload] total_lines=128 ok=128 fail=0 elapsed=0.6s
[upload] trace_ids (3):
  4f3c1a8b9d2e4f6081a3c5b7d9e1f2a3
  8e1c0b2d4f6a8c9e1d3b5a7c9e1f0a2b
  1a2b3c4d5e6f70819203a4b5c6d7e8f9
```

**认证说明**：上传脚本连接本地 Collector（`localhost:4318`，无鉴权），
Langfuse 的 `pk/sk` 认证由 Collector 侧 `otel-collector-config.yaml` 处理，
脚本本身不需要配置密钥。

### Cleanup

Trace files (`*.jsonl`) whose mtime predates `file_retention_days` are
lazily deleted by the exporter itself (a sweep runs at most every 64
exports). No manual cleanup is required.

### Stop and Clean Up

```bash
# Stop services (keep data)
docker-compose down

# Stop and remove all data
docker-compose down -v
```

## Enable in Jiuwenswarm

编辑 `~/.jiuwenswarm/config/config.yaml`，将 `team_observability.enabled` 设为 `true`，重启即可。

```yaml
team_observability:
  enabled: true
  endpoint: http://localhost:4317
```

## Application Configuration

### Method 1: OTLP gRPC → Collector → Langfuse (Recommended)

Application sends traces to local Collector. Auth is handled by Collector.

```yaml
team_observability:
  enabled: true
  exporter: "otlp_grpc"
  endpoint: "http://localhost:4317"
  sample_rate: 1.0
```

### Method 2: OTLP HTTP → Langfuse Direct

Application sends traces directly to Langfuse Cloud (bypasses local Collector).

```yaml
team_observability:
  enabled: true
  exporter: "otlp_http"
  endpoint: "https://cloud.langfuse.com/api/public/otel/v1/traces"
  langfuse_public_key: "pk-lf-xxx"
  langfuse_secret_key: "sk-lf-xxx"
```

### Method 3: Console Output (Debug)

```yaml
team_observability:
  enabled: true
  exporter: "console"
```

Traces printed as JSON to console.

## Configuration Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | True | Enable/disable observability |
| `service_name` | str | "openjiuwen-agent-teams" | OTel resource service name |
| `exporter` | str | "otlp_grpc" | Exporter type: `otlp_grpc`, `otlp_http`, `console`, `file` |
| `endpoint` | str | "http://localhost:4317" | OTLP endpoint URL |
| `traces_dir` | str | "~/.jiuwenswarm/.trace" | Output dir for the `file` exporter (empty → default) |
| `file_retention_days` | int | 7 | Trace files older than this are lazily pruned (`file` exporter) |
| `sample_rate` | float | 1.0 | Sampling rate (0.0-1.0) |
| `langfuse_public_key` | str | "" | Required for direct Langfuse connection |
| `langfuse_secret_key` | str | "" | Required for direct Langfuse connection |

## Customizing Langfuse Keys

The default keys (`pk-lf-jiuwen` / `sk-lf-jiuwen`) are configured in:

1. **docker-compose.yml** - Langfuse initialization:
   ```yaml
   LANGFUSE_INIT_PROJECT_PUBLIC_KEY: pk-lf-jiuwen
   LANGFUSE_INIT_PROJECT_SECRET_KEY: sk-lf-jiuwen
   ```

2. **otel-collector-config.yaml** - Collector authentication:
   ```yaml
   exporters:
     otlphttp/langfuse:
       headers:
         Authorization: "Basic <base64(pk:sk)>"
   ```

To generate a new Base64 auth header:
```bash
echo -n "pk-lf-xxx:sk-lf-xxx" | base64
```

Or in PowerShell:
```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("pk-lf-xxx:sk-lf-xxx"))
```

## Production Notes

1. **Security**: Change `NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`, and database passwords in `docker-compose.yml` before exposing outside localhost.

2. **Sampling**: Set `sample_rate < 1.0` in production (e.g., 0.1) to reduce trace volume.

3. **Databases**: For production, use managed Postgres and ClickHouse instead of docker-compose volumes.

4. **Remove debug exporter**: In `otel-collector-config.yaml`, remove `debug` from exporters once stable:
   ```yaml
   exporters: [otlphttp/langfuse]
   ```

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Service orchestration: OTel Collector, Langfuse, Postgres, ClickHouse, Redis, MinIO |
| `otel-collector-config.yaml` | Collector pipeline: receivers, processors, exporters |
| `upload_traces_to_langfuse.py` | Upload per-day `.jsonl` trace files to the collector (offline two-phase) |

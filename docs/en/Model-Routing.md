# Model Routing: Task-Driven Model Selection

---

## Concepts

### What is Model Routing

Model Routing is JiuwenSwarm's task-adaptation system for multi-model environments. It solves the core problem: **which model to use** — automatically selecting the most suitable model based on task type and difficulty.

- **Score-based routing** solves "which model to use": it calculates a target score based on task type and difficulty, then selects the model with the closest `model_score` from the capability table — heavy tasks get strong models, light tasks get weak models, avoiding overkill or underkill.

Think of model routing as a "three-stage smart dispatch": **Health check → Single-model skip → Score routing**. If a model endpoint is unhealthy, it's filtered out first; if only one model is available, routing is skipped; otherwise, it matches by closest score.

#### How Score-Based Routing Dispatches

The core of score-based routing is **target score → closest match**:

1. The classifier determines the task type (category) and difficulty from the user's prompt, producing a raw score.
2. The score table (`score_table`) is queried for the exact `(category, difficulty)` score; if not found, the default score for that difficulty is used (easy=10, medium=30, hard=50); if the classifier's difficulty is outside (easy, medium, hard), the default score is 50.
3. **Hard difficulty expertise constraint**: If the difficulty is hard, the candidate range is narrowed to models whose `model_expertise_category` includes the task category. For example, coding/hard only selects models tagged with coding expertise. If no expertise match is found, the full table is used as fallback (same fallback strategy as vision constraints).
4. **model_type constraint**: If `required_model_type` is explicitly injected (e.g., `"vision"`, `"coding"`), candidates are limited to models with matching `model_type`; when not injected, specialized models are excluded.
5. Among candidates, the model with `model_score` closest to the target score is selected; ties favor the higher score (quality first).

#### Why Hard Tasks Need Expertise Constraints

Medium/easy tasks don't require specialized models — general-purpose models can handle formatting and simple summaries. But hard tasks often require deep understanding: coding/hard needs complex algorithms, reasoning/hard needs rigorous reasoning chains. If only score matching is used, a score=90 general-purpose chat model might be selected, but it may not excel at writing code.

Expertise constraints ensure hard tasks are prioritized for **models that claim expertise in the relevant domain**. If a model is tagged `model_expertise_category=["coding", "reasoning"]`, it indicates specialized optimization in programming and reasoning; coding/hard requests will be routed to it rather than a similarly-scored chat-only model.

When no model is tagged with the relevant expertise, the full table is used as fallback — no empty constraint is applied.

#### How model_type Constraints Work

Model routing supports generic type constraints via the `model_type` field, replacing the old `_has_image` hardcoded branch. `_detect_model_type(ctx)` reads the required model type from context, currently only supporting **explicit injection**:

```python
# Write to ctx.extra during before_invoke
ctx.extra["_required_model_type"] = "vision"   # Route to vision models
ctx.extra["_required_model_type"] = "coding"   # Route to coding models
ctx.extra["_required_model_type"] = "audio"    # Route to audio models
```

Constraint logic:

| `required_model_type` | Behavior |
|----------------------|------|
| Non-empty (e.g., `"vision"` / `"coding"`) | Candidates limited to models with `model_type == required_model_type`; if no match, fall back to full table |
| Empty (default) | Exclude specialized models (non-empty `model_type`); if all are specialized, fall back to full table |

#### How Health Checks Protect

Model endpoint health checks run as a **background async loop**, started on the first `before_invoke` and running at configurable intervals. Routing decisions read from a cached status map — **never blocking** the routing path.

- **Background loop**: Periodically calls `update_health` every `interval_seconds` (default 600s). First invoke lazily starts the loop; subsequent invokes just read cached results.
- **Cache TTL**: Default 600s (10 minutes), using `time.monotonic()` unaffected by system clock adjustments
- **Capability verification**: General models use plain-text ping; vision models receive a red square image to verify visual capability; audio models receive a voice WAV to verify audio capability
- **All-unhealthy fallback**: When all models are unhealthy, fall back to the full table — never block routing
- **Consecutive judgment**: A model is marked unhealthy only after `max_consecutive_failures` (default 2) consecutive failures; it recovers only after `recovery_consecutive_successes` (default 1) consecutive successes
- **Non-blocking**: Routing reads cached `_status_map` directly; the health check loop updates the cache in the background for the next invoke

#### Routing Lifecycle

Routing executes once per invoke in the `before_invoke` hook (priority 95), before any model calls. Each new user turn triggers a fresh routing cycle. There is no per-invoke dedup mechanism needed — `before_invoke` naturally fires exactly once per invoke.

#### What Problems It Solves

| Problem | Traditional Approach | Model Routing's Approach |
|---------|---------------------|------------------------|
| Heavy and light tasks use the same model | Manual switching or fixed strongest model | Auto-select by score — heavy tasks get strong, light tasks get weak |
| Hard tasks assigned to non-expert models | No expertise distinction, arbitrary selection by score/brand | hard → expertise constraint → select models claiming domain expertise |
| Same model with multiple APIs not distinguished | Not distinguished, token stats conflated | client_id derived from (api_base+api_key) hash, independent accounting |
| Specific request types select wrong models | No distinction, image/coding/audio may select wrong | model_type explicit injection → constrain to matching type models |
| Model endpoints down but still routed | Not detected, routes to down endpoint | Async health check → filter unhealthy endpoints |
| Health check blocks routing | Synchronous health check before each call | Background loop + cached status, routing never waits |
| Model capability parameters scattered | Hard-coded in config | External JSON (capability_map/mapper), user-customizable |

### Basic Workflow

```text
Startup → Load capability table + classifier + score table + health checker
  ↓
First user request → lazily start health check background loop
  ↓
Each user request (before_invoke):
  ↓
[Health Check] Read cached status (non-blocking)
  ├─ Filter unhealthy models (all-unhealthy fallback to full table)
  └─ ↓
[Check] Single-model skip?
  ├─ Yes → Output the only model, skip the rest
  └─ No ↓
[Normal Routing] Classifier → (raw_score, category, difficulty)
  ↓
task_score(category, difficulty, mapper) → target score
  ↓
[Constraint 1] Hard difficulty expertise (difficulty=="hard" → prefer expertise-matched models; no match → full table fallback)
  ↓
[Constraint 2] model_type constraint (explicit injection → match type; not injected → exclude specialized)
  ↓
model_score closest match → Recommended model
  ↓
apply_routing=True? set_llm to switch model / No: recommend only
```

### Runtime Behavior

Model routing runs as a DeepAgentRail with priority 95, automatically executing in `before_invoke` on each user turn. Users don't need to trigger it manually — as long as `model_routing.enabled` is configured, all requests go through routing.

Health checks run in a background async loop that starts on the first `before_invoke` and periodically refreshes the cached status map. Routing decisions always read from cache — they never block on HTTP health checks.

After routing completes, the decision is written to `ctx.extra["model_routing_decision"]`, containing:

| Field | Description |
|-------|-------------|
| `recommended_model_id` | The client_id of the recommended model |
| `analysis` | TaskAnalysis: category, difficulty, target_score, predicted_input_tokens |
| `reasoning` | Routing reasoning process ("classifier score=90; score match: target=90 → big-model(score=90)") |
| `prior_calls_otel` | OTel span list for prior calls within this invoke |
| `model_usage_stats` | Current token usage statistics snapshot |

---

## Operations Guide

### 1. Prepare Model Configuration

Model routing requires two prerequisites:

- **Multiple models configured in config.yaml**: `models.defaults` must contain at least 2 model entries (with `model_client_config`). With only 1 model, routing is automatically skipped.
- **Capability table file ready**: `routing_state/model_capability_map.json` exists (auto-copied from package template on first startup).

See [Configuration](Configuration.md) for model configuration details.

### 2. Enable Model Routing

Navigate to:

```text
Left sidebar → Configuration → Model Routing
```

Toggle **Enable Model Routing** and save.

Related configuration items:

| Config Item | Default | Description |
|------------|---------|-------------|
| `model_routing.enabled` | `false` | Enable model routing |
| `model_routing.apply` | `false` | Whether routing actually switches models (`set_llm`); `false` = recommend only |
| `model_routing.stats_path` | empty string | Stats file path; empty = default location |
| `model_routing.health_check.enabled` | `true` | Enable health checks |
| `model_routing.health_check.interval_seconds` | `600` | Health check cache TTL (seconds) |

> **Tip**: `apply=false` is a safe starting point — routing only produces recommendations without switching models, allowing you to observe routing effectiveness before enabling actual switching.

### 3. Configure Vision Models

Image-containing requests require at least one `model_type: vision` model. Set this in model entries:

```yaml
models:
  defaults:
    - model_client_config:
        model_name: GLM-5.1
        ...
      # General text model (default model_type="")
  vision:
    model_client_config:
      model_name: qwen-vl-max
      client_provider: DashScope
      api_key: "your-key"
      api_base: "https://..."
      # Vision-specific model, participates in routing for image requests
```

You can also mark `model_type: vision` directly on `models.defaults` entries:

```yaml
models:
  defaults:
    - model_client_config: {...}
      model_type: vision       # ← Mark as vision model
```

Vision models are excluded for non-image requests; for image requests, only vision models are selected (fallback to full table if no vision models).

### 4. Configure Model Expertise

Hard-difficulty tasks are preferentially routed to models claiming expertise in the relevant domain. Mark expertise via `model_expertise_category` in model entries:

```yaml
models:
  defaults:
    - model_client_config:
        model_name: deepseek-coder-v2
        client_provider: DeepSeek
        api_key: "your-key"
        api_base: "https://..."
      model_expertise_category: ["coding"]    # ← Mark coding expertise
    - model_client_config:
        model_name: GLM-5.1
        client_provider: DashScope
        api_key: "your-key"
        api_base: "https://..."
      model_expertise_category: ["reasoning", "coding"]  # ← Mark reasoning + coding expertise
    - model_client_config:
        model_name: qwen3-max
        ...
      # No expertise mark → not in expertise candidates for hard tasks
```

Expertise marking effects:

| Scenario | No Expertise Mark | With Expertise Mark |
|----------|-------------------|-------------------|
| coding/hard | Full table by score (general model may be selected) | First limit to coding-tagged models → then by score |
| reasoning/hard | Same as above | First limit to reasoning-tagged models → then by score |
| chat/easy / format/medium | **No constraint** — full table by score | **No constraint** — easy/medium don't trigger expertise filtering |
| No model tagged with relevant expertise | — | Fallback to full table, no empty constraint |

> **Tip**: If your model genuinely excels in a domain (e.g., DeepSeek-Coder for programming, Claude for reasoning), mark the corresponding expertise. Hard tasks will be routed to it preferentially.

You can also override in `model_capability_map.json`:

```json
{
  "models": {
    "deepseek-coder-v2": {"model_score": 80, "model_expertise_category": ["coding"]},
    "GLM-5.1": {"model_score": 57, "model_expertise_category": ["reasoning", "coding"]}
  }
}
```

### 5. model_type Explicit Injection

Model routing supports explicit model_type constraints via `ctx.extra["_required_model_type"]`, controlling routing to specific model types. This is currently the only source for model_type detection.

#### Injection Method

During the `before_invoke` phase, write to `ctx.extra` from the adapter layer, frontend, or another rail:

```python
# Method 1: Inject in a Rail's before_invoke
class MyRail(DeepAgentRail):
    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        # Detected image input → inject vision
        if self._has_image_input(ctx):
            ctx.extra["_required_model_type"] = "vision"
        # Detected audio input → inject audio
        elif self._has_audio_input(ctx):
            ctx.extra["_required_model_type"] = "audio"

# Method 2: Inject in the adapter layer
class JiuWenClawDeepAdapter:
    async def _invoke(self, ctx):
        # Code mode → inject coding
        if self._mode == "code":
            ctx.extra["_required_model_type"] = "coding"
```

#### Injection Values and Routing Behavior

| Injection Value | Routing Behavior | Typical Scenario |
|----------------|-----------------|------------------|
| `"vision"` | Candidates limited to `model_type=="vision"` models | Image requests, file attachments with images |
| `"coding"` | Candidates limited to `model_type=="coding"` models | Programming mode, code generation tasks |
| `"audio"` | Candidates limited to `model_type=="audio"` models | Voice input, audio processing |
| `""` or not injected | Exclude specialized models (non-empty model_type); if all specialized, fall back to full table | Default behavior |

#### Model-side model_type Configuration

Mark `model_type` in `config.yaml` model entries:

```yaml
models:
  defaults:
    - model_client_config: {...}
      # General text model (default model_type="")
    - model_client_config: {...}
      model_type: coding          # ← Mark as coding model
    - model_client_config: {...}
      model_type: vision          # ← Mark as vision model
    - model_client_config: {...}
      model_type: audio           # ← Mark as audio model
```

You can also override in `model_capability_map.json`:

```json
{
  "models": {
    "deepseek-coder-v2": {"model_score": 80, "model_type": "coding"},
    "qwen-vl-max": {"model_score": 45, "model_type": "vision"}
  }
}
```

#### How to Add a New model_type

1. Configure the new `model_type` on target models in `config.yaml` or `model_capability_map.json` (e.g., `"audio"`)
2. Write `ctx.extra["_required_model_type"] = "audio"` from the injection point (adapter / rail / frontend)
3. Routing takes effect automatically — `_decide_and_select` filters candidates by `required_model_type`

### 6. Customize Classifier and Score Table

The classifier and score table are controlled by `classifier_mapper.json`. On first startup, it's copied from the package template to the user directory:

```text
~/.jiuwenswarm/config/routing_state/classifier_mapper.json
```

#### Score Table

The `score` field defines `(category, difficulty)` to target score mappings:

```json
{
  "score": {
    "chat.easy": 5,
    "chat.medium": 20,
    "chat.hard": 45,
    "coding.easy": 15,
    "coding.medium": 40,
    "coding.hard": 65,
    "reasoning.hard": 55,
    "summarization.medium": 30,
    "format.easy": 5
  }
}
```

- Format: `"category.difficulty"` → integer score.
- Higher score → stronger model selected; lower score → weaker model selected.
- Uncovered combinations use difficulty defaults: easy=10, medium=30, hard=50.

> **Tip**: If light tasks keep selecting large models, lower the corresponding score (e.g., `"format.easy": 5→3`); if heavy tasks select weak models, raise the score (e.g., `"coding.hard": 65→85`).

#### Classifier

The `classifier` field defines classifier behavior. It uses the **text injection** approach — `source` is the function body text of `async def classify(prompt_text)`, compiled into an async function via exec:

```json
{
  "classifier": {
    "imports": ["re", "json"],
    "source": "import logging\nfrom jiuwenclaw.agentserver.deep_agent.rails.model_routing.classifier import (\n    _build_llm_model, _parse_classifier_response, _lookup_score\n)\n\n_log = logging.getLogger(\"ModelRouting\")\n\nif not _EXTRAS.get(\"api_base\"):\n    _log.debug(\"no api_base → fallback\")\n    return 50, \"unknown\", \"hard\"\n\nmodel = _build_llm_model(_EXTRAS)\nif model is None:\n    _log.debug(\"build model failed → fallback\")\n    return 50, \"unknown\", \"hard\"\n\n_log.debug(\"LLM call start, prompt=%r\", prompt_text[:80])\nmessages = [\n    {\"role\": \"system\", \"content\": _EXTRAS.get(\"system_prompt\")},\n    {\"role\": \"user\", \"content\": prompt_text[:4000]},\n]\ntry:\n    resp = await model.invoke(messages)\n    content = getattr(resp, \"content\", \"\") or \"\"\n    category, difficulty = _parse_classifier_response(content, _CATEGORIES, _DIFFICULTIES)\n    score = _lookup_score(category, difficulty)\n    _log.debug(\"result: %s/%s score=%d\", category, difficulty, score)\n    return score, category, difficulty\nexcept Exception as exc:\n    _log.warning(\"LLM call failed: %s → fallback\", exc)\n    return 50, \"unknown\", \"hard\"",
    "extras": {
      "api_base": "https://your-llm-endpoint/v1",
      "api_key": "your-api-key",
      "model_name": "your-classifier-model",
      "client_provider": "OpenAI",
      "temperature": 0,
      "system_prompt": "You are a task classifier. Classify the user input into a category and difficulty. Output ONLY a JSON object. No explanation, no markdown.\n\nCategories:\n- chat: greeting, small talk, casual conversation\n- reasoning: logic, analysis, planning, research, investigation, math\n- coding: write code, debug, programming\n- summarization: summarize, condense, compress, extract key points\n- format: convert between formats (JSON, CSV, table, list)\n\nDifficulty:\n- easy: simple, short task\n- medium: moderate complexity\n- hard: complex, long task\n\nExamples:\n\"hello\" -> {\"category\":\"chat\",\"difficulty\":\"easy\"}\n\"write a Python sort function\" -> {\"category\":\"coding\",\"difficulty\":\"medium\"}\n\"summarize this article\" -> {\"category\":\"summarization\",\"difficulty\":\"medium\"}\n\"convert this CSV to JSON\" -> {\"category\":\"format\",\"difficulty\":\"easy\"}\n\"prove that sqrt(2) is irrational\" -> {\"category\":\"reasoning\",\"difficulty\":\"hard\"}\n\nOutput ONLY: {\"category\":\"<category>\",\"difficulty\":\"<difficulty>\"}"
    }
  }
}
```

Injected data available in classifier source:

| Variable | Type | Description |
|----------|------|-------------|
| `_EXTRAS` | `dict` | classifier.extras field |
| `_CATEGORIES` | `tuple` | categories list |
| `_DIFFICULTIES` | `tuple` | difficulties list |
| `_SCORE_TABLE` | `dict` | `{(cat, diff)→int}` score table |
| `_DEFAULT_SCORE` | `dict` | `{diff→int}` default scores |

Tool functions are not auto-injected — import them when needed in source:

```python
from jiuwenclaw.agentserver.deep_agent.rails.model_routing.classifier import (
    _build_llm_model, _parse_classifier_response, _lookup_score
)
```

#### Behavior Without a Classifier

If `classifier_mapper.json` is missing the `classifier` field, or the classifier fails to load, routing falls back to `(raw_score=50, category="unknown", difficulty="hard")`. The target score is fixed at 50, selecting the model with model_score closest to 50. This ensures routing still produces decisions even when the classifier is unavailable — just without task-lightness differentiation.

### 7. Customize Model Capability Mapping

Model capability mapping is controlled by `model_capability_map.json`, containing two parts:

- **vendor_map**: model_name substring → (model_group, model_provider) prefix mapping (22 default entries).
- **models**: model_name exact match → capability field overrides (~70 default entries), primarily `model_score`.

```text
~/.jiuwenswarm/config/routing_state/model_capability_map.json
```

```json
{
  "vendor_map": [
    {"prefix": "glm", "group": "GLM", "provider": "zhipu"},
    {"prefix": "qwen", "group": "Qwen", "provider": "alibaba"}
  ],
  "models": {
    "GLM-5.1": {"model_score": 57},
    "deepseek-chat": {"model_score": 35}
  }
}
```

For models not covered by the capability map or custom-trained models, `model_score` can also be set in `config.yaml` model entries:

```yaml
models:
  defaults:
    - model_client_config:
        model_name: GLM-5.1
        client_provider: DashScope
        api_key: "your-key"
        api_base: "https://..."
      model_score: 55          # ← Manually set model_score
    - model_client_config:
        model_name: deepseek-chat
        client_provider: DeepSeek
        api_key: "your-key"
        api_base: "https://..."
      # No model_score → will use model_capability_map.json mapping
```

`model_score` is the core score for routing. Adjusting model scores directly affects routing selection:

- Higher score → more likely to be selected for heavy tasks.
- Lower score → more likely to be selected for light tasks.
- Ties favor the higher score (quality first).

> **Tip**: If a model is never selected, check whether its `model_score` is too far from task scores. You can override a specific model's score in the `models` field.

### 8. View Routing Logs

The routing process outputs via logs, observable in server-side logs:

```text
[ModelRouting] classifier: [coding,hard] score=90 in_tok=4 model_type=coding -> recommend=f345159ce878
[ModelRouting] classifier: [format,easy] score=10 in_tok=2 model_type=(none) -> recommend=abc123
[ModelRouting] skipped (single model); in_tok=4
[ModelRouting] applied set_llm -> GLM-5.1
[ModelRouting] filtered 1 unhealthy models: ['def456']
[ModelRouting] all models unhealthy, falling back to full table
[ModelRouting] health check background loop started
```

| Log Keyword | Meaning |
|-------------|---------|
| `classifier: [...]` | Normal routing: classification result, target score, model_type, recommended model |
| `skipped (single model)` | Single-model skip |
| `applied set_llm ->` | Actual model switch |
| `filtered N unhealthy models` | Health check filtered unhealthy models |
| `all models unhealthy, falling back` | All unhealthy, falling back to full table |
| `health check background loop started` | Async health check loop started on first invoke |

---

## Configuration Reference

### Configuration Overview

```yaml
model_routing:
  enabled: true             # Enable model routing
  apply: false              # Whether routing actually switches models
  stats_path: ""            # Stats file path (empty = default location)
  health_check:
    enabled: true           # Enable health checks
    interval_seconds: 600   # Cache TTL (seconds)
    timeout_seconds: 10     # Single request timeout
    max_rounds: 1           # Retry rounds
    max_consecutive_failures: 2          # Consecutive failure threshold
    recovery_consecutive_successes: 1    # Consecutive successes needed to recover
    health_check_prompt: "hi"            # Minimal prompt
    max_tokens: 1                        # Minimal output
```

### `model_routing.enabled`

Whether to enable model routing. Default `false`.

When enabled, routing logic executes in `before_invoke` on each user turn. When disabled, all requests use the default model — no classification or model selection.

### `model_routing.apply`

Whether routing actually switches models (`set_llm`). Default `false`.

- `false`: Routing only produces recommendation results and decision information, without switching models. Suitable for observing routing effectiveness.
- `true`: After routing, the agent's current model is actually switched to the recommended model, and config's `model_name`, `model_client_config`, and `model_config_obj` are synced. Subsequent model calls use the routed model.

> **Note**: With `apply=true`, all requests are routed to the recommended model. It's recommended to first observe routing logs in `apply=false` mode, confirming routing selections are reasonable before enabling actual switching.

### `model_routing.stats_path`

Storage path for model statistics file. When empty, the default location is used:

```text
~/.jiuwenswarm/config/routing_state/model_routing_list.json
```

The statistics file records each model's token usage (input/output/call_count/last_used), accumulated across sessions. On routing startup, existing statistics are merged into the capability table; on hot reload, the full table is written back.

### `model_routing.health_check.*`

Health check configuration. See the "How Health Checks Protect" section above.

Key points:

- Health checks run as a **background async loop**, started lazily on the first `before_invoke`.
- `interval_seconds` controls both the cache TTL and the loop interval.
- Routing reads cached status and never blocks on health check HTTP requests.

### Full Configuration Example

```yaml
model_routing:
  enabled: true
  apply: true                # Actually switch models
  stats_path: ""             # Use default path
  health_check:
    enabled: true
    interval_seconds: 600    # Cache TTL 10 minutes
    timeout_seconds: 10      # Single request timeout
    max_rounds: 1            # Retry rounds
    max_consecutive_failures: 2
    recovery_consecutive_successes: 1
    health_check_prompt: "hi"
    max_tokens: 1
```

### External JSON Configuration Files

Two JSON files are stored in the `routing_state` directory, auto-copied from package templates on first startup. Users can modify and override them:

```text
~/.jiuwenswarm/config/routing_state/
├── classifier_mapper.json      Classification/difficulty/score table + classifier config
├── model_capability_map.json   Vendor prefix mapping + model score overrides
├── model_routing_list.json     Token statistics (auto-generated, do not edit manually)
```

#### `classifier_mapper.json`

| Field | Type | Description |
|-------|------|-------------|
| `categories` | `list[str]` | Task category list (chat/reasoning/coding/summarization/format) |
| `difficulties` | `list[str]` | Difficulty list (easy/medium/hard) |
| `score` | `dict` | `"category.difficulty"` → target score |
| `classifier.imports` | `list[str]` | Module names to inject into exec namespace |
| `classifier.source` | `str` | classify function body text |
| `classifier.extras` | `dict` | Classifier-specific config (rules/category_utterances/length_signal, etc.) |

#### `model_capability_map.json`

| Field | Type | Description |
|-------|------|-------------|
| `vendor_map` | `list[{prefix, group, provider}]` | model_name substring → brand group/vendor mapping |
| `models` | `dict{name → {model_score, model_type, ...}}` | model_name exact match → capability field overrides |

---

## FAQ

### Why doesn't routing take effect after enabling?

Possible causes:

- Only 1 model configured → routing is automatically skipped ("skipped (single model)").
- `model_routing.enabled` is not enabled.
- `model_routing.apply=false` → routing only recommends without switching, model still uses default.

Check logs for `[ModelRouting]` related output.

### Why do heavy tasks keep selecting weak models?

Possible causes:

- `model_score` configuration is unreasonable — weak model score too high or strong model score too low.
- Score table target score for heavy tasks is too low (e.g., `coding.hard` only 30).
- Classifier misjudgment — heavy tasks classified as light tasks.

Check the `classifier: [...]` line in logs for category, difficulty, and score, and compare against `classifier_mapper.json` score table and `model_capability_map.json` model scores.

### Why are image requests selecting non-vision models?

Possible causes:

- No `model_type: vision` model configured → full table fallback, selects closest score.
- `_required_model_type` not injected → routing doesn't know a vision model is needed.

Ensure config has vision model entries, and inject `ctx.extra["_required_model_type"] = "vision"` during `before_invoke`.

### Why does the classifier fail to load?

Logs will show `[ModelRouting] classifier load skipped: ...`. Possible causes:

- `classifier_mapper.json` missing `classifier` field → fallback to no-classifier mode.
- `classifier.source` text has syntax errors → exec compilation fails.
- `imports` references unavailable modules.

Classifier failure doesn't affect routing — falls back to `(50, unknown, hard)`, target score fixed at 50.

### How to track token usage for same model with multiple APIs?

When the same model_name has multiple api_keys (e.g., DeepSeek 3-key budget allocation), each entry's `model_id` is derived as a 12-character hash from `(model_name|api_base|api_key)`, with independent token accounting. You can also explicitly specify `client_id` in config:

```yaml
model_client_config:
  model_name: deepseek-chat
  api_base: "https://api.deepseek.com"
  api_key: "sk-aaa"
  client_id: "my-deepseek-aaa"    # ← Explicit, no hash derivation
```

### Where can I view routing decision information?

Routing decisions are written to `ctx.extra["model_routing_decision"]`, containing recommended model, task analysis, reasoning process, OTel spans, and statistics snapshot. This information is primarily for internal rail chain use, not directly displayed to users. Users can observe the routing process through server-side logs.

### Difference between `apply=false` and `apply=true`?

| Mode | Behavior | Use Case |
|------|----------|----------|
| `apply=false` | Routing only recommends models, doesn't switch. Subsequent calls use default model | Observe routing effectiveness, debug scores/classification |
| `apply=true` | Routing actually switches models (`set_llm`), subsequent calls use recommended model | Production use, auto-select models by task |

### Why is a model endpoint marked unhealthy when it seems fine?

- Vision model health checks send a red square image; the response must include "red"/"红" keywords to pass. Pure text models can't identify images and will fail verification.
- Audio model health checks send a voice WAV; the response must include "你好"/"hello" keywords to pass.
- If the model endpoint responds slowly, it may timeout. Increase `timeout_seconds`.

### How to inject model_type to route to coding models?

Write during the `before_invoke` phase in the adapter layer or rail:

```python
ctx.extra["_required_model_type"] = "coding"
```

Ensure config has a `model_type: coding` model entry, and routing will automatically constrain to that model.

### Does health check slow down routing?

No. Health checks run as a background async loop — routing reads cached status directly and never waits for HTTP health check requests to complete. On first invoke, the loop is lazily started and the cache is populated asynchronously. Until the first health check completes, all models are treated as healthy (conservative default).

## Related Documentation

- [Configuration](Configuration.md)

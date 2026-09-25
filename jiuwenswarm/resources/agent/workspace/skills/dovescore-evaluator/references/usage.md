# DoveScore Usage Notes

## When DoveScore Helps

DoveScore is designed for source-target alignment where the target may contain accurate standalone facts but still mislead by changing order, chronology, or causal implication.

Good fits:

- Long-form factual summaries
- Biographies or timelines
- News, reports, and event narratives
- Rewrites that might alter causal or temporal order
- Montage-style lies made from true statements

Poor fits:

- Generic semantic similarity
- Style or fluency scoring
- Open-ended quality judgments without a source text

## Interpreting Results

- High `event_score` with low `order_score` means events were mostly factual but their sequence is suspect.
- Low `descriptive_score` points to non-event factual mismatches.
- `ordered_source` and `ordered_target` are useful for explaining where event order diverged.
- `events`, `descriptives`, `event_scores`, and `descriptive_scores` should be inspected before making a high-stakes conclusion.

## Common Commands

Short text:

```bash
python scripts/run_dovescore.py --source "Alice woke early. She brushed her teeth." --target "Alice brushed her teeth. Alice woke early."
```

Long text:

```bash
python scripts/run_dovescore.py --source-file source.txt --target-file target.txt --output result.json
```

For safety, `--source-file`, `--target-file`, and `--output` must be relative paths
inside the current workspace. Absolute paths and paths containing `..` are rejected.

Include raw extracted events and descriptives:

```bash
python scripts/run_dovescore.py \
  --source-file source.txt \
  --target-file target.txt \
  --include-details
```

Custom model:

```bash
python scripts/run_dovescore.py --source-file source.txt --target-file target.txt --backbone gpt-4o-mini
```

OpenAI-compatible endpoint:

```bash
python scripts/run_dovescore.py \
  --source-file source.txt \
  --target-file target.txt \
  --backbone your-model-name \
  --base-url "http://your-server:8000/v1"
```

`--base-url` can point to an OpenAI-compatible provider, gateway, or self-hosted
server such as vLLM. It also defaults from `DOVESCORE_BASE_URL` or
`OPENAI_BASE_URL`, then from common JiuwenSwarm model env vars such as
`API_BASE` or `MODEL_API_BASE`.

API keys are resolved in this order: `--api-key`, `DOVESCORE_API_KEY`,
`OPENAI_API_KEY`, `API_KEY`, then `MODEL_API_KEY`.

Safety limits:

```bash
python scripts/run_dovescore.py \
  --source-file source.txt \
  --target-file target.txt \
  --timeout-seconds 300 \
  --max-input-chars 20000
```

`--timeout-seconds` bounds non-demo evaluation time. `--max-input-chars` caps the
combined source and target size so large accidental inputs do not trigger
unexpected model cost.

UI-safe demo:

```bash
python scripts/run_dovescore.py --demo
```

`--demo` does not require DoveScore, an API key, or network access. Use it when presenting the skill in the interface.

## Troubleshooting

If import fails, install DoveScore:

```bash
pip install git+https://github.com/dannalily/DoveScore.git
```

If authentication fails, set:

```bash
export OPENAI_API_KEY="..."
```

## Interface Output Guidance

Default output is intentionally concise:

- `total_score`
- `alignment_level`
- `event_score`
- `order_score`
- `descriptive_score`
- `interpretation`
- `note`

Only show raw `events`, `descriptives`, `ordered_source`, `ordered_target`, and per-fact scores when the user explicitly asks for debugging details or when using `--include-details`.

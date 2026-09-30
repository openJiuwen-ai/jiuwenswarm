---
name: dovescore-evaluator
description: Evaluate long-form information alignment between source and target text with DoveScore, including factual accuracy, descriptive facts, event extraction, and event-order consistency. Use when checking whether generated, summarized, rewritten, or reordered text preserves source facts and temporal or causal sequence.
---

# DoveScore Evaluator

Use this skill when a user asks whether a target text is faithful to a source text and event order matters. DoveScore is best for long-form information alignment, montage-style lies, narrative rewrites, summaries, biographies, reports, timelines, and other outputs where true facts can still become misleading if reordered.

## 功能概述

DoveScore Evaluator 用于评估源文本与目标文本之间的长文本信息对齐情况。它不仅检查目标文本中的事实是否被源文本支持，还会关注事件顺序是否一致，适合用于摘要、改写、时间线、新闻报道、人物传记和其他包含事件链条的长文本评估。

该 skill 会调用 DoveScore 输出整体分数、事件事实准确率、事件顺序一致性、描述性事实准确率。默认只展示必要摘要，避免界面输出过长；如果需要排查具体事件和描述性事实，可使用 `--include-details` 输出原始明细。

## 配置方式

DoveScore 依赖在默认 JiuwenSwarm 环境中不强制安装，避免影响常规 CI 和普通用户的依赖同步。需要使用该 skill 时，先安装 DoveScore：

```bash
pip install git+https://github.com/dannalily/DoveScore.git
```

随后配置 OpenAI API key：

```bash
export OPENAI_API_KEY="your-api-key"
```

默认模型为 `gpt-4o-mini`，也可以在运行 `scripts/run_dovescore.py` 时通过 `--backbone` 指定其他模型。
如果使用 OpenAI-compatible endpoint，可以通过 `--base-url`、`DOVESCORE_BASE_URL`
或 `OPENAI_BASE_URL` 指定服务地址。脚本也兼容 JiuwenSwarm/agent
框架中常见的通用环境变量：`API_KEY` / `MODEL_API_KEY` 和
`API_BASE` / `MODEL_API_BASE`。

## Requirements

DoveScore is intentionally not installed by default. If it is not installed, install it with:

```bash
pip install git+https://github.com/dannalily/DoveScore.git
```

For local development from a DoveScore checkout, this is also acceptable:

```bash
pip install -e /path/to/DoveScore
```

Set the API key as `OPENAI_API_KEY` or pass it with `--api-key`. The default
backbone is `gpt-4o-mini`. For OpenAI-compatible APIs, pass `--base-url` or set
`DOVESCORE_BASE_URL` / `OPENAI_BASE_URL`. The runner also falls back to common
JiuwenSwarm model environment variables: `API_KEY`, `MODEL_API_KEY`,
`API_BASE`, and `MODEL_API_BASE`.

## Workflow

1. Get both inputs from the user: the reference `source` text and the `target` text to evaluate.
2. Prefer file input for long text. Save or use existing files, then run:

```bash
python scripts/run_dovescore.py --source-file source.txt --target-file target.txt
```

File inputs and `--output` must be relative paths inside the current workspace.
Absolute paths and paths containing `..` are rejected.

3. For short text, direct arguments are acceptable:

```bash
python scripts/run_dovescore.py --source "source text" --target "target text"
```

For OpenAI-compatible endpoints:

```bash
python scripts/run_dovescore.py \
  --source "source text" \
  --target "target text" \
  --backbone your-model-name \
  --base-url "http://your-server:8000/v1"
```

4. For a UI-safe contrast demo that does not require dependencies or an API key, run:

```bash
python scripts/run_dovescore.py --demo
```

5. Report the overall score first, then explain event accuracy, order consistency, descriptive accuracy, and any extracted facts that clarify the judgment.
6. If the user needs machine-readable output, pass `--output result.json`.
7. Use `--include-details` only when the user asks for extracted events, descriptives, or per-fact debugging details.
8. Keep the default safety limits unless the user intentionally requests a larger run:
   `--timeout-seconds 300` and `--max-input-chars 20000`.

## Output Fields

See `references/usage.md` when you need detailed interpretation guidance or troubleshooting notes.

Core fields:

- `total_score`: overall alignment score.
- `event_score`: factual correctness of event facts.
- `order_score`: consistency of verified event order between source and target.
- `descriptive_score`: factual correctness of descriptive facts.
- `interpretation`: short description of what the score means.
- `details`: raw DoveScore output, present only when `--include-details` is used.

## Boundaries

Do not present DoveScore as a general semantic similarity metric. It evaluates source-target information alignment and is especially useful when temporal, causal, or ordered-event consistency is part of the question.

For a contrast demo, see [references/demo.md](references/demo.md).

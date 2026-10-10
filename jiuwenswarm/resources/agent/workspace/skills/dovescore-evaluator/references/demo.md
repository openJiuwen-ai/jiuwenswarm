# DoveScore Contrast Demo

## Demo Goal

展示 DoveScore 解决的问题：普通相似度或人工快速浏览可能会觉得 target 和 source “几乎一样”，但 target 可能改错了关键事实或事件顺序。

## Scenario

Source:

```text
The Eiffel Tower is in Paris. It was completed in 1889 for the Exposition Universelle.
```

Target:

```text
The Eiffel Tower is in Paris. It was completed in 1989 for the Exposition Universelle.
```

Without DoveScore:

```text
The target has very high lexical overlap with the source and may look faithful at a glance.
```

This is risky because the target changes one key descriptive fact: `1889` becomes `1989`.

With DoveScore demo mode:

```bash
python scripts/run_dovescore.py --demo
```

Example demo output:

```json
{
  "demo": "dovescore_contrast",
  "question": "Does the target faithfully preserve the source facts?",
  "source": "The Eiffel Tower is in Paris. It was completed in 1889 for the Exposition Universelle.",
  "target": "The Eiffel Tower is in Paris. It was completed in 1989 for the Exposition Universelle.",
  "without_skill": {
    "likely_judgment": "Looks faithful because almost every word overlaps.",
    "missed_problem": "The year changed from 1889 to 1989."
  },
  "with_dovescore": {
    "metric": "dovescore",
    "total_score": 0.5,
    "alignment_level": "low",
    "event_score": 1.0,
    "order_score": 1.0,
    "descriptive_score": 0.0,
    "finding": "The target is fluent and similar, but one descriptive fact is unsupported."
  },
  "takeaway": "DoveScore catches source-target factual mismatches that surface similarity or quick reading can miss."
}
```

## What The Skill Adds

- It focuses the evaluation on source-target factual support rather than surface similarity.
- It separates event facts, event order, and descriptive facts.
- It can surface cases where the text is fluent and similar but factually unsupported.

## How To Explain It In A Demo

First show that the two sentences are almost identical. Then point out the changed year. The skill's value is that it turns this hidden factual mismatch into an explicit alignment score and score breakdown.

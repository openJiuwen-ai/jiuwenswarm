## Description Optimization — Protocol

The description field in SKILL.md frontmatter is what triggers the skill. Follow every step.

---

### Step 1: Generate 8 eval queries (4 positive, 4 negative)

Save as `<workspace>/trigger_eval.json`:

```json
[
  {"query": "...", "should_trigger": true},
  {"query": "...", "should_trigger": false}
]
```

Queries must be realistic — what an OpenClaw user would actually type. Include file paths, personal context, column names, company names, URLs, a little backstory. Mix lengths; some casual/lowercase/typos. Focus on edge cases, not clear-cut requests.

Bad: `"Format this data"`, `"Extract text from PDF"`

Good: `"ok so my boss just sent me this xlsx file (its in my downloads, called something like 'Q4 sales final FINAL v2.xlsx') and she wants me to add a column that shows the profit margin as a percentage. The revenue is in column C and costs are in column D i think"`

**Positives (4):** different phrasings of the same intent — some formal, some casual. Include cases where the user doesn't explicitly name the skill or file type but clearly needs it. Throw in uncommon cases and competing-skill scenarios where this one should win.

**Negatives (4):** near-misses are most valuable — share keywords or concepts with the skill but actually need something else. Adjacent domains, ambiguous phrasing where naive keyword match would trigger. Avoid obviously irrelevant queries (`"write a fibonacci function"` as a PDF skill negative is useless — it tests nothing).

With only 8 queries, every one must pull weight.

---

### Step 2: Review with user (MUST wait for confirmation)

Present queries grouped:

**Should trigger (4):**
1. `"..."` ✅
2. ...

**Should NOT trigger (4):**
1. `"..."` ❌
2. ...

Ask:
- Any should-trigger queries that should actually be negative?
- Any should-not-trigger that should actually be positive?
- Anything to add, remove, or rephrase?
- Does coverage look good?

Apply edits, confirm again if changed. Save final set as JSON. **Do NOT skip this — bad eval queries produce bad descriptions and waste the whole loop.**

---

### Step 3: Optimization loop (agent-driven)

**Key principle:** patch the installed skill's description in place each iteration so the subagent sees exactly one description (the candidate). Do NOT create a `_eval-<name>-<hex>` twin — when both exist the agent can read either and the trigger signal is muddied.

#### Setup (run once)

1. **Backup:** `cp <skill-path>/SKILL.md <skill-path>/SKILL.md.bak-<random-8-hex>`. Record the backup path.
2. **Restore-guard mindset:** treat the rest as `try { ... } finally { restore }`. **Teardown MUST run on every exit path** — success, max-iterations, error, interrupt. Leaving the skill half-edited corrupts the user's installed environment.
3. **Create workspace:** `<workspace>/description-optimization/`.
4. `current_description` = the original description from the snapshotted SKILL.md.

#### For each iteration (1 to 3)

**A. Patch.** Rewrite ONLY the `description:` field in `<skill-path>/SKILL.md` to `current_description`. Leave `name:` and the body untouched.

**B. Run all 8 queries as routing probes.** Trigger detection is a routing question — don't let the subagent execute the task. Spawn each with this prompt:

> *"User just typed: <query>. Do NOT execute, plan, or use tools. Reply with one line only: `SKILLS_USED: <skill names, or 'none'>` — listing skills you would actually load."*

Parse the reply: `<skill-name>` in the list → `triggered: true`; otherwise `false`. Malformed/missing → re-run once, then log `null` and count as fail.

`pass = (should_trigger == triggered)`.

**C. Score.** With TP/TN/FP/FN over the 8 results:
- `passed / 8`
- `precision = TP / (TP + FP)`
- `recall = TP / (TP + FN)`
- `accuracy = (TP + TN) / 8`

**D. Log — MANDATORY, do this BEFORE step E.** Append to `<workspace>/description-optimization/history.json` (a JSON array, one object per iteration):
```json
{
  "iteration": N,
  "description": "the full candidate tested this iteration",
  "passed": X, "total": 8,
  "precision": 0.0, "recall": 0.0, "accuracy": 0.0,
  "results": [
    {"query": "...", "should_trigger": true, "triggered": true, "pass": true}
  ]
}
```
Per-query `results` are required — they're the only diagnostic record across iterations. **Writing this before E ensures a crash in the improve step doesn't lose the iteration.**

**E. Exit checks.** All 8 pass → `all_passed`, exit. Iteration 3 just done → `max_iterations`, exit. Otherwise continue to F.

**F. Improve description.** Look at THIS iteration's failures:
- Positives that missed: what about the description fails to evoke the skill for these phrasings?
- Negatives that fired: what's too broad or too keyword-heavy?

Write a new `current_description`. It must obey the description limits from the skill-creator SKILL.md "Frontmatter — hard constraints" section (Chinese MUST ≤ 512 chars, English MUST ≤ 1024 chars). Aim for 100–200 words:
- Generalize from failures — don't enumerate specific queries.
- **Structurally different phrasing each iteration.** With only 3 iterations, don't make iteration 2 a small tweak of iteration 1 — diversify aggressively.
- Imperative form ("Use this skill for...").
- Focus on user intent, not implementation.

Loop back to A.

#### Teardown — MANDATORY, runs in every exit path

```
mv <skill-path>/SKILL.md.bak-<hex> <skill-path>/SKILL.md
```

Use `mv` so the backup vanishes on success — a leftover `.bak-*` file means teardown didn't complete. Verify the restored `description:` matches the original. The skill is now back to its pre-optimization state.

---

### Step 4: Pick winner and apply

**Order matters: teardown FIRST, then present, then apply (only with user approval).**

1. Read `<workspace>/description-optimization/history.json`. Verify it has exactly as many entries as iterations you ran (missing entries = a logging step was skipped — investigate before proceeding).
2. Rank by: `passed` (higher better) → tie-break `precision` → tie-break `recall` → tie-break earliest `iteration` (prefer simpler/earlier when tied). Top entry's description = `best_description`.
3. Show the user: original vs. `best_description`, per-iteration scores, exit reason, key observations (which queries flipped between iterations, what patterns failed).
4. **Approved** → edit `<skill-path>/SKILL.md` and write `best_description` into `description:`. **Declined** → do nothing; teardown already restored the original.
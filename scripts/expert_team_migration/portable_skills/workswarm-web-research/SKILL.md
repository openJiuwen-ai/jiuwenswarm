---
name: workswarm-web-research
description: Research current external facts with WorkSwarm native search and webpage fetch tools, keeping authoritative-source evidence and truthful partial-result fallbacks.
---

# WorkSwarm Web Research

Use this Skill only when current external facts can materially change the answer.

1. Call `free_search(query, max_results, timeout_seconds)` to discover candidate sources.
2. Treat result snippets as discovery hints, not final evidence.
3. Select the most authoritative and directly relevant source, then call
   `fetch_webpage(url, max_chars, timeout_seconds)` to inspect its content.
4. Preserve the source URL, publication or effective date when available, query date,
   and applicability limits in the internal result.
5. If search or fetch is unavailable, return a clearly marked partial result. Do not
   replace missing evidence with model memory or invent current prices, policies,
   availability, schedules, weather, or safety facts.

## Search budget and stopping rules

- For each research question, retain at most 3 effective authoritative sources.
- Stop once 2 independent authoritative sources corroborate the conclusion; do not
  keep fetching equivalent evidence.
- After 3 consecutive `fetch_webpage` failures, stop fetching for that question and
  mark the unsupported item as `待核实` in the result.
- Do not expand the search beyond these limits unless the user explicitly asks for
  deep or exhaustive research.

Do not start a browser sub-agent or invoke a platform-specific search script.

# Conservative numeric evidence guard

`jiuwenswarm.common.numeric_evidence.numeric_block(claim, source)` returns
`True` when a quantity in a claim does not occur in its source text.
It is a small standard-library utility extracted from a research-paper agent.

It recognizes signed numbers, scientific notation, thousands separators and
basic percent, time, mass and length units. Units are normalized by dimension:
`1000 ms` matches `1 second`, but `50%` does not match a unitless `0.5`.
Number boundaries prevent `10` from matching `110` and ignore model identifiers
such as `o1` and `Scientist-v2`. Comparisons use `decimal.Decimal`.

```python
from jiuwenswarm.common.numeric_evidence import numeric_block

assert numeric_block("The score was 10", "The score was 110")
assert not numeric_block("It took 1000 ms", "It took 1 second")
```

Run the included synthetic checks without third-party packages:

```shell
python jiuwenswarm/common/numeric_evidence.py
```

## Limits and integration

This is quantity membership, **not semantic entailment**: it cannot establish
which metric, entity or experimental condition owns a matching number. It is
not a complete numeric parser (for example, locale-specific number formatting,
ranges, LaTeX expressions and arbitrary units are not supported).
It should complement, not replace, contextual evidence checking.

This initial contribution adds only the reusable guard and its self-check.
It does not register a new agent, change existing tool behavior, certify a
paper, or report a scientific accuracy improvement. The source research agent
uses it to flag numeric mismatches in quoted evidence; that larger workflow
is intentionally outside this focused contribution.

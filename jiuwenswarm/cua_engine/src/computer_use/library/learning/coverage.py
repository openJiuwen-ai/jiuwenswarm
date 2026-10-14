"""Bounded JSON Pointer variation counts; values never enter the report."""
import math
import re
from ..._json import dumps
from ...protocol.primitives import utf16Length

limits = dict(parameters=32, nodesPerCase=1024, depth=8, pathLength=160)
_emptyObject, _emptyArray = object(), object()


def summarizeInputCoverage(receipts):
    positive = [r for r in receipts if r["kind"] == "positive" and r["passed"]]
    rows = {}; truncated = False
    for receipt in positive:
        remaining = limits["nodesPerCase"]
        def add(path, value):
            nonlocal truncated
            if path not in rows:
                if len(rows) >= limits["parameters"]: truncated = True; return
                rows[path] = dict(caseCount=0, values=set())
            row = rows[path]; row["caseCount"] += 1
            # JS Set distinguishes booleans from numbers, equates int/float/-0,
            # and has a single NaN. Empty containers are two separate sentinels.
            kind = "number" if type(value) in (int, float) else type(value)
            if kind == "number" and math.isnan(value): value = "NaN"
            row["values"].add((kind, value))
        def visit(value, path, depth):
            nonlocal remaining, truncated
            remaining -= 1
            if remaining < 0 or depth > limits["depth"] or utf16Length(path) > limits["pathLength"]: truncated = True; return
            if value is None or type(value) in (str, int, float, bool): add(path, value); return
            if not isinstance(value, (list, dict)): truncated = True; return
            hasChild = False
            for key, item in (enumerate(value) if isinstance(value, list) else value.items()):
                remaining -= 1
                if remaining < 0: truncated = True; break
                hasChild = True; key = str(key)
                if utf16Length(key) > limits["pathLength"]: truncated = True; continue
                visit(item, path + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1)
            if not hasChild and remaining >= 0 and path: add(path, _emptyArray if isinstance(value, list) else _emptyObject)
        visit(receipt["input"], "", 0)
    return dict(positiveCases=len(positive), parameters=[dict(path=path, caseCount=row["caseCount"], distinctValues=len(row["values"]))
        for path, row in sorted(rows.items(), key=lambda pair: pair[0].encode("utf-16-be", "surrogatepass"))], truncated=truncated)


def inputCoverageMarkdown(coverage):
    shown = coverage["parameters"][:8]
    def escape(text): return re.sub(r"[\\`*_\[\]<>]", lambda m: "\\" + m[0], dumps(text))
    return "\n".join(["Positive test input coverage", "",
        f"{coverage['positiveCases']} accepted positive cases. Counts describe tested parameter variation only; they do not guarantee the supported input range. Values are omitted. Paths are JSON Pointers into the argument array.", "",
        *(f"- {escape(row['path'])}: present in {row['caseCount']}/{coverage['positiveCases']} cases; {row['distinctValues']} distinct value(s)." for row in shown),
        *(["Coverage listing truncated; see evidence.json. When coverage.truncated is true, recorded counts may be incomplete."] if coverage["truncated"] or len(shown) < len(coverage["parameters"]) else [])])

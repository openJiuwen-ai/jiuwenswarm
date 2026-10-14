"""Bounded raw scalar differences; no inferred action effects or task verdicts."""
import hashlib
from ..._json import dumps, utf8
from ..._identity import Record
from ...protocol.primitives import utf16Length, safeInteger
from ._util import slice16

LIMIT = dict(nodes=8192, depth=48, baselineBytes=2*1024*1024, valueChars=8192,
             examplesPerExecution=4, exampleBytes=2048, detailGroups=16, detailTargets=8, detailBytes=8192)


def _hasText(v): return isinstance(v, str) and bool(v)
def _size(v): return len(utf8(dumps(v)))
def _clip(v, n): return v if utf16Length(v) <= n else slice16(v, 0, n) + "…"


def _identity(node):
    return dumps([node["role"], *(None if key not in node or (key in ("name", "description") and node["role"] in ("textField", "textArea", "comboBox") and node[key] == node.get("value")) else [key, node[key]] for key in ("identifier", "url", "subrole", "name", "description"))])


def _pathsAgree(old, path, complete):
    if old["completePath"] and complete: return dumps(old["path"]) == dumps(path)
    common = min(len(old["path"]), len(path))
    return common == 0 or dumps(old["path"][-common:]) == dumps(path[-common:])


def _groupKey(g): return dumps([g["context"], g["before"], g["after"], g["gap"], g["ancestors"]])


def _retainExample(examples, g):
    key = hashlib.sha256(utf8(_groupKey(g))).hexdigest()
    existing = next((e for e in examples if e["key"] == key), None)
    if existing is not None: existing["count"] += 1; return
    if len(examples) >= LIMIT["examplesPerExecution"]: return
    t = g["targets"][0]
    examples.append(dict(key=key, label=_clip(t.get("label", t["role"] + "#" + t["ref"]), 24),
        before=_clip(g["before"]["value"], 28), after=_clip(g["after"]["value"], 28),
        beforeExecutionId=g["before"]["executionId"], afterExecutionId=g["after"]["executionId"], count=1))


def _retain(groups, change, limit):
    old = next((g for g in groups if _groupKey(g) == _groupKey(change)), None)
    if old is not None:
        old["count"] += 1
        if len(old["targets"]) < LIMIT["detailTargets"]: old["targets"].append(change["targets"][0])
        return
    groups.append({**change, "targets": list(change["targets"])})
    groups.sort(key=lambda g: (g["before"]["value"] == "", g["after"]["order"]))
    if len(groups) > limit: groups.pop()


def _endpoint(e):
    return dict(executionId=e["executionId"], observedId=e["observedId"], value=_clip(e["value"], 100),
                **(dict(valueTruncated=True, valueLength=utf16Length(e["value"])) if utf16Length(e["value"]) > 100 else {}),
                **({"childCoverage": _clip(e["partialReason"], 120)} if e.get("partialReason") else {}))


def valueChangeSummary(executionIds=None):
    wanted = set(executionIds) if executionIds is not None else None
    counts, details, nodes = {}, [], {}
    current = key = session = None
    epoch = order = byteCount = 0
    coverage = dict.fromkeys(("observations", "compared", "unknownObservations", "menuViewsSkipped", "contextBreaks", "sessionBreaks",
                             "identityBreaks", "unknownValues", "absentNodes", "limitedObservations", "oversizedValues", "baselineEvictions"), 0)
    def reset():
        nonlocal current, key, byteCount
        current = key = None; byteCount = 0; nodes.clear()
    def forget(ref):
        nonlocal byteCount
        old = nodes.pop(ref, None)
        if old is not None: byteCount -= old["bytes"]
    def consume(event):
        nonlocal session, epoch, order, current, key, byteCount
        if event.get("kind") in ("connection", "protocol_hello") or (event.get("kind") == "command_end" and event.get("status") == "error" and event.get("stage") == "connection"):
            if key: coverage["sessionBreaks"] += 1
            session = event["session"] if event.get("kind") == "protocol_hello" and _hasText(event.get("session")) else None
            epoch += 1; reset(); return
        if event.get("kind") != "native_observation": return
        if event.get("phase") != "task":
            if key: coverage["contextBreaks"] += 1
            reset(); return
        coverage["observations"] += 1; order += 1
        o = event.get("observation"); observed = event.get("observed")
        if not isinstance(o, dict) or not _hasText(event.get("scope")) or not _hasText(o.get("document")) or not _hasText(o.get("current")) \
            or not isinstance(observed, dict) or not _hasText(observed.get("id")) or not safeInteger(event.get("executionId")) or o.get("unavailable") or not o.get("root"):
            coverage["unknownObservations"] += 1; reset(); return
        context = dict(session=session, epoch=epoch, scope=event["scope"], document=o["document"], window=o["current"])
        nextKey = dumps(context)
        if key != nextKey:
            if key: coverage["contextBreaks"] += 1
            reset(); key = nextKey; current = context
        if o.get("context", {}).get("menuQuery") is True: coverage["menuViewsSkipped"] += 1; return
        subtree = o.get("context", {}).get("subtreeRef")
        windows = [w for w in o.get("windows", []) if w.get("ref") == o["current"]] if isinstance(o.get("windows"), list) else []
        if len(windows) != 1 or (_hasText(windows[0].get("document")) and windows[0]["document"] != o["document"]) \
            or ("subtreeRef" in o.get("context", {}) and (not _hasText(subtree) or subtree != o["root"].get("ref"))):
            coverage["unknownObservations"] += 1; reset(); return
        rows, refs = [], set()
        stack = [dict(node=o["root"], path=[], owner=None)]
        invalid = limited = False
        while stack:
            item = stack.pop(); n = item["node"]
            if not isinstance(n, dict) or not _hasText(n.get("ref")) or not _hasText(n.get("role")) or not isinstance(n.get("children"), list) or n["ref"] in refs:
                invalid = True; break
            if len(rows) >= LIMIT["nodes"] or len(item["path"]) >= LIMIT["depth"]: limited = True; break
            token = _identity(n)
            if utf16Length(token) > LIMIT["valueChars"]: limited = True; break
            refs.add(n["ref"])
            owner = n["ref"] if n["role"] in ("window", "sheet") else item["owner"]
            rows.append({**item, "owner": owner})
            path = [*item["path"], dict(ref=n["ref"], identity=token)]
            stack.extend(dict(node=child, path=path, owner=owner) for child in reversed(n["children"]))
            if len(stack) > LIMIT["nodes"]: limited = True; break
        if invalid or limited or (not subtree and not any(r["node"]["ref"] == o["current"] and r["node"]["role"] in ("window", "sheet") for r in rows)):
            coverage["limitedObservations" if limited else "unknownObservations"] += 1; reset(); return
        visible = [r for r in rows if r["owner"] in (None, o["current"])] if subtree else [r for r in rows if r["owner"] == o["current"]]
        seen = {r["node"]["ref"] for r in visible}
        observedByRef = {r["node"]["ref"]: {**r, "identity": _identity(r["node"])} for r in rows}
        for ref, old in list(nodes.items()):
            known = [*old["path"], dict(ref=ref, identity=old["identity"])]
            for i, ancestor in enumerate(known):
                now = observedByRef.get(ancestor["ref"])
                if now is not None and (now["identity"] != ancestor["identity"] or (now["owner"] is not None and now["owner"] != o["current"])
                    or not _pathsAgree(dict(path=old["path"][:i], completePath=old["completePath"]), now["path"], not subtree)):
                    forget(ref); coverage["identityBreaks"] += 1; break
        for ref, old in list(nodes.items()):
            if (not subtree or ref == subtree or any(p["ref"] == subtree for p in old["path"])) and ref not in seen:
                forget(ref); coverage["absentNodes"] += 1
        for row in visible:
            node, path = row["node"], row["path"]
            old = nodes.get(node["ref"])
            if not isinstance(node.get("value"), str) or utf16Length(node["value"]) > LIMIT["valueChars"]:
                if old is not None: forget(node["ref"]); coverage["unknownValues"] += 1
                if isinstance(node.get("value"), str) and utf16Length(node["value"]) > LIMIT["valueChars"]: coverage["oversizedValues"] += 1
                continue
            token, complete = _identity(node), not bool(subtree)
            same = old is not None and old["identity"] == token and _pathsAgree(old, path, complete)
            after = dict(executionId=event["executionId"], observedId=observed["id"], order=order, value=node["value"],
                         **({"partialReason": node["partialReason"]} if _hasText(node.get("partialReason")) else {}))
            if old is not None and not same: coverage["identityBreaks"] += 1
            if same:
                coverage["compared"] += 1
                if old["endpoint"]["value"] != node["value"]:
                    count = counts.setdefault(after["executionId"], dict(count=0, fromNonempty=0, examples=[]))
                    count["count"] += 1
                    if old["endpoint"]["value"] != "": count["fromNonempty"] += 1
                    change = dict(context=current, before=old["endpoint"], after=after, gap=order-old["endpoint"]["order"]-1,
                        ancestors="both" if old["fullOwner"] and complete else "subtree", count=1,
                        targets=[dict(ref=node["ref"], role=node["role"], **({"label": node["name"]} if _hasText(node.get("name")) else {}))])
                    if old["endpoint"]["value"] != "": _retainExample(count["examples"], change)
                    if wanted is not None and after["executionId"] in wanted: _retain(details, change, LIMIT["detailGroups"])
            preserve = same and not complete and len(old["path"]) >= len(path)
            entry = dict(identity=token, path=old["path"] if preserve else path, completePath=old["completePath"] if preserve else complete,
                         fullOwner=complete, endpoint=after, bytes=0)
            entry["bytes"] = _size(entry); forget(node["ref"])
            if entry["bytes"] > LIMIT["baselineBytes"]: coverage["baselineEvictions"] += 1; continue
            while len(nodes) >= LIMIT["nodes"] or byteCount + entry["bytes"] > LIMIT["baselineBytes"]:
                forget(next(iter(nodes))); coverage["baselineEvictions"] += 1
            nodes[node["ref"]] = entry; byteCount += entry["bytes"]

    def finish():
        exampleBytes = detailBytes = 0; projected = {}
        def detail(g):
            return {**g["context"], "targets": [{**t, **(dict(label=_clip(t["label"], 60), **({"labelTruncated": True} if utf16Length(t["label"]) > 60 else {})) if t.get("label") else {})} for t in g["targets"]],
                    "before": _endpoint(g["before"]), "after": _endpoint(g["after"]), "interveningObservations": g["gap"], "ancestors": g["ancestors"], "omittedTargets": g["count"]-len(g["targets"])}
        boundedDetails = {}
        for g in details:
            d = detail(g); n = _size(d)
            if detailBytes + n > LIMIT["detailBytes"]: continue
            detailBytes += n; boundedDetails.setdefault(g["after"]["executionId"], []).append(d)
        for id, row in counts.items():
            if wanted is not None and id not in wanted: continue
            examples, shown = [], 0
            for g in row["examples"]:
                extra = f" (+{g['count']-1} targets)" if g["count"] > 1 else ""
                example = f"{g['label']}{extra}: {dumps(g['before'])} → {dumps(g['after'])} [exec {g['beforeExecutionId']}→{g['afterExecutionId']}]"
                n = _size(example)
                if exampleBytes + n > LIMIT["exampleBytes"]: continue
                exampleBytes += n; examples.append(example); shown += g["count"]
            bounded = boundedDetails.get(id, [])
            projected[id] = dict(count=row["count"], fromNonempty=row["fromNonempty"], examples=examples, omittedExampleChanges=row["fromNonempty"]-shown,
                **(dict(details=bounded, omittedChanges=row["count"]-sum(len(g["targets"]) for g in bounded)) if wanted is not None and id in wanted else {}))
        return Record(coverage={**coverage,
            "meaning": "Raw values differ at recorded endpoints; listed under the later execution, not attributed to its commands. Missing values/nodes are unknown, never empty. Ancestors=subtree relies partly on the matching envelope; invisible ref reuse and unseen intermediate changes remain unknown.",
            "limits": dict(exampleBytes=LIMIT["exampleBytes"], detailBytes=LIMIT["detailBytes"]),
            "preview": "Examples use … for clipping; detailed value/label clipping is marked. Same-endpoint transitions may group distinct targets. Examples only sample nonempty starting values; details prefer those then earlier observations."}, executions=projected)
    return Record(consume=consume, finish=finish)

"""Experience lifecycle. A task log is source material, never validation proof."""
from computer_use._json import loads as jsonLoads
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import uuid
from ..._identity import Record
from ..._json import dumps, utf8
from ...client.host import computerHost
from ...client.execution import eager
from ...mcp.journal import now
from ...protocol.schema import obj, string, array, number
from ...protocol.primitives import trim, trimEnd
from .. import fileURLToPath, pathToFileURL
from ..execution import importFile
from .context import learningContext, recordLearning, withCheckCapture, presentCheck, publicationCompleted
from .static import checkModule
from .runner import checkCases, runCase
from .costs import summarizeCalls, summarizeExecution
from .targets import targetHints
from .journal import readLearningLog
from .coverage import summarizeInputCoverage
from .value_changes import valueChangeSummary
from .presentation import sourcePresentation
from .observation_probes import replayPreparation
from .diagnostics import lastObservation
from ._util import clone, slice16, resolve, member

slug = string(pattern=r"^[a-z][a-z0-9-]{0,63}\Z", patternLabel="/^[a-z][a-z0-9-]{0,63}$/")
_ids = array(number(integer=True, min=0, exclusiveMin=True), min=1, max=100)
proposalSchema = obj(dict(app=slug, title=string(min=1, max=160), method=string(pattern=r"^[a-zA-Z][a-zA-Z0-9_]*\Z", patternLabel="/^[a-zA-Z][a-zA-Z0-9_]*$/"),
    contract=obj({key: string(min=1) for key in ("inputs", "preconditions", "postconditions", "effects", "stop")}),
    sourceExecutionIds=_ids, code=string(), readme=string(min=1, max=100000), cases=string()))
_begin, _end = "<!-- computer-use:generated -->", "<!-- /computer-use:generated -->"
_receipts, _latest = {}, {}
_sourceCommands = {"findElement", "click", "scroll", "drag", "typeText", "paste", "pressKey", "clickElement", "openElementMenu", "performElementAction", "pressKeyElement", "pasteElement", "focusElement", "typeTextElement"}


def _sha(value): return hashlib.sha256(utf8(value)).hexdigest()
def _json(value): return (json.dumps(jsonLoads(dumps(value)), indent=2, ensure_ascii=False) + "\n").encode("utf-8", "backslashreplace").decode("utf-8")
def _read(file):
    with open(file, encoding="utf-8", newline="") as stream: return stream.read()


def _optional(file):
    try: return _read(file)
    except FileNotFoundError: return ""


def _write(file, content, *, exclusive=False, mode=0o600):
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC), mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream: stream.write(content)


def _atomic(file, content):
    temporary = Path(str(file) + "." + str(uuid.uuid4()) + ".tmp")
    try:
        _write(temporary, content, exclusive=True); os.replace(temporary, file)
    finally: temporary.unlink(missing_ok=True)


def _base(directory):
    # Protect handwritten Python entries and legacy entries during migration.
    return _sha(_json([_optional(directory / name) for name in ("README.md", "index.py", "index.mjs")]))


def _entry(readme, block):
    start, finish = readme.find(_begin), readme.find(_end)
    if (start < 0) != (finish < 0) or (start >= 0 and finish < start): raise ValueError("Malformed managed README section; preserve it and repair manually.")
    section = f"{_begin}\n{block}\n{_end}"
    return trimEnd(readme) + "\n\n" + section + "\n" if start < 0 else readme[:start] + section + readme[finish + len(_end):]


def _packageCalls(events):
    pairs, byId = [], {}
    for event in events:
        if event.get("kind") not in ("package_call_start", "package_call_end"): continue
        id = event.get("id") if isinstance(event.get("id"), str) else None
        pair = byId.get(id) if id is not None else None
        if pair is None:
            pair = {}; pairs.append(pair)
            if id is not None: byId[id] = pair
        pair["start" if event["kind"] == "package_call_start" else "end"] = event
    result = []
    from ...protocol.primitives import finite
    for pair in pairs:
        start, end = pair.get("start", {}), pair.get("end", {})
        def text(key): return end[key] if isinstance(end.get(key), str) else start[key] if isinstance(start.get(key), str) else None
        result.append(dict(id=text("id"), entry=text("entry"), method=text("method"), started="start" in pair, completed="end" in pair,
            returned=end["returned"] if type(end.get("returned")) is bool else None,
            **({"error": slice16(end["error"], 0, 4096)} if isinstance(end.get("error"), str) else {}),
            errors=end["errors"] if isinstance(end.get("errors"), list) else None,
            cost={key: end[key] if finite(end.get(key)) else None for key in ("commands", "observations", "actions", "elapsedMs")}))
    return result


@eager
async def source(options=None):
    options = obj(dict(executionIds=_ids.optional())).parse({} if options is None else options)
    executionIds = options.get("executionIds"); context = learningContext()
    values, events = valueChangeSummary(executionIds), []
    async for event in readLearningLog(context.log):
        values.consume(event)
        if event.get("phase") == "task" or event.get("kind") in ("candidate_created", "candidate_check"): events.append(event)
    starts = [e for e in events if e.get("kind") == "execution_start" and e.get("phase") == "task"]
    hints, changes = targetHints(events), values.finish()
    if executionIds is not None and any(not any(e["executionId"] == id for e in starts) for id in executionIds): raise ValueError("executionIds must come from this task's source() index.")
    packageBases, drafts = {}, []
    for e in events:
        if e.get("kind") != "candidate_created": continue
        directory = Path(os.path.abspath(fileURLToPath(e["draft"])))
        try: proposalText = _read(directory / "proposal.json")
        except FileNotFoundError: proposalText = None
        baseState = "unknown"
        if proposalText is not None:
            try: metadata = jsonLoads(proposalText)
            except ValueError as cause: raise ValueError(f"Invalid draft proposal JSON at {directory / 'proposal.json'}.") from cause
            if not isinstance(metadata, dict): raise ValueError(f"Invalid draft proposal object at {directory}.")
            if "base" in metadata:
                if not isinstance(metadata["base"], str): raise ValueError(f"Invalid draft proposal base at {directory}: expected a string.")
                appDirectory = directory.parent.parent
                if appDirectory not in packageBases: packageBases[appDirectory] = _base(appDirectory)
                baseState = "current" if metadata["base"] == packageBases[appDirectory] else "stale"
        checks = {}
        for item in events:
            r = item.get("receipt")
            if item.get("kind") == "candidate_check" and isinstance(r, dict) and r.get("directory") and Path(os.path.abspath(r["directory"])) == directory:
                checks[r["caseId"]] = {**{key: r[key] for key in ("id", "caseId", "hash", "passed")},
                    **{key: r[key] for key in ("validationHash", "error", "errorStack") if r.get(key)},
                    "evidence": pathToFileURL(directory / ".checks" / r["hash"] / (r["id"] + ".json"))}
        drafts.append(dict(draft=e["draft"], app=e["app"], method=e["method"], baseState=baseState, historicalChecks=list(checks.values())))
    executions = []
    for e in starts:
        if executionIds is not None and e["executionId"] not in executionIds: continue
        related = [item for item in events if item.get("phase") == "task" and item.get("executionId") == e["executionId"]]
        end = next((item for item in reversed(related) if item.get("kind") == "execution_end"), None)
        textPages = [item for item in related if item.get("kind") == "command_end" and item.get("method") == "readElementText" and item.get("status") == "ok" and isinstance(item.get("result"), dict) and isinstance(item["result"].get("text"), str)]
        evidence = [item for item in related if item.get("kind") in ("observation", "action", "package_call_start", "package_call_end") or item in textPages
            or (item.get("kind") in ("command_start", "command_end") and item.get("method") in _sourceCommands)
            or (item.get("kind") == "command_end" and (item.get("method") == "listApps" and item.get("status") == "ok" or item.get("status") == "error"))
            or (item.get("kind") == "execution_end" and isinstance(item.get("error"), str))] if executionIds is not None else None
        cost = summarizeExecution(related)
        if executionIds is None:
            cost = {**{key: cost[key] for key in ("commands", "reads", "inputs", "other", "observations", "observationChars")},
                    "beforeFirstInput": {key: cost["beforeFirstInput"][key] for key in ("boundary", "commands", "reads")},
                    "failedCommandCount": len(cost["failedCommands"])}
        targets = hints.get(e["executionId"], [])
        executions.append(dict(id=e["executionId"], code=e["code"], completed=end is not None, isError=end.get("isError") if end else None, **(dict(targets=targets) if executionIds is not None else dict(targetCount=len(targets))),
            **({"error": end["error"]} if end and isinstance(end.get("error"), str) else {}), cost=cost, packageCalls=_packageCalls(related),
            **({"valueChanges": changes.executions[e["executionId"]]} if e["executionId"] in changes.executions else {}),
            observations=sum(item.get("kind") == "observation" for item in related), textPages=len(textPages), actions=sum(item.get("kind") == "action" for item in related),
            failures=[dict(method=item.get("method"), error=item.get("error")) for item in related if item.get("kind") == "command_end" and item.get("status") == "error"],
            **({"evidence": sourcePresentation(evidence, related)} if evidence is not None else {})))
    return Record(view="detail" if executionIds is not None else "index", task=context.task, log=pathToFileURL(context.log), drafts=drafts, valueChangeCoverage=changes.coverage, executions=executions)


@eager
async def draft(input):
    context, value = learningContext(), proposalSchema.parse(input)
    checkModule(value["code"], "package"); checkModule(value["cases"], "cases")
    evidence = await source()
    if any(not any(e["id"] == id and e["completed"] for e in evidence.executions) for id in value["sourceExecutionIds"]): raise ValueError("Source execution IDs must name completed task executions from this task.")
    events = [e async for e in readLearningLog(context.log) if e.get("phase") == "task" and e.get("executionId") in value["sourceExecutionIds"]]
    if not any(e.get("kind") in ("native_observation", "observation") for e in events): raise ValueError("Candidate needs an observed UI source, not just a claimed successful answer.")
    appDirectory = fileURLToPath(context.root) / value["app"]
    directory = appDirectory / ".drafts" / str(uuid.uuid4()); directory.mkdir(parents=True, mode=0o700)
    metadata = {key: v for key, v in value.items() if key not in ("code", "readme", "cases")}
    _write(directory / "proposal.json", _json(dict(**metadata, base=_base(appDirectory), task=context.task, sourceLog=evidence.log, created=now())))
    for name, content in {"index.py": value["code"], "README.md": value["readme"], "cases.py": value["cases"], "source.json": _json(events)}.items(): _write(directory / name, content, exclusive=True)
    result = Record(draft=pathToFileURL(directory) + "/", status="draft")
    recordLearning(dict(kind="candidate_created", **result, app=value["app"], method=value["method"]))
    return result


async def _load(url):
    context = learningContext(); directory, root = fileURLToPath(url), fileURLToPath(context.root)
    relative = Path(os.path.relpath(directory, root)).parts
    if len(relative) != 3 or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", relative[0]) or relative[1] != ".drafts" or not re.fullmatch(r"[a-f0-9-]{36}", relative[2]):
        raise ValueError("Expected a draft URL returned by draft() in the current libraryRoot.")
    files = {name: _read(directory / name) for name in ("index.py", "README.md", "cases.py", "proposal.json", "source.json")}
    metadata = jsonLoads(files["proposal.json"])
    if metadata["task"]["id"] != context.task["id"] or metadata["app"] != relative[0]: raise ValueError("Draft belongs to a different task/application.")
    proposalSchema.parse({**{key: metadata[key] for key in ("app", "title", "method", "contract", "sourceExecutionIds")}, "code": files["index.py"], "readme": files["README.md"], "cases": files["cases.py"]})
    checkModule(files["index.py"], "package"); checkModule(files["cases.py"], "cases")
    validatedFiles = {key: content for key, content in files.items() if key != "README.md"}
    return Record(context=context, directory=directory, appDirectory=root / metadata["app"], files=files, metadata=metadata,
                  hash=_sha(_json(files)), validationHash=_sha(_json(validatedFiles)))


def _immutableFiles(directory, files):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in files.items():
        try: _write(directory / name, content, exclusive=True, mode=0o400)
        except FileExistsError:
            if _read(directory / name) != content: raise


def _snapshot(candidate):
    directory = candidate.directory / ".checks" / candidate.hash
    _immutableFiles(directory, candidate.files)
    return directory


@eager
async def check(url, caseId):
    candidate = await _load(url)
    if _base(candidate.appDirectory) != candidate.metadata["base"]: raise ValueError("Package changed since draft creation; create a new draft against the current package and revalidate. No check was run; existing package preserved.")
    directory, nonce = _snapshot(candidate), str(uuid.uuid4())
    cases = importFile(directory / "cases.py", nonce).cases
    checkCases(cases, False)
    c = next((item for item in cases if item["id"] == caseId), None)
    if c is None: raise ValueError(f"Unknown case {caseId}; available: {', '.join(item['id'] for item in cases)}.")
    key = f"{candidate.directory}:{caseId}"; _latest[key] = nonce
    create = getattr(importFile(directory / "index.py", nonce), "create", None)
    if not callable(create): raise ValueError("Package must export create(computer).")
    factory = candidate.context.get("computerFactory")
    host = factory() if factory is not None else computerHost()
    try: result = await withCheckCapture(lambda: runCase(member(host, "computer"), create, candidate.metadata["method"], c))
    finally: await resolve(member(host, "close")())
    if (await _load(url)).validationHash != candidate.validationHash: raise ValueError("Executable draft or contract changed during validation; no check recorded.")
    receipt = dict(id=str(uuid.uuid4()), taskId=candidate.context.task["id"], directory=str(candidate.directory), hash=candidate.hash, validationHash=candidate.validationHash,
        caseId=caseId, kind=c["kind"], input=c["input"], **result, cost=summarizeCalls(result["calls"]), freshBindings=True,
        environment=dict(platform=sys.platform, python=platform.python_version(), desktopMode=os.environ.get("COMPUTER_USE_DESKTOP", "shared")), time=now())
    evidence = directory / (receipt["id"] + ".json")
    _write(evidence, _json(receipt), exclusive=True, mode=0o400)
    _receipts[receipt["id"]] = clone(receipt)
    if _latest.get(key) == nonce: _latest[key] = receipt["id"]
    recordLearning(dict(kind="candidate_check", receipt=receipt))
    presented = Record(**{**receipt, "evidence": pathToFileURL(evidence), **({"lastObservation": lastObservation(result["calls"])} if not receipt["passed"] else {}),
        "calls": [{**{key: value for key, value in call.items() if key != "result"},
                   **({"result": call["result"]} if isinstance(call.get("result"), dict) and "status" in call["result"] else {})} for call in result["calls"]]})
    presentCheck(presented, candidate.context.executionId)
    return presented


@eager
async def replayPrepare(url, checkId):
    candidate = await _load(url); receipt = _receipts.get(checkId)
    if not receipt or receipt["directory"] != str(candidate.directory) or receipt["taskId"] != candidate.context.task["id"] or receipt["passed"] is not False or receipt["terminalStage"] != "prepare" or receipt["candidateStarted"] is not False:
        raise ValueError("replayPrepare requires this worker's failed prepare check for the same task and draft, before the candidate started.")
    directory = _snapshot(candidate)
    cases = importFile(directory / "cases.py", str(uuid.uuid4())).cases; checkCases(cases, False)
    c = next((item for item in cases if item["id"] == receipt["caseId"]), None)
    if c is None: raise ValueError(f"Recorded case {receipt['caseId']} is missing from the current draft.")
    calls = [call for call in receipt["calls"] if call["phase"] == "prepare"]
    diagnostic = await replayPreparation(c.get("prepare", lambda _: None), calls, str(receipt.get("error")))
    if (await _load(url)).validationHash != candidate.validationHash: raise ValueError("Executable draft or contract changed during preparation diagnosis.")
    result = Record(**diagnostic, caseId=receipt["caseId"], sourceCheckId=receipt["id"], sourceHash=receipt["hash"], sourceValidationHash=receipt["validationHash"], currentValidationHash=candidate.validationHash)
    recordLearning(dict(kind="prepare_replay", **result))
    return result


@eager
async def publish(url, checkIds):
    c = await _load(url)
    if not isinstance(checkIds, list) or any(not isinstance(id, str) for id in checkIds): raise ValueError("publish expects receipt IDs from check(), not a passed flag.")
    selected = [_receipts.get(id) for id in checkIds]
    def mismatch(): return ValueError("All cases must pass for this exact code, contract, source and cases in this MCP session; edited executable drafts, failed/latest checks and fabricated receipts cannot be published.")
    if not selected or any(not r or not r["passed"] or r["directory"] != str(c.directory) or r["validationHash"] != c.validationHash for r in selected): raise mismatch()
    cases = importFile(c.directory / ".checks" / selected[0]["hash"] / "cases.py").cases; checkCases(cases)
    if len(selected) != len(cases) or any(not any(r["caseId"] == test["id"] and _latest.get(f"{c.directory}:{test['id']}") == r["id"] for r in selected) for test in cases): raise mismatch()
    demonstrated = [r for r in selected if r["kind"] == "positive" and r.get("verification", {}).get("positiveEvidence") in ("observed-change", "observed-return")]
    if len({dumps(r["input"]) for r in demonstrated}) < 2:
        raise ValueError("Publication requires two distinct positive inputs with an observed state change or an independently asserted, UI-grounded return. With bindInput, fixed fixture selectors may distinguish tested targets without becoming method parameters. Already-satisfied/repeat-only cases do not establish the operation; prepare an authorized nontrivial starting state or assert expectedReturn for data extraction.")
    inputCoverage = summarizeInputCoverage(selected)
    root = fileURLToPath(c.context.root); lock = root / ".publish.lock"
    try: lock.mkdir(mode=0o700)
    except FileExistsError: raise RuntimeError("Another publisher holds .publish.lock; do not overwrite it. Check for an interrupted publisher.") from None
    try:
        if _base(c.appDirectory) != c.metadata["base"]: raise ValueError("Package changed since draft creation; rebase the candidate and revalidate. Existing package preserved.")
        revision = c.appDirectory / ".revisions" / c.hash
        evidence = _json(dict(hash=c.hash, validationHash=c.validationHash, proposal=c.metadata, checks=selected, inputCoverage=inputCoverage))
        _immutableFiles(revision, {**c.files, "evidence.json": evidence})
        module = pathToFileURL(revision / "index.py")
        readmeFile, rootFile = c.appDirectory / "README.md", root / "README.md"
        current, index = _optional(readmeFile), _optional(rootFile)
        start, finish = current.find(_begin), current.find(_end)
        generated = "" if start < 0 else trim(current[start + len(_begin):finish])
        methodStart, methodEnd = f"<!-- computer-use:method {c.metadata['method']} -->", f"<!-- /computer-use:method {c.metadata['method']} -->"
        contract, details = c.metadata["contract"], ".revisions/" + c.hash
        block = "\n".join([methodStart, "Method: " + c.metadata["method"], "Entry: " + module,
            "Status: active; revision: " + c.hash, "Generated experience: " + c.metadata["title"],
            *(key.capitalize() + ": " + contract[key] for key in ("inputs", "preconditions", "postconditions", "effects", "stop")),
            "Load this method's Entry with loadPackage(entryURL). Other methods may use different Entries and arguments.",
            f"[Calling details]({details}/README.md) · [Validation evidence]({details}/evidence.json)", methodEnd])
        previous, previousEnd = generated.find(methodStart), generated.find(methodEnd)
        if (previous < 0) != (previousEnd < 0) or previousEnd < previous: raise ValueError("Malformed method discovery entry; preserve it and repair manually.")
        catalog = "\n\n".join(filter(None, (generated, block))) if previous < 0 else generated[:previous] + block + generated[previousEnd + len(methodEnd):]
        _atomic(readmeFile, _entry(current, catalog))
        link = c.metadata["app"] + "/README.md"
        if f"]({link})" not in index: _atomic(rootFile, trimEnd(index) + f"\n\n- [{c.metadata['app']}]({link})\n")
        recordLearning(dict(kind="candidate_published", hash=c.hash, module=module))
        published = Record(status="active", revision=c.hash, module=module, readme=pathToFileURL(readmeFile), inputCoverage=inputCoverage)
    finally: shutil.rmtree(lock)

    publicationCompleted(dict(kind="published", publication=dict(app=c.metadata["app"], method=c.metadata["method"], **{key: published[key] for key in ("module", "revision", "readme")})), c.context)
    return published


@eager
async def suspend(app, reason):
    context = learningContext(); slug.parse(app)
    if not isinstance(reason, str) or not trim(reason): raise ValueError("Explain the observed failure; infrastructure errors alone do not invalidate a package.")
    lock = fileURLToPath(context.root) / ".publish.lock"; lock.mkdir(mode=0o700)
    try:
        file = fileURLToPath(context.root) / app / "README.md"; current = _read(file)
        if _begin not in current: raise ValueError("No generated entry to suspend; handwritten package preserved.")
        _atomic(file, _entry(current, f"Status: suspended. Do not use this generated revision.\nReason: {reason}"))
        publicationCompleted(dict(kind="suspended", app=app), context)
        recordLearning(dict(kind="candidate_suspended", app=app, reason=reason))
        return Record(status="suspended")
    finally: shutil.rmtree(lock)

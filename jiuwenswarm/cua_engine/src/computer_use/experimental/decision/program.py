"""Isolated desktop-program prototype: planner code, observation answers as data."""
import ast
import asyncio
import copy
import sys
import weakref
from collections.abc import MutableMapping
from datetime import datetime, timezone
from uuid import uuid4
from jsonschema import Draft202012Validator
from ..._identity import Record
from ..._json import dumps
from ...client.computer import App, selectWorkingWindow
from ...client.execution import eager, operationCheckpoint
from ...client.observation_data import observationData
from ...protocol.primitives import trim, utf16Length

_queries = weakref.WeakSet()


class InvalidProgramAnswer(ValueError): pass


def requireInlineSchema(value):
    if isinstance(value, dict):
        if any(isinstance(value.get(key), str) for key in ("$ref", "$dynamicRef", "$recursiveRef", "$id")): raise ValueError("Use an inline observation schema without references or a schema id.")
        for child in value.values(): requireInlineSchema(child)
    elif isinstance(value, list):
        for child in value: requireInlineSchema(child)


def objectSchema(properties): return dict(type="object", properties=properties, required=list(properties), additionalProperties=False)


class _Query:
    def __init__(self, view, question, schema, bind):
        self.view, self.question, self.schema, self.bind = view, question, schema, bind


class ProgramView:
    def __init__(self, session, app, state, image, wholeWindow=True, observedAt=None, retained=False):
        self.session, self.app, self.state, self.image = session, app, state, image
        self.wholeWindow, self.observedAt, self.retained = wholeWindow, observedAt or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), retained
        self.data = observationData(state)
        if self.data is None: raise RuntimeError("Structured observation unavailable; update the desktop SDK.")
        self._noLaterOperation, self._sameEventLoopTurn = operationCheckpoint(), True
        # As with setImmediate, let the awaiting program resume before expiring
        # the capture. call_soon runs before that task's wake-up callback and
        # would force a duplicate final read after every real asynchronous read.
        asyncio.get_running_loop().call_later(0, self._expire)
    def _expire(self): self._sameEventLoopTurn = False
    def canReuseAtCompletion(self): return self._sameEventLoopTurn and self._noLaterOperation()
    @property
    def text(self): return self.state.text
    @property
    def screenshot(self): return self.state.screenshot
    @property
    def overview(self): return self.state.overview
    @property
    def document(self): return self.data.get("identity", {}).get("document")
    @property
    def current(self): return self.data.get("identity", {}).get("current")
    @property
    def windows(self): return self.data.get("identity", {}).get("windows", ())
    def evidence(self, image=None):
        show = self.image if image is None else image
        return Record(text=self.state.text, **({"app": self.data["instance"]} if self.data.get("instance") else {}), observedAt=self.observedAt, **({"retained": True} if self.retained else {}), images=[png for png in (self.state.screenshot, self.state.overview) if png is not None] if show else [])
    def _query(self, question, schema, bind):
        if not isinstance(question, str) or not trim(question): raise ValueError("An observation question must be nonempty.")
        requireInlineSchema(schema)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        def validate(value):
            errors = list(validator.iter_errors(value))
            if errors: raise InvalidProgramAnswer("; ".join(f"{'.'.join(map(str, error.absolute_path)) or 'answer'}: {error.message}" for error in errors))
            return bind(copy.deepcopy(value))
        query = _Query(self, question, schema, validate)
        _queries.add(query)
        return query
    def elements(self, descriptions):
        if not isinstance(descriptions, dict) or not descriptions or any(not isinstance(key, str) or not key or not isinstance(value, str) or not trim(value) for key, value in descriptions.items()): raise ValueError("Describe at least one element.")
        properties = {name: dict(anyOf=[dict(type="string", pattern=r"^(?:0|[1-9][0-9]*)(?![\s\S])"), dict(type="null")], description=description) for name, description in descriptions.items()}
        def bind(values):
            output = Record()
            for name, ref in values.items():
                if ref is not None and ref not in self.data["refs"]: raise InvalidProgramAnswer("Element is outside this observation. Use an observed decimal reference or JSON null.")
                output[name] = None if ref is None else self.app.element(ref)
            return output
        return self._query("Locate each requested control in the CURRENT observation. Return its decimal reference as a JSON string, not its label, value or cell address. Use JSON null if it cannot be identified unambiguously.", objectSchema(properties), bind)
    def window(self, description):
        refs = self.data["windows"]
        schema = dict(anyOf=[dict(type="string", enum=list(refs)), dict(type="null")]) if refs else dict(type="null")
        def bind(value):
            ref = value["ref"]
            if ref is not None and ref not in refs: raise InvalidProgramAnswer("Window is outside this catalogue.")
            return None if ref is None else selectWorkingWindow(self.app, ref)
        return self._query("Select the window described below from the CURRENT window catalogue. Put the exact observed reference in ref, or null if unknown. Do not infer ownership from a title alone.\n"+description, objectSchema(dict(ref=schema)), bind)
    def value(self, question, schema): return self._query(question, schema, lambda value: value)


class SavedApps(MutableMapping):
    def __init__(self): object.__setattr__(self, "_values", {})
    def __getitem__(self, key): return self._values[key]
    def __setitem__(self, key, value):
        if not isinstance(key, str) or not trim(key) or key != trim(key): raise ValueError("Use a nonempty saved App name without surrounding whitespace.")
        if not isinstance(value, App): raise ValueError("Save only an SDK App handle; observe it again when current state is needed.")
        self._values[key] = value
    def __delitem__(self, key): del self._values[key]
    def __iter__(self): return iter(self._values)
    def __len__(self): return len(self._values)
    def __getattr__(self, key):
        try: return self[key]
        except KeyError: raise AttributeError(key) from None
    def __setattr__(self, key, value): self[key] = value
    def __delattr__(self, key): del self[key]


class DecisionProgramWorkspace:
    def __init__(self): self.apps, self._last, self._generation = SavedApps(), None, 0
    def start(self, app, selector, source, output=None):
        named = selector.startswith("@")
        if named:
            name = selector[1:]
            if name not in self.apps: raise ValueError(f"Saved App {dumps(name)} is unavailable; select an observed App or an existing saved name.")
            app = self.apps[name]
        self._generation += 1
        generation = self._generation
        previous, self._last = self._last, None
        sameApp = previous is not None and (previous["view"].app is app if named else previous["view"].data.get("instance") == selector)
        initial = previous["view"] if sameApp and previous["unchanged"]() else None
        def complete(view):
            if generation == self._generation: self._last = dict(view=view, unchanged=operationCheckpoint())
        return DecisionProgramSession(initial.app if initial is not None else app, source, output, dict(apps=self.apps, initial=initial, complete=complete))


class _Returns(ast.NodeTransformer):
    def __init__(self, result): self.result = result
    def visit_Return(self, node):
        return [ast.copy_location(ast.Assign(targets=[ast.Attribute(value=ast.Name(id=self.result, ctx=ast.Load()), attr="value", ctx=ast.Store())], value=node.value or ast.Constant(None)), node), ast.copy_location(ast.Return(), node)]
    def visit_FunctionDef(self, node): return node
    def visit_AsyncFunctionDef(self, node): return node
    def visit_Lambda(self, node): return node
    def visit_ClassDef(self, node): return node


def _ignoreFinalizer(generator):
    # A stopped program must never execute planner finally blocks later during
    # GC or loop shutdown. Capture these hooks only while creating the first
    # asend object, without yielding to other tasks. Natural completion/errors
    # retain normal Python finally behavior. No live generator is kept globally.
    pass


class DecisionProgramSession:
    def __init__(self, app, source, output=None, continuation=None):
        if not isinstance(source, str) or not trim(source) or utf16Length(source) > 65536: raise ValueError("Provide a nonempty desktop program of at most 65536 characters.")
        self.app, self._observedApp = app, app
        self._last = self._readInThisContinuation = self._pending = None
        self._image = self._busy = self._ended = self._firstIteration = False
        self._completeObservation = (continuation or {}).get("complete")
        @eager
        async def observe(app, options=None):
            options = options or {}
            readOptions = {key: value for key, value in options.items() if key != "image"}
            self._observedApp, self._image = app, options.get("image") is True
            self._last = self._readInThisContinuation = None
            view = ProgramView(self, app, await app.read(readOptions), self._image, "menuPath" not in readOptions)
            self._last = self._readInThisContinuation = view
            return view
        def missingOutput(*_): raise RuntimeError("Printing requires the MCP host's output bindings.")
        prior = (continuation or {}).get("initial")
        initial = ProgramView(self, app, prior.state, prior.image, True, prior.observedAt, True) if prior is not None else None
        self._last, self._image = initial, prior.image if prior is not None else False
        # Python async generators cannot return a value. Rewrite only returns
        # belonging to this program into a host result slot followed by return;
        # nested functions keep their ordinary semantics.
        tree = ast.parse(source, filename="<desktop-program>", mode="exec")
        name, resultName = "__program_"+uuid4().hex, "__result_"+uuid4().hex
        transformed = _Returns(resultName).visit(tree)
        body = [ast.If(test=ast.Constant(False), body=[ast.Expr(value=ast.Yield(value=ast.Constant(None)))], orelse=[]), *transformed.body]
        fn = ast.AsyncFunctionDef(name=name, args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=arg) for arg in ("app", "observe", "print", "printImage", "initial", "apps", resultName)], kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[], type_params=[])
        module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        namespace = {}
        exec(compile(module, "<desktop-program>", "exec"), namespace)
        self._result = Record(value=None)
        output = output or {}
        apps = continuation["apps"] if continuation is not None and "apps" in continuation else SavedApps()
        self._iterator = namespace[name](app, observe, output.get("print", missingOutput), output.get("printImage", missingOutput), initial, apps, self._result)

    @eager
    async def next(self, answer=None):
        if self._busy or self._ended: raise RuntimeError("Program is busy or ended; no program input dispatched.")
        if (not isinstance(answer, dict) or answer.get("id") != self._pending["id"]) if self._pending is not None else answer is not None:
            raise RuntimeError("Stale or unexpected answer; program was not resumed.")
        self._busy, self._readInThisContinuation = True, None
        try:
            pending, self._pending, value = self._pending, None, None
            if pending is not None:
                try: value = pending["query"].bind(answer.get("value"))
                except InvalidProgramAnswer as error:
                    if pending["rejections"] == 0:
                        return self._ask(pending["query"], 1, "Answer rejected before the program resumed; no operation from this answer was dispatched. "+str(error)+' Correct the answer once using the same observation and schema; use JSON null, not the string "null", for unknown nullable targets.')
                    raise
            if not self._firstIteration:
                old = sys.get_asyncgen_hooks()
                try:
                    sys.set_asyncgen_hooks(firstiter=_ignoreFinalizer, finalizer=_ignoreFinalizer)
                    pendingNext = self._iterator.asend(value)
                    self._firstIteration = True
                finally: sys.set_asyncgen_hooks(*old)
            else: pendingNext = self._iterator.asend(value)
            try: query = await pendingNext
            except StopAsyncIteration:
                value = self._result.value
                if value is not None and not isinstance(value, str): raise ValueError("Return a brief string report or nothing; the host collects final evidence.")
                self._ended = True; self._iterator = None
                return Record(kind="finish", summary=value if value is not None else "Program completed; verify the current UI.", **(await self._finalEvidence(True)))
            if not isinstance(query, _Query) or query not in _queries: raise ValueError("Yield only a question from an observed view.")
            if query.view.session is not self or query.view is not self._last: raise ValueError("Question is not from this program's latest view. Observe again.")
            return self._ask(query)
        except Exception as error:
            self._ended = True; self._iterator = None
            return Record(kind="error", summary=str(error)+". Program stopped; preceding operations may have executed. Do not replay the program.", **(await self._finalEvidence()))
        finally: self._busy = False

    def _ask(self, query, rejections=0, answerError=None):
        identifier = str(uuid4())
        self._pending = dict(id=identifier, query=query, rejections=rejections)
        return Record(kind="question", request=Record(id=identifier, question=query.question, schema=query.schema, **({"answerError": answerError} if answerError is not None else {}), **query.view.evidence()))

    @eager
    async def stop(self, reason):
        if self._busy: raise RuntimeError("Wait for the active program operation before stopping.")
        self._ended, self._pending, self._iterator = True, None, None
        return Record(kind="error", summary=reason+". Program stopped without replay.", **(await self._finalEvidence()))

    async def _finalEvidence(self, reuseExplicitRead=False):
        try:
            last = self._readInThisContinuation
            if reuseExplicitRead and last is not None and last is self._last and last.wholeWindow and last.canReuseAtCompletion():
                if self._completeObservation: self._completeObservation(last)
                return dict(evidence=last.evidence(), evidenceSource="program_observation")
            view = ProgramView(self, self._observedApp, await self._observedApp.read(), self._image)
            if self._completeObservation: self._completeObservation(view)
            return dict(evidence=view.evidence(), evidenceSource="final_observation")
        except Exception as error: return dict(observationError=str(error))

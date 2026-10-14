"""Explicit package invocation boundaries; never changes desktop recovery."""
import asyncio
from contextvars import ContextVar
import hashlib
import importlib.util
import inspect
import sys
import time
from types import SimpleNamespace
import uuid
from . import fileURLToPath
from .._identity import Record
from ..client.execution import eager

_calls=ContextVar("computer_use_package_call",default=None)


def currentPackageCall():return _calls.get()


def _call(entry,method,phase):
    return Record(id=str(uuid.uuid4()),started=int(time.time()*1000),entry=entry,method=method,phase=phase,
                  commands=0,observations=0,actions=0,pending=set(),errors=[],receipts=[])


def importFile(file, nonce=None):
    name="_computer_use_package_"+hashlib.sha256(str(file).encode()).hexdigest()+("_"+str(nonce).replace("-", "") if nonce is not None else "")
    if name in sys.modules:return sys.modules[name]
    spec=importlib.util.spec_from_file_location(name,file)
    if spec is None or spec.loader is None:raise ImportError("Package Entry must be a Python module")
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    try:spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name,None);raise
    return module


def packageLoader(computer,root,hooks):
    active=None
    @eager
    async def load(entry):
        nonlocal active
        try:file=fileURLToPath(entry).resolve(strict=True)
        except ValueError as error:raise ValueError("loadPackage expects the installed package's local Entry file URL.") from error
        directory=fileURLToPath(root).resolve(strict=True)
        if file==directory or not file.is_relative_to(directory):raise ValueError("loadPackage entry must be inside libraryRoot.")
        initialize=_call(entry,"create","initialize")
        token=_calls.set(initialize)
        try:
            module=importFile(file)
            create=getattr(module,"create",None)
            if not callable(create):raise ValueError("Package must export create(computer).")
            api=create(computer)
            if inspect.isawaitable(api):
                if inspect.iscoroutine(api):api.close()
                elif isinstance(api,asyncio.Future):api.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)
                raise ValueError("create(computer) must synchronously return an object of methods without operating the UI.")
            if type(api) is dict:properties=dict(api)
            elif isinstance(api,SimpleNamespace):properties=vars(api).copy()
            else:
                raise ValueError("Package API must expose ordinary methods/data, not accessors.")
        finally:
            initialize["phase"]="finished";_calls.reset(token)
        def tracked(key,method):
            @eager
            async def invoke(*args,**kwargs):
                nonlocal active
                if currentPackageCall() is not None:raise RuntimeError("Nested package execution is not supported; compose methods sequentially in the caller.")
                if active is not None and active["phase"]=="finished" and not active["pending"]:active=None
                if active is not None:raise RuntimeError("Await the previous package invocation before starting another; concurrent desktop workflows are not supported.")
                call=_call(entry,str(key),"run");active=call
                value=error=None;returned=False
                try:
                    hooks["start"](call)
                    token=_calls.set(call)
                    try:
                        value=method(*args,**kwargs)
                        if inspect.isawaitable(value):value=await value
                    finally:_calls.reset(token)
                    if call["pending"]:raise RuntimeError("Package left UI operations unawaited. Started operations were allowed to settle before this result; observe before continuing.")
                    returned=True
                except BaseException as caught:error=caught
                finally:
                    call["phase"]="draining"
                    if call["pending"]:await asyncio.gather(*list(call["pending"]),return_exceptions=True)
                    call["phase"]="finished";active=None
                try:hooks["finish"](call,dict(returned=returned,**({"error":error} if not returned else {})))
                except Exception:
                    if returned:raise
                if not returned:raise error
                return value
            return invoke
        return SimpleNamespace(**{key:tracked(key,value) if callable(value) else value for key,value in properties.items()})
    return load

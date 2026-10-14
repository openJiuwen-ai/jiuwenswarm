"""Persistent Python worker. User code never shares the MCP server's stdout."""
from computer_use._json import loads as jsonLoads
import ast
import asyncio
import base64
import builtins
from contextvars import ContextVar
import inspect
import io
import os
import pprint
import signal
import socket
import sys
import time
from .. import Computer
from .._identity import Record, WeakIdentityMap
from .._json import dumps
from ..protocol.png import PNG, decodePNG
from ..client.host import (setActionHandler,setObservationHandler,setPreviewHandler,recordStructuredObservations,
    setDiagnosticHandler,setExecutionGuard,setTextPageHandler,computerHost)
from ..client.preview import latestPreviewSink
from ..client.images import imageDelivered,imageLabel,imageMetadata,requireImageDelivery,resetImageDelivery
from ..client.result_presentation import resultReceipt,textSourceLabel
from ..observation.action_presentation import ActionPresentation
from ..library import resolveLibraryRoot
from ..library.execution import currentPackageCall,packageLoader
from ..library.learning.context import (beginLearning,recordLearning,isCapturingCheck,setCheckPresentationHandler,setPublicationHandler,learningKey)
from ..library.learning.check_presentation import formatCheck
from ..library.learning.inspection import inspectionComputer
from .output import OutputBuffer
from .presentation import ObservationOutput,ObservationPresenter

_runs=ContextVar("computer_use_execution",default=None)


class Runtime:
    def __init__(self,send,computerFactory=Computer):
        self.send,self.computerFactory=send,computerFactory
        self.experienceEnabled=os.environ.get("COMPUTER_USE_EXPERIENCE")!="0"
        self.presenter=ObservationPresenter()
        self.commandRuns,self.commandPackages,self.activePackages={},{},{}
        self.computers=[]
        requireImageDelivery()
        setCheckPresentationHandler(self.checkPresentation);setPublicationHandler(self.publication)
        setExecutionGuard(self.guard);setDiagnosticHandler(self.diagnostic)
        setActionHandler(self.action);setTextPageHandler(self.textPage);setObservationHandler(self.observation)
        if os.environ.get("COMPUTER_USE_REMOTE_PREVIEW")=="1":
            async def preview(event):self.send(dict(preview=event))
            setPreviewHandler(latestPreviewSink(preview))
        self.taskNamespace=self.namespace()
        self.compileNamespace=None

    def guard(self):
        run=_runs.get()
        if run is None or not run["active"] or run["phase"]!="code":
            raise RuntimeError("Computer operation belongs to an ended tool execution. Effect: no command sent. Next: await every operation within a current tool call; do not use background timers for input.")
        pkg=currentPackageCall()
        if pkg is not None and pkg["phase"]!="run":
            raise RuntimeError("Package initialization or a closing/ended package call cannot start new UI operations. Effect: no command sent by this operation.")
        pending=asyncio.get_running_loop().create_future()
        run["pending"].add(pending)
        if pkg is not None:pkg["pending"].add(pending)
        def done():
            run["pending"].discard(pending)
            if pkg is not None:pkg["pending"].discard(pending)
            if not pending.done():pending.set_result(None)
        return done

    def diagnostic(self,event):
        recordLearning(event)
        run=_runs.get();executionId=run["id"] if run is not None else None
        trace=event.get("traceId")
        if isinstance(trace,str):
            if event["kind"]=="command_start":
                self.commandRuns[trace]=executionId
                if len(self.commandRuns)>1024:self.commandRuns.pop(next(iter(self.commandRuns)))
                pkg=currentPackageCall()
                if pkg is not None and pkg["phase"] in ("run","draining"):
                    pkg["commands"]+=1;self.commandPackages[trace]=pkg
            else:executionId=self.commandRuns.get(trace)
            pkg=self.commandPackages.get(trace)
            if event["kind"]=="command_end":
                if pkg is not None and event.get("status")=="error":pkg["errors"].append(dict(method=str(event["method"]),error=event.get("error")))
                self.commandPackages.pop(trace,None)
        elif event["kind"]=="backend_diagnostic":executionId=None
        self.send(dict(diagnostic=event,**({"executionId":executionId} if executionId is not None else {})))

    def emit(self,block,mergeText=False):
        run=_runs.get()
        if run is None or not run["active"]:return
        run["output"].append(block,mergeText)
        self.send(dict(output=block,mergeText=mergeText,executionId=run["id"]))

    def flushObservations(self,run):
        def emit(text,observation):
            self.emit(dict(type="text",text=text))
            if observation["state"].get("screenshot") is not None:run["imageContexts"].set(observation["state"]["screenshot"],True)
        def emitImage(png):
            if not any(image is png for image in run["images"]):self.printImage(png)
        run["observations"].flush(emit,emitImage)

    def checkPresentation(self,result,executionId):
        run=_runs.get()
        if run is None or not run["active"] or run["phase"]!="code" or run["id"]!=executionId:return
        try:
            self.flushObservations(run)
            self.emit(dict(type="text",text=formatCheck(result)))
            run["checkResults"].set(result,True)
        except Exception as error:run.setdefault("checkPresentationError",error)

    def publication(self,event,owner):
        run=_runs.get()
        if run is None or not run["active"] or run["phase"]!="code" or run["id"]!=owner["executionId"] or run.get("learningTaskId")!=owner["taskId"]:return
        if event["kind"]=="published":run["publication"]=dict(event["publication"])
        elif run.get("publication",{}).get("app")==event["app"]:run.pop("publication",None)

    def action(self,receipt):
        run=_runs.get()
        if run is None or not run["active"]:return
        recordLearning(dict(kind="action",receipt=receipt))
        if isCapturingCheck():return
        pkg=currentPackageCall()
        if pkg is not None and pkg["phase"] in ("run","draining"):
            pkg["actions"]+=1;pkg["lastAction"]=receipt;pkg["receipts"].append(receipt);return
        self.flushObservations(run)
        self.emit(dict(type="text",text=run["actions"].render(receipt)))

    def textPage(self,source,page):
        run=_runs.get()
        if run is None or not run["active"] or isCapturingCheck():return
        pkg=currentPackageCall()
        if pkg is not None and pkg["phase"] in ("run","draining"):return
        self.flushObservations(run)
        self.emit(dict(type="text",text=textSourceLabel(source)),True)
        run["textPages"].set(page,True)

    def observation(self,event):
        run=_runs.get()
        if event["kind"] in ("invalidate","discard-pending"):
            if event["kind"]=="invalidate":self.presenter.invalidate(event["source"])
            if run is not None:
                if event["kind"]=="invalidate":run["observations"].invalidate(event["source"])
                else:run["observations"].discardPending(event["source"])
            for pkg in self.activePackages.values():
                if pkg.get("last",{}).get("observation",{}).get("source") is event["source"]:pkg["last"]["invalidated"]=True
            return
        if run is None or not run["active"]:return
        observation=event["observation"]
        if not isCapturingCheck():
            pkg=currentPackageCall()
            if pkg is not None and pkg["phase"] in ("run","draining"):
                pkg["observations"]+=1;pkg["last"]=dict(observation=observation,command=pkg["commands"],invalidated=False)
            else:run["observations"].observe(observation)
        data=dict(kind="observation",scope=observation["scope"],view=observation["view"],text=observation["state"]["text"])
        if "observed" in observation:data["observed"]=observation["observed"]
        recordLearning(data)
        self.send(dict(diagnostic=dict(**data,elementRefs=list(observation["refs"])),executionId=run["id"]))

    def packageEvent(self,kind,pkg,**fields):
        event=dict(kind=kind,id=pkg["id"],entry=pkg["entry"],method=pkg["method"],**fields)
        recordLearning(event)
        run=_runs.get()
        self.send(dict(diagnostic=event,**({"executionId":run["id"]} if run is not None else {})))

    def finishPackage(self,pkg,outcome):
        self.activePackages.pop(pkg["id"],None)
        run=_runs.get()
        if run is None or not run["active"]:return
        self.packageEvent("package_call_end",pkg,returned=outcome["returned"],commands=pkg["commands"],observations=pkg["observations"],actions=pkg["actions"],errors=pkg["errors"],
                          elapsedMs=int(time.time()*1000)-pkg["started"],**({"error":str(outcome.get("error"))} if not outcome["returned"] else {}))
        if isCapturingCheck():return
        self.emit(dict(type="text",text=f"Package {pkg['method']} [{'returned' if outcome['returned'] else 'threw'}]: {pkg['commands']} native commands, {pkg['observations']} observations, {pkg['actions']} action receipts, {len(pkg['errors'])} command errors. Function return is not independent proof of task completion. Internal observations/actions remain in this execution's log."))
        if pkg["receipts"]:self.emit(dict(type="text",text="\n".join(run["actions"].render(receipt) for receipt in pkg["receipts"])))
        if pkg["errors"]:self.emit(dict(type="text",text="\n".join(f"Package command error ({f['method']}): {f['error']}" for f in pkg["errors"])))
        last=pkg.get("last")
        if last is None:
            self.emit(dict(type="text",text="Package supplied no final UI observation. Read the relevant app to verify."));return
        if last["invalidated"] or last["command"]!=pkg["commands"] or pkg["pending"]:
            self.emit(dict(type="text",text="Earlier package observation (later commands or invalidation followed; not current state):\n"+last["observation"]["state"]["text"]));return
        self.emit(dict(type="text",text="Last package observation (other internal views are in the log):"))
        run["observations"].observe(last["observation"]);self.flushObservations(run)

    def print(self,*values,sep=" ",end="\n",file=None,flush=False):
        run=_runs.get()
        if run is None or not run["active"]:return
        if file is not None and file not in (sys.stdout,sys.stderr):
            _originalPrint(*values,sep=sep,end=end,file=file,flush=flush);return
        if len(values)==1:
            if run["checkResults"].has(values[0]):return
            receipt=resultReceipt(values[0])
            if receipt is not None and run["actions"].has(receipt):return
            if receipt is not None:
                self.emit(dict(type="text",text=f"Earlier action [{receipt['status']}]: {receipt['target']}. {receipt['detail']}"));return
            if run["textPages"].has(values[0]):values=(dict(values[0]),)
        text=(" " if sep is None else sep).join(value if isinstance(value,str) else pprint.pformat(value,depth=6,sort_dicts=False) for value in values)
        if end not in (None,"\n"):text+=end
        self.emit(dict(type="text",text=text),True)
        observed=run["observations"].printed(text)
        if observed is not None and observed["state"].get("screenshot") is not None:run["imageContexts"].set(observed["state"]["screenshot"],True)

    def printImage(self,png):
        if not isinstance(png,(bytes,bytearray,memoryview)):raise TypeError("printImage() expects PNG bytes, such as state.screenshot.")
        data=base64.b64encode(png).decode("ascii")
        decodePNG(data)
        run=_runs.get()
        self.emit(dict(type="text",text=imageLabel(png,run is not None and run["imageContexts"].has(png))))
        self.emit(dict(type="image",mimeType="image/png",data=data))
        if run is not None and run["active"]:
            run["images"].append(png)
            self.send(dict(diagnostic=dict(kind="image_output",capture=imageMetadata(png)),executionId=run["id"]))

    async def finish(self,error=None):
        run=_runs.get()
        if run is None or not run["active"] or run["phase"]!="code":return
        run["phase"]="draining"
        if run["pending"]:
            if error is None:error=RuntimeError("Computer operations were not awaited. Pending operations finished before this result; inspect their receipts and observations before continuing.")
            await asyncio.gather(*list(run["pending"]),return_exceptions=True)
        if error is None:error=run.get("checkPresentationError")
        try:self.flushObservations(run)
        except Exception as failure:
            if error is None:error=failure
        for png in run["images"]:imageDelivered(png)
        run["active"]=False;run["phase"]="finished"
        result=dict(content=run["output"].content.copy())
        if run.get("publication") is not None:result["_meta"]={learningKey:dict(taskId=run["learningTaskId"],publication=run["publication"])}
        message=None
        if error is not None:
            message=(type(error).__name__+": "+str(error))[:4096]
            result["content"].append(dict(type="text",text=message+"\nEarlier statements may have executed. Observe before retrying an action."));result["isError"]=True
        elif not result["content"]:
            result["content"].append(dict(type="text",text="Completed (no output). Reads return observations automatically; use print() for other values."))
        recordLearning(dict(kind="execution_end",isError=result.get("isError") is True,**({"error":message} if message is not None else {})))
        self.send(result)
        return result

    def namespace(self,compile=False):
        computer=self.computerFactory();self.computers.append(computer)
        visible=inspectionComputer(computer) if compile else computer
        def start(pkg):
            run=_runs.get()
            if run is None or not run["active"] or run["phase"]!="code":raise RuntimeError("Invoke packages only within an active tool call.")
            self.activePackages[pkg["id"]]=pkg;self.packageEvent("package_call_start",pkg)
            if not isCapturingCheck():
                self.flushObservations(run)
                self.emit(dict(type="text",text=f"Package {pkg['method']}: running; intermediate reads/actions are recorded in the execution log. Effects are not rolled back on failure."))
        namespace=dict(__name__="__computer_use__",__builtins__=builtins,computer=visible,print=self.print,printImage=self.printImage,
                       decisionModule="computer_use.experimental.decision")
        if self.experienceEnabled:
            root=resolveLibraryRoot()
            namespace.update(libraryRoot=root,loadPackage=packageLoader(visible,root,dict(start=start,finish=self.finishPackage)),learningModule="computer_use.library.learning")
        return namespace

    async def execute(self,message):
        code,id_,history=message["code"],message["executionId"],message["history"]
        learning=message.get("learning")
        if self.experienceEnabled:beginLearning(resolveLibraryRoot(),learning,id_,code,lambda:computerHost(self.computerFactory))
        recordStructuredObservations(learning is not None)
        if history["resetText"]:self.presenter.invalidate()
        if history["resetImages"]:resetImageDelivery()
        run=Record(id=id_,active=True,phase="code",output=OutputBuffer(),observations=ObservationOutput(self.presenter,history["incremental"]),images=[],pending=set(),
                   actions=ActionPresentation(),textPages=WeakIdentityMap(),imageContexts=WeakIdentityMap(),checkResults=WeakIdentityMap(),
                   **(dict(learningTaskId=learning["id"]) if learning is not None and learning["phase"]=="learn" else {}))
        token=_runs.set(run)
        try:
            try:
                namespace=self.taskNamespace
                if learning is not None and learning["phase"]=="learn":
                    if self.compileNamespace is None or self.compileNamespace[0]!=learning["id"]:self.compileNamespace=(learning["id"],self.namespace(True))
                    namespace=self.compileNamespace[1]
                compiled=compile(code+"\n","computer-use-agent.py","exec",flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,dont_inherit=True)
                evaluated=eval(compiled,namespace,namespace)
                if inspect.isawaitable(evaluated):await evaluated
            except BaseException as error:return await self.finish(error)
            return await self.finish()
        finally:_runs.reset(token)

    async def close(self):
        for computer in self.computers:
            transport=computer._controller.transport
            if hasattr(transport,"close"):transport.close()
        for computer in self.computers:
            transport=computer._controller.transport
            if hasattr(transport,"waitClosed"):await transport.waitClosed()


_originalPrint = builtins.print


class ConsoleWriter(io.TextIOBase):
    """Discard raw process output; only the explicit print hook emits content."""
    def __init__(self,runtime):self.runtime=runtime
    def write(self,text):
        if not isinstance(text,str):raise TypeError("write() argument must be str")
        return len(text)
    def flush(self):pass


async def main():
    port=os.environ.pop("COMPUTER_USE_WORKER_PORT",None)
    if port is not None:
        token=os.environ.pop("COMPUTER_USE_WORKER_TOKEN","")
        if len(token)!=64 or any(char not in "0123456789abcdef" for char in token):
            raise RuntimeError("Invalid internal worker authentication.")
        reader,writer=await asyncio.open_connection("127.0.0.1",int(port),limit=10*1024*1024)
        writer.write((token+"\t"+str(os.getpid())+"\n").encode("ascii"));await writer.drain()
    else:
        ipc=socket.socket(fileno=3);ipc.setblocking(False)
        reader,writer=await asyncio.open_connection(sock=ipc,limit=10*1024*1024)
    def send(message):writer.write((dumps(message)+"\n").encode("utf-8"))
    runtime=Runtime(send)
    sys.stdout=sys.stderr=ConsoleWriter(runtime)
    builtins.print=runtime.print
    async def stop():
        await runtime.close();writer.close()
    loop=asyncio.get_running_loop()
    try:loop.add_signal_handler(signal.SIGTERM,lambda:asyncio.create_task(stop()))
    except (NotImplementedError,RuntimeError):pass
    current=None
    try:
        import json
        while line:=await reader.readline():
            if current is not None and not current.done():raise RuntimeError("Worker received concurrent executions")
            current=asyncio.create_task(runtime.execute(jsonLoads(line)))
        if current is not None and not current.done():current.cancel()
    finally:
        await runtime.close();writer.close();await writer.wait_closed()


if __name__=="__main__":asyncio.run(main())

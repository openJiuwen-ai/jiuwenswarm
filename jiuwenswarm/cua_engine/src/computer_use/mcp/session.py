"""Supervise one persistent Python worker and its inherited native lifetimes."""
from computer_use._json import loads as jsonLoads
import asyncio
import json
import os
import socket
import sys
from .._json import dumps
from ..client.execution import eager
from ..client.preview import isPreviewEvent
from ..library import resolveLibraryRoot,fileURLToPath
from .history import HistoryGate,historyKey
from .journal import Journal
from .output import OutputBuffer,isOutput

resetNotice="Execution session cleared. Recreate App variables and read again. Actions may already have executed; do not replay them automatically."


def executionError(message):return dict(content=[dict(type="text",text=message)],isError=True)


class ExecutionSession:
    def __init__(self,timeoutMs=45000,onPreview=None,demoForeground=False):
        self.demoForeground,self._experience=demoForeground,True
        self.timeoutMs,self.onPreview=timeoutMs,onPreview
        self._child=self._pending=self._retiring=self._resetting=self._exited=None
        self._writer=self._reader=None
        self._closed=self._starting=False
        self._startTask=None
        self._generation=0
        self._nextId=1
        self._journal,self._history=Journal(),HistoryGate()
        self._libraryRoot=None
        self._tasks=set()

    def _track(self,coroutine):
        task=asyncio.create_task(coroutine)
        self._tasks.add(task);task.add_done_callback(self._tasks.discard)
        return task

    @eager
    async def execute(self,code,signal=None,history=None,learning=None,title=None,onProgress=None,experience=True):
        if self._closed:return executionError("Execution session is closed.")
        if self._resetting is not None:
            result=await asyncio.shield(self._resetting)
            return result if result.get("isError") else await self.execute(code,signal,history,learning,title,onProgress,experience)
        if self._retiring is not None:return executionError("Native cleanup is still pending. Call computer_use_reset before continuing; no code was executed.")
        if self._pending is not None or self._starting:return executionError("Another call is running; await it before starting the next.")
        if signal is not None and signal.is_set():return executionError("Call cancelled before execution.")
        if not experience and learning is not None:return executionError("Experience is disabled; learning metadata was refused. No code was executed.")
        if self._child is not None and self._experience != experience:
            reset=await self.reset("Experience mode changed.")
            if reset.get("isError"):return reset
            result=await self.execute(code,signal,history,learning,title,onProgress,experience)
            result["content"].insert(0,dict(type="text",text="Experience mode changed; previous Python variables, imports and observations were cleared. Recreate handles and read again; completed actions remain."))
            return result
        self._experience=experience
        self._starting=True
        generation=self._generation
        starting=self._track(self._start());self._startTask=starting
        def started(_):
            if self._startTask is starting:self._startTask=None;self._starting=False
        starting.add_done_callback(started)
        try:await asyncio.shield(starting)
        except asyncio.CancelledError:
            await asyncio.shield(self.reset("Call cancelled."));raise
        except Exception as error:return executionError(str(error))
        if generation!=self._generation or self._closed:return executionError("Execution was reset before code was sent. No code was executed; recreate variables before continuing.")
        if signal is not None and signal.is_set():return executionError("Call cancelled before execution.")
        id_=self._nextId;self._nextId+=1
        policy=self._history.begin(history)
        self._journal.record(dict(kind="execution_start",executionId=id_,title=title,code=code,history=policy,learning=learning,experience=experience))
        loop=asyncio.get_running_loop()
        future=loop.create_future()
        pending=dict(id=id_,title=title,startedAt=loop.time(),output=OutputBuffer(),future=future,timer=None,cancel=None)
        def stalled():self.reset(f"Execution exceeded {self.timeoutMs} ms without native command progress.")
        def progress():
            if pending["timer"] is not None:pending["timer"].cancel()
            pending["timer"]=loop.call_later(self.timeoutMs/1000,stalled)
            try:
                if onProgress is not None:
                    result=onProgress()
                    if hasattr(result,"__await__"):self._track(result)
            except Exception:pass
        def cleanup():
            if pending["timer"] is not None:pending["timer"].cancel()
            if pending["cancel"] is not None:pending["cancel"].cancel()
        pending.update(progress=progress,cleanup=cleanup)
        pending["timer"]=loop.call_later(self.timeoutMs/1000,stalled)
        self._pending=pending
        if signal is not None:
            async def cancel():
                await signal.wait();self.reset("Call cancelled.")
            pending["cancel"]=self._track(cancel())
        try:
            self._writer.write((dumps(dict(code=code,executionId=id_,history=policy,**({"learning":learning} if learning is not None else {})))+"\n").encode("utf-8"))
            await self._writer.drain()
        except Exception as error:self.reset(f"Cannot send code: {error}")
        try:return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(self.reset("Call cancelled."));raise

    async def _start(self):
        if self._child is not None:return
        if self._experience:
            if self._libraryRoot is None:self._libraryRoot=resolveLibraryRoot()
            self._journal.record(dict(kind="library_root",url=self._libraryRoot))
        env=dict(os.environ,COMPUTER_USE_EXPERIENCE="1" if self._experience else "0",COMPUTER_USE_REMOTE_PREVIEW="1" if self.onPreview is not None else "",COMPUTER_USE_DEMO_FOREGROUND="1" if self.demoForeground else "0",COMPUTER_USE_LIFETIME_FD="4")
        if self._experience:env["COMPUTER_USE_PACKAGES_DIR"]=str(fileURLToPath(self._libraryRoot))
        if os.name=="nt":
            from .windows_worker import startWindowsWorker
            child,self._reader,self._writer,job,listener=await startWindowsWorker(env)
            self._child=child
            async def exited():
                try:
                    await child.wait()
                    await job.waitEmpty()
                finally:
                    job.close()
                    await listener.wait_closed()
            self._exited=self._track(exited())
            self._track(self._receive(child,self._reader))
            return
        parent,childSocket=socket.socketpair()
        lifeRead,lifeWrite=os.pipe()
        try:
            child=await asyncio.create_subprocess_exec(sys.executable,"-m","computer_use.driver_exec",str(childSocket.fileno()),str(lifeWrite),sys.executable,"-m","computer_use.mcp.runtime",
                stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(childSocket.fileno(),lifeWrite),env=env)
        except BaseException:
            parent.close();os.close(lifeRead);raise
        finally:childSocket.close();os.close(lifeWrite)
        self._child=child
        parent.setblocking(False)
        self._reader,self._writer=await asyncio.open_connection(sock=parent,limit=12*1024*1024)
        lifetime=asyncio.StreamReader()
        transport,_=await asyncio.get_running_loop().connect_read_pipe(lambda:asyncio.StreamReaderProtocol(lifetime),os.fdopen(lifeRead,"rb",buffering=0))
        async def exited():
            try:
                await child.wait()
                while await lifetime.read(65536):pass
            finally:transport.close()
        self._exited=self._track(exited())
        self._track(self._receive(child,self._reader))

    async def _receive(self,child,reader):
        try:
            while line:=await reader.readline():
                if self._child is not child:continue
                message=jsonLoads(line)
                if not isinstance(message,dict):raise ValueError("Execution process returned invalid output.")
                if "preview" in message:
                    if isPreviewEvent(message["preview"]):
                        try:
                            if self.onPreview is not None:self.onPreview(message["preview"])
                        except Exception:pass
                elif "diagnostic" in message:
                    event=message["diagnostic"]
                    if isinstance(event,dict):
                        self._journal.record({**event,"executionId":message.get("executionId")})
                        if self._pending is not None and message.get("executionId")==self._pending["id"] and event.get("kind") in ("command_start","command_end"):self._pending["progress"]()
                elif "output" in message:
                    pending=self._pending
                    if pending is None or message.get("executionId")!=pending["id"]:continue
                    if not isOutput(message["output"]):raise ValueError("Execution process returned invalid output.")
                    block=message["output"]
                    self._journal.record(dict(kind="output",executionId=pending["id"],content=dict(type="image",mimeType=block["mimeType"]) if block["type"]=="image" else block))
                    pending["output"].append(block,message.get("mergeText") is True)
                else:
                    if not isinstance(message.get("content"),list) or any(not isOutput(block) for block in message["content"]):raise ValueError("Execution process returned invalid output.")
                    self._finish(message)
            if self._child is child:self.reset(f"Execution process exited ({child.returncode}).")
        except Exception as error:
            if self._child is child:self.reset(f"Execution process failed: {error}")

    def _finish(self,result):
        pending,self._pending=self._pending,None
        if pending is None:return
        duration=(asyncio.get_running_loop().time()-pending["startedAt"])*1000
        self._journal.record(dict(kind="execution_end",executionId=pending["id"],durationMs=duration,isError=result.get("isError",False),
            content=[dict(type="image",mimeType=block["mimeType"],bytes=len(block["data"])*3//4) if block["type"]=="image" else block for block in result["content"]]))
        result["_meta"]={**result.get("_meta",{}),"computer-use":{**self._journal.metadata(),"executionId":pending["id"],"durationMs":duration,
                                **({"title":pending["title"]} if pending["title"] else {})},historyKey:self._history.complete()}
        pending["cleanup"]()
        if not pending["future"].done():pending["future"].set_result(result)

    def reset(self,reason="Execution reset requested."):
        if self._resetting is not None:return self._resetting
        self._generation+=1
        starting=self._startTask
        child,self._child=self._child,None
        if child is not None and child.returncode is None:
            try:child.kill()
            except ProcessLookupError:pass
        earlier=self._retiring if self._retiring is not None else self._exited
        self._exited=None
        if self._writer is not None:self._writer.close();self._writer=None
        if self._pending is not None:self._pending["cleanup"]()
        async def retire():
            if starting is not None:
                try:await asyncio.shield(starting)
                except Exception:pass
            # Process creation yields in Python. A reset during that yield must
            # retire the newly created worker before admitting any later call.
            late,self._child=self._child,None
            if late is not None and late.returncode is None:
                try:late.kill()
                except ProcessLookupError:pass
            if self._writer is not None:self._writer.close();self._writer=None
            exited,self._exited=self._exited,None
            if earlier is not None:await asyncio.shield(earlier)
            if exited is not None and exited is not earlier:await asyncio.shield(exited)
        retired=self._track(retire());self._retiring=retired
        async def reset():
            failure=None
            try:
                if retired is not None:await asyncio.wait_for(asyncio.shield(retired),5)
            except TimeoutError:
                failure="Native cleanup is still pending; capture stop is unconfirmed. Actions may already have executed; do not replay them. Retry computer_use_reset before continuing."
            except Exception as error:failure=str(error)
            if retired is None or retired.done():
                if self._retiring is retired:self._retiring=None
            elif retired is not None:
                def clear(_):
                    if self._retiring is retired:self._retiring=None
                retired.add_done_callback(clear)
            result=executionError(reason+"\n"+(failure if failure is not None else resetNotice))
            result["content"]=(self._pending["output"].content.copy() if self._pending is not None else [])+result["content"]
            self._journal.record(dict(kind="execution_reset",reason=reason,complete=failure is None))
            self._finish(result);self._history=HistoryGate()
            if failure is not None:return executionError(failure)
            try:
                if self.onPreview is not None:self.onPreview(dict(kind="reset"))
            except Exception:pass
            return dict(content=[dict(type="text",text="Execution session reset. Python variables, App/Element handles and observations cleared; session capture streams stopped and preview reset requested. Apps, documents, files and completed actions are unchanged. The next computer_use call starts a fresh environment.")])
        task=asyncio.create_task(reset());self._resetting=task
        def clear(_):
            if self._resetting is task:self._resetting=None
        task.add_done_callback(clear)
        return task

    def close(self):
        self._closed=True
        return self.reset("MCP connection closed.")

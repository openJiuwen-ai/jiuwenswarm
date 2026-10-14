from contextvars import ContextVar
import os
from ..._identity import Record
from ..._json import dumps
from ...protocol.schema import obj,string,enum
from ...mcp.journal import now
from .. import fileURLToPath
from .journal import compactLearningEvent

learningKey="computer-use-learning"
taskSchema=obj(dict(id=string(pattern=r"^[a-zA-Z0-9_-]{1,100}\Z",patternLabel="/^[a-zA-Z0-9_-]{1,100}$/"),
                    phase=enum("task","learn"),goal=string(min=1,max=100000),answer=string(max=100000).optional()))
_checkCapture=ContextVar("computer_use_check_capture",default=False)
_context=None


def isCapturingCheck():return _checkCapture.get() is True


async def withCheckCapture(run):
    token=_checkCapture.set(True)
    try:return await run()
    finally:_checkCapture.reset(token)


def beginLearning(root,task,executionId,code,computerFactory=None):
    global _context
    _context=None
    if task is None:return
    directory=fileURLToPath(root)/".learning"/task["id"]
    _context=Record(root=root,task=task,executionId=executionId,log=str(directory/"events.jsonl"))
    if computerFactory is not None:_context["computerFactory"]=computerFactory
    try:
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        recordLearning(dict(kind="execution_start",code=code,task=task))
    except Exception as error:_context["error"]=str(error)


def recordLearning(event):
    if _context is None or _context.get("error") is not None:return
    try:
        fd=os.open(_context["log"],os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            stream.write(dumps({**compactLearningEvent(event),"taskId":_context["task"]["id"],"phase":_context["task"]["phase"],"executionId":_context["executionId"],"time":now()})+"\n")
    except Exception as error:_context["error"]=str(error)


def learningContext():
    if _context is None or _context["task"]["phase"]!="learn":raise RuntimeError("Experience generation requires host learning metadata with phase=learn. Ordinary tasks only read installed packages.")
    if _context.get("error") is not None:raise RuntimeError(f"Learning evidence could not be recorded: {_context['error']}. Candidate cannot be published.")
    return _context


_checkPresentationHandler = None
_publicationHandler = None


def setCheckPresentationHandler(handler):
    global _checkPresentationHandler
    _checkPresentationHandler = handler


def presentCheck(result, executionId):
    if _checkPresentationHandler is not None: _checkPresentationHandler(result, executionId)


def setPublicationHandler(handler):
    global _publicationHandler
    _publicationHandler = handler


def publicationCompleted(event, context):
    if event["kind"] == "published" and context.get("error"):
        raise RuntimeError(f"Publication files may already exist, but completion evidence could not be recorded: {context['error']}. Inspect before retrying.")
    if _publicationHandler is not None:
        _publicationHandler(event, dict(taskId=context["task"]["id"], executionId=context["executionId"]))

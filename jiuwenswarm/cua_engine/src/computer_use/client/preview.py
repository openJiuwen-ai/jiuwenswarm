from computer_use._json import loads as jsonLoads
import asyncio
import codecs
import json
from ..protocol.primitives import utf16Length

_handler = None


def setPreviewHandler(handler):
    global _handler
    _handler = handler


def getPreviewHandler(): return _handler


def isPreviewEvent(value):
    return isinstance(value, dict) and (value.get("kind") == "reset" or value.get("kind") == "frame"
        and isinstance(value.get("name"), str) and "png" in value and (value["png"] is None or isinstance(value["png"], str)))


def latestPreviewSink(send):
    busy, pending = False, None
    async def drain():
        nonlocal busy, pending
        busy = True
        while pending is not None:
            event, pending = pending, None
            try: await send(event)
            except Exception: pass
        busy = False
    def sink(event):
        nonlocal pending, busy
        pending = event
        if not busy:
            busy = True
            asyncio.create_task(drain())
    return sink


async def readPreviews(stream, receive):
    buffer, discard = "", False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while chunk := await stream.read(65536):
            for index, part in enumerate(decoder.decode(chunk).split("\n")):
                if index:
                    if not discard and buffer:
                        try:
                            event = jsonLoads(buffer)
                            if isPreviewEvent(event): receive(event)
                        except Exception: pass
                    buffer, discard = "", False
                if not discard:
                    buffer += part
                    if utf16Length(buffer) > 8 * 1024 * 1024: buffer, discard = "", True
    except (OSError, ValueError): pass

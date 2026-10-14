from .._json import dumps, utf8


class OutputBuffer:
    def __init__(self):
        self.content=[]
        self._bytes=self._textBytes=0
        self._mergeText=False
    def append(self, block, mergeText=False):
        last=self.content[-1] if self.content else None
        append=mergeText and self._mergeText and last is not None and last["type"]=="text" and block["type"]=="text"
        size=len(utf8(dumps(block["text"] if append else block)))
        if self._bytes+size>8*1024*1024 or not append and len(self.content)>=128:
            raise RuntimeError("Output limit reached (8 MiB / 128 blocks). Emit less text or fewer screenshots.")
        textBytes=len(utf8(block["text"]))+(1 if append else 0) if block["type"]=="text" else 0
        if self._textBytes+textBytes>256*1024:
            raise RuntimeError("Output limit reached (256 KiB UTF-8 text per call). This text block was not emitted; earlier output and effects remain. Inspect a smaller part of the value or evidence file; for UI use a relevant subtree, menu or readText page. Do not repeat preceding actions just to print their result.")
        self._bytes+=size;self._textBytes+=textBytes
        if append:last["text"]+="\n"+block["text"]
        else:self.content.append(block)
        self._mergeText=mergeText


def isOutput(value):
    return isinstance(value,dict) and (value.get("type")=="text" and isinstance(value.get("text"),str)
        or value.get("type")=="image" and value.get("mimeType")=="image/png" and isinstance(value.get("data"),str))

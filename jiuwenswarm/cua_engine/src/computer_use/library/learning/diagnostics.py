from ...protocol.primitives import finite, utf16Length
from ._util import clone, slice16

textLimit = 12000


def lastObservation(calls):
    for index in range(len(calls)-1, -1, -1):
        call = calls[index]; value = call.get("result")
        if call.get("method") not in ("read", "readText") or "error" in call or not isinstance(value, dict) or not isinstance(value.get("text"), str): continue
        result = dict(text=slice16(value["text"], 0, textLimit))
        if call["method"] == "readText":
            for key in ("offset", "total", "next"):
                if finite(value.get(key)): result[key] = value[key]
            if "next" in value and value["next"] is None: result["next"] = None
        return dict(callIndex=index, phase=call["phase"], method=call["method"], path=clone(call["path"]), args=clone(call["args"]), result=result,
                    textChars=utf16Length(value["text"]), truncated=utf16Length(value["text"]) > textLimit, laterCalls=len(calls)-index-1)
    return None

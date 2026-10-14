from ..protocol.validation import decodeResult as decodeWireResult
from ..protocol.primitives import frozen
from ..protocol.captures import captureMetadata
from ..observation.formatting import jsonString
from ..observation.observation import renderObservation
from ..observation.desktop import renderDesktop
from ..observation.semantics import composeControls


def decodeResult(method, value):
    decoded = decodeWireResult(method, value)
    if method == "readDesktop":
        text = renderDesktop(decoded["observation"])
        if decoded["screenshot"] is not None:
            text += "".join("\nImage note: " + jsonString(reason)
                for reason in captureMetadata(decoded["screenshot"]).get("stopReasons", ()))
        return {**decoded, "text": text}
    if method == "readApp":
        observation, observed = decoded["observation"], decoded["observed"]
        if observation["root"] is not None:
            observation = {**observation, "root": composeControls(observation["root"], observation["context"].get("inheritedBlocks"))}
        rendered = renderObservation(observation, observed)
        refs = {}
        for ref in rendered["refs"]:
            if ref not in decoded["refs"]:
                raise ValueError("protocol: rendered element has no action binding")
            refs[ref] = decoded["refs"][ref]
        identity = dict(current=observation["current"], windows=observation["windows"])
        if "document" in observation: identity["document"] = observation["document"]
        if "applicationMenu" in observation: identity["focusedWindow"] = observation["applicationMenu"]["focusedWindow"]
        result = {key: decoded[key] for key in ("instance", "appRef", "observed", "scope", "window", "screenshot")}
        result.update(windows=frozen(decoded["windows"]), text=rendered["text"], coverage=rendered["coverage"],
                      contextRefs=rendered["contextRefs"], overview=decoded.get("overview"), refs=refs,
                      roots=rendered["roots"], imageContext=rendered["imageContext"], identity=identity)
        if "catalogScope" in decoded: result["catalogScope"] = decoded["catalogScope"]
        return result
    return decoded

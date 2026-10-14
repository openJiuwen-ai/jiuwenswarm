from computer_use._json import loads as jsonLoads
import json
from ...protocol.primitives import trim


def compactLearningEvent(event):
    if event.get("kind")!="command_end" or not isinstance(event.get("result"),dict) or "roots" not in event["result"]:return event
    return {**event,"result":{key:value for key,value in event["result"].items() if key!="roots"},"observationRecordedSeparately":True}


async def readLearningLog(file):
    with open(file,encoding="utf-8",newline="") as stream:
        for number,line in enumerate(stream,1):
            if not trim(line):continue
            try:event=jsonLoads(line)
            except ValueError:raise ValueError(f"Learning journal has invalid JSON at line {number}; evidence is incomplete.") from None
            if not isinstance(event,dict):raise ValueError(f"Learning journal has an invalid event at line {number}.")
            yield compactLearningEvent(event)

from .formatting import jsonString


def textSourceLabel(source):
    observed = jsonString(source["observed"]["id"]) if source.get("observed") else "unknown"
    target = f"element {source['ref']}" if source.get('ref') is not None else 'query result'
    return f"Captured text: {target}; observation={observed}; snapshot, not a live read."

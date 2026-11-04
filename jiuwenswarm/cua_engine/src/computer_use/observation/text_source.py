from .formatting import json_string


def text_source_label(source):
    observed = json_string(source["observed"]["id"]) if source.get("observed") else "unknown"
    target = f"element {source['ref']}" if source.get('ref') is not None else 'query result'
    return f"Captured text: {target}; observation={observed}; snapshot, not a live read."

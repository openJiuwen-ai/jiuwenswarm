"""Runtime-side managed-task code consumed directly by AgentServer modules.

These modules were previously nested under ``extensions/video_duplex`` although
they are wired straight into ``interface_deep``/``agent_ws_server`` through
imports, not the extension mechanism. The ``RemoteTaskEndpoint`` half of the
checkpoint bridge lives here; the Gateway writer stays in the extension.
"""

"""Gateway-owned copies of AgentServer helpers.

Gateway must not import ``jiuwenswarm.server``.  The modules in this package are
copies of the AgentServer code Gateway still executes in-process (shared-directory
fallback, attachment storage, git watch, hooks).  AgentServer keeps its own
copies; do not import them from here back into ``jiuwenswarm.server``.
"""

# Runtime Worker callback registry ownership

Code sessions initialize in threads with temporary event loops, while the resident Worker handles configuration reloads on its business event loop. Both paths access `Runner.callback_framework`. The SDK registry uses an `asyncio.Lock`: once concurrent registration binds it to an initializer loop, registration or removal on another loop can fail with `is bound to a different event loop`, exhausting `agent.reload_config` retries.

Before constructing the Server, loading extensions or initializing agents, the Worker installs loop dispatch on the current `Runner.callback_framework` instance:

- Asynchronous registration and removal run on the Worker business loop. Initializer loops await their results, and cancellation propagates to the forwarded task.
- Synchronous registration, removal, filters, hooks and queries run on that same loop. Initializer threads wait for the original return value or exception. Decorators use the synchronous registration path.
- Calls already on the business loop execute directly. Event triggering and user callback execution retain their existing SDK semantics.

Initializer threads remain in place. Module loading and expensive initialization still run in those threads; only registry operations move to the business loop. Keep expensive initialization and user callbacks out of registry operations.

Registry ownership lasts for one Worker process. After Server shutdown and SDK cleanup, the Worker closes dispatch, cancels outstanding forwarded tasks, releases waiting threads and rejects late registrations. Startup closes an unpublished registry owner on construction failure; a constructed Server remains attached to the existing Runtime cleanup path.

This fix changes neither Core source, dependency versions, SDK private locks nor Gateway retry policy. Dispatch is installed only at Server Worker startup. It does not provide general SDK support for sharing objects across arbitrary processes or event loops. Each new Worker must use a new process and registry instance; do not rebind the same instance to another loop in one process, or mutate the SDK's exposed registry dictionaries directly from initializer threads.

Chinese documentation: [Runtime Worker 回调注册表的事件循环归属](../zh/Runtime_Worker_Callback_Registry.md).

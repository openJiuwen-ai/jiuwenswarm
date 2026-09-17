import assert from "node:assert/strict";

import { TaskLifecyclePortImpl } from "../dist/core/supervision/task-lifecycle-port.js";
import { CancelError } from "../dist/core/supervision/protocol.js";

// 测试用依赖 mock
class MockDeps {
  constructor() {
    this.cancellableWork = false;
    this.sessionId = "test-session";
    this.connectionAlive = true;
    this.cancelResult = true;
    this.sentInterrupts = [];
    this.interruptResultHandlers = [];
    this.connectionLostHandlers = [];
    this.stopHandlers = [];
  }

  getSnapshot() {
    return {
      cancellableWork: this.cancellableWork,
      sessionId: this.sessionId,
    };
  }

  cancel(opts) {
    return this.cancelResult;
  }

  sendEventOnly(method, params) {
    const id = `req_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    this.sentInterrupts.push({ method, params, id });
    return id;
  }

  onInterruptResult(handler) {
    this.interruptResultHandlers.push(handler);
    return () => {
      this.interruptResultHandlers = this.interruptResultHandlers.filter((h) => h !== handler);
    };
  }

  onConnectionLost(handler) {
    this.connectionLostHandlers.push(handler);
    return () => {
      this.connectionLostHandlers = this.connectionLostHandlers.filter((h) => h !== handler);
    };
  }

  onStop(handler) {
    this.stopHandlers.push(handler);
    return () => {
      this.stopHandlers = this.stopHandlers.filter((h) => h !== handler);
    };
  }

  isConnectionAlive() {
    return this.connectionAlive;
  }

  // 测试辅助方法
  fireInterruptResult(requestId, sessionId, success, message) {
    for (const h of this.interruptResultHandlers) {
      h(requestId, sessionId, success, message);
    }
  }

  fireConnectionLost() {
    for (const h of this.connectionLostHandlers) {
      h();
    }
  }

  fireStop() {
    for (const h of this.stopHandlers) {
      h();
    }
  }
}

// 1. hasServerTask: 无任务时返回 false
{
  const deps = new MockDeps();
  deps.cancellableWork = false;
  const port = new TaskLifecyclePortImpl(deps);
  assert.equal(port.hasServerTask(), false);
}

// 2. hasServerTask: 有任务时返回 true
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  assert.equal(port.hasServerTask(), true);
}

// 3. cancelAndWaitForIdle: 无任务时立即成功，不发送 interrupt
{
  const deps = new MockDeps();
  deps.cancellableWork = false;
  const port = new TaskLifecyclePortImpl(deps);
  await port.cancelAndWaitForIdle();
  assert.equal(deps.sentInterrupts.length, 0);
}

// 4. cancelAndWaitForIdle: 成功 resolve
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 1000 });
  assert.equal(port.hasWaiter(), true);
  const sentId = deps.sentInterrupts[0].id;
  deps.fireInterruptResult(sentId, "test-session", true);
  await promise;  // 应该 resolve
  assert.equal(port.hasWaiter(), false);
}

// 5. cancelAndWaitForIdle: CANCEL_REJECTED
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 1000 });
  const sentId = deps.sentInterrupts[0].id;
  deps.fireInterruptResult(sentId, "test-session", false, "user rejected");
  await assert.rejects(promise, (err) => {
    assert.ok(err instanceof CancelError);
    assert.equal(err.code, "CANCEL_REJECTED");
    assert.equal(err.message, "user rejected");
    return true;
  });
}

// 6. cancelAndWaitForIdle: CANCEL_TIMEOUT
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 50 });
  await assert.rejects(promise, (err) => {
    assert.ok(err instanceof CancelError);
    assert.equal(err.code, "CANCEL_TIMEOUT");
    return true;
  });
}

// 7. cancelAndWaitForIdle: CANCEL_CONNECTION_LOST
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 1000 });
  deps.fireConnectionLost();
  await assert.rejects(promise, (err) => {
    assert.ok(err instanceof CancelError);
    assert.equal(err.code, "CANCEL_CONNECTION_LOST");
    return true;
  });
}

// 8. cancelAndWaitForIdle: CANCEL_STATE_STOPPED
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 1000 });
  deps.fireStop();
  await assert.rejects(promise, (err) => {
    assert.ok(err instanceof CancelError);
    assert.equal(err.code, "CANCEL_STATE_STOPPED");
    return true;
  });
}

// 9. cancelAndWaitForIdle: CANCEL_ALREADY_PENDING
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const firstPromise = port.cancelAndWaitForIdle({ timeoutMs: 100 });  // 第一次，会被超时 reject
  // 第二次应立即 reject ALREADY_PENDING
  await assert.rejects(
    port.cancelAndWaitForIdle({ timeoutMs: 1000 }),
    (err) => {
      assert.ok(err instanceof CancelError);
      assert.equal(err.code, "CANCEL_ALREADY_PENDING");
      return true;
    },
  );
  // 等待第一次 promise 完成（超时），避免 unhandled rejection
  await firstPromise.catch(() => {});
}

// 10. cancelAndWaitForIdle: 连接已断开时立即 reject
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  deps.connectionAlive = false;
  const port = new TaskLifecyclePortImpl(deps);
  await assert.rejects(
    port.cancelAndWaitForIdle({ timeoutMs: 1000 }),
    (err) => {
      assert.ok(err instanceof CancelError);
      assert.equal(err.code, "CANCEL_CONNECTION_LOST");
      return true;
    },
  );
  assert.equal(deps.sentInterrupts.length, 0);  // 未发送 interrupt
}

// 11. 迟到事件不完成旧 waiter：waiter 已清除后，迟到事件不应抛错
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 50 });
  const sentId = deps.sentInterrupts[0].id;
  await assert.rejects(promise, (err) => err.code === "CANCEL_TIMEOUT");  // waiter 已清除
  // 迟到事件不应抛错
  assert.doesNotThrow(() => {
    deps.fireInterruptResult(sentId, "test-session", true);
  });
}

// 12. requestId 不匹配的事件不完成 waiter
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 200 });
  const sentId = deps.sentInterrupts[0].id;
  // 发送不匹配的 requestId
  deps.fireInterruptResult("wrong_id", "test-session", true);
  assert.equal(port.hasWaiter(), true);  // waiter 仍在

  // 发送正确 requestId 才 resolve
  deps.fireInterruptResult(sentId, "test-session", true);
  await promise;  // 应该 resolve
}

// 13. sessionId 不匹配的事件不完成 waiter
{
  const deps = new MockDeps();
  deps.cancellableWork = true;
  const port = new TaskLifecyclePortImpl(deps);
  const promise = port.cancelAndWaitForIdle({ timeoutMs: 200 });
  const sentId = deps.sentInterrupts[0].id;
  // 发送不匹配的 sessionId
  deps.fireInterruptResult(sentId, "wrong-session", true);
  assert.equal(port.hasWaiter(), true);  // waiter 仍在

  // 发送空 sessionId 时匹配（兼容服务端不回显 sessionId 的情况）
  deps.fireInterruptResult(sentId, "", true);
  await promise;  // 应该 resolve
}

console.log("task-lifecycle-port tests passed");

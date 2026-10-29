// 会话素材 RPC 的按 key 串行队列（#5956）。
// sessionAssets store 的每个操作都是"发一个 RPC，用响应里的全量 assets 覆盖本地
// 状态"。多个操作并发时（连续两次上传各触发一次 register、点 x 取消触发 remove、
// 切换会话触发 refresh），服务端按完成顺序而非请求顺序返回响应，一个先发出但后
// 完成的旧快照会把新快照整个覆盖掉——素材面板上刚上传成功的文件就这么"消失"，
// 切一次会话重新 list 又回来了。
// 这里按 key（sessionId）把操作串成链：前一个操作的状态落地之后，下一个操作才
// 发出请求，保证"响应应用顺序 == 请求发出顺序"。空队列时同步启动，保持既有可见
// 时序（发送消息时 session.assets.register 仍先于 chat.send 发出）；单个操作失败
// 不阻塞后续操作。

export interface AssetOpQueue {
  /** task 排队执行；key 相同的 task 严格按入队顺序、前一个落定后才开始。 */
  run(key: string, task: () => Promise<void>): Promise<void>;
}

export function createAssetOpQueue(): AssetOpQueue {
  const tails = new Map<string, Promise<unknown>>();
  return {
    run(key, task) {
      const previous = tails.get(key);
      const next = previous ? previous.then(task) : task();
      // tail 永远 fulfilled：本操作失败不能卡死同 key 的后续操作。
      const tail = next.catch(() => undefined);
      tails.set(key, tail);
      void tail.then(() => {
        if (tails.get(key) === tail) tails.delete(key);
      });
      return next;
    },
  };
}

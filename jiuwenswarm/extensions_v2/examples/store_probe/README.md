# Store Probe / store 调试扩展

此包监听已有 `context.usage.updated`，在 Handler 中调用 `ops.store.get/set/keys/delete`，
保存递增的 counter 并打印实际返回结果。它只使用公开 Ops，不自行选择目录。

## 安装和验证

1. 使用支持 store 的 Core 版本启动 Swarm，打包本目录的 manifest.yaml、extension.py 和 README.md。
   三个文件均位于 ZIP 根目录，使用现有 `/extension install <ZIP 绝对路径>` 命令安装。
2. 执行 `/extension query example.store-probe`，确认管理入口返回成功激活的事实。
3. 完成一轮正常单 Agent 对话，等待用量事件。查看 AgentServer / Runtime Worker 日志：

   ```text
   [store-probe] set package=example.store-probe previous=0 current=1 saved=True
   [store-probe] get=1 keys=['counter', 'scratch'] scratch_saved=True delete=True remaining=['counter'] session=...
   ```

   安装本身不调用 store；收到用量事件后才创建 JSON。已有同事件 Handler 必须继续 next，
   否则排在它后面的插件不会执行。使用 start_services 时，也可查看当前实例 run/worker-logs 下的日志。
4. 默认数据文件在 `extensions.store_dir`（缺省 `~/.jiuwenswarm/extension/store`）下的 `example.store-probe.json`，内容只有 counter。
   多实例或设置 `JIUWENSWARM_DATA_DIR` 时跟随现有用户数据根。可在相同环境查询实际目录：

   ```powershell
   python -c "from jiuwenswarm.extensions_v2.config import resolve_store_dir; print(resolve_store_dir())"
   ```

5. 新建会话、执行 `/extension reload example.store-probe` 或重启服务后再聊天，counter 应继续增长。
6. 执行 `/extension uninstall example.store-probe`，成功后 JSON 删除；后续用量事件不再打印此包日志。
   `.json.lock` 是跨进程互斥文件，可以保留；重新安装再聊天时 counter 从 0 开始。

counter 统计收到的用量事件次数；示例锁只串行化同一进程内的扩展实例，不提供多次 CRUD 的跨进程事务。
saved=False 或异常不能当作已经保存，提交结果不确定时应先读取数据核实。

## English

Package manifest.yaml, extension.py and README.md at the ZIP root, then install it through the existing extension command.
Complete a normal chat round and inspect the Runtime Worker logs for `[store-probe]` and the package JSON for counter.
Earlier handlers of the usage event must forward with next for this extension to run.
New sessions, reload and server restart preserve counter; successful uninstall deletes the JSON.
The host owns the directory policy. The example uses public Ops and does not implement state or a UI.

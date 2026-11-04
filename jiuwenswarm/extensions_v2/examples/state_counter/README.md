# State Counter 调试包

包 ID：`sample.counter`。使用设计 2.5 的三段逻辑：ready 初始化并请求构树，render 读取 state，业务事件仅 set。
计数显示在会话输入框下方，按钮为 `+1`；展示当前数值及 state.version。
点击按钮和每轮回答完成都会给当前会话的同一个 count 加 1；使用现有 `context.usage.updated` 完成通知。

1. 在使用本次 Core 与 Swarm 源码的服务中，通过现有 `/extension install` 入口选择 `state_counter.zip`。
2. 安装成功后，当前会话已挂载的槽位会补发 ui.ready，面板立即出现，无需刷新浏览器。
   新会话首次显示 `Count: 0 (state version 1)`；已有状态显示保留的值。
3. 点击 `+1`，检查数值和 state.version 增加，WebUI revision 随成功下发增加。
   正常发起一轮对话，回答完成后再加 1；输入文字或仅发送问题时不提前计数。
4. 切换新会话检查独立计数；返回原会话、刷新或重启后检查原值与版本保留。
5. 已安装旧版时，将本包 `extension.py` 替换到 `/extension query sample.counter` 返回的安装目录，
   然后执行 `/extension reload sample.counter`；日常调试也可编辑该文件后重载。
   面板由新版重新构树；重载本身不增加 state.version。
6. 卸载后检查面板清除，配置 state_dir 下的 sample.counter 目录删除。

目录在 `resources/config.yaml` 的保留配置域中定义：

```yaml
extensions:
  store_dir: extension/store
  state_dir: extension/state
```

相对目录以宿主用户数据根为基准，绝对目录直接使用。示例只使用 state，因此不会创建 store 文件。
不要在 ui.render 中 set，也不要在构树 Handler 中再次调用 ops.ui.render。
版本冲突返回拒绝结果，下一次操作重新读取；示例不会自动重放一次点击。
完成通知按最近 256 个 `(session_id, request_id)` 去重，范围是当前扩展激活代次；重载后重新建立去重窗口。
完成通知写入失败只记录日志，不改变原有回答完成结果。未挂载面板时也会保存计数，之后 ready 再展示。
同包可维护多个 key，普通 set 只替换目标 key；同一状态文件里各 key 并列，每个 key 保存自己的 value 和 version。

跨 Worker 采用依赖版本轮询，默认周期 250ms；写入成功表示持久化完成，界面更新异步进行。
直接读取其它包的公开 state 允许，直接写入其它包的状态会被拒绝。
测试覆盖正式 assembly、Registry 安装、ready、click、回答完成、其它 key 保留、重载、卸载及独立进程刷新。
真实浏览器操作与部署环境的多 Worker 路由仍需现场调试。

The panel appears in mounted slots immediately after a successful installation. Button clicks and completed
conversation rounds increment the same session counter. Replace an older installed `extension.py` and run
`/extension reload sample.counter` to update it while preserving state. Recent completion notifications are
deduplicated within the current activation; reloading resets this bounded deduplication window.

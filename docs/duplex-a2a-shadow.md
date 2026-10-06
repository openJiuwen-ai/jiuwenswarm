# Native 最简 APPEND / INTERRUPT

增加监工路由和原 SDK 的执行对接，默认关闭。支持 SDK、Jev、MindsHub 和 Cloudflare Clef；在设置 → 智能体 → A2A 监工中选择后端。

```yaml
duplex_router:
  mode: active
  backend: sdk
  policy: model
  model_name: fast-model
```

仓库默认配置将快模型路由的总等待上限设为 `timeout_seconds: 2.0` 秒，可按接口延迟调整。未设置该字段或设置为 `null` 时，才沿用该快模型的 SDK timeout。

| 结果 | 执行 |
| --- | --- |
| APPEND | 原 steer，下一次原有模型调用前注入 |
| INTERRUPT | 原 SDK 安全暂停；保留当前 ctx，作废旧计划并结合新消息重新规划 |
| 失败 / 超时 / 非法结果 | 原 steer |

快模型只返回 `{"action":"APPEND"}` 或 `{"action":"INTERRUPT"}`，状态哈希只用于诊断记录，不再用于拒绝判断。等待期间接纳另一条消息或执行状态变化，不会仅因此把 INTERRUPT 降级为 APPEND。执行端仍检查任务是否运行中、是否已有内部打断，以及消息是否重复。每条消息只判断一次。慢模型的工具列表和提示不增加内容；不生成状态摘要。快模型读取已有任务、计划、执行位置，以及执行适配器提供的最近已完成操作 `last_action`；未提供时该字段为空。其他模式走原 SDK，不运行影子观察工作线程。

消息收发、排序、工具执行与已读确认均沿用原 Jiuwen。路由在原投递调用内等待判断结果；没有另建后台投递或持久接受机制。

代码：`duplex_shadow.py` 保留原安装入口名，负责消息对接与独立快模型调用；`duplex_native.py` 只实现 supervisor 内的打断适配；`common/duplex_router.py` 判断一次并校验响应。

[2026-10-06 整合记录](duplex-integration-20261006.md) · [删减范围与边界](duplex-simplification.md) · [架构及数据流图](diagrams/README.md)

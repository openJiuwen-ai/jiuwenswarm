# session.receive 规约

最近修订：2026-11-02。关联特性：[F_26](../features/F_26_session-receive.md)。本次开发契约，验收状态见[开发计划 EXTV2-24](../designs/extensions-v2-development-plan.md#extv2-24-跨会话接收入队前扩展)。

## 1. 范围与依据

Worker 在现有持久跨会话消息入队前派发 session.receive。对齐固定 Claude Code Mods 2.1.277 的 origin/text/event、改写与 consumed 语义；当前生产者仅为 peer-send-message，不代表全部 Claude 入站渠道已接入。不新增 Ops。

## 2. 数据与归属

SessionReceiveInput 包含 SessionReceiveOrigin、text、可选 SessionReceiveEvent。Origin 的 kind 与 Claude 对齐；Event 包含 source/kind/data/untrusted_keys，对应 Claude untrustedKeys。当前普通跨会话正文的 event 恒为 None，不将看似 JSON 的文本升级为可信事件。逐字段填写表见 F_26 第 3.2 节。

目标 SessionContext、源/目标归属、请求/工具/链路/幂等事实及实际 enqueue callback 由平台保存，禁止用户输入路由事实。

## 3. 不变量

| 编号 | 不变量 |
| --- | --- |
| S14-01 | origin/event 固定，只允许非空白 text 改写，UTF-8 不超过 32768 字节。 |
| S14-02 | receive USER 的 Ops 绑定目标上下文；send USER 绑定源上下文。 |
| S14-03 | SessionReceiveConsumed(consumed) 仅在本事件 CORE 尚未开始、且在 next 前生效；原因去空白后 1～512 字符，无新增队列、历史或模型执行。 |
| S14-04 | CORE 校验私有调用和绑定身份，执行真实 enqueue continuation，成功后返回实际入队 text。 |
| S14-05 | 入队后使用平台保存的真实回执；后置 consumed 或改写返回不能撤销/伪造结果。 |
| S14-06 | 一次调用只入队一次；CORE 失败/取消保存至本次状态，再次 next 重新抛出且不重做，不能改成 consumed；原持久幂等键继续跨请求去重；调用退出后不能用继承状态提交。 |

## 4. 接口与失败

结果为 SessionReceiveResult(text) 或 SessionReceiveConsumed(consumed)，不接受用户自造放行回执。业务入口将消费转换为 accepted=False / SESSION_RECEIVE_CONSUMED / consumed/error。上游 send 拒绝不触发 receive。

非法改写/输出和 USER 异常沿用 Core 故障隔离；不能将抛异常当成消费。平台/存储错误、取消向外传播，遵循原持久消息服务结果语义。

## 5. 生命周期与兼容

不持队列锁执行 USER；原锁内复查源与目标的归属及支持模式，原通知和异步投递继续负责后续处理。没有 Handler 时行为与旧服务相同。卸载撤销 USER，不删除既有消息。普通 Web/cron/relay/独立 Runtime 入口未接入时不触发本事件。

## 6. 关联与验证索引

发送契约见 [S_13](S_13_session-send.md)；用户案例、完整调用链、字段解释和实际验证见 [F_26](../features/F_26_session-receive.md)。

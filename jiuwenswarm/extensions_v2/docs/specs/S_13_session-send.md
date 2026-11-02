# session.send 规约

最近修订：2026-11-02。关联特性：[F_25](../features/F_25_session-send.md)。本次开发契约，验收状态统一见[开发计划 EXTV2-23](../designs/extensions-v2-development-plan.md#extv2-23-跨会话发送前扩展)。

## 1. 范围与依据

Worker 持久跨会话消息服务在源/目标授权后、目标持久队列之前派发 session.send。Claude 固定 2.1.277 仅预留该事件；本规约为本地发送适配，不宣称存在对应 Claude 正式 Schema。不新增 Ops 或工具。

## 2. 数据与归属

SessionSendInput 包含 target_session_id、text、input_mode，具体取值及填写说明见 F_25 第 3.2 节。平台持有源 SessionMessageSource、源 SessionContext、已校验元数据和 enqueue continuation，均不由可改正文决定。

## 3. 不变量

| 编号 | 不变量 |
| --- | --- |
| S13-01 | USER 仅可改写非空 text，UTF-8 不超过 32768 字节；目标及投递模式固定。 |
| S13-02 | 会话归属和支持模式在 Hook 前校验；入队锁内沿用原目标复查。 |
| S13-03 | CORE 使用真实平台调用状态；裸 trigger 无发送权限。 |
| S13-04 | next 前可用 SessionSendDenied(deny) 拒绝，原因去空白后 1～512 字符，无接收、入队或执行。 |
| S13-05 | 正常 CORE 调用目标 receive，成功回执只能来自原持久化服务；不等待目标模型完成。 |
| S13-06 | 重复 next 复用本次真实结果；后置返回不能撤销事实或伪造 accepted。 |

## 4. 接口与失败

平台原 send_message 参数与成功回执保持。规则拦截返回 accepted=False，发送拒绝 code=SESSION_SEND_DENIED，接收消费 code=SESSION_RECEIVE_CONSUMED；均包含实际 target_session_id、error 及 deny/consumed。业务授权、参数、删除、幂等和持久化失败沿用 SessionMessagingError。

USER 异常/非法输入输出沿用 Core 的故障隔离，不等同于业务拒绝；取消继续向外传播。原入队后的取消不撤销持久行。不持队列锁等待 Hook。

## 5. 生命周期与兼容

每次真实发送调用建立私有状态，退出即失效。卸载只撤销 USER；不删除队列或平台 CORE。既有重试键、SQLite Schema、消息状态及后台执行保持；旧队列不回放 Hook。重入发送被明确拒绝。

## 6. 关联与验证索引

接收契约见 [S_14](S_14_session-receive.md)；逐字段、完整链路、用户包与实际验证见 [F_25](../features/F_25_session-send.md)。

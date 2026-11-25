# session.authorize / ops.session.authorize 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_16 |
| 最近修订 | 2026-11-04 |
| 关联特性 | [F_29_session-authorize.md](../features/F_29_session-authorize.md) |

## 1. 范围

本规约限定无参 `ops.session.authorize()` 与同名 `session.authorize` 操作事件。宿主可显式绑定 Anthropic 或 OpenAI 凭据；Ops 输出为 `{handle: str, kind: "bearer" | "api-key"}` 或 None。真实凭据自动采集、OAuth 和 HTTP 消费不在本期。

## 2. 契约

事件输入为空映射；输出严格为 `{value: SessionAuthorization | None}` 或 `{deny: str}`。Ops 解包 value，拒绝映射为 SessionAuthorizationDenied。宿主 `bind` 的 provider 支持 `anthropic` 和 `openai`，忽略首尾空白和大小写；同一会话一次只保留一份凭据绑定。逐字段填写、默认、例子和权限见 F_29 第 3/4 节。既有业务触发绑定 SessionContext 后，其活动受管包 Handler 可调用 Ops；不要求 cwd/model/用量数据。

- **宿主显式绑定（签发前置条件）：** Worker 扩展装配后，宿主须先调用 `get_authorization_service().bind(SessionContext(session_id), provider=..., credential=..., kind=...)`，将可信凭据绑定到当前会话；未绑定时默认 CORE 返回 None。插件不负责绑定，本期没有自动凭据来源。

## 3. 不变量

- SA-01：真实凭据仅留在宿主；公开结果只有随机 handle 和 kind，不在事件/metadata/日志/历史中返回或保存密钥。
- SA-02：会话与原调用包身份由平台绑定；签发要求当前 Task、Ops 和调用活动期校验。包代次取 Runtime.invocation_generation(ops) 的原调用代次，不用当前活动包的新代次替代；热重载后旧调用不能按新代次签发。构建缺少校验时不签发。
- SA-03：事件私有调用帧限定 Task 和活动期。无帧的手工 trigger、不合法的参数和同事件重入拒绝。
- SA-04：USER→可选 BUILTIN→唯一 CORE；CORE 的结果可被隐藏/拒绝，但不能被替换为任意非空授权对象。最终核对宿主记录和绑定/包代次。
- SA-05：每次调用至多签发一个句柄。最终 None、deny、取消或失败撤销本次已签发句柄。
- SA-06：句柄归属原调用包代次、会话和绑定代次；重新绑定、解除绑定、凭据到期、包清理和关闭使相应句柄失效。宿主负责调用 bind/unbind 的真实业务时机，本期不自动接线。
- SA-07：无绑定/不支持 provider/到期/不支持构建返回 None；明确拒绝和错误不伪装成 None。服务关闭拒绝新签发。
- SA-08：句柄默认 300 秒有效，受凭据期限约束，懒清理、仅内存；实例上限 1024，满额明确失败，不静默扩大授权或无限增长。
- SA-09：EventBus、Ops 和 Builder 公共接口不变，不提供通用调用 Hook 排除或 next.origin；本期不实现 HTTP 请求或原模型/登录/MCP 凭据采集。

## 4. 验证入口

测试、独立 ZIP、源码基线、真实执行结果及边界统一由 F_29 第 6 节维护。未执行的真实凭据服务/网络/浏览器不列为通过。

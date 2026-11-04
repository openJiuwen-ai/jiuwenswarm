# Web 浏览器能力与请求绑定

最近修订：2026-11-04。本文维护前端接口；配套 Gateway 浏览器桥接与登录示例分别提交。

## 接入边界

生产单例为 `browserClientCapabilities`，通过受信任的宿主适配器调用 `configure(snapshot)`。快照类型见 `client/types.ts`：

- clientProtocolVersion=1、稳定 gatewayInstanceId、当前 connectionEpoch。
- packages 中的 packageId、activationGeneration 和 requestBindings。安装激活后直接具备浏览器能力，不传输额外权限字段。
- 快照由主 WebSocket 的 client.declarations 事件接入；没有额外 bootstrap 请求，也不在请求失败时启用 Mock。

快照只来自宿主接入层，不从渲染树、事件参数或插件填写的物理存储键加载。无配置时保留原有请求和 WebUI 行为；配置非法时拒绝激活并呈现通用错误，不继续使用旧绑定。
已启用新能力的连接关闭或进入重连后，撤销当前声明并暂停业务，等待适配器注入新 connectionEpoch 的快照；浏览器存储保留，不把旧连接声明继续用于新连接。

## localStorage

下行 `client.operation` 的字段为 `clientProtocolVersion`、`invocation_id`、`connection_epoch`、`package_id`、`activation_generation`、`method`、`params`、`deadline`。

- method 仅允许 `local_storage.set` 与 `local_storage.delete`；params 为逻辑 key 和 set 所需的 value。
- deadline 为 Unix 毫秒时间戳，只约束命令执行时限，与保存值的业务有效期无关。
- 命名空间由浏览器分区、稳定 Gateway ID 和 packageId 决定；每个逻辑 key 独占一条原生 localStorage 记录。
- 首版限制为 key 128 UTF-8 字节、JSON 64 KiB、嵌套深度 8。只接收有限 JSON；写删失败在本地显示，不换存储。
- 没有 get/keys、条目版本、条件写入、全站 clear、执行回执、重放或重试。delete 删除执行时的当前值。
- invocation_id 关联发起操作的原请求，供存储变化后的上下文等待原请求结束；不生成无人消费的逐操作标识。
- `subscribeStorage` 只提供 package_id/key，不含 value、旧值或逐操作执行结果；对应插件事件的服务端路由待接入层联调。

本地存储成功后才更新绑定上下文。原标签页直接更新；其他标签页通过 storage 事件按同一命名空间重新读取。原生事件的 oldValue/newValue 不向插件或日志转发。

## 页面刷新

前端支持下行 client.operation 的 method=page.reload、params={}，当前已安装且激活的客户端包无需单独授权。Gateway 通过 with_client_ops(ops) 提供 ops.client.page.reload()；投递端契约由 Gateway 浏览器桥接规约维护。

命令只刷新收到它的当前页面，保留当前 URL，行为为 window.location.reload()。不清理 localStorage/sessionStorage，不修改其他标签页，不重试，也不发送执行回执。插件需要重新初始化页面时，先下发 localStorage 写删，再显式下发刷新命令；同连接按消息顺序执行，各命令独立处理。

收到命令后完成连接归属、包激活代次、参数和 deadline 检查，通过后立即调用刷新函数，不等待原请求回包。声明初始化、重连、存储写删、绑定值变化和其他标签页 storage 事件均不会自动触发刷新。

## 请求绑定

requestBindings 仅由 type/source/target 构成。source.key 读取本包记录；可选 path 为字面字段数组，省略或 [] 使用整个值。

| type | target | 行为 |
| --- | --- | --- |
| http | location=header、name、可选 prefix | 写入实际 HTTP/HTTPS header，包含 fetch SSE、上传、下载及接入的资源请求 |
| websocket | location=message、path、可选 prefix | 写入每条 WS/WSS 业务消息的 request_context 扩展区 |

未声明类型不携带该绑定参数。声明不决定后端是否鉴权，不提供 query/body、routes、service 或公开例外配置。
request_context 是 WS 消息体中的固定扩展容器，类型使用 Record<string, unknown>。容器内部的业务字段由插件通过 target.path 声明；type/id/method/params 等基础协议字段不能被绑定覆盖。宿主将声明且求值成功的字段写入容器，仅在存在可用 WS 绑定值时生成该容器。
同请求同一来源 key 只读取一次；缺值、错误类型、JSON/读取失败或不能用于 header 的值均省略，原请求继续。外部 URL不携带参数。
注册时拒绝同类型重叠目标、非法 header、协议字段和原型路径覆盖。HTTP 已有同名 header 时拒绝冲突，不覆盖原有凭据提供方。

使用 `gatewayFetch` 显式接入 HTTP 调用，不修改全局 fetch。携带绑定 header 的请求不自动跟随重定向，避免将任意声明字段转发到其他服务；显式 manual 保留原生行为，配置按 RequestInit 优先、Request 次之读取。上下文变化或调用者中止时立即使响应包装流出错，丢弃尚未交付给调用者的缓冲数据。
主 WebClient 和独立 `/ws/git` 客户端使用同一份 WebSocket 绑定声明；仅业务消息携带参数，连接握手不写入绑定值。Gateway 的 /ws/git 尚未接入插件策略，不能据此前端绑定宣称完整鉴权。两条连接均在绑定来源变化时取消旧请求等待，利用已有请求 ID 忽略其迟到响应。业务推送按现有事件结构消费；声明被撤销或配置无效期间暂停业务消息消费。
媒体、Markdown 图片和文件下载通过资源适配器获取 Blob 后展示/下载；iframe 内部网页、HTML 相对资源、Markdown 的直接导航链接及桌面原生打开路径仍需与 Gateway 的独立资源授权协议联调，不能据此宣称所有嵌入资源已支持 header 绑定。

## 上下文与 UI 帧

有效绑定值变化后取消旧请求和流，隔离其迟到结果；上下文计数仅保存在前端运行态。业务视图、已有缓存和内存状态不会因此被重置，插件可显式请求整页刷新。
启用客户端能力接入后，UI 帧中的 payload.activation_generation 对应已登记 package_id 的激活代次；前端校验包激活代次、目标、实例与 revision 后消费帧。
独立推送的请求及登录状态归属由 Gateway 接入协议约定。跨上下文的旧推送隔离、HTTP 请求与独立 UI 帧的关联，需结合 Gateway 的请求、订阅和实例生命周期联调确认。前端本地隔离的范围为在途请求和流。
invocation_id 需关联原 WebClient 请求 ID；若后端使用独立调用 ID，宿主适配器通过 setInvocationWaiter 明确关联。
整页刷新会重新启动 JavaScript 运行环境并重建 WebSocket，随后走现有初始化读取和插件视图渲染流程。宿主不逐个清理业务 Store，不识别身份字段。entry 的 render/clear 与 footer 的挂载继续遵循各自契约。

请求上下文就绪后，重新读取初始化配置、模型目录及当前会话尚未完成的元数据和历史，保留已经加载的业务 Store。
同一会话的元数据请求只在同一上下文内合并；旧上下文的成功或失败均不更新当前会话和初始化完成状态。
会话元数据接口只有明确返回 `NOT_FOUND` 才能判定会话不存在，其他失败不提供存在性结论。
历史读取订阅由发起读取的 effect 持有，作用域结束后释放。

## English contract summary

A trusted host adapter injects the declaration snapshot. There is no new bootstrap endpoint or built-in identity service. Client operations provide namespaced localStorage set/delete and explicit page.reload, with no version checks or execution receipts. HTTP and WebSocket bindings apply only to their declared transport; missing or unusable source values are omitted. Gateway authentication remains independent.

Each ready request context reruns initialization reads and unfinished metadata/history reads for the current conversation,
while retaining loaded business stores. Metadata requests are deduplicated only within the same context, and late results
from earlier contexts cannot change the current conversation or initialization status. Only an explicit metadata `NOT_FOUND`
response establishes that the conversation is missing. History subscriptions are owned and released by their initiating effect.

request_context is a fixed extension container in the WS message body, represented by Record<string, unknown>. Plugins declare its business keys through target.path. The host creates the container when at least one WebSocket binding produces a usable value and populates it with those declared values.

Context counters belong to frontend runtime state. Context changes cancel pending requests and streams; existing request IDs and local counters prevent consumption of their late results. Business events use the existing event structure. WebUI frames are validated against package activation, target, instance and revision. The Gateway integration contract defines request and login-state attribution for independent pushes. Cross-context push isolation and HTTP-to-UI correlation remain integration work, while local isolation covers pending requests and streams. Loaded business state is retained until an explicit page reload.

Explicit HTTP clients, binary previews, Markdown images and downloads use the shared transport. The main WebClient and the separate /ws/git client share business-message bindings and cancel pending requests when their context changes; neither injects bindings into the connection handshake. Bound requests reject automatic redirects while preserving explicit manual handling, with RequestInit taking precedence over Request. Context or caller cancellation immediately errors the response wrapper and discards queued unread bytes. Active installed client packages directly support storage, request bindings and page.reload. Declarations contain no grant fields, and commands carry no unused operation identifiers. The page.reload command requires empty parameters. It validates connection ownership, package activation, parameters and deadline once, then immediately calls window.location.reload() without waiting for a request reply. Configuration, reconnection and storage or binding changes do not reload the page automatically. Persistent storage and other tabs remain unchanged. Each command executes independently, without transactions, retries or execution receipts. Gateway/SDK integration is delivered in a separate commit. Embedded HTML resources, direct Markdown navigation and native desktop opening still require an independently authorized Gateway resource contract. An explicit reload recreates the JavaScript environment and WebSocket; the host does not reset business stores individually. Entry remains mounted independently, and footer readiness follows initialization reads. Gateway routing and declaration delivery are provided by the companion integration; the Mock login plugin is delivered separately.

## 普通 WebUI 与客户端能力包共存

client.declarations.packages 仅列出参与浏览器能力的包，不是所有 UI 插件的目录。
普通 UI 帧沿用原有目标、组件、实例和 revision 校验；客户端能力包的 UI 帧额外要求匹配声明的 activation_generation。
携带 activation_generation 但找不到有效声明的帧仍拒绝，不将失活的能力包解释成普通包。
图片在绑定资源尚未返回时不挂载 img；资源上下文变化先移除旧图片并释放旧 URL，待新资源就绪后显示。
真正的请求失败继续遵循原有图片及插件声明的 fallback 行为。
请求上下文变化时移除旧 WebUI 贡献；新上下文就绪后，已挂载的非 entry 插槽重新上报 ui.ready。
该流程只恢复插件贡献，不重置业务 Store，也不自动刷新整个页面。

Client declarations enumerate participating capability packages rather than the complete WebUI catalog.
Ordinary frames retain the existing UI validation. Participating frames require their declared activation generation,
and a generation-tagged frame without a matching declaration is rejected.
Images are mounted after their bound resource becomes available; context changes remove old images and revoke their URLs before reloading.
After the new request context is ready, mounted non-entry slots send ui.ready again to restore their contributions.
This does not reset business stores or reload the page automatically.

# 登录入口、凭证和请求处理

用户需要登录、注册、账号菜单或为请求附加凭证时参考。运行域为 `gateway`。
以下片段仅供参考，不包含账号数据库、完整登录页或生产认证方案；原登录示例要求 Swarm >=0.2.10。

## 关键写法

用 `application.entry` 承载登录页面，`application.sidebar.footer` 展示账号入口，Handler 订阅 `gateway.request` 和所需的 `ui.*` 事件。
manifest 的 `client.requestBindings` 负责从客户端存储附加凭证。例如 HTTP 绑定片段：

```json
{
  "type": "http",
  "source": {"key": "login", "path": ["accessToken"]},
  "target": {"location": "header", "name": "Authorization", "prefix": "Bearer "}
}
```

WebSocket 使用 `type: "websocket"`，目标为 `location: "message"` 和路径 `request_context.headers.Authorization`。
两种绑定都要按真实请求链核对，不能只验证 HTTP 就声称 WebSocket 登录态也已生效。

登录成功后的客户端操作片段；`token` 和 `username` 必须来自实际认证结果：

```python
from agent_gateway.extensions_v2.webui.ops import with_client_ops

client_ops = with_client_ops(ops)
await client_ops.client.local_storage.set("login", {"accessToken": token, "username": username})
await client_ops.client.page.reload()
```

- **业务放行：** 在请求 Handler 中校验凭证；通过时按契约继续默认处理，拒绝时返回合法的 `GatewayRequestOutput`，不调用默认业务。
- **登录自身：** 按真实入口放行登录交互和必要资源，避免未登录时连登录页也无法操作。
- **退出与隔离：** 撤销服务端 token，并删除对应客户端存储。自有账号体系不自动成为平台租户隔离，不能据此承诺账号间数据隔离。
- **权限边界：** 不读取宿主内部认证密钥；页面嵌入和浏览器权限另按 [前端开发说明](../frontend-development.md) 核对。

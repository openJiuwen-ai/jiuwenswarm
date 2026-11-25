# 登录入口、凭证和请求处理

用户需要登录、注册、账号菜单或为请求附加凭证时参考。运行域为 `gateway`。
以下片段仅供参考，不包含账号数据库、完整登录页或生产认证方案；原登录示例要求 Swarm >=0.2.10。

## 1. 先把页面、请求和凭证对应起来

| 需求 | 声明、事件或 Ops | 作用 |
| --- | --- | --- |
| 登录页面 | `application.entry`、`ui.ready`、`ops.ui.render` | 页面就绪后显示登录表单 |
| 账号入口 | `application.sidebar.footer`、`ui.ready` | 已登录时显示账号信息或退出按钮 |
| 登录、注册和退出 | `ui.click`，按实际控件补 `ui.change` 或 `ui.select` | 接收声明过的 action，校验输入后调用自己的账号服务 |
| 每次请求的认证 | `gateway.request` | 区分登录自身请求与受保护业务，决定继续或拒绝 |
| 写入凭证、重新加载 | `with_client_ops(ops)` 下的客户端 Ops | 将真实认证结果写回当前客户端，再请求页面重新加载 |
| 连接清理 | `gateway.disconnect` | 删除该连接的临时表单、页面和账号缓存 |

在 `manifest.ui.items` 中分别声明入口和账号槽位，各用稳定的 id/instanceKey；结构沿用 [最小示例](../example.md#1-事件与效果)，domain 改为 `gateway`。
UI Handler 只接收本系统插件声明的目标和 action，应用级目标的 session_id 为空，连接由宿主上下文确定。

manifest 的 `client.requestBindings` 负责从当前系统插件的客户端存储取出凭证。下面是 client 段，HTTP 与 WebSocket 都要声明：

```json
{
  "requestBindings": [
    {
      "type": "http",
      "source": {"key": "login", "path": ["accessToken"]},
      "target": {"location": "header", "name": "Authorization", "prefix": "Bearer "}
    },
    {
      "type": "websocket",
      "source": {"key": "login", "path": ["accessToken"]},
      "target": {"location": "message", "path": ["request_context", "headers", "Authorization"], "prefix": "Bearer "}
    }
  ]
}
```

绑定只负责携带凭证，是否有效仍由后端判断。不能只测 HTTP，就认为 WebSocket 也已登录。

## 2. 请求 Handler 怎样放行和拒绝

以下是入口对象中的方法，`register` 将 `gateway.request` 绑定到它。`self.is_login_request` 和 `self.verify_token` 是本系统插件需要实现的业务方法，不是平台 Ops。

```python
from dataclasses import replace
from agent_gateway.extensions_v2.models import GatewayRequestInput, GatewayRequestOutput

async def on_request(self, event, ops, next_handler):
    request = event.event_input
    if not isinstance(request, GatewayRequestInput):
        return await next_handler(event)
    if self.is_login_request(request):
        return replace(await next_handler(event), handled=True)
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if token and await self.verify_token(token):
        return replace(await next_handler(event), handled=True)
    return GatewayRequestOutput(
        handled=True, route="deny",
        error={"code": "AUTH_REQUIRED", "message": "请先登录", "status": 401},
    )
```

- **登录自身请求：** `is_login_request` 按真实协议限定入口、事件、package_id、槽位和 action，只放行登录所需的交互及资源。不能把所有 `webui.dispatch` 都当作登录请求，也不能把所有 HTTP 请求都放行。
- **认证方法：** `verify_token` 检查签名或服务端记录、有效期和撤销状态；涉及同步数据库或网络时避免阻塞异步 Handler。表单校验、密码处理和 token 签发由自己的账号服务完成。
- **默认链：** 放行时调用默认链一次，保留其 route、params、response 等字段；`handled=True` 表示本 Handler 已完成认证判断，不等于强制转发。拒绝时直接返回合法回执，不执行业务默认链。
- **显示登录页：** 初次 `ui.ready` 和未认证请求都可能需要显示入口。沿用同一渲染方法，按连接去重；不要让登录页面自身再次进入受保护业务，形成循环。

## 3. 登录成功和退出怎样生效

在登录 action 校验成功后执行下列片段；`token` 和 `username` 必须来自实际认证结果：

```python
from agent_gateway.extensions_v2.webui.ops import with_client_ops

client_ops = with_client_ops(ops)
await client_ops.client.local_storage.set("login", {"accessToken": token, "username": username})
await client_ops.client.page.reload()
```

这是给当前客户端排队发送操作，不是直接修改服务端 localStorage，也不代表客户端已经执行成功。
重新加载后，请求绑定从 `login.accessToken` 取值；后端认证通过，再清除登录页并渲染账号入口。

退出时先撤销服务端 token，再调用 `client_ops.client.local_storage.delete("login")` 和 `client_ops.client.page.reload()`。
连接断开时清理该连接的临时状态；不要因此删除其他客户端的登录态。

## 4. 验证重点

- 未登录时能看见并操作登录页，受保护请求被拒绝；登录成功后 HTTP 和 WebSocket 都携带并校验凭证。
- 错误密码、过期或撤销 token、客户端写入失败都不会显示为已登录；重复点击不会重复签发或重复执行业务。
- 退出后旧 token 不能继续使用，其他连接的状态符合账号服务约定。自有账号体系不自动成为平台租户隔离，不能据此承诺账号间数据隔离。
- 不读取宿主内部认证密钥；网页嵌入和浏览器权限按 [前端开发说明](../frontend-development.md) 核对。

# 全栈应用插件

Application Plugin 可以同时贡献工作区页面、Gateway RPC 和 WebSocket 路由，与
Agent 的 skills、tools、rails 扩展相互独立。

```mermaid
flowchart LR
    Package[插件目录] --> Loader[ExtensionLoader]
    Loader --> Registry[ExtensionRegistry]
    Registry --> Frontend[工作区页面]
    Registry --> RPC[本地 RPC]
    Registry --> WS[WebSocket 路由]
    Host[Gateway Host] -. 服务注入 .-> Registry
```

## 纯前端插件

预构建 iframe 页面无需 Python：

```text
hello-plugin/
├── extension.yaml
└── frontend/
    └── dist/
        ├── index.html
        └── assets/...
```

```yaml
id: hello-plugin
name: Hello plugin
version: 1.0.0
description: A minimal Jiuwen application
author: Your name
min_jiuwenswarm_version: "0.2.5"
package_type: application
permissions: [camera]
frontend:
  - id: hello-page
    nav_key: app:hello-plugin
    title: Hello
    render_mode: iframe
    entrypoint: index.html
    position: 100
```

宿主会自动创建插件实例。`nav_key`、`render_mode`、`entrypoint` 和 `position` 的默认值
分别是 `app:{plugin_id}`、`iframe`、`index.html` 和 `100`。

将插件放入 `~/.jiuwenswarm/application_plugins/` 或在配置中增加搜索目录，重启 Gateway：

```yaml
extensions:
  extension_dirs: "C:/dev/jiuwen-plugins;D:/team/plugins"
```

可通过以下通用接口检查发现结果和静态资源：

```text
GET /api/application-plugins
GET /api/application-plugins/{plugin_id}/assets/{asset_path}
```

## 带后端的插件

需要 RPC、WebSocket 或 Core Agent 时增加 `extension.py`：

```python
from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    FrontendContribution,
)


class MyPlugin(ApplicationPluginExtension):
    plugin_id = "my-plugin"

    async def initialize(self, config):
        self.logger = config.logger

    async def shutdown(self):
        pass

    def bind_web_channel(self, channel, services):
        async def ping(ws, req_id, params, session_id):
            await channel.send_response(
                ws, req_id, ok=True, payload={"pong": True}
            )

        channel.register_method("plugin.my_plugin.ping", ping, local_only=True)

    def frontend_contributions(self):
        return (
            FrontendContribution(
                id="my-page",
                nav_key="app:my-plugin",
                title="My plugin",
                render_mode="iframe",
                entrypoint="index.html",
            ),
        )


async def register_extensions(registry):
    plugin = MyPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
```

纯后端插件可不覆盖 `frontend_contributions()`，纯前端插件也无需覆盖
`bind_web_channel()`。插件可覆盖 `is_enabled()`，禁用时宿主会隐藏功能入口并拒绝新的运行时
RPC 和 WebSocket 连接。

内置 bundled 插件可以在自己的 `frontend/index.tsx` 中导出设置组件：

```tsx
export const applicationPluginId = 'my-plugin';
export const applicationPluginSettings = MyPluginSettings;
```

核心前端会在 **扩展 → 应用插件** 中发现并挂载该组件，但不解释任何业务配置字段。设置组件应
通过插件自己的 RPC 读写环境变量或私有存储。用于重新启用插件的管理 RPC 可在注册时声明：

```python
channel.register_method(
    "plugin.my_plugin.settings",
    settings_handler,
    local_only=True,
    available_when_disabled=True,
)
```

`available_when_disabled` 只应用于配置和启用状态等管理接口，不应赋给业务运行时接口。

需要 Core Agent 或媒体附件处理时，使用宿主注入服务：

```python
def bind_web_channel(self, channel, services):
    core_agent_client = services.require_agent_client()
    services.normalize_media_attachments(params, session_id)
```

业务插件不应修改 `ReqMethod`、`AgentWebSocketServer` 或 Agent Adapter；插件 RPC 通过
`channel.register_method(...)` 注册，核心通信复用已有协议。

Python 扩展与 iframe 均运行在 Jiuwen 信任边界内，只应安装可信代码。`camera`、
`microphone` 和 `display_capture` 会控制 iframe 的媒体权限，其余权限当前仅为能力声明。

内置 `jiuwenswarm/extensions/video_duplex` 是完整示例，其配置和测试也保留在插件目录内。

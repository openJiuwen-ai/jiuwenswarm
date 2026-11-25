# 同时涉及两个运行域的系统插件

只有需求同时用到 Gateway 和 AgentServer 的事件或 Ops 时，才按本文开发组合包。
一个域已经能完成需求时，沿用单域结构；不要仅因有前端页面就拆成两个域。

## 1. 按职责拆分成员

- **Gateway 成员：** 处理应用入口、连接、请求路由、应用外观或桌面形态等 Gateway 能力。
- **AgentServer 成员：** 处理会话执行、工具、上下文、团队或 AgentServer 的页面贡献。
- **整体交付：** 对用户仍是一个系统插件，生成一个 ZIP；内部成员各自声明真实 domain，并由所属进程加载。

以 Harness 工作台为参考，用户选择下一次任务的模式，工作台显示对应状态，Gateway 为新任务选择专家并切换背景。
下面只保留这条开发链路，不附带完整工作台、专家配置和素材；实际代码仍需按目标版本补齐。

| 成员 | 事件 | 主要能力 |
| --- | --- | --- |
| AgentServer | `ui.ready`、`ui.select`，需要按钮时加 `ui.click` | `ops.ui.render` 显示选择与状态；需要用量时再加 `context.usage.updated` 和 `ops.session.usage` |
| Gateway | `gateway.request` | 观察选择、新建会话和首条消息，按约定补充请求参数 |
| Gateway | 背景槽位的 `ui.ready`、`gateway.disconnect` | `ops.ui.asset_url`、`ops.ui.render`、`ops.appearance.apply` 恢复外观；`ops.store` 保存需要恢复的事实，断开时清理连接缓存 |

### 1.1 选择怎样传到新任务

1. **先显示选择。** AgentServer 成员在 `manifest.ui.items` 声明 `application.sidebar.footer` 等实际入口。`ui.ready` 用当前 Ops 渲染 Select；`ui.select` 校验自己的目标、action 和候选值，显示选中结果。
2. **沿用请求协议。** 此交互经现有 `webui.dispatch` 进入 Gateway。Gateway 成员识别固定的目标和 action，将允许的模式记到当前连接；不跨进程共享成员对象，不另造“选择模式事件”。
3. **区分选择与授权。** 请求字段来自客户端，必须检查 package_id、槽位、action、候选范围和当前处理结果；请求已进入 Handler 不代表 AgentServer 已接受选择。模式只映射到预先允许的专家，不接受客户端直接传入任意专家名，也不绕过宿主认证与权限。
4. **等待真正的新任务。** `session.create` 标记当前连接准备创建新任务；`chat.send` 取得实际 session_id，确认是该新任务首次发送后才补参数。用户显式传了 `agent_template_name` 时保留原值，旧会话继续聊天时也保持原样。
5. **保存与恢复。** 连接上的“下一次选择”、会话已经采用的模式、持久偏好分别管理。选择可预览外观；重新打开旧会话时按该会话已确认的模式恢复，不能套用另一个新任务的预览。

工作台使用的选择请求如下，是 `webui.dispatch` 的 params 片段。交互必须由声明过的组件 action 发出，实例还要与自己的贡献对应：

```json
{
  "event": "ui.select",
  "target": {
    "package_id": "sample.harness-workbench",
    "scope": "application", "slot_id": "application.sidebar.footer",
    "instance_key": "harness-workbench.choice", "session_id": ""
  },
  "payload": {"action_id": "harness.next-task-mode", "data": {"value": "codex"}}
}
```

连接 ID 从 `GatewayRequestInput.connection_id` 取得，不从 payload 认领。`codex` 是业务候选值，由系统插件映射到已经存在的专家模板。
需要准备专家时，在异步业务 Handler 中等待真实目录接口，例如 `await catalog.list_agent_templates()`；不在同步 `register` 中执行异步初始化，也不能只改专家名而不验证模板存在。

### 1.2 请求改写保留哪些内容

下面只展示 `chat.send` 已被判定为新任务首条消息之后的参数处理。
`selected_template` 是系统插件验证过的模板名；`is_first_send` 来自自己维护的新任务状态，不是平台 Ops 返回的现成字段。

```python
from dataclasses import replace
from agent_gateway.extensions_v2.models import GatewayRequestInput, GatewayRequestOutput

def rewrite_first_send(
    request: GatewayRequestInput, receipt: GatewayRequestOutput,
    selected_template: str, *, is_first_send: bool,
) -> GatewayRequestOutput:
    if request.method != "chat.send" or receipt.route != "forward" or not is_first_send:
        return receipt
    params = dict(receipt.params if receipt.params is not None else request.params)
    if "agent_template_name" in params or not selected_template:
        return receipt
    params["agent_template_name"] = selected_template
    return replace(receipt, params=params)
```

请求 Handler 调用默认链一次，先检查返回类型和 route，再使用上面的处理；沿用下游已经改写过的 params，不能退回原始请求覆盖它。
首条消息状态还要按连接与会话记录和消费，防止重复发送再次改写；显式字段即使为空也不覆盖。Gateway 断开时清理该连接的选择和临时状态，不删除其他连接的记录。
外观渲染失败应如实报告，不把它改成请求路由成功或失败的依据。

## 2. 声明和打包

```text
system-plugin/
├── manifest.json
├── README.md
└── plugins/
    ├── session-member/
    │   ├── manifest.json
    │   └── plugin/extension.py
    └── gateway-member/
        ├── manifest.json
        └── plugin/extension.py
```

根声明的必要结构如下，展示信息按实际功能填写：

```json
{
  "schemaVersion": 1,
  "package_type": "extension",
  "is_bundle": true,
  "package": {"id": "example.workbench", "version": "0.0.1"},
  "display": {"name": {"zh": "工作台", "en": "Workbench"}},
  "members": [
    {"path": "plugins/session-member"},
    {"path": "plugins/gateway-member"}
  ]
}
```

- **根声明：** 不写 `package.domain`，也不在根声明 Handler 入口。使用 `is_bundle: true` 和非空 `members`。
- **成员声明：** 每个成员保留独立 ID、版本、单个 domain、入口、UI 和实际依赖，沿用单域系统插件契约。成员 ID 互不相同，也不能与根 ID 相同。
- **路径与依赖：** 成员放在根目录内的独立子目录，不嵌套组合包。真实依赖写在成员的 `requires.packages`；不因两个成员在一起就增加无关依赖，不制造环。
- **版本与修改：** 根与成员分别核对版本及身份。正式升级按主流程保留旧源码；本轮验收调试保持各自 ID、domain 和版本不变。整体 README 说明两域分工和协作入口。

例如 session-member 的声明可以从以下结构开始；这是成员 manifest，不是根声明：

```json
{
  "schemaVersion": 1, "package_type": "extension",
  "package": {"id": "example.workbench.session", "version": "0.0.1", "domain": "agentserver"},
  "display": {"name": {"zh": "工作台会话", "en": "Workbench Session"}},
  "extensions": [{"id": "session", "runtime": "backend", "module": "plugin/extension.py"}],
  "ui": {"apiVersion": 1, "items": [{
    "type": "web.slot.contribution", "id": "choice", "instanceKey": "choice",
    "scope": "application", "slot": "application.sidebar.footer",
    "renderer": {"kind": "webui-tree"}
  }]}
}
```

Gateway 成员另用 `example.workbench.gateway`，domain 为 `gateway`，声明自己的入口与背景贡献；代码导入 `agent_gateway.extensions_v2` 的类型。
AgentServer 成员导入 `jiuwenswarm.extensions_v2` 的类型；两个成员的 `plugin/extension.py` 分别提供自己的 `create_extension()`，不能在一个工厂里同时登记两个域。
实际 action 的 package_id 与 instance_key 也要跟着自己成员声明修改，不能直接复制上面的 sample 身份。

对根目录运行同一份 `scripts/finalize_package.py`。打包脚本用 Core 的 `read_bundle_spec` 和 `dependency_levels` 校验成员及依赖，并核对每个成员入口确实包含在 ZIP 中。
测试从 `JIUWEN_SYSTEM_PLUGIN_ZIP` 取得最终文件，先验证根声明，再用两域的真实加载器和临时宿主验证成员；还要验证两域协作的实际数据流。
只验证一个成员通过，不能称为整个系统插件通过。

重点覆盖：

- 选择交互能到达正确成员，非法 action 和候选值不会改变选择；两个连接互不影响。
- 新任务首条消息带上约定模板，第二条不重复改写；已有会话、显式模板和下游已有 params 都保持原意。
- 默认链拒绝或本地处理时不强制转发；模板不存在、取消和外观投递失败时结果真实。
- 两个成员的就绪顺序、重载和连接断开都能处理；重新打开旧会话能恢复其状态。

## 3. 安装与验收

安装的是整个 ZIP，不让用户分别安装成员，也不为绕过校验给根声明补一个假的 domain。

- **先核对入口：** 当前 Swarm 终端 CLI 的 `inspect_zip` 仍要求根 `package.domain`，不能直接安装此类组合包；当前已有的 Web `/extension install <ZIP 绝对路径>` 和市场入口走组合包管理流程。目标版本更新后，以其实际 CLI 实现为准。
- **自动安装受限：** 用户要求自动安装，而当前终端入口不支持组合包时，说明这一具体缺口，保留已验证的 ZIP，并说明已有安装入口；不另写管理客户端、不拆成两次单域安装，也不把交付 ZIP 写成安装完成。
- **核对整体：** 检查根 ID、版本、`overallStatus` 以及每个成员的 domain、状态、实际目录和报告。`pending`、`partial`、`failed` 不能写成全部生效；有意处于 `standby` 的成员要核对同版本活动实例和归属。
- **本轮调整：** 依据安装结果定位根副本及成员副本，再走宿主支持的组合包 reload，检查两个域；不只重载一个成员就宣布全部完成。
- **用户验收：** 全部必要成员就绪后，再验证协作效果；涉及界面时沿用主流程的用户确认循环。

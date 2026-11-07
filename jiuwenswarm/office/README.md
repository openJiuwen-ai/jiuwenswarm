# Office 后端

PowerPoint 任务窗格在 AgentServer 侧的能力：工具合同、在任务窗格执行的客户端工具、人设与 skills、
插入用的会话资产。`server/client_attachments/registry.py` 把应用编号 `office-powerpoint`
登记为包目录 `powerpoint/`；这个目录既是合同，也是对话加载的 agent template（`manifest.json`）。

## 目录

```text
contract.py            读 powerpoint/tools.json，校验 annotations，生成 ToolCard
client_tools.py        PowerPointTool(ClientTool)：build_request 判断能否执行并给出下载链接，complete 处理截图
rails.py               OfficeToolsRail：注册工具、按 supported_tools 过滤每次模型调用
assets.py              publish_powerpoint_asset 与会话资产（office_assets/，经 /file-api/download 下发）
documents.py           list_open_documents：同一用户其他已连接的文档及其对话
runtime.py             会话目录、资产允许目录、把图片存进会话 uploads
powerpoint/            合同 tools.json、profile.json（任务窗格构建时读取）；agent template：
                       manifest.json、persona/（替换默认身份）、rails/（指向 OfficeToolsRail）、skills/
```

## 何时生效

- 任务窗格用 `session.create`（`client_app: "office-powerpoint"`）建对话，再发 `client.attach`
  （对话、文档、`capabilities.supported_tools`）。应用就是对话的 `client_app`，请求里不再带；
  没有已登记 `client_app` 的对话被拒绝（`CONFLICT`）。attach 把 `supported_tools`
  存进会话目录，不安装、不装备插件。
- 任务窗格用 `client.conversations`（`client_app`、文档的 `resource_key`）找这份文档用过的对话。
- 该对话的每一轮（任务窗格发的消息、别的对话发来的消息、续跑）在加载装备插件的同一步给本对话的
  agent 加载 `powerpoint/` 这个 agent template（人设、`OfficeToolsRail`、skills），每个 agent 实例只加载一次；
  `client_app` 为空的对话不受影响。这类对话不装备专家（`agent_template_name` 被拒绝），也不从预热池领取。
- 建这类对话的 agent 时就去掉登记表里该应用 `leaves_out` 的能力：子代理（段落与 `subagent_*` 工具）、
  `send_file_to_user`、input 段、output 段。这些能力不挂上，热更新、每轮补挂和重建 agent 后也不会回来。
- `client_app` 为已登记应用的对话只在该客户端里显示：不出现在 web/desktop 的 session 列表和项目里，
  项目的批量归档也不选它们；agent 的 `session_list` 和 SDK 的 `list_sessions` 同样看不到它们。
  按 session id 发消息仍然可行，跨文档找目标走 `list_open_documents`。分叉这类对话时，新对话保留 `client_app`。

## 工具执行

- 合同里的 17 个工具都是 agent-core `ClientTool`。模型调用时先过所有 `before_tool_call`
  rail（含权限），再以 `client_tool` 中断暂停；流里发 `chat.client_tool_request`，
  `arguments` 是本次调用的参数，`client_tool.downloads` 是参数里各资产 handle 的下载链接。
  任务窗格执行后用 `chat.send`（`source: "client_tool_interrupt"`）续跑。
- 审批只在后端：工具和其他工具一样走 permissions 规则，任务窗格不再自己询问。
- 本文档的 PowerPoint 跑不了的工具不出现在模型的工具列表里；`tool_search` 仍可能找到
  deferred 工具，调用时直接报错说明原因。文档当前没打开时调用立即失败（可重试），
  不会占住这一轮；已发出的调用若窗格消失，由 attach 的 sweep 答成 `client_gone`。
- 截图：当前模型能看图时，图片只送进下一次模型调用（`keep_in_context: False`）；
  不能看图时存进本对话 uploads，结果里给出 `visual_attachment.path`。
- 跨文档协作走 `list_open_documents` + 现有的 `session_send_message`，session 之间只传文字。

## 资产

插入图片、导入 .pptx 时，模型先用 `publish_powerpoint_asset` 发布对话项目目录、agent
workspace 或本对话 uploads 里的文件，拿到它的 descriptor（不透明的 handle、大小、SHA-256）；
各工具的资产参数都直接接收这个 descriptor（`presentation_apply_patch` 另加 `key`）。调用引用本对话的
handle 时，`build_request` 给出 `/file-api/download` 链接；这个下载接口和 `send_file_to_user`
的链接一样，不检查过期和对话，任务窗格下载后按 descriptor 的大小和 SHA-256 校验。
后端不保存演示文稿副本，撤销靠 PowerPoint。

## 测试

```bash
uv run pytest tests/unit_tests/office tests/unit_tests/agentserver/test_session_client_app.py
```

真实 PowerPoint 操作验收不由单元测试代替。

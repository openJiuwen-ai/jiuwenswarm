# Office 后端

Office 任务窗格在 AgentServer 侧的能力：工具合同、在任务窗格执行的客户端工具、人设与 skills、
插入用的会话资产。`server/client_attachments/registry.py` 把应用编号 `office-powerpoint`
登记为包目录 `powerpoint/`；这个目录既是合同，也是对话加载的 agent template（`manifest.json`）。

## 目录

```text
contract.py            读包目录的 tools.json，校验 annotations，生成 ToolCard
client_tools.py        TaskPaneTool(ClientTool)：build_request 判断能否执行并给出下载链接，complete 处理截图；
                       VerifySlideVisualTool：verify_slide_visual
review.py              视觉复核：选审稿模型、审稿调用（60s）、判定、几何摘要、返回给作者的结果
rails.py               OfficeToolsRail：注册所在包的工具，第一次模型调用时按 supported_tools 去掉窗格跑不了的
assets.py              publish_office_asset 与会话资产（office_assets/，经 /file-api/download 下发）
documents.py           list_open_documents：同一用户其他已连接的文档及其对话
runtime.py             会话目录、资产允许目录、把图片存进会话 uploads
powerpoint/            合同 tools.json、profile.json（任务窗格构建时读取）；agent template：
                       manifest.json、persona/（替换默认身份）、rails/（指向 OfficeToolsRail）、skills/；
                       review.md（verify_slide_visual 的审稿清单）
```

`contract.py`、`TaskPaneTool`、`rails.py` 不认识 Office：任何在自己包里声明工具、由客户端执行的应用都是
这一套。现在登记的应用都是 Office 宿主，所以放在这里；出现第一个非 Office 的客户端应用时，把这三块
挪到 `server/client_attachments/`，Office 专属的东西（视觉复核、素材类型、`list_open_documents`）留下。

## 何时生效

- 任务窗格用 `session.create`（`client_app: "office-powerpoint"`）建对话，再发 `client.attach`
  （对话、文档、`capabilities.supported_tools`）。应用就是对话的 `client_app`，请求里不再带；
  没有已登记 `client_app` 的对话被拒绝（`CONFLICT`）。attach 把 `supported_tools`
  存进会话目录，不安装、不装备插件。
- 任务窗格用 `client.conversations`（`client_app`、文档的 `resource_key`）找这份文档用过的对话。
- 该对话的每一轮（任务窗格发的消息、别的对话发来的消息、续跑）在加载装备插件的同一步给本对话的
  agent 加载 `powerpoint/` 这个 agent template（人设、`OfficeToolsRail`、skills），每个 agent 实例只加载一次；
  `client_app` 为空的对话不受影响。这类对话不装备专家（`agent_template_name` 被拒绝），也不从预热池领取。
- `OfficeToolsRail` 从加载它的 template 得知自己的包目录（agent-core 的 `from_plugin_context`，
  `source_root` 即 `manifest.json` 所在目录），包里的 `rails/office_tools.py` 只是转出这个类。
- 建这类对话的 agent 时就去掉登记表里该应用 `leaves_out` 的能力：子代理（段落与 `subagent_*` 工具）、
  `send_file_to_user`、input 段、output 段。这些能力不挂上，热更新、每轮补挂和重建 agent 后也不会回来。
- `client_app` 为已登记应用的对话只在该客户端里显示：不出现在 web/desktop 的 session 列表和项目里，
  项目的批量归档也不选它们；agent 的 `session_list` 和 SDK 的 `list_sessions` 同样看不到它们。
  按 session id 发消息仍然可行，跨文档找目标走 `list_open_documents`。分叉这类对话时，新对话保留 `client_app`。

## 工具执行

- 合同里的 18 个工具都是 agent-core `ClientTool`。模型调用时先过所有 `before_tool_call`
  rail（含权限），再以 `client_tool` 中断暂停；流里发 `chat.client_tool_request`，
  `arguments` 是本次调用的参数，`client_tool.downloads` 是参数里各资产 handle 的下载链接。
  任务窗格执行后用 `chat.send`（`source: "client_tool_interrupt"`）续跑。
- 审批只在后端：工具和其他工具一样走 permissions 规则，任务窗格不再自己询问。
- 窗格连上后的第一次模型调用，rail 把窗格跑不了的工具从 agent 上去掉：模型的工具列表、
  `tool_search` 和 deferred 工具目录都没有它们。之后不再变：一个对话在一个宿主里用。
  调用途中窗格失去支持时，调用直接报错说明是插件还没有这个工具。文档当前没打开时调用立即失败（可重试），
  不会占住这一轮；已发出的调用若窗格消失，由 attach 的 sweep 答成 `client_gone`。
- 截图：当前模型能看图时，图片只送进下一次模型调用（`keep_in_context: False`）；
  不能看图时存进本对话 uploads，结果里给出 `visual_attachment.path`。
- 视觉复核 `verify_slide_visual`：审稿只看清单和这一页，不带对话内容。审稿模型是本对话当前的模型
  （能看图时，判断同上一条），否则是 `models.vision`（与 agent 自己的视觉工具同一门控）；两者都没有时，
  build_request 在上面的检查之后立即失败、不可重试。任务窗格在一次答复里给出这一页的截图和几何测量
  （`{slide_id, geometry}`）。complete 校验答复（恰好一张图、`slide_id` 一致、带几何）后，把包里的审稿清单
  （`review.md`）、几何摘要和截图交给审稿模型，只有这两条消息；
  60s 内没有答复、供应商报错或两次空答复时，返回不带 verdict 的结果。作者拿到 `done|needs_work`、
  `hard_defects`/`advisories`/`expected_change_checks` 和同一张截图（截图的去向同上一条）。
  怎么用这个结果（`needs_work` 不算完成、同一页三轮就停、没有 verdict 时自己看图并说明）只写在人设的编辑循环里。
- 跨文档协作走 `list_open_documents` + 现有的 `session_send_message`，session 之间只传文字。

## 资产

插入图片、导入 .pptx 时，模型先用 `publish_office_asset` 发布对话项目目录、agent
workspace 或本对话 uploads 里的文件，拿到它的 descriptor（不透明的 handle、大小、SHA-256）；
各工具的资产参数都直接接收这个 descriptor（`presentation_apply_patch` 另加 `key`）。调用引用本对话的
handle 时，`build_request` 给出 `/file-api/download` 链接；这个下载接口和 `send_file_to_user`
的链接一样，不检查过期和对话，任务窗格下载后按 descriptor 的大小和 SHA-256 校验。
后端不保存文档副本，撤销靠宿主自己的撤销。

## 桌面安装

桌面版把任务窗格装进本机的 PowerPoint，代码在 `jiuwenswarm/channels/desktop/office_addins/`：

- 窗格构建在前端 dist 的 `office/` 下（`npm run build:desktop`，桌面构建脚本在前端之后执行，失败则构建失败），
  由桌面的 web 进程提供。三个宿主的页面 `taskpane.<host>.html` 都在，旁边只放 `manifest.powerpoint.xml`：
  桌面现在只安装 PowerPoint。Office 只从 HTTPS 加载窗格，所以 web 进程在端口分配表的 `office_tls`（默认 5443，
  随实例 +1000）上另开一个只绑 127.0.0.1 的 HTTPS 监听，用同一个 handler，`/ws`、`/file-api/download` 照常可用。
- 证书在 `<数据目录>/office-addin/tls/`：一个本地 CA 和它签发的 `localhost` 证书；系统只信任这个 CA，
  CA 私钥不落盘，所以它签不了别的证书。距过期不足 30 天时，再点一次安装会换新的一对，先撤销旧 CA 的信任。
- 注册用 Microsoft 的侧载位置：Mac 写 `~/Library/Containers/com.microsoft.Powerpoint/Data/Documents/wef/`，
  Windows 写 `HKCU\SOFTWARE\Microsoft\Office\16.0\Wef\Developer`。清单由 `manifest.powerpoint.xml` 渲染：
  开发 origin 换成本进程的 HTTPS origin、路径加 `/office/`（窗格页是 `/office/taskpane.powerpoint.html`），
  `<Id>` 换成安装版自己的，不与开发侧载互相覆盖。
- 用户安装的单位是套件：`SUITES` 一张表定义每个套件含哪些应用（现在只有 `office` → `powerpoint`，Word、Excel
  的窗格落地时加入 `office`）。一次安装为整个套件做一次证书、信任和监听，再注册套件里的每个应用。
- 接口只在桌面模式下存在，且只认桌面窗口的 Cookie，否则 404：`GET /desktop-api/office-addins` 返回
  `{"suites": {"office": {"installed", "problems", "apps": {"powerpoint": bool}}}}`；
  `POST /desktop-api/office-addins/<suite>/install|remove` 返回该套件的状态。`installed` 表示套件里每个应用都已注册。
  状态每次现读注册和证书，`problems` 取值 `host_not_found`、`permission_denied`、`certificate_untrusted`、
  `certificate_expiring`、`tls_unavailable`、`stale_origin`；修复就是再点一次安装。web 进程启动后会按当前端口
  重写已装的清单。
- 没有任何应用还注册着时，移除会撤销 CA 信任、删除证书并关掉 HTTPS 监听。Windows 卸载程序执行
  `--desktop-remove-office-addins` 做同样的清理。
- 已知限制：Mac 没有卸载钩子，删除 app 前要先在桌面里移除；macOS 上 PowerPoint 必须先打开过一次；
  PowerPoint 可能要重启才读到新清单；从源码运行桌面版时要先在 `packages/jiuwenswarm-office-addin` 执行
  `npm run build:desktop`。

## 测试

```bash
uv run pytest tests/unit_tests/office tests/unit_tests/agentserver/test_session_client_app.py
```

真实 PowerPoint 操作验收不由单元测试代替。

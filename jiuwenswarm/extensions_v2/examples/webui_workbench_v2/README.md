# 插件交互工作台

[English](README_EN.md)

`webui.workbench-v2` 演示十三个 WebUI slot，以及右侧 `Panel` 和 `Iframe` 根节点。侧栏主页面为纯展示的登录页壳子，任务检查使用固定数据，文件页签读取当前会话的真实工作区，网页页签直接加载 example.com 示例页面。插件保存筛选状态及平台返回的文件数据，通过下行完整树更新界面；文件组件自行处理展开、选中和提示文案。示例不调用模型或外部业务 API。

## 安装与启用

前提是运行包含本次 WebUI 协议和生产装配改动的 Gateway、AgentServer 与 Web 前端。仅复制本示例到旧版本服务，不会自动获得新组件支持。仓库根目录可用 `uv run jiuwenswarm-start debug --skip-build` 启动已有前端构建；重新构建前端时，按仓库约束在前端目录执行 `npm exec vite build`。

仓库根目录已准备好 [webui_workbench_v2.zip](../../../../webui_workbench_v2.zip)，可直接用于手动安装，无需先运行打包脚本。将 ZIP 放在运行 AgentServer 的机器可读取的位置，然后在 Web 单 Agent 模式的输入框执行，例如：

```text
/extension install D:\code\jiuwen-plugin\jiuwenswarm_8175\webui_workbench_v2.zip
/extension query webui.workbench-v2
```

已有同 ID 的旧版本时，先执行 `/extension uninstall webui.workbench-v2`，成功后再安装新 ZIP 并查询。安装不会覆盖激活中的旧包。当前 ZIP 版本为 1.0.9，须同时更新 Gateway、AgentServer 和 Web 前端；只替换 ZIP 或刷新旧前端不能获得新组件和会话文件接口。

Linux/macOS 使用实际绝对路径，如 `/home/me/work/webui_workbench_v2.zip`。这是 Web 命令，不是 shell 命令。安装成功即激活，无需另执行 enable。安装结果会给出真实安装目录；服务启动只加载安装目录，不扫描源码中的 `examples/`。管理命令可以在新建空对话使用，但插件渲染仍需要真实会话。

安装完成后进入已有真实会话，刷新页面或重新进入会话，让 Host 发送 `ui.ready`。首次 ready 就能生成演示树；再次 ready 恢复当前激活实例的任务状态。文件实例 ready 时重新读取根目录并清空预览；网页实例 ready 时重新发布 Iframe。

## 访问与操作

| 位置 | 内容及访问方式 |
| --- | --- |
| `application.menu` → `application.menu.main` | 点击侧栏“交互工作台 / Workbench”打开登录页壳子；两处实例均为 `workbench` |
| `task.function` → `task.function.main` | 点击任务区“任务检查 / Inspect tasks”；两处实例均为 `task-inspector` |
| `application.entry` | 浏览器首次进入时的全屏欢迎入口，插件决定展示与结束 |
| `application.settings` | 设置内容区域显示“自动摘要 / Auto summary”开关，状态属于当前会话 |
| `conversation.input.above` | 筛选摘要与最近操作结果 |
| `conversation.input.below` | 紧凑操作次数提示 |
| `conversation.panel.right` | “工作区文件”和“网页预览”两个页签；分别由 Panel 和 Iframe 根提供，与原生内容互斥 |
| `conversation.turn.content.after` | 已结束助手轮次的状态快照 |
| `conversation.turn.footer.actions` | “刷新本轮 / Refresh round”和“本轮详情 / Round details” |
| `conversation.header.title.after` | 工作台状态 Tag |
| `conversation.header.actions` | 刷新当前会话工作台的按钮 |

登录页只包含一个图标和同排的“登录”“退出”两个蓝色按钮。插件 `login_page()` 使用已有 Box、Image 和 Button 声明纵向排列、水平与垂直居中以及间距。两个按钮不声明事件，不调用接口，也不维护登录状态；页面没有表单、页签、说明文字或演示开关。

任务区的“任务检查 / Inspect tasks”继续提供筛选和结果；Select 支持搜索与清空，TreeSelect 支持项目选择与清空，回车应用已完成的输入。

轮次位置的 `instance_key` 来自实际 `requestId`。首次挂载保存该轮快照；后续 ready 恢复快照。其他轮次和其他会话的修改不会覆盖它；点击“刷新本轮”才把当前筛选摘要写入该轮。消息尚未结束、缺少 `requestId` 或宿主隐藏脚部元信息时，对应位置可能不挂载。

页面宿主只提供通用的响应式边距和可伸展的列向实例容器；根 Box 是否填满、居中方向和内容排列均由插件布局决定，前端没有登录页专用分支。测试 1.0.9 时，使用包含实例容器更新的 Web 前端构建，再安装新 ZIP。

## 首次进入的全屏欢迎入口

版本 1.0.9 将 application.entry 从聊天空态迁到应用根部，覆盖整个应用视口。宿主首次进入时发送 ui.ready，payload 为 `{reason: "first_visit"}`；插件使用自己的 Box 布局声明居中图标、欢迎文字和“开始使用”按钮，点击按钮通过 ops.ui.render 发布 clear。插件可在 ready 时直接 clear 来跳过入口，宿主没有欢迎内容或登录业务分支。

同一浏览器、同一站点通过 localStorage 的 `workswarm.webui.entry.visited` 记录首次触发。记录在请求前写入，展示中刷新、关闭后重新打开、新建会话或切换账号都不会再次触发。没有安装贡献或请求失败也会消耗首次机会。没有有效内容时保留正常应用；入口显示期间背景不能操作，断线退出后不自动重开。

入口使用空 session_id 和独立 application_id，Gateway 注入真实连接路由，沿用既有 WebUI 通道，不创建聊天会话，也不新增 HTML/JS 文件传输接口。新包需要同步包含应用目标支持的 Gateway、AgentServer 和前端。协议与边界见 [S_04](../../docs/specs/S_04_webui-application-entry.md)。

手动测试请先安装新包，再进入更新后的前端。如果已有首次记录，可在浏览器开发者工具中只删除 `workswarm.webui.entry.visited` 这个 localStorage 项，或执行以下语句后刷新；无需清空其他站点数据：

```javascript
localStorage.removeItem('workswarm.webui.entry.visited');
```

依次检查首次展示、点击开始使用退出、展示中刷新不重开，以及重新打开和切换会话不重开。两个浏览器连接之间的入口和交互应互不影响。本轮仅核对源码及 ZIP，未执行自动测试、构建或浏览器联调。

## 右侧面板更新

本轮 ZIP 版本为 1.0.9。同包在右侧声明两个贡献：`workbench.details` 使用实例 `workbench`，根节点为 `Panel`；`workbench.iframe` 使用实例 `iframe-preview`，根节点为 `Iframe`。右侧 ready 未指定实例时，插件一次发布两个独立实例；指定有效实例时，只恢复该实例。宿主根据根节点标题生成页签。

文件页签使用 `Panel(title, children)`，标题和正文在同一贡献内绑定，正文接受 slot 注入的样式。插件用一个 `Box` 并排组合 `FileTree` 与 `FilePreview`，仅提供目录数据、结构化状态和预览引用。文件组件负责展开、选中、加载和错误提示，插件不保存这些交互状态，也不拼接提示文案。两个文件组件各自注册，可以由其他插件单独使用。组件与平台接口见 [F_07](../../docs/features/F_07_ui-contributions.md#2026-10-27-会话文件组件实施方案)。

展开右侧面板后，默认保持原有内容；点击“工作区文件 / Workspace files”或“网页预览 / Web preview”显示对应插件实例，点击原生入口返回。插件入口与正文只在展开及全屏模式下显示。收起后的浮窗只显示原生内容，保留插件选择，再次展开时恢复；从浮窗点击某个原生区域的展开按钮时，则显示对应原生页面。插件更新不自动抢占正文；撤销或卸载选中的面板后回到原生内容。多个插件入口可横向滚动。仍沿用工具面板的显示条件，不强制打开原本隐藏或不可用的右侧面板。

单 Agent 聊天和代码会话均可浏览文件。有项目时显示会话已绑定项目根，无项目时显示已绑定任务根，包含 `work/` 和 `outputs/`。团队模式本版显示不支持提示。未取得有效绑定时显示工作区不可用，浏览操作不会创建新工作区，也不会退回全局目录。

文件实例收到 `ui.ready` 时读取根目录并清空预览，首次展开尚未加载的目录时再请求子目录。每层最多 100 项、总共 512 项、最多 8 层，同时受帧大小预算限制；超过时由组件显示截断提示。组件最多保留 100 个展开目录，并通过 `ui.change` 提供本次请求的完整 `expandedKeys`；插件只把当前目录数据中的 key 映射为路径，再调用 `ops.session.files`。已经加载的目录由组件直接展开或收起，不重复请求，收起目录保留当前预览。后续目录请求失败时，插件保留最近的有效目录数据并标记读取失败；组件收起仍未加载的目录，允许再次展开重试。文件创建和删除不会自动推送；重新进入会话、让文件实例再次收到 ready 时会重新读取根目录。本版不提供文件刷新按钮，聊天顶部的工作台刷新也不重新读取文件。

本版只读预览 UTF-8 文本、代码、Markdown 和常见图片，文本最多 1 MiB，图片最多 4 MiB，另受消息传输大小限制。HTML 按代码显示；其他格式和二进制内容显示不支持提示。选择文件时，插件只调用 `ops.session.preview` 取得引用，不重新读取目录。预览组件通过限定当前会话的资源引用读取内容，内容不放入渲染树。空文件、文件消失、超限和读取失败由前端组件显示对应状态。组件不提供编辑、保存或写文件功能。

## 网页预览页签

网页实例使用 schema v2 的 `Iframe`，和 `Panel` 并列为右侧根类型。Iframe 只能作为渲染树根，不能嵌套在 Panel、Box 或其他组件内，也不接受 children、events 或 srcdoc。示例只下发标题与 URL：

```json
{"component":"Iframe","props":{"title":"网页预览 / Web preview","src":"https://example.com"}}
```

示例直接加载 `https://example.com`，不依赖平台 `public/` 下的演示资源，也不通过 postMessage 接入宿主。文件操作和工作台筛选更新不会重新发布网页实例。1.0.7 按用户要求从百度切换为 example.com，用于测试外部网页展示。插件通过既有渲染链路发送 src 和 title；浏览器直接请求 src，不由插件传输 HTML 或打包后的 JS。安装 ZIP 不会自动提供插件目录内的网页资源。

src 支持 HTTP(S) 绝对地址、`//host`、`/path` 和相对 Web 宿主的常规路径，不限制为 localhost。平台拒绝 URL 凭据、反斜杠、ASCII 控制字符，以及 `http:foo`、`///host` 这类跨端解释不一致的写法。HTTPS 页面嵌入 HTTP 地址等混合内容限制由浏览器决定。

宿主固定设置 `sandbox="allow-scripts allow-forms"`，插件不能放开同源权限、弹窗或顶层导航。外部站点还需允许浏览器嵌入，`X-Frame-Options` 或 CSP `frame-ancestors` 可能阻止显示；依赖父页 DOM、同源访问或同源存储的页面也可能无法在此 sandbox 中运行。替换 src 前应确认目标页面兼容这些限制。

## 重置、修改与卸载

点击“重置 / Reset”恢复当前会话的筛选和演示开关，保留操作计数和已有轮次快照。插件数据仅存于扩展激活实例内存；进程重启或成功重载后重新初始化。文件树的展开和选中属于前端组件，插件只缓存平台返回的目录与预览结果。

修改安装响应给出的安装副本后执行：

```text
/extension reload webui.workbench-v2
/extension query webui.workbench-v2
```

reload 读取安装副本，不重新读取原 ZIP；修改仓库源码后，需要把改动同步到安装副本或重新打包安装。重载成功后重新挂载页面可生成新激活实例的树。卸载使用：

```text
/extension uninstall webui.workbench-v2
/extension query webui.workbench-v2
```

卸载沿用平台清理和 revision 机制。当前管理入口未提供独立的 enable/disable 命令。

## 协议与验证边界

manifest `schemaVersion: 1`、`ui.apiVersion: 1` 与渲染帧 `schema_version: 2` 含义不同。本包使用 `events['ui.click']` 等 v2 事件；业务参数位于声明动作的 `args`，控件值位于规范事件的 `data`。dispatch 响应仅是回执，不能作为新树。通用版本策略与校验规则见 [F_08](../../docs/features/F_08_frontend-render.md)。

`application.entry` 使用应用目标，不需要真实聊天会话；其他位置仍使用真实 `session_id`，菜单和设置尚不支持无会话渲染。右侧贡献使用两个稳定实例，事件和更新按实例隔离；其余 single slot 本包各创建一个实例。欢迎入口按贡献 order、包 ID 和贡献 ID 选择一个插件；其他 single 位置按最近更新选择实例。`message.assistant.meta` 与旧 `applicationPlugins` 不属于本包十三项目标。

本包交由用户手动测试。建议顺序：安装并查询 → 真实会话首次 ready → 菜单主页面 → 控件操作及辅助区域回显 → 第二会话与两个真实轮次隔离 → 重新挂载恢复 → 重载、卸载及重连。确认侧栏登录页仅显示居中图标和两个蓝色按钮，点击按钮不发送业务请求；在窄屏下检查排列。设置区域与现有设置模块比较对齐、文字、分组边界和窄屏布局；任务检查另检查中文输入、空态及明暗主题。

文件面板还需分别检查项目／无项目、聊天／代码会话；展开空目录，选择文本、代码、Markdown、图片、空文件及不支持格式；快速切换文件与会话，删除文件后检查读取失败状态，再重新进入会话检查根目录。检查中文名、空格和嵌套路径、窄面板滚动，以及收起浮窗、再次展开、全屏和卸载后的行为；确认没有文件刷新按钮，聊天顶部的工作台刷新不触发文件读取。本轮没有新增或运行自动测试、构建、服务或浏览器联调，以上为待执行的手动步骤。

网页页签还需检查 example.com 页面加载、窄屏、原生／文件／网页页签切换及会话隔离；操作文件或工作台筛选，确认网页未因无关更新重新加载。站点嵌入限制和 sandbox 兼容性须在浏览器中验证。

初版定向 pytest 曾通过 7 项，使用真实包加载与 WebUI Runtime，传输使用测试替身。后续修复没有继续运行测试；真实网络往返和浏览器布局尚未验收。已执行记录及其范围见 [03 实施验收记录](../../../../fontenddesign/03_工作台插件实施验收记录.md)。

## 修改源码后重新打包

需要从修改后的源码重建 ZIP 时，在仓库根目录执行以下 Python 脚本。ZIP 根目录直接包含 `manifest.yaml` 和 `webui_workbench_v2/`：

```python
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

source = Path("jiuwenswarm/extensions_v2/examples/webui_workbench_v2").resolve()
archive = Path("webui_workbench_v2.zip").resolve()
with ZipFile(archive, "w", ZIP_DEFLATED) as output:
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            output.write(path, path.relative_to(source))
print(archive)
```

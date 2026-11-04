# 插件交互工作台

[English](README_EN.md)

`webui.workbench-v2` 演示 14 个已登记的 WebUI slot（共 15 项贡献，右侧面板包含两个实例），覆盖 `Avatar`、`Card`、`Dropdown`、`FileTree`、`FilePreview`、`Panel`、`Iframe`、`Switch`、`Button`、`Tag`、`Box`、`Text` 等目录组件。侧栏主页面与任务检查主页面均使用 `Iframe` 根加载示例页面，右侧面板同时提供工作区文件页签（`Panel` 根，内含 `FileTree` 与 `FilePreview`）和网页预览页签（`Iframe` 根）。插件保存会话状态及平台返回的文件数据，通过下行完整树更新界面；文件组件自行处理展开、选中和提示文案。示例不调用模型或外部业务 API。

## 安装与启用

前提是运行包含本次 WebUI 协议和生产装配改动的 Gateway、AgentServer 与 Web 前端。热安装时在仓库根目录执行 `python scripts/install_workbench_plugin.py --zip-only`，仅生成 `webui_workbench_v2.zip`；不带参数会直接部署安装目录，之后须重启服务加载，不要再安装同版本 ZIP。

将生成的 ZIP 放在运行 AgentServer 的机器可读取的位置，然后在 Web 单 Agent 模式的输入框执行，例如：

```text
/extension install /path/to/webui_workbench_v2.zip
/extension query webui.workbench-v2
```

已有同 ID 的旧版本时，先执行 `/extension uninstall webui.workbench-v2`，成功后再安装新 ZIP 并查询。安装成功即激活，无需另执行 enable。服务启动只加载安装目录，不扫描源码中的 `examples/`。管理命令可以在新建空对话使用，会话级插件渲染需要真实会话；应用范围槽位（`application.entry`、`application.sidebar.footer`）不要求会话。

安装完成后进入已有真实会话，刷新页面或重新进入会话，让 Host 发送 `ui.ready`。首次 ready 即可生成演示树；再次 ready 恢复当前激活实例的任务状态。文件实例 ready 时重新读取根目录并清空预览；网页实例 ready 时重新发布 Iframe。

## 访问与操作

| 位置 | 内容及访问方式 |
| --- | --- |
| `application.entry` | 首次访问展示欢迎卡片和 Avatar，点击开始使用关闭；ready 被接受后记录访问标记，刷新不再挂载 |
| `application.sidebar.footer` | 左侧图标栏底部的用户菜单（`Avatar` + `Dropdown`，实例 `user-menu`）：应用范围、single 容量 |
| `application.menu` → `application.menu.main` | 点击侧栏“交互工作台 / Workbench”打开主页面（`Iframe` 根）；两处实例均为 `workbench` |
| `task.function` → `task.function.main` | 点击任务区“任务检查 / Inspect tasks”打开任务主页面（`Iframe` 根）；两处实例均为 `task-inspector` |
| `application.settings` | 设置内容区域显示“天气详情 / Weather details”开关，状态属于当前会话 |
| `conversation.input.above` | Token 天气演示与右侧“隐藏／显示”按钮，使用 `Box` 横向排列、窄屏换行；宿主提供浅色圆角横条，集群模式下位于活动面板上方 |
| `conversation.input.below` | 周报总结、报错修复、PPT 制作、闲时任务四个快捷按钮，图标分别为 `file-text`、`wrench`、`presentation`、`clock` |
| `conversation.panel.right` | “工作区文件”（`Panel` 根，实例 `workbench`）与“网页预览”（`Iframe` 根，实例 `iframe-preview`）两个页签，与原生内容互斥 |
| `conversation.session.content.after` | 已结束助手轮次的真实 Token 用量，复用 ghost Button 样式和 database 图标，不绑定点击动作；执行轮次菜单操作后另显示结果行 |
| `conversation.turn.footer.actions` | `👍` 单符号按钮与 `…` 下拉入口并排，菜单包含“刷新本轮 / Refresh round”和“本轮详情 / Round details” |
| `conversation.header.title.after` | 工作台状态 Tag |
| `conversation.header.actions` | 刷新当前会话工作台的纯图标按钮；插件声明 `type="text"`、`icon="refresh"` 和 `aria-label` |

轮次位置的 `instance_key` 来自实际 `requestId`。用量显示为 `用量：22.8M tokens` 等文案，
沿用 `Button(type="text", size="md", icon="database")` 的外观，不声明 `ui.click`，仅用于展示。
取自 `ops.session.usage()` 的本轮 `turn.total_tokens`，不是上下文占用量或会话累计账单。
插件监听 `context.usage.updated` 并读取宿主快照，只更新对应会话、对应 requestId 的展示；重复通知覆盖数值，不累加。
真实零值显示 `用量：0 tokens`；缺失显示“未上报”，不使用固定数字，也不从输入和输出推算总数。

首次挂载查询可用用量，后续 ready 保留已缓存数值；通知早于挂载时也会缓存。
“刷新本轮”重新查询用量、保存当前筛选摘要，并显示该轮刷新次数；“本轮详情”显示 requestId 和已保存的摘要。
这些操作只更新当前轮次，其他轮次和会话的修改不会覆盖它。插件重新激活后缓存清空，
公共查询只提供最近可用回合；只恢复 requestId 匹配的数值，其他历史轮次显示“未上报”。
该能力依赖宿主发布用量或保存 provider 汇总；未提供数据的运行模式保持“未上报”。

普通按钮统一使用 `size="md"`：高 32px、字号 14px、左右内边距 16px、图文间距 8px、圆角 8px，无默认阴影。
这些尺寸由宿主主题提供，插件只声明 size；输入框下方的四个快捷按钮也使用该规格。

轮次菜单使用 `Dropdown(icon="ellipsis", ariaLabel="本轮操作 / Round actions")`，展开期间保留浅灰背景。
其左侧通过插件渲染树添加 `Button(value="👍", type="text")`，仅展示符号，不绑定点赞业务动作；两者使用横向 Box 排列。
打开菜单不发送业务事件；选中菜单项发送 `ui.select`，动作 `workbench.turn.select`，data.key 为 `refresh` 或 `details`。
独立 Button 也支持纯图标模式（省略 `value` 并设置 `icon="ellipsis", type="text"`，`aria-label` 作为可访问名称及悬停提示）。

输入框上方固定展示 `Token 天气： ☀️ 晴 9% · 91k/1.0M · 5h 1% · $1.06` 和灰色的“上下文充裕”，这些是演示数据，不代表实时用量。该视图属于本工作台插件，无需安装独立的 `token_weather` 插件。点击右侧“隐藏”后保留“Token 天气”和“显示”按钮；显隐状态按会话隔离，后续 ready 恢复当前状态，重新激活插件后重置。插件只使用 `Box`、`Text`、`Button`，不包裹 `Card`；背景、圆角和内边距由宿主 slot 提供。

## 侧栏用户入口

`application.sidebar.footer` 是应用范围的 single 槽位，不要求聊天会话。用户菜单统一由本工作台的 `user-menu` 实例提供，无需另安装 `webui.user-menu`。插件在该槽位渲染使用 `src` 的 `Avatar` 触发区与 `Dropdown` 菜单，“个人资料 / Profile”和“退出登录 / Sign out”绑定 `ui.select`。个人资料仅返回演示选择回执，不打开页面；退出登录按浏览器连接记录未登录状态并改渲染使用 `value` 的“登录”按钮，点击该按钮（`ui.click`，动作 `workbench.user.login`）切回用户菜单。状态按 `target.connection_id` 隔离；断连时平台清理实例，插件侧的演示状态随激活存续，重连后重新触发 `ui.ready`。示例不接入真实登录。

## 右侧面板：工作区文件与网页预览

同包在右侧声明两个贡献：`workbench.details` 使用实例 `workbench`，根节点为 `Panel`；`workbench.iframe` 使用实例 `iframe-preview`，根节点为 `Iframe`。右侧 ready 未指定实例时，插件一次发布两个独立实例；指定有效实例时，只恢复该实例。宿主根据根节点标题生成页签。

文件页签使用 `Panel(title, children)`，插件用一个 `Box` 并排组合 `FileTree` 与 `FilePreview`，仅提供目录数据、结构化状态和预览正文。两个文件组件各自注册，可以由其他插件单独使用。组件与平台接口见 [F_07](../../docs/features/F_07_ui-contributions.md#44-文件能力目录与预览)。

单 Agent 聊天和代码会话均可浏览文件。有项目时显示会话已绑定项目根，无项目时显示已绑定的运行工作区；自动任务工作区包含 `work/` 和 `outputs/`，Code 配置的默认会话工作区沿用其实际目录布局。团队模式显示不支持提示；未取得有效绑定时显示工作区不可用。

文件实例收到 `ui.ready` 时调用 `ops.session.files` 读取根目录并清空预览；展开目录时组件通过 `ui.change` 提供 `expandedKeys`，插件将 key 映射为路径后再次调用 `ops.session.files`。每层最多 100 项、总共 512 项、最多 8 层、96 KiB 预算。选择文件时，插件调用 `ops.session.read_file` 并将结果交给 `FilePreview`。16 KiB 以内的 UTF-8 文本仍通过 content 展示；更大文本（最多 1 MiB）、图片和 PDF 通过 downloadUrl 加载，由 FilePreview 内部处理。URL 指向有效期为 10 分钟的文件快照，过期后重新选择文件即可刷新。PDF 在同源下载响应类型及文件头检查通过后使用原生 iframe，实际显示依赖浏览器内建 PDF 查看器；其他二进制或超限文本返回对应状态提示。

文件树右侧分界线支持拖拽，也可聚焦后用左右方向键调整宽度。目录展开或预览更新后保留滚动位置和宽度；清空实例或断连后重置，不写入持久存储。

网页实例使用 `Iframe` 根，只能作为允许该根组件的槽位（`conversation.panel.right`、`application.menu.main`、`task.function.main`）的渲染树根，不能嵌套在 `Panel`、`Box` 或其他组件内，也不接受 children、events 或 srcdoc。宿主固定设置 `sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads"`。
右侧“网页预览”加载 `https://httpbin.org/forms/post`，用于手工检查网页加载、输入、选项和表单提交。该页面响应未声明 `X-Frame-Options` 或限制性的 `frame-ancestors`，适合作为 iframe 联调页；网络可用性仍取决于目标站点。
普通点击、输入和 iframe 内导航不需要同源权限；加载提示不拦截鼠标。相对路径页面也使用不透明 origin，依赖存储或同源请求的页面可能不兼容，不能通过添加 `allow-same-origin` 解决。安全取舍见 [F_08](../../docs/features/F_08_frontend-render.md#46-iframe-权限与点击交互)。

## 重置、修改与卸载

修改安装响应给出的安装副本后执行：

```text
/extension reload webui.workbench-v2
/extension query webui.workbench-v2
```

reload 读取安装副本，不重新读取原 ZIP；修改仓库源码后，需要把改动同步到安装副本或重新打包安装。卸载使用：

```text
/extension uninstall webui.workbench-v2
/extension query webui.workbench-v2
```

## 协议与验证边界

manifest `schemaVersion: 1`、`ui.apiVersion: 1` 与渲染帧 `schema_version: 1` 含义不同。本包使用 `events['ui.click']` 等组件事件；业务参数位于声明动作的 `args`，控件值位于规范事件的 `data`。dispatch 响应仅是回执，不能作为新树。通用版本策略与校验规则见 [F_08](../../docs/features/F_08_frontend-render.md)。

`application.entry` 与 `application.sidebar.footer` 使用当前 Web 连接的应用身份，不带 `session_id`；其余槽位需要真实会话。新建空对话不挂载 `application.menu`、`task.function` 和 `application.settings`。右侧贡献使用两个稳定实例，事件和更新按实例隔离；其余 single 位置按最近更新选择实例。`message.assistant.meta` 不属于本包目标。

## 修改源码后重新打包

需要从修改后的源码重建 ZIP 时，可在仓库根目录执行 `python scripts/install_workbench_plugin.py --zip-only`，或运行以下 Python 脚本（ZIP 根目录直接包含 `manifest.yaml` 和 `webui_workbench_v2/`）：

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

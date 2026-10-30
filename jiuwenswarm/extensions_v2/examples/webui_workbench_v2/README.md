# 插件交互工作台

[English](README_EN.md)

`webui.workbench-v2` 演示 12 个已登记的 WebUI slot，覆盖 `Input`、`Select`、`TreeSelect`、`Slider`、`Switch`、`Button`、`Card`、`Tag`、`Empty`、`Image`、`Iframe`、`Box`、`Text` 等目录组件。侧栏主页面为纯展示的登录页壳子，任务检查使用固定数据，右侧网页页签直接加载 example.com 示例页面。插件保存筛选状态，通过下行完整树更新界面。示例不调用模型或外部业务 API。

本副本是面向当前分支的裁剪版：去除了参考分支中的 `application.entry` 全屏欢迎入口与 `FileTree`/`FilePreview` 工作区文件页签（这两项能力在当前分支暂缓），`conversation.panel.right` 只保留 `Iframe` 网页预览实例。

## 安装与启用

前提是运行包含本次 WebUI 协议和生产装配改动的 Gateway、AgentServer 与 Web 前端。需要从源码重建 ZIP 时，在仓库根目录执行：

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

将 ZIP 放在运行 AgentServer 的机器可读取的位置，然后在 Web 单 Agent 模式的输入框执行，例如：

```text
/extension install D:\path\to\webui_workbench_v2.zip
/extension query webui.workbench-v2
```

已有同 ID 的旧版本时，先执行 `/extension uninstall webui.workbench-v2`，成功后再安装新 ZIP 并查询。安装成功即激活，无需另执行 enable。安装结果会给出真实安装目录；服务启动只加载安装目录，不扫描源码中的 `examples/`。管理命令可以在新建空对话使用，但插件渲染仍需要真实会话。

安装完成后进入已有真实会话，刷新页面或重新进入会话，让 Host 发送 `ui.ready`。首次 ready 就能生成演示树；再次 ready 恢复当前激活实例的任务状态。

## 访问与操作

| 位置 | 内容及访问方式 |
| --- | --- |
| `application.menu` → `application.menu.main` | 点击侧栏“交互工作台 / Workbench”打开登录页壳子；两处实例均为 `workbench` |
| `task.function` → `task.function.main` | 点击任务区“任务检查 / Inspect tasks”；两处实例均为 `task-inspector` |
| `application.settings` | 设置内容区域显示“自动摘要 / Auto summary”开关，状态属于当前会话 |
| `conversation.input.above` | 筛选摘要与最近操作结果 |
| `conversation.input.below` | 紧凑操作次数提示 |
| `conversation.panel.right` | `Iframe` 网页预览实例 `iframe-preview`，与原生内容互斥 |
| `conversation.turn.content.after` | 已结束助手轮次的状态快照 |
| `conversation.turn.footer.actions` | “刷新本轮 / Refresh round”和“本轮详情 / Round details” |
| `conversation.header.title.after` | 工作台状态 Tag |
| `conversation.header.actions` | 刷新当前会话工作台的按钮 |

登录页只包含一个图标和同排的“登录”“退出”两个按钮。两个按钮不声明事件，不调用接口，也不维护登录状态。

任务区提供筛选和结果：`Input` 支持中文输入与回车应用，`Select` 支持搜索、选择与清空，`TreeSelect` 支持项目选择与清空，`Slider` 支持分数阈值（步进 5），`Switch` 支持自动摘要开关，`Button` 触发应用与重置。

轮次位置的 `instance_key` 来自实际 `requestId`。首次挂载保存该轮快照；后续 ready 恢复快照。其他轮次和其他会话的修改不会覆盖它；点击“刷新本轮”才把当前筛选摘要写入该轮。

## 重置、修改与卸载

点击“重置 / Reset”恢复当前会话的筛选和演示开关，保留操作计数和已有轮次快照。插件数据仅存于扩展激活实例内存；进程重启或成功重载后重新初始化。

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

manifest `schemaVersion: 1`、`ui.apiVersion: 1` 与渲染帧 `schema_version: 2` 含义不同。本包使用 `events['ui.click']` 等 v2 事件；业务参数位于声明动作的 `args`，控件值位于规范事件的 `data`。dispatch 响应仅是回执，不能作为新树。通用版本策略与校验规则见 [F_08](../../docs/features/F_08_frontend-render.md)。

所有位置均使用真实 `session_id`，菜单和设置尚不支持无会话渲染。右侧贡献使用稳定实例 `iframe-preview`，事件和更新按实例隔离；其余 single slot 本包各创建一个实例。`message.assistant.meta` 与旧 `applicationPlugins` 不属于本包目标。

网页实例使用 `Iframe`，只能作为渲染树根，不能嵌套在 `Panel`、`Box` 或其他组件内，也不接受 children、events 或 srcdoc。示例只下发标题与 URL：

```json
{"component":"Iframe","props":{"title":"网页预览 / Web preview","src":"https://example.com"}}
```

宿主固定设置 `sandbox="allow-scripts allow-forms"`，插件不能放开同源权限、弹窗或顶层导航。外部站点还需允许浏览器嵌入，`X-Frame-Options` 或 CSP `frame-ancestors` 可能阻止显示。

## 手动测试建议

安装并查询 → 真实会话首次 ready → 菜单主页面 → 控件操作及辅助区域回显（中文输入、Select 搜索/清空、TreeSelect、Slider、Switch）→ 第二会话与两个真实轮次隔离 → 右侧网页页签加载 example.com → 重新挂载恢复 → 重载、卸载及重连。

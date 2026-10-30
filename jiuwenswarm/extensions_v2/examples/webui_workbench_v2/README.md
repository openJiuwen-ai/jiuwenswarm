# 插件交互工作台

[English](README_EN.md)

`webui.workbench-v2` 演示 12 个已登记的 WebUI slot（共 13 项贡献，右侧面板包含两个实例），覆盖 `FileTree`、`FilePreview`、`Panel`、`Iframe`、`Switch`、`Button`、`Tag`、`Box`、`Text` 等目录组件。侧栏主页面与任务检查主页面均使用 `Iframe` 根加载示例页面，右侧面板同时提供工作区文件页签（`Panel` 根，内含 `FileTree` 与 `FilePreview`）和网页预览页签（`Iframe` 根）。插件保存会话状态及平台返回的文件数据，通过下行完整树更新界面；文件组件自行处理展开、选中和提示文案。示例不调用模型或外部业务 API。

## 安装与启用

前提是运行包含本次 WebUI 协议和生产装配改动的 Gateway、AgentServer 与 Web 前端。需要从源码打包或部署时，可在仓库根目录执行 `python scripts/install_workbench_plugin.py`（或 `--zip-only` 仅生成 `webui_workbench_v2.zip`）。

将生成的 ZIP 放在运行 AgentServer 的机器可读取的位置，然后在 Web 单 Agent 模式的输入框执行，例如：

```text
/extension install /path/to/webui_workbench_v2.zip
/extension query webui.workbench-v2
```

已有同 ID 的旧版本时，先执行 `/extension uninstall webui.workbench-v2`，成功后再安装新 ZIP 并查询。安装成功即激活，无需另执行 enable。服务启动只加载安装目录，不扫描源码中的 `examples/`。管理命令可以在新建空对话使用，但插件渲染仍需要真实会话。

安装完成后进入已有真实会话，刷新页面或重新进入会话，让 Host 发送 `ui.ready`。首次 ready 即可生成演示树；再次 ready 恢复当前激活实例的任务状态。文件实例 ready 时重新读取根目录并清空预览；网页实例 ready 时重新发布 Iframe。

## 访问与操作

| 位置 | 内容及访问方式 |
| --- | --- |
| `application.menu` → `application.menu.main` | 点击侧栏“交互工作台 / Workbench”打开主页面（`Iframe` 根）；两处实例均为 `workbench` |
| `task.function` → `task.function.main` | 点击任务区“任务检查 / Inspect tasks”打开任务主页面（`Iframe` 根）；两处实例均为 `task-inspector` |
| `application.settings` | 设置内容区域显示“自动摘要 / Auto summary”开关，状态属于当前会话 |
| `conversation.input.above` | 筛选摘要与最近操作结果 |
| `conversation.input.below` | 紧凑操作次数提示 |
| `conversation.panel.right` | “工作区文件”（`Panel` 根，实例 `workbench`）与“网页预览”（`Iframe` 根，实例 `iframe-preview`）两个页签，与原生内容互斥 |
| `conversation.turn.content.after` | 已结束助手轮次的状态快照 |
| `conversation.turn.footer.actions` | “刷新本轮 / Refresh round”和“本轮详情 / Round details” |
| `conversation.header.title.after` | 工作台状态 Tag |
| `conversation.header.actions` | 刷新当前会话工作台的按钮 |

轮次位置的 `instance_key` 来自实际 `requestId`。首次挂载保存该轮快照；后续 ready 恢复快照。其他轮次和其他会话的修改不会覆盖它；点击“刷新本轮”才把当前筛选摘要写入该轮。

## 右侧面板：工作区文件与网页预览

同包在右侧声明两个贡献：`workbench.details` 使用实例 `workbench`，根节点为 `Panel`；`workbench.iframe` 使用实例 `iframe-preview`，根节点为 `Iframe`。右侧 ready 未指定实例时，插件一次发布两个独立实例；指定有效实例时，只恢复该实例。宿主根据根节点标题生成页签。

文件页签使用 `Panel(title, children)`，插件用一个 `Box` 并排组合 `FileTree` 与 `FilePreview`，仅提供目录数据、结构化状态和预览正文。两个文件组件各自注册，可以由其他插件单独使用。组件与平台接口见 [F_07](../../docs/features/F_07_ui-contributions.md#44-文件能力目录与预览)。

单 Agent 聊天和代码会话均可浏览文件。有项目时显示会话已绑定项目根，无项目时显示已绑定任务根（包含 `work/` 和 `outputs/`）。团队模式显示不支持提示；未取得有效绑定时显示工作区不可用。

文件实例收到 `ui.ready` 时调用 `ops.session.files` 读取根目录并清空预览；展开目录时组件通过 `ui.change` 提供 `expandedKeys`，插件将 key 映射为路径后再次调用 `ops.session.files`。每层最多 100 项、总共 512 项、最多 8 层、96 KiB 预算。选择文件时，插件调用 `ops.session.read_file` 读取单文件 UTF-8 文本（上限 16 KiB），并交给 `FilePreview` 展示；二进制、图片或超大文件返回对应状态提示。

网页实例使用 `Iframe` 根，只能作为允许该根组件的槽位（`conversation.panel.right`、`application.menu.main`、`task.function.main`）的渲染树根，不能嵌套在 `Panel`、`Box` 或其他组件内，也不接受 children、events 或 srcdoc。宿主固定设置 `sandbox="allow-scripts allow-forms"`。

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

所有槽位均使用真实 `session_id`。右侧贡献使用两个稳定实例，事件和更新按实例隔离；其余 single 位置按最近更新选择实例。`message.assistant.meta` 与 `application.entry` 不属于本包目标。

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

# 完整系统插件示例

示例在 [assets/package_example/greeting_package/](../assets/package_example/greeting_package/)，用于理解现有事件、系统插件加载和安装验证。
它面向具备 WebUI 能力的 Swarm，其他宿主或业务按实际契约开发。

需要参考时，先按文件索引定位系统插件声明、Handler 或某项测试，再读取对应片段；不默认展开全部示例源码和测试。执行示例测试无需先阅读所有测试实现。

## 1. 事件与效果

系统插件 ID 为 `example.greeting-panel`。输入区上方槽位产生 `ui.ready` 后，Handler 继续默认处理，再调用 `ops.ui.render` 显示“你好，系统插件已生效”。

- **复用能力：** 已有 `ui.ready`、`ops.ui.render` 和 `conversation.input.above` 槽位。
- **默认动作：** 沿用下游结果，默认拒绝时不渲染成功界面。
- **业务范围：** 不新增平台事件、Ops、页面或触发代码，也不通过模型生成问候语。

| 文件 | 职责 |
| --- | --- |
| [manifest.json](../assets/package_example/greeting_package/manifest.json) | 系统插件身份、运行域、代码入口和 UI contribution |
| [greeting/extension.py](../assets/package_example/greeting_package/greeting/extension.py) | 工厂、Registrar 登记和 USER Handler |
| [README.md](../assets/package_example/greeting_package/README.md) | 普通使用者的安装、操作和验证说明 |
| [test_package.py](../assets/package_example/test_package.py) | 正式打包 ZIP 的真实加载、渲染、重载、卸载，以及宿主 CLI 到临时宿主的验证 |
| [test_build_package.py](../assets/package_example/test_build_package.py) | manifest、ZIP 布局、排除规则和覆盖行为 |

## 2. 运行隔离验证

先按 [执行环境说明](package-workflow.md#15-复用宿主的执行环境) 选择宿主已有的 Python 或桌面后端，记作 `<PYTHON_ENTRY>`。
使用同一入口运行示例测试；PowerShell 调用时在命令前加 `&`：

```text
"<PYTHON_ENTRY>" "<SKILL_ROOT>/scripts/run_tests.py" "<SKILL_ROOT>/assets/package_example"
```

测试范围分别说明：

- **系统插件行为：** Core 加载器、Swarm 门面、WebUIRuntime、Handler 和 Ops 保持真实，安装目录使用临时目录。
- **传输内容：** 页面下行接到内存记录器，检查实际渲染帧，不声称浏览器已经显示。
- **客户端链路：** 通过宿主 CLI 和本机临时管理通道，检查最终 ZIP 被真实加载并产生问候语；协议失败覆盖由平台 CLI 测试维护。
- **运行环境：** 测试不会安装到用户正在使用的服务，也不代替真实页面验收。

## 3. 打包并安装示例

先生成最终 ZIP，输出位置放在系统插件源码目录之外：

```text
"<PYTHON_ENTRY>" "<SKILL_ROOT>/scripts/build_package.py" "<SKILL_ROOT>/assets/package_example/greeting_package" "<输出目录>/greeting-package.zip"
```

用户同意安装后，按 [安装与状态核对](package-installation.md) 选择入口，通过 `bash` 执行；源码、Wheel 示例：

```text
jiuwenswarm extension install "<输出目录>/greeting-package.zip"
jiuwenswarm extension query example.greeting-panel
```

安装后继续验证：

1. 核对报告成功、当前系统插件已激活，以及实际安装目录。
2. 安装报告中的页面通知会让已就绪的槽位重新发送 `ui.ready`；不为了证明显示效果而启动或查找浏览器。
3. 使用当前环境提供的提问能力，请用户确认输入区上方的实际效果。

已有同 ID 的系统插件时，先确定更新方式，不直接重复安装。
普通使用者可以按系统插件 README 在 Web 输入框执行 `/extension` 命令，CLI 未完成安装时不能将人工教程当作自动安装完成。

## 4. 改成用户需要的系统插件

- **业务选择：** 重新确定系统插件名称、模块名、已有事件、匹配条件和实际处理，不只替换问候文案后就声称支持其他业务。
- **真实字段：** 示例 Text 使用 `props.value`，系统插件身份来自 `ops.package_metadata`，会话来自 `ops.session_context`。
- **返回结果：** 保留合法输出和默认动作次数，检查真实 `UiRenderOutput` 状态。
- **测试与文档：** 按本系统插件实际行为补测试、README 和安装后的验收步骤。

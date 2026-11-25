# 系统插件示例索引

先按需求选择最接近的一个示例，阅读它的 README、manifest 和下表指定的入口；其余模块只在实现用到时再读。
不要一次加载全部示例、素材和测试。可以复用已验证的结构，只为本次新增行为补查事件和 Ops。

| 用户需要 | 示例与运行域 | 优先读取 | 可参考的能力 |
| --- | --- | --- | --- |
| 桌宠、窗口形态、资源导入和设置 | [应用形态](../assets/examples/application-forms/README.md)，gateway | `application_forms/extension.py`，需要资源格式时再读 `RESOURCE_FORMAT.md` | ApplicationSurface、应用形态槽位、资源设置、持久化和音效 |
| 上下文用量、Agent 执行过程和工具批次 | [上下文驾驶舱](../assets/examples/context-observer/README.md)，agentserver | `context_observer/extension.py`，再按需读 `loop.py`、`dashboard.py` | context.usage.updated、turn.step、tool.call、ops.session、受管页面资源 |
| 登录、注册、凭证注入和账号入口 | [认证与登录](../assets/examples/login/README.md)，gateway | `manifest.json` 的 client 段、`auth_login_demo/extension.py`、`identity.py` | gateway.request、application.entry、客户端存储和凭证绑定；要求 Swarm >=0.2.10，仅演示自有认证 |
| 团队成员、任务状态和依赖关系图 | [团队协作](../assets/examples/team-collaboration/README.md)，agentserver | `team_collab_viz/extension.py`、`snapshot.py`；页面问题再读 `web/` | team.monitor_event、ops.team、会话绑定、只读本机页面服务及释放 |
| 轻量用量状态条、设置开关和详情 | [Token Weather](../assets/examples/token-weather/README.md)，agentserver | `token_weather_glass/extension.py`、`views.py` | 真实用量、显示偏好、动态页面资源；比驾驶舱更适合参考设置与状态条联动 |
| 动态背景、配色、开场动画和主题导入 | [主题背景](../assets/examples/theme/README.md)，gateway | `workswarm_theme/extension.py`、`media.py`；导入时再读 `schema.py`、`transfer.py` | MediaSurface、application.background/splash、ops.appearance、ops.splash、数据存储 |
| 一个功能同时改变会话行为和应用外观 | [Harness 工作台](../assets/examples/harness-workbench/README.md)，两个域 | 根 manifest，再按域读 `plugins/` 中的 manifest 与 `extension.py` | 组合包、Harness 选择、首次消息改写、专家准备、会话 UI 和 Gateway 主题 |

六个单域例子分别保留不同业务能力；已有的问候示例用于参考最小真实加载和生命周期测试，见下文。
涉及网页权限先查 [前端开发说明](frontend-development.md)，同时需要两域时再查 [组合包开发](bundle-development.md)。

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

## 5. 示例验证与适用范围

[隔离验证](../assets/examples/test_examples.py) 从打包文件安装到临时 AgentServer 或 Gateway，检查 Handler 发布和实际下行帧。
Harness 工作台还检查两域共同安装激活，以及选择 Codex 后，新会话首条消息带上对应专家；不启动 Claude Code、Codex 或模型调用。
组合包整体安装测试要求目标 Gateway 已支持 `sync_container_members`，按目标仓库锁定版本核对依赖，不用旧环境的失败推断平台缺少组合包能力。
页面传输使用内存记录器，未把协议验证写成真实桌面窗口、摄像头或第三方网页已经验收。

- **主题背景：** 从 2.0.0 示例适配为 2.0.1，使用独立 Gateway 的导入路径。
- **团队协作：** 从 1.0.0 示例适配为 1.0.1，删除旧位置的展示字段，保留顶层 display。
- **组合包：** 使用提供的 Harness 工作台 1.0.0 示例，适配为 1.0.1；修正异步专家目录调用，补全内置专家 README，并缩短 persona 文件名以兼容 Windows 路径。
- **登录示例：** 保留 >=0.2.10 的兼容性要求；隔离测试声明这一目标版本并验证对应实现，不代表较旧发行版可以安装。

只运行当前需要的用例；查看具体断言时再打开测试文件。示例只是参考，开发新系统插件仍需核对目标环境和自己的业务行为。

# 输入区问候面板系统插件

本系统插件在已有会话的输入区上方显示“你好，系统插件已生效”。槽位挂载产生 `ui.ready` 后，通过已有 `ops.ui.render` 返回后端问候语。

## 1. 运行条件

- **宿主能力：** JiuwenSwarm 已启用 Extensions V2 与 WebUI，提供 `ui.ready`、`ops.ui.render` 和 `conversation.input.above` 槽位。
- **系统插件身份：** package_id 为 `example.greeting-panel`，版本为 `1.0.0`，运行域为 `agentserver`。
- **界面前提：** 已就绪页面收到安装通知后会重新发送 `ui.ready`；通知只表示页面收到更新，不代替用户确认效果。
- **验证范围：** 相邻测试验证系统插件加载、CLI 安装链路和下行帧；界面是否符合预期由用户在确认卡片中判断。

本系统插件不发起模型调用，也不新增平台事件或页面入口。具体支持范围以目标环境检查和实际测试结果为准。

## 2. 安装与验证

取得由开发 skill 正式打包的最终 ZIP 后，在 Web 单 Agent 输入框执行：

```text
/extension install "<ZIP 文件的绝对路径>"
/extension query example.greeting-panel
```

按以下步骤确认结果：

1. 核对安装报告成功、系统插件已激活，以及查询返回的实际安装目录。
2. 安装报告通知已就绪页面重新发送 `ui.ready`；不要求开发流程另行启动或查找浏览器。
3. 在确认卡片中请用户确认输入区上方是否出现问候语。

安装时注意：

- **路径位置：** ZIP 必须对运行服务可读；远程部署先把文件传到运行侧。
- **已有版本：** 同 ID 的系统插件已存在时先确定更新方式。首次安装后收到修改意见时，更新安装副本并 reload，不卸载后重新安装。
- **安装选择：** 使用本开发 skill 的 agent 在用户同意安装后，通过宿主提供的 `extension` 管理命令安装；普通使用者按上面的 Web 命令即可。
- **效果区别：** 安装成功表示系统插件已加载并激活；只有第 3 步完成后，才能说明页面显示已经验证。

界面没有更新时，检查宿主版本、槽位 contribution、`ui.ready` 上报和渲染错误，不通过发送模型提示替代验证。

## 3. 修改与热重载

在查询返回的安装目录中，修改 `greeting/extension.py` 的 `GREETING`，再执行：

```text
/extension reload example.greeting-panel
/extension query example.greeting-panel
```

- **重载内容：** reload 读取安装副本，修改最初源码目录或 ZIP 不会自动改变它。
- **验证方式：** reload 成功后重新显示确认卡片，由用户确认新文本。
- **源码维护：** 后续开发继续维护源码目录，修改后重新测试和打包，不把安装副本当作唯一源码保存位置。

## 4. 卸载

```text
/extension uninstall example.greeting-panel
/extension query example.greeting-panel
```

核对系统插件不再激活、对应面板撤销，并按需要检查资源清理结果。

## 5. 开发说明

- **入口：** `greeting/extension.py` 的 `create_extension()` 创建入口对象，通过 Registrar 将 `on_ready` 登记为 USER Handler，仅匹配目标槽位。
- **身份与会话：** Handler 继续默认处理后，使用可信的会话和系统插件身份渲染。
- **失败与重复：** 默认拒绝或渲染失败时返回相应结果；重复 `ui.ready` 更新同一实例，不积累重复面板。
- **测试：** 修改本系统插件时补充相邻测试，使用正式打包 ZIP 验证实际帧内容、未命中、会话隔离、失败、取消、重载和卸载。

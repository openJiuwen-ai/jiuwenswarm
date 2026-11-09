# 插件设置 location 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_18 |
| 最近修订 | 2026-11-07 |
| 关联特性 | [F_32](../features/F_32_settings-location.md)、[F_33](../features/F_33_token-weather-glass.md) |

## 1. 范围与职责

AgentServer 域活动插件通过原 application.settings 声明设置菜单、分类、配置项。宿主负责统一设置树、位置解析、访问策略和外层布局；插件提供已有 WebUI 树和事件读写。Gateway 域插件使用同一套声明，配置项须显式为 application 范围；Gateway 在本目录的应答中追加其包的声明，并只认领自己包的实例，见 [Gateway S_10 第 2 节](../../../gateway/extensions_v2/docs/specs/S_10_application-settings.md)。

## 2. 声明与标识

manifest ui.items 使用 type=web.slot.contribution、slot=application.settings、局部 id 和 location。kind 为 menu/section/item，省略默认 item。id 匹配 `[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}`，同包 UI 声明中唯一。

| kind | location 必需字段 | 内容 |
| --- | --- | --- |
| menu | 无 | title、白名单 icon；不能有 renderer、scope、instanceKey |
| section | menu | title；不能有 renderer、scope、instanceKey |
| item | menu、section | renderer={kind:webui-tree}；可选 title、description、scope、instanceKey |

title/description 使用 zh/en 字典，两种语言均须非空，每项 UTF-8 最长 1024 字节。description 必须伴随 title。item 提供 title 时宿主使用标准标签/描述/控件行；省略 title 时将完整组件树放入共享 SettingRow.content，插件保留已有标签布局。菜单、分类必须有标题。

item 的 scope 为 session/application，默认 session。instanceKey 默认声明 id，可显式填写非空、无首尾空白、无竖线且 UTF-8 不超过 200 字节的既有实例标识。同包、同 scope 的配置项不能重复使用一个 instanceKey。

节点公开 ID 为 `<package_id>::<id>`。宿主节点沿用 registry ID，例如 general 菜单下的 general/display 分类。全部有效菜单先注册，再解析分类和配置项；父级可由其他活动插件提供。无配置项的分类、菜单按宿主现有规则隐藏。

## 3. 排序与失败

- SL-01：location 可附加 before 或 after，二者互斥，指向同一父级中的稳定 ID；未指定时追加。
- SL-02：父级存在而锚点缺失或属于其他父级时追加。父级不存在报告 PARENT_MISSING，只阻止相关贡献及依赖它的后代，不选择其他父级。
- SL-03：原生同级顺序不变。插件先确定原生节点间隙，再对同间隙相对顺序做稳定拓扑排序。输入按包 ID 及包内声明顺序排列；重复表达同一顺序不构成环。
- SL-04：排序环及受其阻塞的贡献报告 ORDER_CYCLE；重复节点报告 DUPLICATE_ID，保留原生节点。不忽略有效约束来消除环。
- SL-05：只添加节点，不替换宿主配置或修改访问策略。隐藏和只读逐级收紧。

## 4. 查询与渲染

webui.settings.catalog 为 Web 连接的只读请求，参数为空，无需 session。Gateway 取 AgentServer 的 `{revision, items, errors}` 后追加 Gateway 域包的声明（没有时原样返回），revision 为公开目录的 SHA-256。items 来自 application.settings 声明，包含包身份及归一化元数据，不泄露安装路径。item 元数据包含 scope、instance_key 和 presentation=row/content；菜单、分类没有渲染目标。

所有控件都使用 application.settings，没有另一个设置承载槽位。宿主按目录的包、instance_key 和 scope 挂载与发送 ui.ready；上行适配器拒绝未声明实例或不匹配的范围。session 项使用真实活动会话，application 项 session_id 为空且按可信连接投递。application.settings 允许这两个范围；其他槽位的固定范围不改变。

前端进入设置、重连及 focus 时查询。过期响应不能覆盖新查询，相同 revision 保持控件身份。失败明确提示，原生设置仍可访问，不把失败当作空目录。session 项在无活动会话时不加入可见树；切换会话、菜单与清理遵循对应范围的挂载生命周期，不清理其他范围。

配置值由插件处理；客户端展示布尔偏好通过 Switch.preference 读写，显示使用实例级 UiRenderInput.visible_when，契约见 [S_17 §2.4](S_17_webui-component-props.md#24-渲染实例的显示条件)。设置开关不绑定状态条实例的显示条件。

## 5. 迁移与验证

保留既有会话范围配置项时，manifest 需补 location，并可用 instanceKey 对应已有处理器实例。工作台在统一入口迁移后已按用户要求移除天气演示及其设置项；独立 Token Weather 继续声明 application.settings、显式 application 范围。新声明不接受旧的独立设置类型或承载槽位；无 location 的旧设置声明需补齐位置。

验证入口：前端 tests/pluginSettings.test.mjs、tests/webuiComponentsReact.test.mjs，以及后端 tests/unit_tests/extensions_v2/webui/test_token_weather_glass.py。执行结果及限制统一维护在 F_31，不将组件重放预览认定为真实模型端到端验证。

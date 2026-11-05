# WebUI 组件 Props 权威规约

| 项目 | 约定 |
| --- | --- |
| 维护范围 | 插件侧声明、AgentServer / Gateway 后端校验、Web 前端校验与渲染 |
| 最近修订 | 2026-11-05 |
| 适用协议 | `webui-tree`，`schema_version=1`；本文件修订不等于自动升级 wire 版本 |
| 相关设计与验证记录 | [F_01 组件目录与渲染适配](../../../channels/web/frontend/src/features/webui/docs/features/F_01_antd-components.md) |
| 相邻行为契约 | [S_01 组件运行与交互](../../../channels/web/frontend/src/features/webui/docs/specs/S_01_components.md) |

**P-01：本文件是 WebUI 组件属性的唯一权威规范。** 插件构造渲染树、后端接受渲染与交互、前端解析和显示节点，都必须遵循本文件。
Ant Design 文档、示例插件、前后端规则表和已经安装的插件副本都不能独立定义另一套属性。
后端负责最终拒绝非法输入，但“后端负责校验”不代表后端可以脱离本文件修改契约。

当前字段表覆盖前端登记的 24 个组件（包括 Icon、Form、Form.Item、MediaSurface）。
AgentServer 支持其中的 21 个组件；Gateway 使用独立校验器，支持 Form / Form.Item、MediaSurface、Button.form、
Box 扩展布局、Input.autoComplete、Image.fit 和 Dropdown 富菜单，暂不支持独立 Icon 节点和 Dropdown.icon。
Gateway 与 AgentServer 共同支持的属性使用相同名称和语义；宿主支持范围不同不代表可以恢复旧版 Button、Text 或 Avatar 属性。
本文是人工维护的规范，当前没有从本文自动生成规则的能力。修改本文后，开发者或编码代理必须按第 7 章完成实现、调用方和验证同步；只改本文不代表运行行为已经改变。

## 1. 三端职责与维护顺序

| 端 | 必须遵循的内容 | 实现位置 |
| --- | --- | --- |
| 插件侧 | 组件名、字段名、类型、必填项、嵌套结构、事件声明；不得向宿主传 React 组件或任意 HTML 属性 | `RenderNode`、插件视图构造函数、事件 Handler 和安装包 |
| 后端 | 同样的属性与事件规则；在登记实例、分配有效版本和执行 Handler 前校验 | `component_rules.py`、`schema.py`、`action_validation.py` |
| 前端 | 同样的规则和默认值；按规范映射组件，并且只发送节点已声明的事件 | `catalog`、`protocol`、`renderer`、`runtime`、页面宿主 |

后续增加、删除、重命名 props 或改变类型、枚举、默认值、事件数据时，必须遵循以下顺序：

1. 先修改本文件的字段表、组件约束和相关功能清单。写明旧写法如何迁移，是否保留别名。
2. 按第 6、7 章核对三端实现；新增组件还要登记目录、渲染器和测试。
3. 更新仓库中的示例、共享用例及用户文档，核对已安装插件是否需要更新副本或重装。
4. 运行与改动相关的验证，并在 F_01 记录范围、结果和未完成项。未验证的运行环境不能写成已生效。

**P-02：当用户要求“修改权威文件”时，默认包含实现同步。** 用户明确要求只改文档时除外。
实现与本文不一致时，应修正实现或明确登记尚未完成的同步项，不能为了让现有代码通过而偷偷修改规范。

## 2. 通用规则

### 2.1 节点、props 与 children

节点只允许 `component`、`props`、`children`、`events`。`component` 大小写敏感，必须使用第 4 章中的名称。
**P-03：所有 props 对象和嵌套结构均不接受表外字段。** 没有明确允许 `null` 的字段，不得用 `null` 代替省略。

| 字段 | 内容 |
| --- | --- |
| `component` | 已登记组件名，例如 `Button` |
| `props` | 组件字段表规定的对象；省略表示空对象，仍须满足必填项 |
| 节点级 `children` | 子组件节点数组，只用于允许嵌套的组件 |
| `events` | 第 5 章允许的事件绑定；不声明时省略，声明时必须为非空对象 |

`Button.value` 和 `Text.value` 是文案；二者不接受 `props.children`、`label` 或节点子组件。
`Tag.props.children`、`Divider.props.children`、`Avatar.props.children` 是各自声明的字符串属性，继续保留。
`Tabs.props.items[].children` 是页签内容节点，`TreeSelect` / `FileTree` 数据项里的 children 是各自的数据子项，不能混为一类。

Python `RenderNode.children=()` 是模型默认值，序列化时省略；直接构造 JSON 时，叶子组件应省略节点级 children，不能传 `children: []`。
不接受任意 `className`、`style`、`onClick`、`srcdoc` 或 ReactNode；Avatar 仅开放第 4.19 节登记的三个 style 字段。
宿主的 `appearance`、主题 token、连接状态和本地交互状态也不是插件 props。

### 2.2 基础类型、尺寸与默认值

| 类型或记号 | 约束 |
| --- | --- |
| `string` | 合法 Unicode；默认不超过 UTF-8 16 KiB；空字符串是否有效由组件约束决定 |
| `LocalizedText` | string 或完整 `{zh: string, en: string}`；客户端按当前界面语言选择，各语言分别受文本上限约束 |
| `LocalizedText` | string 或 `{zh: string, en: string}`；两项必填、无额外键，每项最多 16 KiB，随宿主界面语言切换 |
| `number` | 有限数值，不接受 NaN、Infinity 或布尔值 |
| `integer` | 满足表中范围的整数，不接受布尔值 |
| `boolean` | 只接受 true / false |
| `T[]` | 同类型数组，默认每个数组不超过 100 项；对象默认不超过 100 个字段 |
| `Size` | 推荐 `small / medium / large`；兼容 `sm / md / lg`，前端准备阶段转换为推荐值 |
| `CompactSize` | `small / medium / sm / md`；Switch、Card 不接受 large / lg |
| `InputVariant` | `outlined / borderless / filled / underlined` |
| `InputStatus` | `error / warning`；省略表示正常 |
| `OptionValue` | 非空字符串或有限数值；同一 Select 的选项值类型必须相同 |
| `Selection` | 单选为 OptionValue 或 null；多选 / tags 为 OptionValue 数组 |
| `TreeSelection` | 单选为字符串或 null；多选 / 勾选为字符串数组 |
| `IconName` | [图标白名单](../../../channels/web/frontend/src/features/webui/icons/whitelist.json) 的 icons 键和 aliases 键，包括 refresh |
| `ButtonColor` | default / primary / danger，以及 blue / purple / cyan / green / magenta / pink / red / orange / yellow / volcano / geekblue / lime / gold |
| URL | 相对宿主地址或 HTTP(S) 地址；不接受 javascript、data、file 等协议；Iframe、MediaSurface 另有更严格规则 |

属性表中的“默认值”只列目录明确补入的值。“—”表示目录不补值，不表示强制传 null；组件自己的显示回退在表后说明。
**P-04：`value` 优先于 `defaultValue`，`checked` 优先于 `defaultChecked`。** 显式空字符串、false、null 和空数组不得被真值判断误当成缺省。

通用规模限制：整帧不超过 256 KiB，组件树不超过 8 层 / 512 个节点；props 嵌套不超过 24 层。
Tabs 隐藏页签的节点也计入整树预算。更小的组件字段限制优先。

### 2.3 子节点和槽位

| 组件 | 节点级 children | 限制 |
| --- | --- | --- |
| Box、Card | 允许 | 普通组合布局 |
| Panel | 必须非空 | 只能作为树根；不能嵌套其他 Panel / Iframe / MediaSurface |
| Divider | 允许 | 只能包含 Text；不能同时提供 props.children；vertical 时不能包含非空标题 |
| Dropdown | 与 icon 二选一 | 自定义触发区恰有一个展示型子树；Gateway 只支持此方式，其他宿主提供 icon 时不得再提供子节点 |
| Form | 必须非空 | Gateway 页面使用；仅包含 Form.Item，不允许嵌套 Form |
| Form.Item | 必须一个 | Gateway 页面使用；必须位于 Form 内，仅包含一个不声明上行事件的 Input |
| MediaSurface | 不允许 | Gateway 应用背景使用；只能作为树根 |
| Tabs | 不允许 | 内容必须放在 items[].children |
| 其余已实现组件 | 不允许 | 包括 Button、Text、Iframe、文件组件和媒体组件 |

Panel / Iframe 只能作为允许其使用的槽位的树根。
`conversation.panel.right` 的根白名单为 Panel / Iframe，二者的 title 同时作为右侧页签名称。
`application.menu.main` 和 `task.function.main` 允许 Iframe 专用根，也允许普通组件树。
`task.function` 与 `task.function.main` 使用应用作用域，按当前 Web 连接隔离，不携带 `session_id`；
空对话也可挂载，切换会话不改变实例归属或关闭已打开的任务页面。入口与页面共享包和实例标识。
切到聊天时，当前应用范围任务页面仅隐藏、保留 iframe；返回同一任务入口不重建页面。
MediaSurface 只能作为 Gateway 槽位 `application.background` 的树根，该槽位也只接受 MediaSurface 根。
其他挂载位置和作用域见 [catalog/slots.ts](../../../channels/web/frontend/src/features/webui/catalog/slots.ts)、[后端 constants.py](../../webui/constants.py) 与 [F_07 槽位与交互](../features/F_07_ui-contributions.md)。
槽位容量、页面导航和插件启停由宿主控制。Button.openPanel 仅表达显式点击后的同包、同会话面板打开意图，不绕过槽位声明、实例存在和生命周期校验。

### 2.4 渲染实例的显示条件

`UiRenderInput.visible_when` 是可选的仅关键字参数，与 root 同级，不属于任何组件 props。replace 帧的可选 `visible_when` 使用 PreferenceBinding；缺省表示该实例始终可见。只接受 key/default，key 去空白非空且最多 128 UTF-8 字节，default 为布尔值；不能声明包身份，实际命名空间来自可信 target.package_id，绑定要求该身份非空。wire 不接受 null；clear 请求不允许附带显示条件。

显示条件与每次 replace 一起完整替换，省略即移除之前的条件；后端快照、下行预算、失败回滚均包含它。客户端按实例身份保存，不按标题或包内槽位猜测实例。宿主依据该条件控制外层展示：right 对应 Tab，普通槽位对应实例容器；内部组件不感知此条件。隐藏移除容器和浏览上下文，保留运行时实例和后台观测。当前选中项隐藏后回到已有原生页签，重新开启不自动选中，也不改变用户手动关闭页签的状态。设置开关可独立保留，不自动隐藏同包所有贡献。

已隐藏的源或目标不能产生打开面板请求。存储读取失败由实例错误边界呈现，不当作关闭偏好。客户端偏好控制显示统一使用实例级 visible_when；Box 和 Iframe 均不接受 visibleWhen 属性，也不保留组件级显示偏好的别名。Box.visibleFrom 继续按响应式断点控制子树活动状态。

## 3. 嵌套数据结构

以下对象同样拒绝表外字段。带“必填”的字段必须存在。

| 结构 | 全部字段 | 附加约束 |
| --- | --- | --- |
| `ButtonLoading` | delay: number（可选） | Button.loading 可为 boolean 或此对象；当前没有要求 delay 非负或为整数 |
| `PreferenceBinding` | key: string（必填，<=128 UTF-8 字节）；default: boolean（必填） | key 去空白后非空；包身份由宿主提供，偏好只作用于当前客户端、同包所有会话 |
| `SelectOption` | value: OptionValue（必填）；label: string（必填）；disabled: boolean | values 唯一，不能混合字符串和数值；tags 的 value 必须为字符串 |
| `TreeOption` | value: string（必填）；title: string（必填）；key: string；disabled / disableCheckbox / selectable / checkable: boolean；children: TreeOption[] | value / 实际 key 非空且全树唯一；省略 key 时取 value；最多 8 层 / 512 项 |
| `TabItem` | key: string（必填）；label: string（必填）；disabled: boolean；children: RenderNode[] | key 非空且唯一；内容节点按普通树遍历、定位和计数 |
| `DropdownItem` | key: string（必填）；label: string / RenderNode[]（必填）；type: item；disabled / danger: boolean | key 去空白后非空且唯一；label 字符串去空白后非空，节点数组非空。type 与节点数组仅在 Gateway 支持；items 非空 |
| `DropdownDivider` | type: divider（必填）；key: string | Gateway 分隔项；显式 key 去空白后非空且与普通项一起检查唯一性；不可选择 |
| `DropdownAlign` | points: string[]（必填）；offset: number[] | Gateway 定位；points 恰有两个 tl/tc/tr/cl/cc/cr/bl/bc/br 锚点；offset 提供时恰有两个 -128..128px 偏移 |
| `FormRule` | required: boolean；min / max: integer >= 0；pattern / message: string | 至少包含 required/min/max/pattern 之一；min <= max；pattern 为有效正则；提供 message 时去空白后非空 |
| `FileTreeNode` | key / title / path: string（必填）；kind: file / directory（必填）；children: FileTreeNode[]；loaded / truncated: boolean | key 非空且唯一，path 唯一；目录数据最多 8 层 / 512 项；file 不接受 children / loaded / truncated；loaded=false 时不得提供 children |
| `PreviewFile` | name / path: string（必填）；mimeType / content / downloadUrl: string；size: integer >= 0 | name 去空白后非空；至少提供 content 或 downloadUrl；downloadUrl 遵循 URL 规则 |
| `ImagePreview` | src: string（必填） | 预览原图地址，遵循 URL 规则 |
| `EventBinding` | id: string（必填）；args: JSON 对象 | id 去空白后非空且 <= 200 UTF-8 字节；args 省略补 {}，不可为 null，序列化后 <= 8 KiB |

文件 path 必须是工作区相对路径：用 `/` 分隔，不能以 `/` 开头，不能含反斜杠、冒号、NUL、空段、`.` 或 `..`。
FileTree 子项路径必须以父目录路径加 `/` 为前缀。
`PreviewFile.content` 即使表示文件正文，也仍受单字段 16 KiB 限制；大正文应由受管 downloadUrl 提供。

## 4. 组件属性完整清单

本章各表是该组件的全部顶层 props；表后列出依赖字段、交互和显示行为。
“必填”不包含子节点要求，子节点另见第 2.3 节。第 5 章列出事件 data 的完整结构。

### 4.1 Box

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `direction` | `row` / `column` | 否 | `"row"` | 子节点排列方向 |
| `gap` | `none` / `sm` / `md` / `lg` / number >= 0 | 否 | `"md"` | 子节点间距档位；数值为 Gateway 页面使用的 px 间距 |
| `align` | `stretch` / `start` / `center` / `end` / `baseline` | 否 | `"stretch"` | 交叉轴对齐 |
| `justify` | `start` / `center` / `end` / `between` / `around` / `evenly` | 否 | `"start"` | 主轴对齐 |
| `wrap` | boolean | 否 | `false` | 允许换行 |
| `flex` | number（>= 0，<= 1000） | 否 | — | 弹性伸缩权重 |
| `width` / `height` | number / px / % / auto | 否 | — | Gateway 页面宽高 |
| `minHeight` / `maxWidth` | number / px / % | 否 | — | Gateway 页面尺寸边界，不接受 auto |
| `marginInline` | number / px / % / auto | 否 | — | Gateway 页面行内外边距 |
| `padding` | none / sm / md / lg | 否 | — | Gateway 页面主题内边距 |
| `backgroundImage` | string | 否 | — | Gateway 页面背景图片 URL |
| `backgroundFit` | cover / contain | 否 | — | Gateway 背景适配；显示回退 cover |
| `visibleFrom` | sm / md / lg | 否 | — | Gateway 最小显示宽度，分别为 576 / 768 / 992px |

允许普通子节点，不声明事件。gap 与 size 是不同枚举，不得把 medium 写入 gap。
尺寸与 marginInline 的数值和 px 值为 0..4096，百分比为 0..100%；背景地址遵循 URL 规则。
Box 不接受 background、style 或 className 等自定义背景与样式属性；外观使用宿主公共组件和主题样式。
按客户端偏好隐藏整项贡献使用第 2.4 节的实例级显示条件。
低于 visibleFrom 断点时隐藏并停用子树交互，保留已挂载内容的本地状态。

### 4.2 Text

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `value` | LocalizedText | 否 | — | 文本内容，完整中英文值跟随界面语言切换 |
| `type` | `secondary` / `success` / `warning` / `danger` | 否 | — | 语义文字颜色；省略使用普通文字 |
| `size` | Size | 否 | — | 组件尺寸 |
| `strong` | boolean | 否 | — | 加粗 |
| `italic` | boolean | 否 | — | 斜体 |
| `underline` | boolean | 否 | — | 下划线 |
| `delete` | boolean | 否 | — | 删除线 |
| `code` | boolean | 否 | — | 代码样式 |
| `mark` | boolean | 否 | — | 高亮标记 |
| `keyboard` | boolean | 否 | — | 键盘按键样式 |
| `disabled` | boolean | 否 | — | 禁用文字样式 |
| `ellipsis` | boolean | 否 | — | 单行省略 |

省略 value 显示空文案，字号显示回退 medium；不接受任何 children 或事件。宿主 appearance.style 在适配器中最后覆盖排版样式。
天气等业务状态通过普通 Text.value 的本地化符号与文案、Text.type 的语义色表达；状态分级由插件负责，不新增专用组件或属性。

### 4.3 Button

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `value` | LocalizedText | 否 | — | 按钮文案，完整中英文值跟随界面语言切换 |
| `type` | `default` / `primary` / `dashed` / `link` / `text` | 否 | — | 组件类型 |
| `color` | ButtonColor | 否 | — | 语义颜色 |
| `variant` | `outlined` / `dashed` / `solid` / `filled` / `text` / `link` | 否 | — | 外观样式 |
| `shape` | `default` / `circle` / `round` / `square` | 否 | — | 形状 |
| `size` | Size | 否 | — | 组件尺寸 |
| `danger` | boolean | 否 | — | 危险操作样式 |
| `ghost` | boolean | 否 | — | 幽灵按钮 |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `block` | boolean | 否 | — | 占满容器宽度 |
| `loading` | boolean / ButtonLoading | 否 | — | 加载状态，或延迟显示加载态的对象 |
| `href` | string | 否 | — | 链接地址，遵循 URL 规则 |
| `target` | `_self` / `_blank` | 否 | — | 链接打开位置 |
| `icon` | IconName | 否 | — | 白名单图标名 |
| `iconPlacement` | `start` / `end` | 否 | — | 图标在文案前或后 |
| `aria-label` | LocalizedText | 否 | — | 纯图标按钮必需的无障碍名称 |
| `openPanel` | string（<= 128 UTF-8 字节） | 否 | — | 已接受的点击回执后选中同包、同会话的活动右侧面板实例 |
| `form` | string，最多 200 UTF-8 字节 | 否 | — | Gateway 页面中同一实例的 Form.name；去空白后非空 |
| `openPanel` | string，最多 128 UTF-8 字节 | 否 | — | 同包、同会话右侧面板的实例 key |

字号显示回退 medium。icon 存在且 value 去空白后为空时，必须提供非空 aria-label；target 只能与 href 一起提供。loading.delay 当前只要求有限数值。支持 ui.click；加载、禁用或断连时不能派发。
form 引用必须有效；点击时先校验表单，成功后直接发送字段值，失败时展示本地错误并且不发送事件。
普通按钮始终发送空 data。表单能力不改变 value、type、variant 或无障碍属性的名称。
openPanel 遵循 `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`。只有用户实际点击且 ui.click 返回明确 accepted 回执、源实例仍存活、目标已在 conversation.panel.right 渲染时，当前客户端打开并选中该目标。目标身份从可信源实例派生，不接受其他包或会话。普通 render、失败、未知或迟到的失效交互不得切换页签。显式再次打开可恢复本客户端关闭的插件 Tab；关闭/隐藏释放 iframe 浏览上下文，后台观测不因此停止。

openPanel 仅接受 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`；实例必须已挂载且活动。渲染更新不触发跳转，拒绝回执不跳转。

### 4.4 Input

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |
| `size` | Size | 否 | `"medium"` | 组件尺寸 |
| `variant` | InputVariant | 否 | `"outlined"` | 外观样式 |
| `status` | InputStatus | 否 | — | 组件状态 |
| `value` | string | 否 | — | 当前输入文本 |
| `defaultValue` | string | 否 | `""` | 初始值；value 优先 |
| `placeholder` | string | 否 | — | 未输入或未选择时的提示 |
| `maxLength` | integer（>= 0） | 否 | — | 最大 UTF-16 长度，含初始值与事件值 |
| `allowClear` | boolean | 否 | — | 允许清空 |
| `showCount` | boolean | 否 | — | 显示字符计数 |
| `type` | `text` / `email` / `tel` / `url` / `password` | 否 | `"text"` | 输入类型 |
| `readOnly` | boolean | 否 | — | 只读；仍允许 ui.click |
| `autoComplete` | string | 否 | — | Gateway 原生自动填充提示；浏览器可以忽略 off |

初始化 value / defaultValue 和事件值均不得超过 maxLength，长度按 UTF-16 计算。支持 ui.click、ui.change、ui.press_enter；readOnly 仅允许 click。输入法组合期间标识 isComposing，组合提交不重复发送 Enter；未绑定 change 也允许本地编辑。

### 4.5 Switch

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `checked` | boolean | 否 | — | 当前开关状态 |
| `defaultChecked` | boolean | 否 | `false` | 初始开关状态；checked 优先 |
| `loading` | boolean | 否 | — | 加载状态 |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `ariaLabel` | LocalizedText | 否 | — | 无障碍名称 |
| `size` | CompactSize | 否 | `"medium"` | 组件尺寸 |
| `checkedChildren` | string | 否 | — | 开启状态文案 |
| `unCheckedChildren` | string | 否 | — | 关闭状态文案 |
| `preference` | PreferenceBinding | 否 | — | 宿主管理的客户端布尔偏好，与 checked / defaultChecked 互斥；读写成功后同步 checked，默认使用绑定的 default |

checked 优先于 defaultChecked。支持 ui.click、ui.change，初始化不派发；加载、禁用或断连时不能操作。仅支持 CompactSize。
客户端偏好仅用于界面显示；持久化失败保留旧值并显示本地化错误，不保存敏感配置。

### 4.6 Select

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |
| `size` | Size | 否 | `"medium"` | 组件尺寸 |
| `variant` | InputVariant | 否 | `"outlined"` | 外观样式 |
| `status` | InputStatus | 否 | — | 组件状态 |
| `options` | SelectOption[] | 是 | — | 可选项 |
| `value` | Selection | 否 | — | 当前选择，结构由 mode 决定 |
| `defaultValue` | Selection | 否 | — | 初始选择，结构由 mode 决定 |
| `placeholder` | string | 否 | — | 未输入或未选择时的提示 |
| `allowClear` | boolean | 否 | — | 允许清空 |
| `mode` | `multiple` / `tags` | 否 | — | 省略为单选；multiple 多选，tags 允许新标签 |
| `maxCount` | integer（>= 1） | 否 | — | 多选或 tags 的最大选择数 |
| `maxTagCount` | integer（>= 0） / `responsive` | 否 | — | 显示标签数量或自适应标签数量 |
| `showSearch` | boolean | 否 | — | 允许搜索 |

options 的 value 非空、唯一且同类型；tags 仅接受字符串，允许创建非空新值。单选 value / defaultValue 为标量或 null，多选 / tags 必须为无重复数组，且不超过 maxCount。未提供 showSearch 时，准备阶段按是否声明 mode 决定。支持 ui.select、ui.change、ui.search、ui.clear，门禁见第 5 章。

### 4.7 TreeSelect

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |
| `size` | Size | 否 | `"medium"` | 组件尺寸 |
| `variant` | InputVariant | 否 | `"outlined"` | 外观样式 |
| `status` | InputStatus | 否 | — | 组件状态 |
| `treeData` | TreeOption[] | 是 | — | 树数据 |
| `value` | TreeSelection | 否 | — | 当前选择，结构由有效多选模式决定 |
| `defaultValue` | TreeSelection | 否 | — | 初始选择，结构由有效多选模式决定 |
| `placeholder` | string | 否 | — | 未输入或未选择时的提示 |
| `allowClear` | boolean | 否 | — | 允许清空 |
| `multiple` | boolean | 否 | — | 允许多选 |
| `treeCheckable` | boolean | 否 | — | 使用复选框并启用多选 |
| `showCheckedStrategy` | `SHOW_CHILD` / `SHOW_PARENT` / `SHOW_ALL` | 否 | `"SHOW_CHILD"` | 勾选结果展示策略 |
| `showSearch` | boolean | 否 | — | 允许搜索 |
| `treeDefaultExpandAll` | boolean | 否 | — | 初始展开全部节点 |
| `treeDefaultExpandedKeys` | string[] | 否 | — | 初始展开节点 key 列表 |
| `maxTagCount` | integer（>= 0） | 否 | — | 最多显示的标签数 |

value / 实际 key 在全树非空且唯一，省略 key 时使用 value。准备阶段按 treeCheckable 或 multiple 决定有效多选；treeCheckable=true 与 multiple=false 冲突。showSearch 缺省跟随有效多选。单选值为字符串或 null，多选值为无重复字符串数组；值须存在。treeDefaultExpandedKeys 唯一且存在，不能与 treeDefaultExpandAll=true 同时提供非空展开列表。最多 8 层 / 512 项。支持 ui.select、ui.change、ui.clear。

### 4.8 Slider

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `min` | number | 否 | `0` | 下界 |
| `max` | number | 否 | `100` | 上界 |
| `step` | number | 否 | `1` | 步长 |
| `value` | number | 否 | — | 当前数值 |
| `defaultValue` | number | 否 | — | 初始值；value 优先 |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |
| `marks` | `Record<string, string>` | 否 | — | 数值位置到标签文案的映射 |
| `dots` | boolean | 否 | `false` | 只允许边界与 marks 候选 |
| `included` | boolean | 否 | `true` | 显示已选区间 |
| `reverse` | boolean | 否 | `false` | 反向显示 |
| `orientation` | `horizontal` / `vertical` | 否 | `"horizontal"` | 方向 |

要求 max > min、step > 0 且能整除区间（容差 1e-7）。marks 的 key 必须表示区间内的有限数值，数值意义不能重复。dots=true 时只能取 min / max / marks，否则按步长。未提供 defaultValue 时，准备阶段选择最接近 0 的合法候选，同距选较小值。value / defaultValue / ui.change 均检查合法候选。

### 4.9 Tabs

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `items` | TabItem[] | 是 | — | 页签及其内容子树 |
| `activeKey` | string | 否 | — | 当前激活页签 key |
| `defaultActiveKey` | string | 否 | — | 初始激活页签 key |
| `type` | `line` / `card` | 否 | `"line"` | 页签样式 |
| `tabPlacement` | `top` / `end` / `start` / `bottom` | 否 | `"top"` | 页签栏位置 |
| `size` | Size | 否 | `"medium"` | 组件尺寸 |
| `centered` | boolean | 否 | `false` | 页签栏居中 |
| `destroyOnHidden` | boolean | 否 | `false` | 销毁隐藏页签内容 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |

items.key 非空且唯一；activeKey / defaultActiveKey 必须存在且未禁用。省略激活值时，适配器选择首个未禁用页签，无可用项则不激活。内容只放 items[].children，包含隐藏内容的整树一起校验和计数。支持 ui.change。

### 4.10 Empty

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `image` | string | 否 | — | default / simple 内置图或合法 URL |
| `description` | string | 否 | — | 空态描述 |

image 只接受 default、simple 或合法 URL；不接受子节点或事件。省略文案与图片时采用适配器 / Ant Design 空态显示。

### 4.11 Image

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `src` | string | 是 | — | 资源地址，遵循 URL 规则 |
| `alt` | string | 否 | — | 资源替代文本 |
| `width` | number / string | 否 | — | 宽度，取值规则见表后 |
| `height` | number / string | 否 | — | 高度，取值规则见表后 |
| `fallback` | string | 否 | — | 加载失败时的替代图片 URL |
| `preview` | boolean / ImagePreview | 否 | `true` | 启用预览，或指定原图地址 |
| `fit` | contain / cover | 否 | — | Gateway 页面图片适配方式 |

src、fallback、preview.src 均检查 URL。width / height 可为 0..4096 的数值、auto、非负数字 px（<=4096）或百分比（<=100）；数字字符串允许小数，不接受任意 CSS 表达式。无子节点或业务事件，图片预览由本地组件处理。

### 4.12 Divider

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `children` | string | 否 | — | 字符串文案；不是节点级 children |
| `orientation` | `horizontal` / `vertical` | 否 | `"horizontal"` | 方向 |
| `variant` | `solid` / `dashed` / `dotted` | 否 | `"solid"` | 外观样式 |
| `titlePlacement` | `start` / `center` / `end` | 否 | `"center"` | 标题沿分隔线的位置 |
| `plain` | boolean | 否 | `false` | 普通文字标题样式 |
| `size` | Size | 否 | — | 组件尺寸 |

允许 Text 子节点，或 props.children 字符串标题，二者互斥。orientation=vertical 时不能提供非空字符串标题或任何标题节点。size 未补目录默认值。无事件。

### 4.13 Card

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `title` | string | 否 | — | 标题 |
| `extra` | string | 否 | — | 卡片右上角字符串文案 |
| `cover` | string | 否 | — | 封面图片 URL |
| `hoverable` | boolean | 否 | `false` | 悬浮反馈 |
| `size` | CompactSize | 否 | `"medium"` | 组件尺寸 |
| `variant` | `outlined` / `borderless` | 否 | `"outlined"` | 外观样式 |

cover 必须为合法 URL。允许普通子节点，无业务事件；extra 只接受字符串，不接受任意 ReactNode。仅支持 CompactSize。

### 4.14 Tag

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `children` | string | 否 | — | 字符串文案；不是节点级 children |
| `color` | `default` / `success` / `processing` / `warning` / `error` | 否 | — | 语义颜色 |
| `variant` | `filled` / `solid` / `outlined` | 否 | `"filled"` | 外观样式 |
| `disabled` | boolean | 否 | `false` | 禁用组件 |
| `href` | string | 否 | — | 链接地址，遵循 URL 规则 |
| `target` | `_self` / `_blank` | 否 | `"_self"` | 链接打开位置 |

文案仍使用 props.children，不能改为 value。href 遵循 URL 规则；缺少 href 时 target 不映射为链接。不接受节点级子节点或业务事件。

### 4.15 Panel

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `title` | string（<= 200 UTF-8 字节） | 是 | — | 宿主页签名称，去空白后必须非空 |

只允许作为具备专用根许可的树根，必须有非空节点 children；不能嵌套 Panel / Iframe。title 用于宿主页签名称。无事件。

### 4.16 FileTree

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `treeData` | FileTreeNode[] | 是 | — | 树数据 |
| `selectedKey` | string / null | 否 | — | 选中文件 key；null 表示无选择 |
| `expandedKeys` | string[]（最多 100 项） | 否 | — | 已展开的目录 key 列表 |
| `disabled` | boolean | 否 | — | 禁用组件 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |
| `emptyText` | string | 否 | — | 空内容提示 |
| `message` | string | 否 | — | 状态补充说明 |
| `status` | `ready` / `unavailable` / `unsupported` / `error` | 否 | — | 组件状态 |
| `truncated` | boolean | 否 | — | 整体目录结果被截断 |
| `readFailed` | boolean | 否 | — | 目录读取失败标记 |

treeData 遵循第 3 章的路径、父子目录与预算约束。selectedKey 只能指向 file 或为 null，expandedKeys 必须唯一且全部指向 directory。status 省略按 ready 处理；未提供受控展开 / 选择值时由适配器保留本地交互状态。支持 ui.select、ui.change，disabled 或非 ready 时禁止操作。

### 4.17 FilePreview

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `file` | PreviewFile / null | 是 | — | 文件信息；null 表示当前无文件 |
| `emptyText` | string | 否 | — | 空内容提示 |
| `status` | `ready` / `missing` / `unsupported` / `too_large` / `forbidden` / `unavailable` / `error` | 否 | — | 组件状态 |

file=null 表示无文件；非空对象至少提供 content 或 downloadUrl，name 去空白后非空，path 遵循相对路径规则。文本、图片、PDF 通过受管资源预览，HTML 只作文本显示。内部 PDF iframe 不等于通用 Iframe。无子节点或业务事件。

### 4.18 Iframe

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `src` | LocalizedText | 与 srcDoc 二选一 | — | 网页或插件资源地址；每种语言分别遵循本组件的严格 URL 规则 |
| `srcDoc` | LocalizedText | 与 src 二选一 | — | 插件生成的完整 HTML 文档，每种语言最长 16 KiB，去空白后非空 |
| `title` | LocalizedText（每种语言 <= 200 UTF-8 字节） | 是 | — | 页面名称 / 右侧页签名称，各语言去空白后必须非空 |
| `minWidth` | number（>= 0，<= 4096） | 否 | `0` | iframe 文档的最小宽度，单位 px；容器不足此宽度时由宿主外层提供横向滚动 |

只允许作为具备专用根许可的树根，无子节点或事件。URL 额外拒绝控制字符、反斜杠、三斜杠开头、凭据、无效 authority 与端口。宿主固定 sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads"，不含 allow-same-origin，插件不可覆盖。iframe 宽度取容器可用宽度与 minWidth 中较大者；URL 和内联文档采用相同规则，不提供适应 / 桌面宽度切换。容器尺寸变化只调整布局，不重建浏览上下文；插件发送 replace 仍遵循整树重建契约。同 URL/文档刷新、URL 模式的外链打开与 15 秒慢加载提示为宿主功能。仅在 active 且 connected 时创建，隐藏 / 断连时卸载，不承诺保活。src 与 srcDoc 必须且只能提供一个；地址、文档和标题独立按宿主语言选择，切换文档释放旧浏览上下文，不接受不完整的语言映射。

以 `/extension-assets/` 开头的插件资源地址由宿主按配置的 Gateway API 基址解析；保留基址的路径前缀。没有显式 API 基址时保持同源相对地址。Electron FrontendOnly 的 file:// 页面使用桌面注入的 HTTP(S) API 基址；普通宿主相对网页和外部绝对地址保持原语义。iframe、来源展示和打开外链使用同一解析后的地址。

插件文件 URL 的部署基址由宿主统一解析，前端不按文件所在后端进程查浏览器能力清单。HTML 继续通过原生 iframe URL 加载，保留相对 CSS 引用。

srcDoc 不开放宿主权限；仍使用同一固定 sandbox。文档随语言变化重建；没有 src 时不显示打开外链按钮。

宿主工具栏只读展示来源：URL 文档显示实际使用的 src（插件资源为解析后的 Gateway 地址），内联文档显示随宿主语言切换的“插件页面 / Plugin page”。来源文字可选择复制，超出可用宽度时单行省略，悬停显示完整值。此处不提供地址编辑，也不表示跨域 iframe 内部导航后的当前 URL。


### 4.19 Avatar

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `src` | string | 否 | — | 头像 URL；不接受旧字段 url |
| `children` | string | 否 | — | 原样显示的头像文本；不是节点级 children |
| `alt` | string | 否 | — | 资源替代文本 |
| `gap` | number（>= 0，<= 4096） | 否 | — | AntD 文本缩放时的左右间距，单位 px |
| `style` | object | 否 | — | 只接受 backgroundColor、color、fontSize |
| `style.backgroundColor` / `style.color` | string | 否 | — | 3 / 4 / 6 / 8 位十六进制 CSS 颜色 |
| `style.fontSize` | number（>= 1，<= 4096） | 否 | — | 数值像素字号 |
| `size` | `small` / `medium` / `large` / `sm` / `md` / `lg` / `default` / number（>= 1，<= 4096） | 否 | `"medium"` | 标准尺寸、default 别名或像素数值 |
| `shape` | `circle` / `square` | 否 | `"circle"` | 形状 |

src 显式提供时须为非空合法 URL；允许仅有文本或空头像。图片优先，图片失败后的 children 显示、文本缩放和 gap 使用 AntD 原生行为。
alt 只描述图片，不作为显示文本；宿主不从账号名推导首字素、配色或字号，相关展示由插件声明。
style 不接受其他键，宿主 appearance.style 最后覆盖组件样式。size 的 default 别名映射为 medium，
数值只要求有限且在 1..4096，不额外要求整数。无节点级 children 或事件。

### 4.20 Dropdown

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `items` | (DropdownItem / DropdownDivider)[] | 是 | — | 菜单项；分隔项仅在 Gateway 支持 |
| `trigger` | `click` / `hover` | 否 | `"click"` | 展开触发方式 |
| `placement` | bottomLeft / bottomRight / topLeft / topRight；Gateway 另支持 top / bottom / left / leftTop / leftBottom / right / rightTop / rightBottom | 否 | `"topLeft"` | 菜单弹出位置 |
| `disabled` | boolean | 否 | `false` | 禁用组件 |
| `ariaLabel` | string | 否 | — | 图标触发区必需的无障碍名称 |
| `icon` | IconName | 否 | — | 作为触发区的白名单图标，与非空节点 children 二选一 |
| `align` | DropdownAlign | 否 | — | Gateway 菜单锚点与偏移 |
| `autoFocus` | boolean | 否 | — | Gateway 菜单自动聚焦；显示回退 false |
| `destroyOnHidden` | boolean | 否 | — | Gateway 菜单隐藏时销毁 |
| `styles` | {root?: {width?: number}} | 否 | — | Gateway 菜单宽度，0..4096px；不接受其他样式字段 |

items 必须非空，key 非空且唯一，label 去空白后非空。非空节点 children 与 icon 必须且只能提供一种；icon 模式要求非空 ariaLabel。支持 ui.select，仅允许选择有效未禁用项；菜单开关为本地状态。
Gateway 仅支持展示型触发子树，不接受 icon。展示子树和菜单 label 节点数组只允许 Box、Text、Avatar、Image、Tag，
不接受事件或交互组件，Image 必须显式 preview=false。节点数组按普通树计入深度和节点预算；分隔项不可选择。

### 4.21 Icon

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `name` | IconName | 是 | — | 白名单图标名 |
| `size` | Size | 否 | — | 组件尺寸 |
| `aria-label` | string | 否 | — | 提供时去空白后必须非空；省略时作为装饰图标 |

名称使用共享白名单。size 省略时显示回退 medium；提供 aria-label 时以其去空白后的值作为 img 名称，省略时 aria-hidden。无子节点或事件，不接受任意 SVG、CSS 类或样式。

### 4.22 Form（Gateway）

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `name` | string，最多 200 UTF-8 字节 | 是 | — | 去空白后非空，同一实例内唯一 |
| `layout` | vertical / horizontal / inline | 否 | — | 显示回退 vertical |
| `disabled` | boolean | 否 | — | 禁用表单输入 |
| `ariaLabel` | string | 否 | — | 无障碍名称 |
| `requiredMark` | boolean | 否 | — | false 隐藏必填星号，不改变必填校验 |

仅允许非空 Form.Item 子节点，不允许嵌套 Form；本身不声明事件。隐藏页签和暂时停用保留字段值；
断连、replace、clear、实例释放或内容实际销毁时重新初始化。失焦及 Button.form 确认时本地校验。

### 4.23 Form.Item（Gateway）

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `name` | string，最多 200 UTF-8 字节 | 是 | — | 去空白后非空，同一 Form 内唯一；按字面解释，不接受 __proto__ / constructor / prototype |
| `label` | string | 否 | — | 字段标签 |
| `rules` | FormRule[] | 否 | — | 字符串校验规则，不接受函数和自定义 validator |

必须位于 Form 内且恰有一个 Input 子节点，Input 不得声明上行事件。初始值取 Input.value、Input.defaultValue 或空字符串。

### 4.24 MediaSurface（Gateway）

| 属性 | 类型 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `kind` | `image` / `video` / `html` | 是 | — | 内容类型 |
| `src` | string | 是 | — | 内容地址，遵循表后的素材地址规则 |
| `title` | string（<= 200 UTF-8 字节） | 是 | — | 内容名称，去空白后必须非空；用作 iframe 标题 |
| `poster` | string | 否 | — | 封面与回退图片，遵循素材地址规则 |
| `fit` | `cover` / `contain` | 否 | `"cover"` | 图片、视频与封面的适配方式 |
| `position` | `center` / `top` / `bottom` / `left` / `right` | 否 | `"center"` | 图片、视频与封面的对齐位置 |
| `loop` | boolean | 否 | `true` | 视频循环播放 |
| `muted` | boolean | 否 | `true` | 视频静音；宿主可强制静音 |
| `dim` | number（0..0.9） | 否 | `0` | 暗化遮罩的不透明度 |
| `blur` | number（0..40） | 否 | `0` | 模糊半径（px） |
| `opacity` | number（0.1..1） | 否 | `1` | 整体不透明度 |

素材地址规则：只接受以 `/extension-assets/` 开头的站内路径，通常由 `ops.ui.asset_url` 生成。
前缀后至少有包 ID、代次与文件三段；每段非空，解码后不得为 `.` / `..`，不得包含 `/`、反斜杠或控制字符。
路径只允许 RFC 3986 路径字符，`%` 后必须是两位十六进制且解码结果为合法 UTF-8；可带查询串与片段；
整串只允许除空格与反斜杠外的可打印 ASCII 字符。

只允许作为 `application.background` 的树根，无子节点或事件。HTML 内容固定在 sandbox="allow-scripts" 的 iframe 中运行，
不含 allow-same-origin，插件不可覆盖。fit、position 不作用于 HTML 内容。
输入、焦点、静音、就绪超时、减少动态和与页面的消息协议是宿主行为，不是 props，见
[Gateway 媒体容器规约 S_07](../../../gateway/extensions_v2/docs/specs/S_07_media-surface.md)。

## 5. 事件、状态与错误

**P-05：事件名放在节点的 events，不是 props。** 只有下表列出的组件事件可以绑定；其余组件不得声明事件。
对象 data 的字段按下表完整校验，不接受额外字段。

| 组件 | 事件 | data 全部字段 | 门禁 |
| --- | --- | --- | --- |
| Button | ui.click | 普通按钮为 {}；Gateway 表单按钮为 {字段名: string} | 节点已绑定，未 disabled / loading，连接有效；表单字段集合与引用 Form 完全一致 |
| Input | ui.click | value: string（必填） | 当前编辑值；readOnly 仍允许 click |
| Input | ui.change | value: string、isComposing: boolean（均必填） | readOnly 禁止；maxLength 按 UTF-16 长度检查；组合输入期间标识为 true |
| Input | ui.press_enter | value: string（必填） | readOnly 和输入法组合期间不派发 |
| Switch | ui.click、ui.change | checked: boolean（必填） | loading / disabled 禁止；初始化不派发 |
| Select | ui.select | value: OptionValue（必填） | 本次单个选项；普通模式不得选择禁用项；tags 允许非空新字符串 |
| Select | ui.change | value: Selection（必填） | 整体选择；检查 mode、重复值和 maxCount；清为 null 需 allowClear；初始禁用值可保留但不能通过事件新增 |
| Select | ui.search | searchValue: string（必填） | showSearch 有效时发送，允许空串 |
| Select | ui.clear | {} | allowClear 有效时发送 |
| TreeSelect | ui.select | value: string（必填） | 本次单项；节点必须可选或可勾选且未禁用 |
| TreeSelect | ui.change | value: TreeSelection（必填） | 整体选择，必须匹配 multiple / treeCheckable；值唯一且有效 |
| TreeSelect | ui.clear | {} | allowClear 有效时发送 |
| Slider | ui.change | value: number（必填） | 检查区间、步长或 dots 候选；每次有效变更独立发送 |
| Tabs | ui.change | activeKey: string（必填） | 必须对应已存在且未禁用的页签 |
| Dropdown | ui.select | key: string（必填） | 对应已存在且未禁用的菜单项；本地展开 / 关闭不派发业务事件 |
| FileTree | ui.select | key: string（必填） | 已存在的 file；disabled 或非 ready 时禁止 |
| FileTree | ui.change | key: string、expanded: boolean（必填）；expandedKeys: string[]（可选） | key 必须是 directory；完整 expandedKeys 唯一且全为目录，是否含 key 必须与 expanded 一致 |

普通交互 payload 必须包含 revision、node_key、action_id、args、data，并匹配当前实例已声明的事件和参数。
只有当前代次已显示、渲染成功且连接有效的节点可以派发；非法更新不会替换上一帧有效界面。
`ui.ready` 是宿主挂载确认，不是组件事件：一般发送空 payload；application.entry 可发送 `{"reason":"first_visit"}`。
版本、显示确认、断连及状态隔离的完整规则见 S_01 和 F_08，本文不另建一套运行时协议。

| 失败 | 结果 |
| --- | --- |
| 未知组件、未知 props、缺必填、类型或依赖约束错误 | 后端拒绝整棵树并返回 SCHEMA_ERROR；前端同样拒绝非法帧 |
| 插件仍传 url、label 等已删除字段 | 按未知 props 处理，不静默丢弃，也不自动猜测替代字段 |
| 事件未声明、data 错误、禁用节点或不允许的选项 | invalid_action / SCHEMA_ERROR，业务 Handler 不执行 |
| 实例不存在或 revision 过期 | inactive 或 stale / TARGET_INACTIVE |
| AgentServer 启动失败或 SDK 接口缺失 | 属于运行环境故障，不能通过放宽 props 校验处理 |

## 6. 涉及功能与源码入口

本章列出 props 变化可能影响的实际功能；维护时按变化范围检查，不要求每次修改所有文件。
新增引用此协议的功能时，必须同时把入口补入本章。

### 6.1 按职责检查

| 功能 | 主要入口 | 需要同步的内容 |
| --- | --- | --- |
| 插件渲染树与 wire 序列化 | [models.py](../../webui/models.py)：RenderNode、node_to_payload、_content_to_payload | 字段结构、嵌套节点、事件声明；不能序列化掉应报错的旧字段 |
| 插件 API 与业务视图 | [webui 导出](../../webui/__init__.py)、[工作台视图](../../examples/webui_workbench_v2/webui_workbench_v2/views.py)、[Token Weather](../../examples/token_weather/token_weather/handlers.py) | 公开名称、构造函数、helper、事件 Handler 与示例输出 |
| 后端属性与事件数据校验 | [component_rules.py](../../webui/component_rules.py)：COMPONENT_RULES、validate_props、validate_event_data | 完整字段、类型、枚举、必填、依赖约束和事件语义 |
| Gateway 页面校验与示例 | [Gateway WebUI](../../../gateway/extensions_v2/webui)、[登录示例](../../examples/auth_login_demo/auth_login_demo/views.py) | 共用组件的新 props、Gateway 表单与菜单扩展、字段集合及应用级资源 |
| FS / MCP 探测面板 | [FS Probe](../../examples/fs_probe/fs_probe/views.py)、[MCP Probe](../../examples/mcp_probe/mcp_probe/views.py) | Button 文案与样式、Text 状态与代码排版；单行截断使用 ellipsis，不恢复 max_lines |
| 整树、根节点和规模校验 | [schema.py](../../webui/schema.py)：validate_request、validate_root、_validate_node、_validate_events；[constants.py](../../webui/constants.py) | children、Tabs 内容遍历、专用根、预算和错误码 |
| 已发布实例、版本与事件门禁 | [registry.py](../../webui/registry.py)、[action_validation.py](../../webui/action_validation.py)：validate_upstream、_find_node | 快照、节点路径、版本、action / args / data 核对 |
| 后端渲染、清空和连接释放 | [runtime.py](../../webui/runtime.py)、[render_core.py](../../webui/core_handlers/render_core.py)、[action_core.py](../../webui/core_handlers/action_core.py) | 在何处拒绝、是否发布、旧实例与断连清理 |
| 贡献发现与页面标题 | [contributions.py](../../webui/contributions.py)、[useWebUIPanels.tsx](../../../channels/web/frontend/src/features/webui/react/useWebUIPanels.tsx) | title、支持的 renderer / 根类型、实例与槽位映射 |
| Gateway 转发 | [webui_handlers.py](../../../gateway/channel_manager/web/webui_handlers.py)、[transport.py](../../webui/transport.py)、[adapter.py](../../webui/adapter.py) | webui.dispatch、下行帧与错误转发；通常不解释组件 props，不能另加字段映射 |
| 前端目录与默认值 | [componentCatalog.ts](../../../channels/web/frontend/src/features/webui/catalog/componentCatalog.ts)、[catalog/types.ts](../../../channels/web/frontend/src/features/webui/catalog/types.ts)、[catalog/components](../../../channels/web/frontend/src/features/webui/catalog/components) | 白名单、字段类型、required、defaults、validate、prepare、validateEvent |
| 前端解析、规范化与不可变快照 | [protocol](../../../channels/web/frontend/src/features/webui/protocol)：checkPropValue、normalizeTree、process；[contracts](../../../channels/web/frontend/src/features/webui/contracts) | 与后端同样的类型和子节点规则、默认值、递归内容、版本和限制 |
| 渲染器与全部组件适配器 | [SchemaRenderer.tsx](../../../channels/web/frontend/src/features/webui/renderer/SchemaRenderer.tsx)、[validate.ts](../../../channels/web/frontend/src/features/webui/renderer/validate.ts)、[componentRegistry.ts](../../../channels/web/frontend/src/features/webui/renderer/adapters/componentRegistry.ts) | 登记组件与规则完全匹配、props 到 React / Ant Design 的映射 |
| 交互、状态与显示确认 | [createWebUIRuntime.ts](../../../channels/web/frontend/src/features/webui/runtime/createWebUIRuntime.ts)、[componentState.ts](../../../channels/web/frontend/src/features/webui/renderer/componentState.ts)、[WebUIInstanceView.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIInstanceView.tsx) | 回调失效、replace / clear、连接、显示确认及状态隔离 |
| 通信与页面 Provider | [webClientTransport.ts](../../../channels/web/frontend/src/features/webui/transport/webClientTransport.ts)、[createWebUIAppRuntime.ts](../../../channels/web/frontend/src/features/webui/assembly/createWebUIAppRuntime.ts)、[WebUIProvider.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIProvider.tsx) | 帧订阅与事件封装；不得根据组件增加另一套网络请求通道 |
| 图标白名单 | [icons/whitelist.json](../../../channels/web/frontend/src/features/webui/icons/whitelist.json)、[generate-webui-icons.mjs](../../../channels/web/frontend/scripts/generate-webui-icons.mjs) | 图标内容仍只改白名单；生成前端名称 / 导入与后端枚举，本文不重复全部图标名 |
| 主题、宿主外观与语言 | [WebUIThemeProvider.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIThemeProvider.tsx)、[renderer/types.ts](../../../channels/web/frontend/src/features/webui/renderer/types.ts)、[zh / en locales](../../../channels/web/frontend/src/i18n/locales) | 尺寸、样式映射、aria、提示与宿主覆盖；不能变为任意插件 style |
| 文件目录、预览和受管资源 | [fileResources.ts](../../../channels/web/frontend/src/features/webui/renderer/fileResources.ts)、[session 文件能力](../../handler_ops)；FileTree / FilePreview 适配器 | 路径、状态、downloadUrl、MIME、安全读取、资源过期与取消 |
| 插件安装、重载和启停 | [management/registry.py](../../management/registry.py)、[config.py](../../config.py)、[安装脚本](../../../../scripts/install_workbench_plugin.py) | 安装副本、包版本、旧字段迁移；reload 不读取原 ZIP 或 examples |

### 6.2 按组件检查显示功能

| 组件 | 前端规则文件 | 适配器目录 / 功能 | 插件或宿主调用场景 |
| --- | --- | --- | --- |
| Box | basic.ts | webuiBox：行列布局、间距与宿主覆盖 | 工作台组合视图、Token Weather、用户菜单布局 |
| Text | basic.ts | webuiText：文案、语义色与排版 | 工作台文字、Token Weather、Tabs / Divider 内容 |
| Button | basic.ts | webuiButton：标签、图标、链接、loading、点击 | 侧栏入口、任务入口、聊天头部、轮次用量与操作、登录 / 登出视图 |
| Input | input.ts | webuiInput：编辑、输入法组合、Enter | 插件输入与参数编辑 |
| Switch | switch.ts | webuiSwitch：开关状态与 click / change | 插件设置与工作台切换 |
| Select | select.ts | webuiSelect：选择、tags、搜索与清空 | 插件选项与整体选择更新 |
| TreeSelect | treeSelect.ts | webuiTreeSelect：树形选择 / 勾选 | 分层选项、节点权限与展开 |
| Slider | slider.ts | webuiSlider：数值和 marks | 插件参数滑块 |
| Tabs | tabs.ts | webuiTabs：页签内容与激活 | 插件内部页签；区别于右侧宿主页签 |
| Empty | empty.ts | webuiEmpty：空态图片与描述 | 空列表、无数据提示 |
| Avatar | avatar.ts | webuiAvatar：src 图片、children 文本、受控 style、尺寸与形状 | 首次欢迎页、Workbench 和登录示例的侧栏用户菜单 |
| Image | image.ts | webuiImage：图片、fallback 与预览 | 插件图片与大图预览 |
| Divider | divider.ts | webuiDivider：标题和方向 | 插件内容分段 |
| Card | card.ts | webuiCard：标题、封面与内容 | 欢迎页、插件内容卡片 |
| Tag | tag.ts | webuiTag：文案、状态色和链接 | 工作台标签、状态标识 |
| Panel | panel.ts | webuiPanel：专用根容器 | ToolPanel / ExpandedPanel 的插件页签、工作区文件页签 |
| FileTree | fileTree.ts | webuiFileTree：目录选择、展开、宽度及滚动恢复 | 工作区目录、Session Workspace Ops、文件预览联动 |
| FilePreview | filePreview.ts | webuiFilePreview：文本、图片、PDF 与状态 | 受管文件下载 / 预览；内部 PDF iframe 不等于通用 Iframe 节点 |
| Iframe | iframe.ts | webuiIframe：固定沙箱、活跃生命周期、最小宽度与刷新 | 右侧网页预览、侧栏主页面、任务主页面 |
| Dropdown | dropdown.ts | webuiDropdown：图标或自定义触发区、菜单选择 | 侧栏用户菜单、轮次菜单 |
| Icon | icon.ts | webuiIcon：白名单、尺寸与无障碍 | 独立装饰 / 语义图标 |
| MediaSurface | mediaSurface.ts | webuiMediaSurface：图片、视频、沙箱页面、封面回退与消息协议 | 外观插件的应用背景 |

上述规则文件均位于前端 catalog/components，适配器目录均位于 renderer/adapters。
修改专用根组件 title、生命周期或布局时，要同时核对这些入口；修改某个普通文案字段不要求重写网关和所有宿主。

### 6.3 页面宿主与全部槽位

| 功能 / 槽位 | 页面与宿主入口 | props 变化时关注 |
| --- | --- | --- |
| 首次欢迎页：application.entry | [WebUIAppRoot.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIAppRoot.tsx)、[WebUIEntryHost.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIEntryHost.tsx) | Avatar、Card、Button、首次显示与关闭 |
| 侧栏菜单与用户入口：application.menu、application.sidebar.footer | [SessionSidebar/index.tsx](../../../channels/web/frontend/src/components/SessionSidebar/index.tsx) | 按钮、头像、Dropdown 触发区与菜单 |
| 任务功能入口：task.function | [ConversationSidebar.tsx](../../../channels/web/frontend/src/multi-session/sidebar/ConversationSidebar.tsx) | 入口组件、导航及应用作用域 |
| 菜单 / 任务主页面：application.menu.main、task.function.main | [App.tsx](../../../channels/web/frontend/src/App.tsx)、[WebUINavigation.tsx](../../../channels/web/frontend/src/features/webui/react/WebUINavigation.tsx)：WebUINavigationHost / WebUIPageHost | 普通树与 Iframe 专用根、页面切换 |
| 右侧页签：conversation.panel.right | [ToolPanel/index.tsx](../../../channels/web/frontend/src/components/ToolPanel/index.tsx)、[ExpandedPanel.tsx](../../../channels/web/frontend/src/components/teamArea/ExpandedPanel.tsx)、[useWebUIPanels.tsx](../../../channels/web/frontend/src/features/webui/react/useWebUIPanels.tsx) | Panel / Iframe 的 title、页签选择、关闭、清理、滚动与隐藏生命周期 |
| 聊天标题和操作：conversation.header.title.after、conversation.header.actions | [ChatPanel/index.tsx](../../../channels/web/frontend/src/components/ChatPanel/index.tsx) | 文案、图标、Button 与 Dropdown |
| 聊天输入区：conversation.input.above、conversation.input.below | [ChatPanel/index.tsx](../../../channels/web/frontend/src/components/ChatPanel/index.tsx) | 输入、选择、禁用、草稿和局部布局 |
| 消息与轮次：message.assistant.meta、conversation.turn.footer.actions、conversation.session.content.after | [MessageItem.tsx](../../../channels/web/frontend/src/components/ChatPanel/MessageItem.tsx) | 消息归属、轮次操作、状态与事件参数 |
| 设置页：application.settings | [SettingsPageLayout.tsx](../../../channels/web/frontend/src/features/settings/SettingsPageLayout.tsx) | 表单组件、共享布局与滚动 |
| 应用背景：application.background（Gateway） | [WebUIAppRoot.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIAppRoot.tsx)、[WebUIBackgroundHost.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIBackgroundHost.tsx) | MediaSurface 根、全窗口底层、不接收输入、宿主策略 |

普通槽位通过 [WebUIHost.tsx](../../../channels/web/frontend/src/features/webui/react/WebUIHost.tsx) 挂载实例；
右侧页签由 useWebUIPanels 管理，二者共用 WebUIInstanceView 和运行时的挂载租约。
宿主 React props（如 appearance、layout、active）不属于第 4 章的插件 props，不可直接下发到渲染树。

## 7. 变更同步与验收

### 7.1 按变更类型执行

| 变更 | 必须同步 |
| --- | --- |
| 新增 / 删除 / 重命名普通 props | 本文、后端规则、前端目录、适配器、相关插件视图、共享正反用例和说明 |
| 修改对象 / 数组结构 | 同上，另核对递归遍历、序列化、节点路径与大小限制 |
| 修改默认值或 size / variant 语义 | 前端 defaults / prepare 与适配器回退、后端等价的缺省校验、主题和受影响宿主 |
| 修改事件及 data | 插件 Handler、两端事件规则、action_validation、适配器 emit、runtime 门禁及共享事件用例 |
| 新增 / 删除组件 | componentCatalog、componentRegistry、后端 COMPONENT_RULES、子节点 / 根限制、组件数量与示例 |
| 改动 Panel / Iframe / MediaSurface 的根规则或 title | 两端槽位与整树规则、useWebUIPanels、页面导航、右侧宿主与背景宿主 |
| 修改图标集合 | 图标白名单与生成产物、图标引用方；本文只在 IconName 规则变化时修订 |
| 不兼容变更 | 明确协议版本 / 兼容策略，更新插件安装副本并重新加载或重装；不能只升级前端 |

已有明确决策：Avatar 使用 src，不保留 url；Button / Text 文案使用 value，不保留 props.children；
Button 不使用旧 label，按钮样式 default 对应 type="default"，不能写 variant="default"。
Text 的 tone / weight / monospace 分别迁移为 type / strong / code；type 不提供 neutral 或 info，普通文字省略 type。
max_lines=1 迁移为 ellipsis=true；多行截断没有对应属性，移除该限制并保留业务已有的内容长度限制。
不得因为 Ant Design 或其他组件也叫 children / label / src，就擅自把所有组件字段统一改名。

已安装插件是独立文件副本。修改仓库 examples、重新安装 Python 依赖或仅执行 reload，都不会把新源码复制到旧安装包。
应先更新正确包 ID / 版本的安装副本或重新打包安装，再 reload 对应包；确认实例和新渲染帧使用新规则。
用户菜单已统一由 webui.workbench-v2 的 user-menu 实例承载，无需另安装 webui.user-menu。
头像使用 src，菜单提供个人资料演示选择和退出登录，登录按钮使用 value；通过工作台源码和安装副本统一维护。

### 7.2 验证入口与完成条件

| 范围 | 验证入口 |
| --- | --- |
| 全部组件字段、类型、必填、枚举 | [webuiComponentCases.json](../../../channels/web/frontend/tests/fixtures/webuiComponentCases.json)、test:webui-contracts；Python test_webui_events.py |
| 事件数据和门禁 | [webuiEventCases.json](../../../channels/web/frontend/tests/fixtures/webuiEventCases.json)、test:webui-events；后端事件与实例测试 |
| 完整树、子节点与递归内容 | test:webui-components、test:webui-protocol；后端 validate_root 回归 |
| 实际适配器、主题、显示与交互 | test:webui-components、test:webui-renderer；浏览器实际验证另行记录 |
| 版本、断连、释放与通信 | test:webui-runtime、test:webui-transport；后端 test_webui_runtime.py / test_application_slots.py |
| 插件示例与安装副本 | test_workbench_turn_actions.py、test_token_weather.py、包安装查询与实际渲染结果 |
| Gateway 共用字段与页面树 | [test_gateway_ui_render.py](../../../../tests/unit_tests/gateway/extensions_v2/test_gateway_ui_render.py)、webuiGatewayCases.json；复用 webuiComponentCases.json / webuiEventCases.json 中宿主共同支持的部分 |
| 登录与探测示例 | [test_login_example.py](../../../../tests/unit_tests/gateway/extensions_v2/test_login_example.py)、[test_probe_example_props.py](../../../../tests/unit_tests/extensions_v2/webui/test_probe_example_props.py)；实际安装 ZIP 与空态、成功、失败面板树 |

后端测试入口位于 [tests/unit_tests/extensions_v2/webui](../../../../tests/unit_tests/extensions_v2/webui)，前端命令以 package.json 为准。
只运行受影响的功能测试，遵守仓库不运行格式化 / 静态检查的约束；用户选择手工页面验收时，不擅自进行桌面操作。

完成同步需确认：本文与三端字段一致；新增字段确实被适配器消费；旧字段按约定兼容或拒绝；
合法、非法及适用的状态切换已验证；示例和安装副本的更新方式明确；实际未执行项已记录。
当前尚无机器自动保证本文与所有实现逐项一致，不能把维护约定表述成已具备生成或自动迁移能力。

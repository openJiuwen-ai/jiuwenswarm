# WebUI 组件规则规约

最近修订：2026-11-02。关联设计：[F_01](../features/F_01_antd-components.md)。

## 范围与适用条件

本规约定义组件目录的属性和事件数据规则，以及插件端的渲染与交互校验契约。
目录登记与页面渲染是独立职责；登记规则不等于已启用对应组件或槽位。

`componentCatalog` 声明 Box、Text、Button、Input、Switch、Select、TreeSelect、Slider、Tabs、
Empty、Avatar、Image、Divider、Card、Tag、Panel、FileTree、FilePreview、Iframe、Dropdown。字段、默认值、必填与事件数据的唯一实现定义在
[`catalog/components/`](../../catalog/components/)，本文不复制完整属性表。
槽位目录包含应用范围的 `application.entry` 与 `application.sidebar.footer`，其余 13 个槽位为会话范围。

## 接口与不变量

- C-01：`createCatalog` 拒绝重复组件、重复槽位、未声明的默认属性和白名单之外的组件事件。
- C-02：组件事件白名单为 ui.click、ui.change、ui.press_enter、ui.search、ui.clear、ui.select。
  组件只能声明自身规则中的事件，不因事件名在白名单中就自动支持该事件。
- C-03：`checkPropValue(rule, value, limits, depth?)` 校验单项属性或事件数据，合法时返回 `null`，
  非法时返回字符串；它不执行组件 `validate`、`prepare`，也不发送请求。
- C-04：object 规则拒绝未知或缺失的必填字段，array、record 和递归规则执行类型、
  列表长度、文本字节和深度限制；nodes 规则只检查数组规模，完整子树校验由节点遍历器负责。
- C-05：节点遍历器在属性类型及必填校验通过后调用组件 `validate` 处理跨字段限制，
  再由 `prepare` 在合法属性上补充默认值。
- C-06：组件协议版本常量为 1。普通上行交互使用 C-08 定义的 payload，不接受扁平交互字段。
- C-07：插件节点的 `events` 为非空事件映射，每项只接受 `id` 和可选对象 `args`；省略 args 表示 `{}`，
  显式 null 被拒绝。旧 `action` 字段已移除，前后端均拒绝该字段。
- C-08：普通 `webui.dispatch` 请求的 payload 只接受 `node_key`、`revision`、`action_id`、`args`、`data` 五个字段。
  node_key 定位当前下发树，revision 为正整数且等于当前实例版本，事件、动作和参数必须匹配该节点声明。
  data 按组件事件规则校验；禁用组件、非法选项、Slider 非步进值等在业务 Handler 前被拒绝。
- C-09：`ui.ready` 通常使用空 payload，`application.entry` 接受 `{"reason": "first_visit"}`，均不要求实例已登记。普通交互在目标不存在时返回 `inactive/TARGET_INACTIVE`，
  版本不符返回 `stale/TARGET_INACTIVE`，动作或数据非法返回 `invalid_action/SCHEMA_ERROR`。
  适配器校验目标包的槽位贡献；包、实例、会话身份不做字符串强制转换。
- C-10：插件 registry 保留已下发树的只读快照，后续原始字典修改不改变已声明事件。
  门禁只保证进入事件链时有效，不锁住 Handler 的整个执行期。
- C-11：独立前端协议管线只接受 events 节点，拒绝 action 和未知节点字段；规范化树与输入隔离并递归冻结。
  事件 args 支持有规模限制的 JSON 对象，省略时补为 `{}`。
- C-12：Tabs 的内容节点使用与普通子节点相同的遍历器和预算，路径形如 `0.items.1.children.0`。
  Panel/Iframe 只能作为目录允许槽位的根节点，右侧面板根组件必须匹配其 `rootComponents`。
- C-13：交互绑定同时关联挂载句柄、实例、localFrameId 与节点。只有当前代次已确认显示、
  渲染成功且连接有效时才能发送；禁用或加载中的节点不能发送交互。
  Input 的 ui.click 携带当前编辑值 `{value}`；只读 Input 允许 ui.click，禁止 ui.change 和 ui.press_enter。
- C-14：replace、clear、断连、渲染失败或所属消费者释放均使旧节点绑定失效。
  同 revision 重建也分配新的 localFrameId；旧显示确认和失败反馈不能作用于新代次。
- C-15：每次合法组件回调独立发送，包括值相同且前一个请求未完成的回调。
  等待回执期间绑定或代次失效时返回 `stale`，不表示服务端副作用已撤销。
- C-16：渲染器只接受规范化节点，插件不能注入 className 或 style；独立的宿主外观参数优先于组件默认样式。
  Box/Button 的 unstyled 外观使用原生元素，Button 仍保留禁用、可访问标签与事件派发语义。
- C-17：WebUI 主题配置仅作用于 Provider 子树，使用宿主语义 token 和语言设置；
  Button 逐字显示插件标签，不自动在两个中文字之间添加空格。
- C-18：Iframe 仅在内容激活且连接有效时持有浏览上下文；隐藏或断连时卸载 iframe，
  重新激活或 src 改变后重新加载，不保留旧文档。
  相对路径与 HTTP(S) 来源均使用不含 `allow-same-origin` 的固定 sandbox；加载提示不覆盖整页、不拦截鼠标。
- C-19：Input 只为已绑定的 ui.change 派发 `{value, isComposing}`；组合输入结束发送一次提交值，
  其后浏览器重复的同值提交不再派发。未绑定 change 时允许本地编辑；disabled、readOnly 和断连时不派发。
  合法 replace 重新初始化输入值，拒绝的更新保留当前草稿；清空发送空字符串。
- C-20：Input 仅在声明 ui.press_enter 时将 Enter 回调派发为 `{value}`，value 取当前编辑值，允许空字符串。
  组合输入中、原生事件标记 isComposing 或 keyCode 为 229 时不提交；disabled、readOnly 和断连时不提交。
  Enter 与 change 分别绑定，每次有效回调独立发送，不等待前一请求完成。
- C-21：Switch 的 checked 优先于 defaultChecked，均未指定时初始化为 false；初始化和合法 replace 不派发事件。
  本地切换仅为已绑定的 ui.change 发送布尔值 `{checked}`；未绑定时仍可切换，disabled、loading 和断连时禁用。
  ui.click 独立绑定并携带切换后的 `{checked}`；同时绑定两项时，点击先派发 change，再派发 click。
- C-22：Select 的 ui.select 发送本次选择的单个选项值 `{value}`，ui.search 发送 `{searchValue}`，ui.clear 发送 `{}`。
  三项分别绑定，不额外派发 ui.change；未绑定事件时仍可本地操作。disabled 或断连时禁用。
  初始化与合法 replace 不派发事件，合法 replace 重新初始化选择，非法更新保留当前选择。
- C-23：TreeSelect 的 ui.select 发送本次选中节点的单个 `{value}`；ui.change 发送整体选择值，
  单选为字符串，multiple 或 treeCheckable 为数组，清空后分别为 null 与空数组；ui.clear 发送 `{}`。
  三项分别绑定，未绑定事件时仍可本地选择。disabled 或断连时禁用；初始化与合法 replace 不派发事件。
  节点 key 缺省取 value，搜索按 title 过滤。普通点选检查 selectable；treeCheckable 模式检查 checkable 和 disableCheckbox，两者均拒绝 disabled 节点。
- C-24：Slider 的 ui.change 发送数值 `{value}`，拖动与键盘调整的每次有效变化都独立派发，不合并、不等待回执。
  仅在已绑定时派发；未绑定时仍可本地调整。初始化与合法 replace 不派发事件，disabled 或断连时禁用。
  数值需满足 min/max 以及 step（或 dots/marks）允许的取值。
- C-25：Tabs 的 ui.change 发送当前激活页的 `{activeKey}`，切换只派发该事件，隐藏页内容不派发其他事件。
  未绑定时仍可本地切换；disabled 页签与断连时不可切换；初始化与合法 replace 不派发事件。
  默认保留已挂载的隐藏页内容状态，destroyOnHidden 时切回后重新初始化。
- C-26：Dropdown 的触发区由子节点提供，点击或键盘展开不派发事件；菜单项选中时只为已绑定的 `ui.select` 发送 `{key}`，
  key 必须存在于 `items` 且菜单项未禁用；初始化与合法 replace 不派发事件。`items` 为空、key 重复或 label 为空时规则拒绝。

例如 Input 的 `ui.change` 接受 `{value: "北京", isComposing: false}`，
缺少 `isComposing` 或传入额外字段时，事件数据规则返回错误。
Select 只声明 ui.select、ui.search、ui.clear，不额外增加 ui.change。
TreeSelect 另外声明 ui.change，用于承载整体选择值。

## 限制与验证入口

规模限制复用 [`protocol/limits.ts`](../../protocol/limits.ts)；组件特有约束以目录源码为准，
例如 TreeSelect 限制 8 层与 512 项，Image 尺寸由组件规则限制。
事件 data 中的文本按单字段 UTF-8 字节数校验，默认上限为 16 KiB；
包含 action_id、args、data、node_key 和 revision 的完整 payload 序列化后按 maxFrameBytes 校验，默认上限为 256 KiB。
前后端遍历全部 Tabs 内容，
隐藏内容也计入 8 层、512 节点预算，Panel/Iframe 不能嵌套为普通子节点。
`application.entry` 与 `application.sidebar.footer` 使用 application 范围，以当前 Web 连接为身份，不要求会话；其他槽位使用 session 范围。

验证入口为 `npm run test:webui-contracts`、`npm run test:webui-protocol`、
`npm run test:webui-runtime`、`npm run test:webui-events`、`npm run test:webui-renderer`
和 `npm run test:webui-components`。

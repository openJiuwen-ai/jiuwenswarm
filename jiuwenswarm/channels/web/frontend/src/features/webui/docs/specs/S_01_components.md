# WebUI 组件规则规约

最近修订：2026-10-30。关联设计：[F_01](../features/F_01_antd-components.md)。

## 范围与适用条件

本规约定义组件目录的属性和事件数据规则，以及插件端的渲染与交互校验契约。
目录登记与页面渲染是独立职责；登记规则不等于已启用对应组件或槽位。

`componentCatalog` 声明 Box、Text、Button、Input、Switch、Select、TreeSelect、Slider、Tabs、
Empty、Image、Divider、Card、Tag、Panel、Iframe。字段、默认值、必填与事件数据的唯一实现定义在
[`catalog/components/`](../../catalog/components/)，本文不复制完整属性表。
目录不包含 FileTree、FilePreview 和 application.entry。

## 接口与不变量

- C-01：`createCatalog` 拒绝重复组件、重复槽位、未声明的默认属性和白名单之外的组件事件。
  当前 action 消费方使用的目录一致性检查仍保留。
- C-02：组件事件白名单为 ui.click、ui.change、ui.press_enter、ui.search、ui.clear、ui.select。
  组件只能声明自身规则中的事件，不因事件名在白名单中就自动支持该事件。
- C-03：`checkPropValue(rule, value, limits, depth?)` 校验单项属性或事件数据，合法时返回 `null`，
  非法时返回字符串；它不执行组件 `validate`、`prepare`，也不发送请求。
- C-04：object 规则拒绝未知或缺失的必填字段，array、record 和递归规则执行类型、
  列表长度、文本字节和深度限制；nodes 规则只检查数组规模，完整子树校验由节点遍历器负责。
- C-05：组件 `validate` 在属性类型及必填校验通过后处理跨字段限制；`prepare` 在合法属性上补充默认值。
- C-06：组件协议版本常量为 1。普通上行交互使用 C-08 定义的 payload，不接受扁平交互字段。
- C-07：插件节点的 `events` 为非空事件映射，每项只接受 `id` 和可选对象 `args`；省略 args 表示 `{}`，
  显式 null 被拒绝。旧 `action` 字段继续保留，禁止与 events 同时出现，不做两者转换。
- C-08：普通 `webui.dispatch` 请求的 payload 只接受 `node_key`、`revision`、`action_id`、`args`、`data` 五个字段。
  node_key 定位当前下发树，revision 为正整数且等于当前实例版本，事件、动作和参数必须匹配该节点声明。
  data 按组件事件规则校验；禁用组件、非法选项、Slider 非步进值等在业务 Handler 前被拒绝。
- C-09：`ui.ready` 使用空 payload，不要求实例已登记。普通交互在目标不存在时返回 `inactive/TARGET_INACTIVE`，
  版本不符返回 `stale/TARGET_INACTIVE`，动作或数据非法返回 `invalid_action/SCHEMA_ERROR`。
  适配器校验目标包的槽位贡献；包、实例、会话身份不做字符串强制转换。
- C-10：插件 registry 保留已下发树的只读快照，后续原始字典修改不改变已声明事件。
  门禁只保证进入事件链时有效，不锁住 Handler 的整个执行期。

例如 Input 的 `ui.change` 接受 `{value: "北京", isComposing: false}`，
缺少 `isComposing` 或传入额外字段时，事件数据规则返回错误。
Select 只声明 ui.select、ui.search、ui.clear，不额外增加 ui.change。

## 限制与验证入口

规模限制复用 [`protocol/limits.ts`](../../protocol/limits.ts)；组件特有约束以目录源码为准，
例如 TreeSelect 限制 8 层与 512 项，Image 尺寸由组件规则限制。
前端 `rootComponents` 和 Tabs `content` 属于目录描述；插件端遍历全部 Tabs 内容，
隐藏内容也计入 8 层、512 节点预算，Panel/Iframe 不能嵌套为普通子节点。
槽位使用 session 范围，不支持应用身份或无会话推送。

验证入口为 `npm run test:webui-contracts`。

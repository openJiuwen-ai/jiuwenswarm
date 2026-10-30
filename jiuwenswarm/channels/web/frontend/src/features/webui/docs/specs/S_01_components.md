# WebUI 组件规则规约

最近修订：2026-10-30。关联实施与验证：[F_01](../features/F_01_antd-components.md)。

## 范围与适用条件

本规约定义独立组件目录的属性和事件数据规则，供后续协议、运行时和渲染器接入。
当前生产入口仍使用既有 action 契约；登记规则不等于已启用对应组件或槽位。
完整节点遍历、Tabs 内容组、根位置、事件绑定及发送门禁尚未迁移，不属于本组已经生效的保证。

`componentCatalog` 声明 Box、Text、Button、Input、Switch、Select、TreeSelect、Slider、Tabs、
Empty、Image、Divider、Card、Tag、Panel、Iframe。字段、默认值、必填与事件数据的唯一实现定义在
[`catalog/components/`](../../catalog/components/)，本文不复制完整属性表。
本组不登记 FileTree、FilePreview 和 application.entry。

## 接口与不变量

- C-01：`createCatalog` 拒绝重复组件、重复槽位、未声明的默认属性和白名单之外的组件事件。
  当前 action 消费方使用的目录一致性检查仍保留。
- C-02：组件事件白名单沿用来源：ui.click、ui.change、ui.press_enter、ui.search、ui.clear、ui.select。
  组件只能声明自身规则中的事件，不因事件名在白名单中就自动支持该事件。
- C-03：`checkPropValue(rule, value, limits, depth?)` 校验单项属性或事件数据，合法时返回 `null`，
  非法时返回字符串；它不执行组件 `validate`、`prepare`，也不发送请求。
- C-04：object 规则拒绝未知或缺失的必填字段，array、record 和递归规则执行来源已有的类型、
  列表长度、文本字节和深度限制；nodes 规则只检查数组规模，完整子树由后续节点遍历器负责。
- C-05：组件 `validate` 在属性类型及必填校验通过后处理跨字段限制；`prepare` 在合法属性上补充来源已有的值。
  二者当前由规则测试独立调用，生产协议遍历器尚未消费它们。
- C-06：来源组件协议版本常量为 1，既有支持版本和网络结构不变，不新增版本协商或转换。

例如 Input 的 `ui.change` 接受 `{value: "北京", isComposing: false}`，
缺少 `isComposing` 或传入额外字段时，事件数据规则返回错误。
Select 只声明 ui.select、ui.search、ui.clear，不额外增加 ui.change。

## 限制与验证入口

规模限制复用 [`protocol/limits.ts`](../../protocol/limits.ts)；组件特有约束以目录源码为准，
例如 TreeSelect 限制 8 层与 512 项，Image 尺寸沿用来源限制。
`rootComponents` 和 Tabs `content` 在本组只是目录描述，不能据此宣称已完成根位置或隐藏页子树校验。
槽位规则保持来源的 session 范围假设，本组不实现应用身份或无会话推送。

验证入口为 `npm run test:webui-contracts`，当前执行结果及剩余接入工作见关联特性记录。

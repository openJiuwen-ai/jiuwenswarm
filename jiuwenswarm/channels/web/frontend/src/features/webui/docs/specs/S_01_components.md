# WebUI 组件规则规约

最近修订：2026-11-04。关联设计：[F_01](../features/F_01_antd-components.md)。

## 范围与适用条件

本规约定义组件目录的属性和事件数据规则，以及插件端的渲染与交互校验契约。
宿主负责插槽位置、基础留白、主题与生命周期；插件通过已开放的 AntD 组件属性及原生布局属性声明具体页面样式。宿主负责通用白名单校验和属性适配，不增加登录页面专用的样式分支。
目录登记与页面渲染是独立职责；登记规则不等于已启用对应组件或槽位。

`componentCatalog` 声明 Box、Text、Button、Input、Switch、Select、TreeSelect、Slider、Tabs、
Empty、Avatar、Image、Divider、Card、Tag、Panel、FileTree、FilePreview、Iframe、Form、Form.Item、Dropdown、Icon。字段、默认值、必填与事件数据统一遵循
[S_17 Props 权威规约](../../../../../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)。
[`catalog/components/`](../../catalog/components/) 和后端规则是该规范的实现；本文维护运行与交互不变量，不复制完整属性表。
槽位目录包含应用范围的 `application.entry` 与 `application.sidebar.footer`，其余 13 个槽位为会话范围。

## 表单、菜单与应用插槽

- Box 新增受控 width/height/minHeight/maxWidth、padding、backgroundImage/backgroundFit 和 visibleFrom；尺寸仅接受有界数值、px/% 或适用字段的 auto，不开放任意 style/className。visibleFrom 的 sm/md/lg 对应 576/768/992px，低于断点时隐藏并停用子树交互。
  gap 接受 none/sm/md/lg 或非负有限数值；数值按 px 直接传给 AntD Flex.gap，例如 gap=48，间距由插件声明。
  marginInline 接受 auto、非负有界数值或 px/% 字符串，直接写入原生 CSS margin-inline；插件可用 width=100%、maxWidth=1200、marginInline=auto 声明内容区居中。
- Image.fit 支持 contain/cover，preview=false 可用于 Logo 和宣传图。背景图和内容图均由插件提供 URL，不内置登录图片或官网样式。
- Input.autoComplete 为可选字符串，直接传给 AntD Input 的原生自动填充属性；插件可声明 off，但浏览器密码管理器可以忽略此偏好，宿主不承诺禁止保存密码弹窗。
- Form.name 在同一渲染实例内唯一；禁止嵌套 Form。可选 requiredMark 为布尔值，直接传给 AntD Form；false 隐藏必填星号，不改变 rules 的必填校验。Form.Item 仅接收 name、可选 label/rules 和一个 Input，字段名按字面解释，同一 Form 内不得重复。
- Form 在保留的隐藏页签或暂时停用的内容中保留字段值；断连、replace、clear、实例结束或 destroyOnHidden 导致实际销毁时重新初始化。
- rules 支持字符串 required/min/max/pattern/message；前端使用 AntD Form 在 blur 和按钮确认时本地校验，Form.Item 展示关联错误。禁止函数、自定义 validator 和表单输入的上行事件声明。
- Button.form 引用当前实例内的 Form.name；校验通过后，以字段名为键直接放入 ui.click.data。无 form 的按钮保持 data={}，无效引用不执行普通点击。
- Dropdown.items 的普通菜单项声明唯一非空 key、label，可选 type=item 和 disabled；分隔项使用 AntD 原生 type=divider，可选 key。所有显式 key 唯一；禁用项及分隔项不接受点击事件。触发器使用 icon 或一棵展示型 children 内容，不接受嵌套交互组件；触发子树中的 Image 必须由插件显式设置 preview=false。展开/收起在本地处理，可点击菜单项发送 ui.select.data.key。
  label 接受非空字符串或非空展示节点数组，复用 Box、Text、Avatar、Image、Tag；节点数组与触发内容使用相同的校验和树规模限制，不接受嵌套交互或图片预览。插件可组合头像与正文 Text，正文颜色由主题决定。
  autoFocus 为可选布尔值，遵循 AntD 默认 false；鼠标展开不主动聚焦菜单项，键盘交互沿用 AntD 行为。styles.root.width 接受 0–4096 的数值（px），直接传给 AntD；具体菜单宽度由插件声明，例如 200。
  可选 destroyOnHidden 直接传给 AntD；插件可声明 true，在关闭时销毁菜单，重新展开时不保留先前的焦点高亮。
  可选 placement 使用 AntD 的 12 种非 Center 方位；可选 align 复用其锚点对齐，points 为两个 tl/tc/tr/cl/cc/cr/bl/bc/br，依次指定菜单与触发器的锚点，offset 为可选的两个有界数值（-128 到 128px）。
  右上方示例为 placement=topLeft、align.points=[bl,tr]、align.offset=[0,0]，将菜单左下角对齐头像触发器右上角。上述字段直接传给 AntD，不增加位置别名或自定义定位算法。
- application.entry 始终保留消费者，首次欢迎 ui.ready 仍只按首访触发。有效 render 可随时显示，clear 关闭当前视图后可重入，渲染失败保持全屏错误；原生 modal dialog 和 inert 阻止底层交互。内容区铺满视口，宽度、背景和留白由插件的 Box 控制。
- application.sidebar.footer 是应用级单实例显示槽位，不依赖聊天 session，沿用已合入的槽位目录；宿主为底部保留 12px 间距。

### English contract summary

The host owns slot placement, baseline spacing, theme and lifecycle. Plugins declare page presentation through the exposed AntD and native layout properties; the renderer performs generic validation and adaptation without login-specific styling branches.

Input.autoComplete is an optional string passed directly to AntD Input. Plugins may declare off; browser password managers can override this preference, so it does not guarantee suppression of browser credential prompts.

Dropdown menu entries support native AntD disabled and type=item properties; dividers use type=divider with an optional key. Explicit keys must be unique. Disabled entries and dividers cannot dispatch actions. A label is a nonempty string or an array of display nodes (Box, Text, Avatar, Image, Tag), using the same validation and tree limits as the trigger; interactive content and image previews are prohibited. Plugins can compose an avatar and regular theme-colored Text. Optional autoFocus follows AntD's false default and preserves native keyboard behavior. Optional destroyOnHidden maps directly to AntD; true destroys the menu on close so reopening does not retain its previous focus highlight. Optional styles.root.width passes a 0–4096 pixel number directly to AntD; plugins choose the actual width, such as 200.

Form owns controlled string fields and local blur/confirmation validation. Retained hidden tabs and temporary content inactivity preserve field values; disconnection, replacement, clearing, instance disposal or destroyed tab content resets them. Its optional boolean requiredMark maps directly to AntD Form: false hides the required marker while preserving required validation. Button.form references a unique Form in the same rendering instance and sends its fields directly in ui.click.data. Ordinary buttons keep empty data. Dropdown declares key/label items, opens locally, and sends the selected key through ui.select. The trigger uses either icon or one display-only child subtree. Images in its trigger subtree must explicitly set preview=false. Optional placement and align use native AntD positioning, with two anchor points and optional offsets bounded to -128..128px. The entry consumer survives clear and uses native modal focus isolation. The sidebar footer is application scoped, uses the published single-view selection without a chat session, and leaves 12px at the bottom.

Box provides bounded dimensions, padding, background-image fitting and explicit minimum-width visibility at 576/768/992px. Its gap accepts none/sm/md/lg or a finite nonnegative number passed directly to AntD Flex.gap in pixels. marginInline accepts auto, bounded nonnegative numbers or px/% values and maps directly to native CSS. Image.fit supports contain/cover. Entry content fills the viewport; plugins compose split or centered pages without a login-specific layout component. See [page layout properties](S_03_page-composition.md).

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
  其余槽位的 rootComponents 只声明可用的专用根组件，普通 Box/Card 等仍可作为根；
  application.menu.main 和 task.function.main 均接受普通组件树。
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
  普通尺寸使用 `size="medium"`（兼容 md，也为未指定时的尺寸）：高 32px、字号 14px、左右 padding 16px、
  图文间距 8px、圆角 8px；纯图标为 32px 方形点击区。按钮无默认阴影，尺寸由宿主主题统一配置。
  Button 的可选 icon 从[图标白名单](../../icons/whitelist.json)选择，例如 `search`、`file-text`、`refresh-cw`；
  旧名称 `refresh` 保留为兼容别名。纯图标按钮（声明 `icon` 且省略 `value`）要求非空 `aria-label`
  作为可访问名称及悬停提示；`type="text"` 使用无边框的 text 按钮，hover 背景使用宿主的
  `--color-action-secondary`。聊天头部保留插件声明的文字、图标和变体，不为所有按钮注入刷新图标。
- C-18：Iframe 仅在内容激活且连接有效时持有浏览上下文；隐藏或断连时卸载 iframe，
  重新激活或 src 改变后重新加载，不保留旧文档。
  相对路径与 HTTP(S) 来源均使用不含 `allow-same-origin` 的固定 sandbox；加载提示不覆盖整页、不拦截鼠标。
  宿主提供本地适应面板/桌面宽度切换；桌面模式的最小视口为 1280px，溢出由外层横向滚动容器承载，
  切换模式不重建文档。重新加载操作重建当前 src；新标签页入口打开配置的 src 并隔离 opener。
  加载 15 秒后显示非阻塞慢加载提示，继续接受 load；超时不等同加载失败，load 不证明远端业务可用。
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
  可选 ui.change 发送完整选择值 `{value}`，单选为字符串或数值，多选/tags 为数组，清空后分别为 null 和空数组。
  四项分别绑定，取消选项也发送 change；未绑定事件时仍可本地操作。disabled 或断连时禁用。
  change 检查模式、选项、重复值和 maxCount；禁用选项只能保留下发的初始选择，不能通过事件新增。
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
  可选 icon 与 Button 共用图标白名单（例如 `ellipsis`、`settings`），改用组件内置的纯图标 text 按钮，此时必须提供非空 ariaLabel，
  icon 与非空 children 必须且只能提供一个，空触发区或两种来源同时存在时前后端拒绝整棵树。
  展开时保留背景；选择可用项后关闭，禁用或断连时关闭且不可展开。
- C-27：FilePreview 的 file 可为 null；非空时 name/path 必填，content 与 downloadUrl 至少提供一个。
  downloadUrl 接受相对路径或 HTTP(S) URL，文本正文优先使用 content。文本 URL 的读取上限为 1 MiB，
  读取失败或内容不受支持时显示原有状态提示；切换文件时中止旧文本请求。
  图片和 PDF 分别由组件内部的原生 img/iframe 加载，不属于插件子节点。
  PDF 只接受带 token 的同源 `/file-api/download` 地址；先以 `Range: bytes=0-4` 检查响应类型为 application/pdf、
  正文以 `%PDF-` 开头，并拒绝重定向，通过后以 inline=1 创建不带 sandbox 的 PDF iframe。
  检查读取有界且切换文件时中止；该分支不改变通用 Iframe 的固定沙箱规则。
  HTML 仍按文本显示，不执行。内嵌 content 继续受通用单字段 16 KiB 预算约束。
- C-28：FileTree 在同一挂载实例中按节点保存展开、选中、滚动位置及本地宽度，合法 replace 后恢复。
  右侧分界线支持拖拽和左右方向键，调整宽度不派发插件事件；宿主外观参数仍优先。
  节点移除或类型改变时丢弃状态；clear、断连或消费者更换时沿用实例状态端口的重置规则。
- C-29：尺寸统一推荐 small/medium/large，sm/md/lg 为兼容别名并在前端准备阶段转换。
  Switch/Card 只有 small/medium 两档；Avatar 使用 `src` 指定图片地址，还接受 default（medium 的别名）与原有像素数值。
- C-30：Button 和 Text 的文案统一使用字符串 `value`，不接受 `props.children` 或节点子组件；宿主将 `value` 映射为 React 子内容。
  Text 样式对齐 Ant Design `Typography.Text`（`type`、`strong`、`italic`、`underline`、`delete`、`code`、`mark`、`keyboard`、`disabled`、`ellipsis`）；宿主 `appearance.style` 最后覆盖外观。
  Button 文案使用 `value`，样式对齐 Ant Design `Button` 属性（`type`、`color`、`variant`、`shape`、`size`、`danger`、`ghost`、`disabled`、`block`、`loading`、`href`、`target`、`icon`、`iconPlacement`、`aria-label`）：`color` 使用 Ant Design 预设色，`loading` 支持布尔或 `{delay}`，`href` 走 HTTP(S)/相对地址校验，`target` 需与 `href` 同时声明；纯图标按钮要求非空 `aria-label`。
- C-31：Icon 是叶子组件，只接受图标白名单中的 `name`（含 `refresh` 兼容别名），不声明子节点与事件。
  `size` 可选 small/medium/large，兼容 sm/md/lg，缺省 medium；省略 `aria-label` 时按装饰图标渲染 `aria-hidden`，
  提供时必须非空并作为可访问名称。宿主不接受白名单外的名称、任意 SVG、className 或 style。

例如 Input 的 `ui.change` 接受 `{value: "北京", isComposing: false}`，
缺少 `isComposing` 或传入额外字段时，事件数据规则返回错误。
Select 与 TreeSelect 的 ui.change 用于承载整体选择值，ui.select 仍表示本次选择的单个选项。

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

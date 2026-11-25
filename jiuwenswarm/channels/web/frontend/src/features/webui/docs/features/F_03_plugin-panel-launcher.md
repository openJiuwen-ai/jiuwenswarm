# 右侧工作区插件页签与空白页

建档日期：2026-11-06。
最近修订：2026-11-24。

关联契约：[S_17 WebUI 组件 Props 权威规约](../../../../../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)、
[F_08 前端渲染](../../../../../../../../extensions_v2/docs/features/F_08_frontend-render.md)。

## 范围与职责

右侧工作区通过 `conversation.panel.right` 槽位收集插件，由用户从页签栏的加号主动创建页签：

- 每次点击加号新增一个**不属于任何插件的空白页签**并激活；空白页签有自己的标题和关闭按钮；
- 空白页签内容是已注册插件列表，点击某个插件后**这个空白页签原地变成该插件页签**，不额外新增页签；
- 若目标插件已经打开，则关闭当前空白页签并激活已有的插件页签，同一个插件实例不会出现两个页签；
- 插件页签与空白页签使用同一套关闭逻辑；没有可用插件时空白页签显示“暂时没有可用的插件”。

插件侧无新增接口：仍按既有 `manifest.yaml` 声明槽位，仍以渲染树根组件的 `title` 作为页签名，
宿主从既有 `ui.ready` → 渲染帧链路取得发现结果。宿主不解析 manifest，也不新增发现请求。

## 采用方案

`useWebUIPanels` 继续负责挂载槽位和发现插件，并为每个条目补充跨会话稳定的
`panelId = packageId|instanceKey`；实例键仍用于渲染。宿主侧通过
[useWebUIPluginPanelTabs.tsx](../../react/useWebUIPluginPanelTabs.tsx) 拿到页签列表、空白页签动作和
裁剪逻辑，不直接接触状态细节；状态由 [pluginPanelTabs.ts](../../react/pluginPanelTabs.ts)
的纯函数维护，并放在 [pluginPanelTabsStore.ts](../../react/pluginPanelTabsStore.ts) 的按槽位注册的
内存 store 中：右侧面板被隐藏或卸载（窄屏、离开聊天页）不丢页签，页面刷新仍从空集合重新收集；
以后新增插件面板区域可按 `slotId` 复用同一套 store 与 hook，互不共享页签。

状态字段：

| 字段 | 含义 |
| --- | --- |
| `tabs` | 有序页签 id：`plugin:<panelId>` 表示插件页签，`draft:<seq>` 表示空白页签 |
| `activeId` | 当前激活的页签 id；`null` 表示原生页签 |
| `draftSeq` | 空白页签的单调序号，保证每次加号都有稳定且不重复的页签标识 |

行为规则：

1. 点击加号追加一个空白页签并激活；连续点击会产生多个彼此独立的空白页签。
2. 在空白页签选择插件：把该空白页签原地替换为插件页签并激活；目标插件已打开时删除空白页签、
   激活已有插件页签；空白页签已不存在时（例如已被关闭）追加插件页签，避免丢失用户选择。
3. 关闭非激活页签时保留当前页面。关闭激活页签时，依次选择右侧、左侧最近的可见页签。
   没有可见邻居时回到当前模式的原生页签。
4. 选择原生页签会退出插件页签，原生页签状态照旧由会话 UI 维护。聊天区代码审查、打开文件等外部入口
   在选择或打开原生页签时同步清除插件激活状态；即使原生页签值没有变化或面板尚未挂载，也会生效。
   已添加的插件页签与空白页签保留，单纯收起和展开面板仍保留插件选择。
5. 关闭只从页签列表移除条目，不记录“已关闭”名单；插件仍在发现结果中，因此新的空白页签仍列出它
   （不再显示“已添加”），可随时重新选择。面板挂载期间插件实例仍由槽位消费者持有，重新选择立即显示当前帧。
6. 页签在切换对话时保留（同一 `panelId` 在目标会话重新出帧后继续显示）；页面刷新后重新收集。
7. 切换对话时重建发现结果的比较基线。发现结果分为可见与偏好隐藏两列：仅被实例级
   `visible_when` 客户端偏好隐藏的插件页签**停靠**（保留在页签列表、仅摘除激活，页签栏只渲染
   可见发现结果命中的页签），偏好重新开启后回到原位且不自动选中；只有同时离开两列的贡献
   （收到 `webui.render.clear` 或卸载）才被永久移除，隐藏期间被清除的贡献同样移除、不复活。
   激活的贡献消失或被停靠时回到原生页签。

空白页签样式沿用语义主题变量，空态使用共享 `EmptyState`。插件条目在名称右侧显示“添加”或“已添加”，
让整卡可点击的行为有明确提示。插件较多时列表纵向滚动（页面根容器 `overflow-y: auto`），
页签栏使用原生横向滚动，激活页签或页签数量变化时在绘制前定位一次。滚动容器固定为 52px 高，
通过 `overflow-x: scroll` 始终预留滚动条空间，页签从固定的顶部位置排布，避免溢出前后与按钮错位。
Chromium 使用 6px 原生滚动条并隐藏两端箭头，其他引擎使用标准细滚动条样式；滑块平时透明，
悬浮页签栏时上色，拖拽由浏览器处理。加号不在滚动容器内，而是与页签列表并列、紧贴列表右边界：
页签不满一行时它跟在最后一个页签后面，溢出时页签在它左侧被裁切并滚动，加号不会压在页签上。
放大/收起按钮固定在右侧操作区。

新页签以最终宽度淡入（200 ms），滚动定位在淡入前完成。插件页签和空白页签关闭时按实测宽度收回并淡出（140 ms），
动画结束后移除，关闭期间页签不可点击。浏览器和应用预览页签即时关闭。`prefers-reduced-motion: reduce` 下
跳过入场和关闭动画。

页签图标：Panel / Iframe 根可以声明可选 `icon`（S_17 的 `IconName`），宿主从图标白名单解析后用于
插件页签和空白页签里的插件条目；省略或无法解析时回退到默认插件图标，空白页签自身也使用默认插件图标。
除该可选字段外，组件字段、默认值和事件未变化。

## 取舍与边界

- **不自动打开**：旧行为把发现到的插件全部挂成页签，本次改为用户从空白页签显式选择，避免插件占用页签栏。
- **空白页签也是普通页签**：它参与激活、关闭与相邻回退，不需要额外的“页面态”字段。
- **仅在展开面板提供入口**：收起态悬浮面板保持原有块；插件页签只属于展开面板。
- **不持久化**：插件贡献是运行期发现结果，刷新后以新的发现结果重新收集；页面不保存插件快照。
- **不做插件级开关**：启停仍由扩展管理链路负责；宿主只消费发现结果。
- **标题与图标来源**：仍要求根组件 `title` 为非空字符串；新增可选 `icon` 只用于页签显示，
  不新增对 manifest `panel.title` / `panel.icon` 的使用。
- **关闭会卸载实例视图**：关闭页签后运行时保留插件实例与最新帧，但 `WebUIInstanceView` 卸载，
  未写入帧的控件本地状态（展开项、输入草稿等）不保留；重新选择按当前帧重新挂载。

## 验证

- `npm run test:webui-plugin-panels`：28 项通过，包含原有 19 项状态规则和新增 9 项原生入口回归验证。
  新增用例覆盖单 Agent / 团队模式切换到代码审查、重复打开同一原生页签、收起后再展开，以及面板未挂载时
  打开浏览器 / 产物；修复前 7 项失败，修复后全部通过。新增回归通过 JSDOM 验证，尚未在真实浏览器验收。
- `npm run test:i18n-locales`：4 项通过，zh/en 键结构与静态引用一致。
- 共享组件用例新增 Panel / Iframe 的合法 `icon`（folder-open、globe）与非法 `icon`（未知名称、非字符串）；
  前端契约套件 152 项通过，AgentServer 与 Gateway 的 `validate_props` 定向校验均按预期接受 / 拒绝。
- 上一轮 Edge 手工验收覆盖了插件列表的两列卡片、hover 描边、“添加/已添加”提示、连续添加两个插件和
  关闭当前页签回退相邻页签；本轮“加号先建空白页签、选择后原地转换”的交互尚未在真实浏览器验收。
  空态、长标题、多页签溢出、窄屏、会话切换和键盘焦点同样待覆盖。
- 2026-11-24 行为规则 7 修正：仅被实例级 `visible_when` 客户端偏好隐藏的插件页签停靠而非移除，
  偏好重新开启后原位恢复且不自动选中（对齐 S_17 §2.4 与 Token Weather 验收记录）；只有同时离开
  可见与隐藏两列发现结果的贡献才永久移除。此前“只移除从可见变为不可见的页签”的表述把偏好隐藏
  误当作贡献消失，导致偏好往返后页签被误删。
- 2026-11-09 页签栏滚动条改为 4px 细条，仅在悬浮或键盘聚焦时显示；滚动条仍走 `::-webkit-scrollbar`，
  非 Chromium 引擎经 `@supports not selector(::-webkit-scrollbar)` 继续隐藏。
- 2026-11-09 加号位置试了两版后定稿：先试“新增后滚动到末端”，生长动画期间内容宽度一直在变，真实浏览器
  里加号位置仍不稳定；再试 `position: sticky` 贴右边界，页签会从按钮下面滑过，视觉上是加号浮在页签上。
  最终改为页签列表外、与列表并列的独立按钮：外层 `.single-agent-tool-tabs__strip` 收缩到内容宽度，
  列表 `flex: 0 1 auto` 在空间不足时收窄并裁切，加号始终有自己的位置，既不覆盖页签也不会被挤出可视区。
- 2026-11-09 页签出入场动画：出现由 `panel-tab-appear` 改成零宽度挤出加回弹，关闭走
  `panel-tab-collapse`（起始宽度由 JS 实测后经 `--tab-shrink-from` 传入，`auto` 无法插值）。
  关闭改为延迟 140 ms 再调用原 `onTabClose`，因此浏览器页签的关闭同样延后 140 ms；面板卸载时
  未完成的关闭会立即补发，避免点击被吞。
- 2026-11-09 滚动条改为按实测溢出渲染：此前页签明明放得下，悬浮时仍会闪一下 4px 细条——原因是亚像素
  取整让 `scrollWidth` 比 `clientWidth` 大不到 1px，Chromium 就按“可滚动”处理并预留轨道。现在宿主用
  `scrollWidth - clientWidth > 1` 判断后加 `--scrollable` 类，未溢出时 `::-webkit-scrollbar` 高度为 0，
  不预留空间；页签数变化、入场动画结束和容器尺寸变化（`ResizeObserver`）时重新测量。
- 上述 2026-11-09 调整当时未运行前端测试，也未在真实浏览器重新验收；其滚动条与入场动画方案已由下述修订替代。
- 2026-11-10 改为原生滚动条与固定高度预留，移除自绘滑块、尺寸监听及延迟二次定位；入场动画改为按最终宽度淡入。
  `npm run test:panel-pages` 的 9 项组件测试通过，包含新增页签仅定位一次、标题更新保留手动滚动位置的回归检查。
  用户已实际测试并确认交互良好；本轮未使用 computer use 或预览图，其他浏览器与系统组合未逐一验收。
- Vite 生产构建未执行，不能把未执行写成通过。
- 2026-11-20 在 JSDOM 中验证另一插件清空时保留尚未渲染的统计页签、关闭空白页签时跳过不可见邻居，以及
  关闭浏览器页签后切换会话。面板组件测试 12 项、页签状态测试 28 项通过。

## English summary

The right workspace discovers plugins through the existing `conversation.panel.right` slot. Each plus
click creates a blank, plugin-agnostic tab that lists the registered plugins; choosing one converts
that same tab into the plugin tab in place. Blank and plugin tabs share activation and close rules:
closing the active tab selects the nearest visible tab to its right, then to its left, and falls back to the
native page when neither is available. Choosing an already open plugin closes the blank tab and
activates the existing one. Tabs survive conversation switches in memory and are re-collected after a
page reload. Discovery splits into visible and preference-hidden results: a tab whose contribution
is hidden only by the instance-level `visible_when` client preference stays parked (kept in the tab
list with its activation dropped, since the tab bar only renders tabs backed by the visible result);
re-enabling the preference restores it in place without re-selecting it. Only contributions that
left both results (`webui.render.clear` or unmount) are permanently removed, including ones cleared
while hidden.
Native navigation requests, including review and
file-opening actions, clear plugin selection even when the native tab value is unchanged or the host is
unmounted; existing tabs remain available, and collapsing the panel preserves selection. Plugins declare nothing new: the manifest slot
and the render-tree root `title` remain the contract.

The tab strip uses a native horizontal scrollbar in a fixed-height container, reserving its lane
even when tabs fit. The thumb is transparent until the header is hovered. New tabs fade in at their
final width and are aligned once before paint; there is no custom thumb or delayed second scroll.

# 插件页面布局配置

application.entry 提供整个视口的承载区域。插件使用通用 Box、Image、Card 和 Form 组合页面；没有登录专用布局组件，也不需要额外引入 AntD Layout/Grid/Splitter。

## 通用属性

| 组件 | 属性 | 约束 |
| --- | --- | --- |
| Box | width、height、minHeight、maxWidth | 有界数值表示 px，或 px/% 字符串；width/height 另支持 auto；尺寸上限 4096px 或 100% |
| Box | padding | none/sm/md/lg，使用 AntD 主题间距 |
| Box | marginInline | auto、非负数值或 px/%；数值按 px，尺寸上限与 width 相同，直接使用 CSS margin-inline |
| Box | gap | none/sm/md/lg 或非负有限数值；数字直接作为 AntD Flex.gap 的 px 值，由插件指定 |
| Box | backgroundImage、backgroundFit | 插件图片 URL；fit 为 cover/contain，默认 cover，居中且不重复 |
| Box | visibleFrom | sm/md/lg 分别从 576/768/992px 开始显示；隐藏区域同时停用交互 |
| Image | fit | contain 完整显示 Logo/宣传图，cover 裁切填满；其余尺寸和 preview 沿用已有属性 |

背景地址、Logo、宣传图、文案和规则由插件提供。官网只作为视觉参考，本实现不复制官网背景，不内置登录页面素材。

## 布局语义

Box.direction 控制横向或纵向排列，align/justify 控制子节点对齐，gap 控制节点间距。width=100% 相对父容器的可用内容区计算；maxWidth 限定内容区上限，marginInline=auto 在可用空间内提供对称留白。具体尺寸和间距由插件声明。

插件可用 visibleFrom 隐藏指定断点以下的内容。隐藏节点不占布局空间，其他节点继续按父容器的对齐配置排列；宿主不根据宣传图、表单或其他业务角色决定显示方式。

背景图和 Image.src 均为插件提供的 URL。受保护的 Gateway 资源经通用资源适配器获取，外部资源不携带请求绑定值。

## English contract summary

Entry fills the viewport. Plugins compose pages with generic Box, Image, Card and Form components. The host defines slot placement and baseline spacing; it does not embed a login template or artwork.

Box.direction, align and justify control arrangement and alignment. Numeric gap is passed directly to native AntD Flex.gap in pixels. Width percentages resolve against the available parent content area. maxWidth and native marginInline=auto allow bounded content with symmetric margins. Specific dimensions and spacing belong to the plugin declaration.

Box.visibleFrom uses minimum widths of 576/768/992px. Hidden nodes leave the layout and stop accepting interaction; remaining nodes follow their parent alignment. Image.fit supports contain/cover. Backgrounds and images are plugin-provided resource URLs. Gateway resources use the shared resource adapter; external URLs receive no request binding values.

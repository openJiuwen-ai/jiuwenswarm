# 前端系统插件开发

涉及页面、嵌入网页、背景或桌面形态时，按当前需要阅读本文件对应小节。
先从 [示例索引](example.md) 选择相近实现，再核对目标宿主的组件和槽位，不从页面外观猜运行域。

## 1. 先找已有能力

| 用户需要 | 优先参考 |
| --- | --- |
| 会话侧栏、上下文和执行过程 | 上下文驾驶舱，AgentServer 的事件与 `ops.session` |
| 团队、成员、任务与依赖图 | 团队协作可视化，Core 的 `team.monitor_event` 与 `ops.team` |
| 登录、凭证和应用入口 | 登录示例，Gateway 的请求链、客户端能力和应用槽位 |
| 背景、配色、启动动画 | 主题背景，Gateway 的 MediaSurface、外观和开场 Ops |
| 桌宠或桌面形态 | 应用形态，Gateway 的 ApplicationSurface 与形态资源 |
| 应用界面与会话行为共同变化 | Harness 工作台组合包，按两域职责拆分成员 |

Swarm 前端目录为 `<SWARM_ROOT>/jiuwenswarm/channels/web/frontend/src/features/webui/`，按问题直接定位：

| 当前问题 | 该目录下的文件 |
| --- | --- |
| 有哪些槽位、支持几项、怎样展示 | `catalog/slots.ts`，例如搜索 `conversation.input.below` |
| 组件名和 props 是否支持 | `catalog/componentCatalog.ts` 和 `catalog/components/` 中对应组件 |
| 组件与槽位的使用规范 | `docs/specs/S_01_components.md` 中对应小节 |
| 槽位在哪里挂载、何时发送 `ui.ready` | `react/WebUIHost.tsx` 与相关 `WebUI*Host.tsx`；只在核对挂载行为时读取 |

先查清单中的对应项，再核对所属域的后端规则和直接测试，不从仓库根搜索槽位。AgentServer 与 Gateway 支持范围分别核对；示例有的能力不能直接推为旧版宿主也有。
某个名称未命中时，按 [源码查找说明](source-reading.md#3-报告能力缺失前的核对) 补查公开导出与实际调用点，再判断缺口。

## 2. 嵌入网页与权限

WebUI 的 `Iframe` 是受管组件，原始 HTML 属性由宿主生成。常见需求分别处理：

| 需求 | 写法与前提 |
| --- | --- |
| HTML 中执行脚本 | 基础沙盒已有 `allow-scripts`，不用伪造组件属性 |
| 网页使用自己的 localStorage、IndexedDB | 用 `src` 指向独立来源的 HTTP(S) 网页，核对宿主已启用文档来源隔离 |
| 摄像头、麦克风、全屏等 | 在 `Iframe.props.permissions` 列出所需浏览器能力，页面照常调用浏览器 API |
| 只预览内联内容 | 使用 `srcDoc`；它不能保留正常来源，声明 permissions 也不能补齐来源或存储能力 |

```json
{
  "component": "Iframe",
  "props": {
    "title": "会议室",
    "src": "https://meeting.example.com/room",
    "permissions": ["camera", "microphone", "fullscreen"]
  }
}
```

这里只示范字段，实际 URL 换成允许嵌入的目标网页。宿主把 `permissions` 转成 `allow`；不要把 `sandbox` 或 `allow` 原始属性塞进 WebUI props。
内置 Personal Web 在来源隔离启用时，会为与工作台不同来源的 HTTP(S) `src` 添加 `allow-same-origin`，保留网页自己的存储来源；同源 URL、`srcDoc` 和 `file://` FrontendOnly 入口沿用基础沙盒。
这不允许网页读取工作台的 DOM 或存储。摄像头、麦克风仍需安全上下文、浏览器或 Electron 授权及操作系统权限。

## 3. HTML 内又嵌套 iframe

系统插件提供的 HTML 内部再放一个 iframe 时，内层也要按需要声明；权限不能突破外层或响应头的限制。

```html
<iframe title="会议室" src="https://meeting.example.com/room"
        sandbox="allow-scripts allow-same-origin allow-forms"
        allow="camera; microphone; fullscreen"></iframe>
```

- **属性区别：** `allow-scripts`、`allow-same-origin` 是 `sandbox` 的空格分隔值；摄像头等是 `allow` 的分号分隔值。不是 `allow="allow-script、allow-same-origin"`。[HTML 属性说明](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe)
- **逐层核对：** 外层 `srcDoc` 或宿主资源响应仍限制来源时，内层写了 `allow-same-origin` 也不能解除祖先限制。依赖存储的应用优先直接用受支持的 `Iframe.src` 加载独立来源网页，避免多套一层受限 HTML。
- **页面限制：** 检查目标网页的 CSP、`frame-ancestors`、X-Frame-Options、Permissions-Policy 和重定向；跨源不是可任意嵌入的保证。
- **访问边界：** 只声明本次需要的权限，不向不可信的宿主同源页面同时授予脚本和同源能力，不通过关闭隔离绕过失败。

## 4. 怎样验证

先用隔离宿主验证 UI 声明、事件和真实下行帧，再检查目标入口的来源、权限和必要业务操作。
区分“接口不存在”“当前发行方式不支持”“权限未声明”“网页拒绝嵌入”和“用户尚未授权”，给出对应处理，不一概要求平台新增原生 webview。
真实设备、外部网站和桌面窗口未验证时按事实记录；安装后有界面变化，仍由主流程的确认卡片完成用户验收。

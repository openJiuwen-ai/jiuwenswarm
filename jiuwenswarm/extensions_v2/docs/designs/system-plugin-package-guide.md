# JiuwenSwarm 系统插件包开发指南

| 项 | 说明 |
| --- | --- |
| 类型 | 开发指南；系统插件包（`package_type: extension`）的清单与说明文档写法 |
| 最近修订 | 2026-11-06 |
| 所属模块 | `jiuwenswarm/extensions_v2/` |
| 字段契约 | [S_02 §1.2、§1.3](../specs/S_02_backend-package-management.md) |
| 界面项 | [F_07](../features/F_07_ui-contributions.md)、[S_17](../specs/S_17_webui-component-props.md) |
| Gateway 请求绑定 | [S_06](../../../gateway/extensions_v2/docs/specs/S_06_local-browser-bridge.md) |
| 示例包 | [token_weather_glass](../../examples/token_weather_glass)、[auth_login_demo](../../examples/auth_login_demo) |

本文面向插件包作者，说明一个系统插件包由哪些文件组成、`manifest.json` 各段如何填写、`README.md` 应说明哪些内容。字段语义以 S_02 为准，本文给出可直接改用的写法与交付前自检项。

## 1. 包结构

```text
example.fs-probe/
├── manifest.json          # 唯一清单
├── README.md              # 中文说明
├── README_EN.md           # 英文说明
├── assets/
│   ├── avatar.png         # 可选，≤ 1 MB
│   └── preview.png        # 可选，≤ 10 MB
└── fs_probe/
    └── extension.py       # extensions[].module 指向的入口
```

- 发布形态为 `.zip`，解压后根目录直接是包内容，不再套一层目录；根目录只能有一份 `manifest.json`。
- 包内路径不得越出包根，不接受符号链接、绝对路径和 `..`。
- 安装后落在 `~/.jiuwenswarm/extension_pkgs/<package.id>/<version>/`，包标识与版本须与目录一致。
- 本地预置包放在 `extensions_v2/examples/` 下；修改源目录不会更新已安装副本，需重新安装或重载。

安装后平台依次完成：解压到安装副本 → 解析 `manifest.json` → 加载 `extensions` 模块并执行注册 → 按 `ui` 段发布界面项。事件到达时只调用已注册的 Handler，README 描述的事件必须在代码中真实注册。

## 2. manifest.json

### 2.1 完整示例

```json
{
  "schemaVersion": 1,
  "package_type": "extension",
  "expose": true,
  "package": {
    "id": "example.fs-probe",
    "version": "1.0.0",
    "domain": "agentserver"
  },
  "display": {
    "name": { "zh": "文件系统探针", "en": "FS Probe" },
    "description": {
      "zh": "扫描会话工作区，读取和写入文件。",
      "en": "Scans the session workspace and reads or writes files."
    },
    "tags": [{ "zh": "文件系统", "en": "Filesystem" }],
    "avatar": "assets/avatar.png",
    "details": {
      "zh": "## 用法\n安装后在右侧面板点击扫描。",
      "en": "## Usage\nAfter installation, click Scan in the right panel."
    },
    "events": [
      {
        "name": "context.usage.updated",
        "description": { "zh": "会话上下文变化时刷新面板", "en": "Refresh the panel when context usage changes" }
      }
    ]
  },
  "extensions": [
    { "id": "fs-probe", "runtime": "backend", "module": "fs_probe/extension.py" }
  ],
  "ui": {
    "apiVersion": 1,
    "items": [
      {
        "type": "web.slot.contribution",
        "id": "fs-probe.panel",
        "slot": "conversation.panel.right",
        "order": 30,
        "renderer": { "kind": "webui-tree" }
      }
    ]
  }
}
```

### 2.2 字段速查

| 字段 | 必填 | 取值与写法 |
| --- | --- | --- |
| `schemaVersion`、`package_type` | 是 | 固定为 `1`、`extension` |
| `package.id` | 是 | 1～128 字符，字母或数字开头，可含点、下划线、连字符；不含 `.` 结尾与 `..` |
| `package.version` | 是 | 语义化版本，与安装目录一致 |
| `package.domain` | 是 | `agentserver` 或 `gateway`，缺失或其它值安装失败 |
| `expose` | 否 | 仅本地预置包使用；为 `true` 才进入广场。对外分发的包不写该字段 |
| `display` | 否 | 见 2.3 |
| `extensions[]` | 是 | `id`、`runtime=backend`、`module`（包内相对路径） |
| `ui` | 否 | `apiVersion=1`，`items[]` 见 2.4 |
| `client` | 否 | 仅 `package.domain: gateway`：`requestBindings`，格式见 S_06 |

`package.domain` 决定由 AgentServer 还是 Gateway 安装并加载，同一个包只归属一个宿主。清单只描述包身份、展示信息、入口与界面位置；运行参数和事件注册关系由扩展代码在注册时声明。

### 2.3 display

| 字段 | 写法 |
| --- | --- |
| `name` | `{zh, en}`，缺省回退包 ID |
| `description` | `{zh, en}`，一句话说明能力范围 |
| `tags` | 双语文本数组，2～4 个为宜 |
| `avatar` | 包内相对路径，支持 png、svg、jpg、jpeg、webp，≤ 1 MB |
| `details` | `{zh, en}` Markdown，补充用法与配置说明 |
| `events` | `[{name, description}]`，列出插件注册的事件 |

所有文案统一写 `{zh, en}` 对象；单语字符串按双语同值处理。`events` 只用于说明插件依赖哪些事件，不影响加载校验，但应与代码中注册的事件保持一致。

### 2.4 ui

- `items[].type` 只接受 `web.slot.contribution`，`renderer.kind` 只接受 `webui-tree`。
- `slot` 取 [F_07 §3.3](../features/F_07_ui-contributions.md) 的白名单，常用位置：`conversation.panel.right`、`conversation.input.above`、`conversation.input.below`、`message.assistant.meta`、`application.entry`、`application.sidebar.footer`。
- 一个包只要有一项声明非法，整包界面项都不发布；新增或改动界面内容后需重载验证。
- 组件属性、嵌套与事件以 [S_17](../specs/S_17_webui-component-props.md) 为准。

## 3. README.md

`README.md` 面向使用者介绍插件，讲清功能、生效机制和带来的价值即可。`display.details` 承载安装、配置等补充说明，两者不重复。

建议结构，篇幅控制在 60 行左右：

1. 功能：插件解决什么问题，提供哪些能力。
2. 生效机制：在什么时机触发，依赖哪些事件或调用哪些 ops，产生什么结果。
3. 优势：相比平台默认行为带来什么改善，适合哪些场景。

写作要求：

- 以使用者视角描述行为和价值，不写类名、行号等实现细节，不贴截图。
- 事件名、ops 名称与代码保持一致，避免同义改写。
- 一个主题一段，篇幅简短，不堆砌背景和重复清单。
- 中文写 `README.md`，英文写 `README_EN.md`，两篇章节一一对应。

### 3.1 结构模板

```markdown
# <插件名>

<一句话说明插件做什么>

## 功能

- <能力一>
- <能力二>

## 生效机制

<在什么时机触发，通过哪些事件和能力处理，产生什么结果>

## 优势

<带来的改善与适用范围>
```

## 4. assets

| 文件 | 约束 | 说明 |
| --- | --- | --- |
| `assets/avatar.*` | ≤ 1 MB | 包图标，png、svg、jpg、jpeg、webp |
| `assets/preview.png` | ≤ 10 MB | 能力预览图，固定路径与文件名 |
| `README.md`、`README_EN.md` | ≤ 64 KiB | 单一语言缺失时回落到另一语言，超出部分截断 |

头像与预览图超限时按缺失处理；README 超过限制时只保留前 64 KiB。这些情况都不影响包加载。

## 5. 交付前自检

1. ZIP 根目录只有一份 `manifest.json`，解压后目录层级与本文第 1 节一致。
2. `package.id`、`package.version` 与安装目录、分发记录一致，版本已递增。
3. `extensions[].module` 指向的文件存在，且位于包根内。
4. `display.events` 与代码注册的事件一致；`ui.items[].slot` 在白名单内。
5. 中英文案齐备，`README.md` 与 `README_EN.md` 章节对应。
6. 资源文件未超过第 4 节限制。
7. 安装后重载，按 README 描述的触发方式验证一次，结果与说明一致。
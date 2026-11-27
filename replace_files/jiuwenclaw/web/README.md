# OpenJiuwen Web 前端

基于 React + TypeScript + Tailwind CSS 构建的 AI 编程助手 Web 界面，设计风格参考 JiuwenClaw。

## 功能特性

### 已实现功能

#### 💬 聊天交互
- **实时对话**：WebSocket 双向通信，支持流式输出
- **Markdown 渲染**：支持代码高亮、列表、链接等格式
- **思考动画**：AI 思考时显示动态指示器
- **消息历史**：显示用户和助手的对话记录

#### 🛠 工具调用
- **工具执行可视化**：显示 AI 调用的工具名称和参数
- **执行结果展示**：显示工具执行成功/失败状态和返回结果
- **可用工具列表**：右侧面板显示当前可用的工具

#### 📋 任务管理
- **Todo 列表**：显示 AI 创建的任务列表
- **状态分组**：按进行中、待处理、已完成分组显示
- **实时更新**：任务状态变化实时同步

#### 📂 会话管理
- **会话列表**：侧边栏显示历史会话
- **会话切换**：点击切换不同会话
- **会话删除**：悬停显示删除按钮，支持删除会话
- **会话持久化**：刷新页面自动恢复上次会话

#### ⚙️ 模式切换
- **BUILD 模式**：默认编码模式
- **PLAN 模式**：规划模式
- **REVIEW 模式**：审查模式

#### 🎨 主题支持
- **浅色主题**：默认，蓝色基调
- **深色主题**：深色背景，优化蓝色可见度
- **系统跟随**：可选跟随系统主题

#### ⏯ 流程控制
- **暂停/继续**：暂停和恢复 AI 处理
- **中断**：中断当前任务，可附加新指令

#### 🎤 语音交互
- **语音输入**：点击麦克风按钮进行语音输入（STT）
- **语音朗读**：鼠标悬停在 AI 回复上显示朗读按钮（TTS）
- **打断演示**：语音输入时可随时打断 AI 处理

## 技术栈

- **框架**：React 18 + TypeScript
- **样式**：Tailwind CSS + CSS Variables
- **状态管理**：Zustand
- **构建工具**：Vite
- **通信**：WebSocket + REST API

## 快速开始

### 环境要求

- Node.js 18+
- npm 或 pnpm

### 安装依赖

```bash
cd jiuwenclaw/web
npm install
```

### 配置后端地址

编辑 `vite.config.ts` 中的 proxy 配置：

```typescript
proxy: {
  '/api': {
    target: 'http://127.0.0.1:19000',  // 修改为你的后端地址
    changeOrigin: true,
  },
  '/ws': {
    target: 'http://127.0.0.1:19000',  // 修改为你的后端地址
    ws: true,
    changeOrigin: true,
  },
}
```

### 启动开发服务器

```bash
npm run dev
```

访问 http://localhost:5173

### 启动后端

```bash
cd jiuwenclaw
PORT=19000 python -m jiuwenclaw.api.app
```

## 后端 API 要求

前端依赖以下后端接口：

### REST API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/config` | GET | 获取服务配置（provider, model 等） |
| `/api/sessions` | GET | 获取会话列表 |
| `/api/sessions/:id` | DELETE | 删除会话 |

### WebSocket

连接地址：`/ws`

说明：
- 前端当前通过统一 WS 端点进行 RPC 通信（`req/res/event`）
- `session_id` 通过请求体 `params.session_id` 传递，不在 URL 路径中传递
- `provider` 默认由后端配置决定；仅在需要覆盖后端默认配置时，才通过 query 传递

#### 客户端 → 服务端消息

统一请求帧（示例）：

```json
{
  "type": "req",
  "id": "req_xxx",
  "method": "chat.send",
  "params": {
    "session_id": "sess_xxx",
    "content": "hello"
  }
}
```

常用 `method`：

| method | 说明 |
|------|------|
| `chat.send` | 发送聊天消息 |
| `chat.interrupt` | 中断/暂停任务 |
| `chat.resume` | 恢复任务 |
| `chat.user_answer` | 提交用户问答结果 |
| `session.list` | 获取会话列表 |
| `config.get` | 获取服务配置 |

#### 服务端 → 客户端消息

请求响应帧（`res`）：

```json
{
  "type": "res",
  "id": "req_xxx",
  "ok": true,
  "payload": {}
}
```

事件推送帧（`event`）：

```json
{
  "type": "event",
  "event": "chat.delta",
  "payload": {
    "session_id": "sess_xxx"
  }
}
```

常见事件：
- `connection.ack`
- `chat.delta`
- `chat.final`
- `chat.tool_call`
- `chat.tool_result`
- `todo.updated`
- `chat.processing_status`
- `chat.interrupt_result`
- `chat.subtask_update`
- `chat.ask_user_question`

## Dev 模式 WS 日志

`npm run dev` 时，前端会把 `/ws` 的请求与响应写入本地日志文件，用于排查通信问题：

- 日志文件：`jiuwenclaw/web/logs/ws-dev.log`
- 每行一条 JSON（JSONL）
- 记录方向：
  - `payload.direction = "outgoing"`：前端发送的 `req`
  - `payload.direction = "incoming"`：后端返回的 `res/event`
  - `payload.direction = "lifecycle"`：连接生命周期（open/error/close）

示例：

```json
{"ts":"2026-02-12T08:10:05.120Z","payload":{"direction":"outgoing","messageType":"req","data":{"type":"req","id":"req_xxx","method":"chat.send","params":{"session_id":"sess_001","content":"你好"}},"at":"2026-02-12T08:10:05.119Z"}}
{"ts":"2026-02-12T08:10:05.150Z","payload":{"direction":"incoming","messageType":"res","data":{"type":"res","id":"req_xxx","ok":true,"payload":{"accepted":true}},"at":"2026-02-12T08:10:05.149Z"}}
{"ts":"2026-02-12T08:10:05.300Z","payload":{"direction":"incoming","messageType":"event","data":{"type":"event","event":"chat.delta","payload":{"session_id":"sess_001","content":"..."}},"at":"2026-02-12T08:10:05.299Z"}}
```

若只看到 `lifecycle error/close (code=1006)`，通常表示后端未启动或 WS 端口不可达。

## 项目结构

```
jiuwenclaw/web/
├── public/
│   └── logo.png           # 应用 Logo
├── src/
│   ├── components/
│   │   ├── ChatPanel/     # 聊天面板
│   │   ├── SessionSidebar/# 会话侧边栏
│   │   ├── StatusBar/     # 状态栏
│   │   ├── TodoList/      # 任务列表
│   │   └── ToolPanel/     # 工具面板
│   ├── hooks/
│   │   └── useWebSocket.ts# WebSocket Hook
│   ├── stores/
│   │   ├── chatStore.ts   # 聊天状态
│   │   ├── sessionStore.ts# 会话状态
│   │   └── todoStore.ts   # Todo 状态
│   ├── types/             # TypeScript 类型定义
│   ├── utils/             # 工具函数
│   ├── App.tsx            # 主应用组件
│   ├── index.css          # 全局样式 + CSS 变量
│   └── main.tsx           # 入口文件
├── index.html
├── tailwind.config.js
├── vite.config.ts
└── package.json
```

### 建议优先实现

1. **侧边栏详情面板** - 查看工具调用的完整输出
2. **聊天附件** - 支持上传文件
3. **Config 配置** - 可视化配置编辑
4. **Logs 日志** - 调试和问题排查必备
5. **Exec Approval** - 安全相关，防止危险命令执行

## 自定义配置

### 修改品牌

1. 替换 `public/logo.png`
2. 修改 `index.html` 中的 `<title>`
3. 修改 `src/App.tsx` 中的品牌文字

### 修改主题颜色

编辑 `src/index.css` 中的 CSS 变量：

```css
:root {
  --accent: #60a5fa;        /* 深色模式主色 */
  --accent-hover: #93c5fd;  /* 悬停色 */
}

:root[data-theme="light"] {
  --accent: #2563eb;        /* 浅色模式主色 */
  --accent-hover: #3b82f6;  /* 悬停色 */
}
```

## License

MIT

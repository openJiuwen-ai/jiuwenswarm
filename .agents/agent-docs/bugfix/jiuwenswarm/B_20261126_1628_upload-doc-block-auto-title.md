---
id: B_20261126_1628_upload-doc-block-auto-title
title: 会话自动标题被【上传文档】协议块污染
type: B
author: whds5279
author_email: 2755136018@qq.com
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/channels/web/frontend/src/utils/documentMessage.ts
  - jiuwenswarm/channels/web/frontend/tests/uploadDocumentBlock.test.mjs
  - tests/unit_tests/server/test_session_metadata.py
designs: []
design_note: 修复既有标题生成与上传协议块注入的跨层契约冲突，不引入新方案。
---

# 会话自动标题被【上传文档】协议块污染

## 现象与影响

英文 UI 下，用户面聊天页单独发送文件（首条用户消息几乎只有上传协议块）时，侧栏会话标题出现中文
`【上传文档】`，常见落盘形态：

- `【上传文档】 @/abs/path/file.pdf`（`_auto_title` 把换行收成空格后写入 `metadata.title`）
- 带提问时：`总结这份文档 【上传文档】 @/abs/path/file.pdf`

预期：带提问时标题只保留提问文本；纯上传时用文件名（basename）；无法取路径则空串，侧栏走
`toDisplaySessionTitle(raw) || fallback`。

关联：GitCode issue #7000 / PR !11008（配对 GitHub #12975）。

## 关联设计与当前契约

无独立设计。依据当前实现契约：

1. 用户面 Web 的 `withUploadDocumentBlock` 在用户提问**之后**追加独占行标记 `【上传文档】` 与
   `@path` / `@"path with space"` 行，供 Agent 识别本轮上传文档。
2. AgentServer 的 `_auto_title` 用首条用户消息生成 `metadata.title`（折叠换行、截断长度）。
3. 侧栏展示经 `toDisplaySessionTitle` → `stripUploadDocumentBlocks`，空串走 fallback。

冲突点：旧 `_auto_title` 把整段消息（含协议块）折叠进标题；协议块标记为固定中文，英文界面下
标题出现硬编码中文。

## 根因与修复决策

**根因**：标题生成契约与【上传文档】协议块注入跨层叠加——前端尾部注入协议文本，后端按全文折叠
生成标题，二者此前无「标题候选须剥离协议块」的约定。

**修复决策**：

1. **剥离优先**：`_auto_title` 在折叠/截断前剥离独占行块、命名变体
   `【上传文档:…】` / `【上传文档：…】`，以及收成单行的
   `(?:^|\s)【上传文档】\s+@[\s\S]*$`。
2. **剩余非空** → 用剩余查询文本作标题。
3. **纯上传**（剥离后空）→ `_title_from_upload_at_path` 取首个 `@path` / `@"path"` 的
   `Path.name`；无可用路径则 `""`，交给侧栏 fallback。
4. **存量坏标题**：不批量改写历史 `metadata.json`；前端 `stripUploadDocumentBlocks` 增加与后端
   **逐字一致**的 collapsed 规则，展示时剥净。

**拒绝的备选**：

- 只改前端展示、不改 `_auto_title`：新会话仍把中文协议块写入 `metadata.title`，其它消费方仍脏。
- 批量迁移历史 metadata：成本高、收益低；展示层剥离已覆盖存量。
- 把协议标记改成英文 i18n：协议是 Agent 侧约定，与标题污染是不同问题；本修复不改块文案。

**前后端同步约束（必守）**：

| 形态 | 后端 `session_metadata.py` | 前端 `documentMessage.ts` |
|---|---|---|
| collapsed | `_UPLOAD_DOC_COLLAPSED_RE`：`(?:^|\s)【上传文档】\s+@[\s\S]*$` | `stripUploadDocumentBlocks` 同形规则 |
| 独占行块 | `_UPLOAD_DOC_BLOCK_RE` 仅 `\n` | 对应规则为 `\r?\n` |

后端块正则只认 `\n`；CRLF 消息依赖 `_UPLOAD_DOC_COLLAPSED_RE` 的 `\s` 兜底（动态实验已证实）。
修改任一侧 collapsed 正则、收窄 `\s`、或「统一」前端去掉 `\r?` 时，必须同步另一侧并回归
CRLF / collapsed 用例。

## 回归证据与适用范围

候选：分支 `fix/upload-session-title` @ `31b400fba7`（文档补登于同分支后续提交）。

- 前端：`npm run test:upload-document-block` — 含 collapsed 坏标题与侧栏展示用例。
- 后端：`pytest tests/unit_tests/server/test_session_metadata.py` — 含带提问、纯上传、collapsed、
  引号路径、内联标记不误伤等 `_auto_title` 用例。
- 检视动态实验：多文件取首 basename、`@"my brief.pdf"`、普通文本含标记词、inline 无空白手打、
  CRLF 等边界与既有规则一致。

未做：历史 `metadata.json` 回填；其它渠道若将来也注入同标记，需复用同一剥离契约。

## 防错模式与资料同步

- 协议块注入点与标题/展示消费点不在同一层时，标题候选必须先按协议形态剥离，再折叠空白。
- 双端同形正则（尤其 collapsed）改一处必改另一处；把 CRLF 兜底关系写进注释或本文，避免静默回归。

## 遗留与后续

- 多文件纯上传标题仅取首个 basename（产品最小实现；可后续 `首文件 +N`）。
- 块在前、提问在后的手打非常规形态：贪婪剥离可能带走提问（与既有块正则一致；正常生成块在末尾）。
- `@""` 极端手打：引号分支可能得到字面 `""`；正常 `formatAtPath` 不产生该形态。

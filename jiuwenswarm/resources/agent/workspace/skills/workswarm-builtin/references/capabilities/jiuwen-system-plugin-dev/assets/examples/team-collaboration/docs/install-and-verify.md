# 安装与验收步骤

## 0. 前置

- 在**运行中的 Swarm 会话内**执行 CLI（独立终端没有 `AGENT_SERVER_PORT` / 实例文件，会报「当前终端没有 Swarm 实例信息」）。
- 用的是桌面发行版：`$env:JIUWENSWARM_EXECUTABLE` → `...\WorkSwarm\resources\backend\workswarm.exe`（`0.2.5.beta1`）。

## 1. 打包

ZIP 根目录直接放 `manifest.yaml`（不要多套一层目录）：

```powershell
Compress-Archive -Path manifest.yaml, README.md, docs, team_collab_viz, web -DestinationPath dist\webui.team-collab-viz-1.0.0.zip -Force
```

## 2. 安装

```powershell
& $env:JIUWENSWARM_EXECUTABLE extension install dist\webui.team-collab-viz-1.0.0.zip --json
& $env:JIUWENSWARM_EXECUTABLE extension query --json
```

`query.before_state.published` 里应出现 `webui.team-collab-viz`，`active` 含 `team-collab-viz`。

## 3. 页签

点任一会话 → 右侧面板 3 秒内出现 **团队信息**、**任务关系** 两个页签（顺序 = manifest 声明顺序，
`order` 字段无语义）。点开页签后：

```powershell
# 从 DOM 取 iframe.src，然后直接请求它（不得连接拒绝）
Invoke-RestMethod "http://127.0.0.1:<src 里的端口>/health" | ConvertTo-Json -Depth 5
```

判定：
- `/health.ok = true`，`/health.port` 与 `src` 里的端口**完全一致**；
- 快照 `scope.kind=session`、`scope.key` = 当前会话 id；
- 页签在、端口已关 = **锚点选错**，直接判不通过（见 `docs/lifecycle.md`）。

## 4. 升级 / 重载

**一律用 `uninstall` + `install`**：

```powershell
& $env:JIUWENSWARM_EXECUTABLE extension uninstall webui.team-collab-viz --json
& $env:JIUWENSWARM_EXECUTABLE extension install dist\webui.team-collab-viz-1.0.0.zip --json
```

- 宿主 `reload` **不会**重发 `ui.ready` → reload 后页签**不会自动回来**，这是宿主行为，**不是缺陷**（L8）。
- 卸载后页签与 iframe 应自动消失；端口释放红线：**5s 内无 LISTEN**（实测 21–106ms，隔离环境下含 GC 约 300–600ms）。

端口释放取证：

```powershell
$start = Get-Date
& $env:JIUWENSWARM_EXECUTABLE extension uninstall webui.team-collab-viz --json
while (((Get-NetTCPConnection -LocalPort <旧端口> -State Listen -ErrorAction SilentlyContinue)).Count -gt 0) {
  Start-Sleep -Milliseconds 200
}
((Get-Date) - $start).TotalMilliseconds
```

## 5. 交互与恢复

| 项 | 判定 |
|---|---|
| 团队信息 | 队长/成员分区、真实状态中文标签、任务进度、整行点击展开（简介 + 执行状态 + 当前任务）、分页、窄面板不溢出 |
| 任务关系 | 带箭头依赖连线、拖动平移、滚轮连续缩放、缩放按钮、全图、横/纵布局、搜索、状态筛选、依赖链聚焦、节点详情浮层 + `Esc` 关闭 |
| 实时更新 | 触发一次团队事件 → 页面 `revision` +1，数据整份替换（`unchanged` 立即重发，不空转） |
| 会话恢复 | 切走再切回同一会话：展开项、选中任务、详情开关、画布视角恢复（来自 `/ui-state`，非内存） |
| 降级 | 制造一次 Ops 失败 → 页面**保留**上次数据并显示过期徽标；`generated_at` 不变、`served_at` 变、`revision` 只 +1 一次 |
| 主题 | 跟随系统深浅色 + 手动切换三态（`system/light/dark`） |

## 6. 排障

- **白屏**先怀疑误用 **F5 / Ctrl+R**：`/chat/<id>` 非根路径下相对 `./assets/` 会被 SPA 回退成 `text/html`（L9）。
  一律用**应用内导航**（点会话、切页签），不要硬刷新宿主渲染进程。
- 页签在但内容空白：先 `curl` `src`，再看 `/health.ok` 与 `bind_error`。
- 页签不出现：确认是 `install` 而不是 `reload`（L8）；确认 manifest 的两条 contribution 的 `slot` 为 `conversation.panel.right`。
- 端口疑似泄漏：`Get-NetTCPConnection -State Listen | ? { $_.OwningProcess -eq <宿主 pid> }` 比对 `/health.port`。

---
id: B_20261117_1019_skill-upload-temp-leak
title: 技能导入的两阶段上传临时目录永久残留
type: B
author: mengliang0116
author_email: mengliang6@huawei.com
modules:
  - jiuwenswarm/server/runtime/skill
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/server/runtime/skill/skills_multipart_http.py
  - jiuwenswarm/server/runtime/skill/skill_manager.py
  - jiuwenswarm/channels/web/app_web.py
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/index.tsx
  - tests/unit_tests/test_app_web_skill_upload.py
designs: []
design_note: 两阶段上传缺少文件生命周期归属，属实现缺陷的小范围修复，不引入新的架构设计；接口与生命周期契约记录在本文。
---

# 技能导入的两阶段上传临时目录永久残留

## 现象

每次通过 Web 导入技能包，系统临时目录（Windows `%TEMP%`，POSIX `/tmp`）都会留下一个
上传目录及其中的 zip，进程重启、导入成功与否都不会删除。长期使用后临时盘被占满。

## 根因

技能导入采用两阶段协议：

1. `POST /file-api/skills/upload-temp` 落盘并返回绝对路径（规避 WebSocket 8MB 帧限制）；
2. 前端通过 WebSocket 调用 `skills.import_upload` 传入该路径完成解压安装。

原实现里这条链路上没有任何一方拥有临时文件生命周期：创建端只在写入失败时删除目录；
消费端解压后不删源 zip；前端没有回收通知；全仓没有 discard 接口、TTL 或启动清理。
因此正常导入、导入失败、同名覆盖取消、关闭页面四条路径都会残留。开发模式下
`vite.config.ts` 另有一份 upload-temp 实现，写入 `<tmp>/jiuwenswarm_skill_upload/<ts>_<name>`，
同样无人回收。

## 修复决策

- 每次上传用 `tempfile.mkdtemp(prefix="jiuwenswarm_skill_upload_")` 建立独立目录，
  目录前缀同时作为归属标识；消费端、discard 与 TTL 清理都只认这个布局。
- `discard_skill_upload`：解析符号链接后要求父目录就是系统临时目录且目录名带固定前缀，
  删除整目录，幂等；其余路径一律拒绝。由 `POST /file-api/skills/upload-discard`（前端 finally）
  与导入终态清理共同调用。
- `handle_skills_import_upload`：导入成功或终态失败后删除源 zip；同名冲突
  （`SKILL_ALREADY_EXISTS` / `SKILL_IMPORT_OVERWRITE_REQUIRED`）保留文件以支持覆盖确认后的二次导入，
  用户取消覆盖时由前端 finally 的 discard 回收。
- `cleanup_stale_skill_uploads`：启动后台线程与每次上传前各执行一次，按 24h TTL 回收；
  用 `lstat` 读 mtime，悬空符号链接同样能按 TTL 回收。
- 前端 `SkillPanel.handleSkillUpload`：在 `finally` 中对本次上传路径发起 discard，不等待结果，
  避免网络异常拖住上传按钮的 loading 态；失败由 TTL 兜底。
- 请求体大小早检：upload-temp 在读取 body 前校验 Content-Length，超限直接 413。
- 保持既有本地服务安全边界：默认绑定 `FRONTEND_HOST`（缺省 localhost），
  `/file-api` POST 统一拒绝 `Sec-Fetch-Site: cross-site`。

## 接口契约

`POST /file-api/skills/upload-temp`

- 请求：`multipart/form-data`，字段 `file`（zip，≤50MB；请求体上限 52MB）。
- 响应：`200 {"path": "<绝对路径>"}`；`413 {"code": "SKILL_FILE_TOO_LARGE", "message", "error"}`。
- 生命周期：文件保留到 `skills.import_upload` 终态、前端 discard 或 24h TTL 到期；
  同名冲突待覆盖确认期间保留。

`POST /file-api/skills/upload-discard`

- 请求：`application/json`，`{"path": "<upload-temp 返回的绝对路径>"}`；
  请求体上限 64KB（`SKILL_UPLOAD_DISCARD_MAX_REQUEST_BYTES`），超限在读取 body 前直接 413，
  避免被超大 Content-Length 拖住。
- 响应 `200`：`{"ok": true, "discarded": <bool>}`。`discarded` 表示本次是否删除了目标目录；
  路径不存在（已回收、重复调用）返回 `false`，属正常幂等结果，不是错误。
- 错误响应：
  - `400 {"code": "SKILL_INVALID_PACKAGE"}`：请求体不是合法 JSON 或非 UTF-8。
  - `400 {"code": "SKILL_UNSAFE_PATH"}`：缺少 `path` 字段或不是字符串。
  - `413 {"code": "SKILL_FILE_TOO_LARGE"}`：请求体超限。
  错误体统一为 `{code, message, error}`。
- 归属校验：`path` 解析符号链接后，其父目录必须直接位于系统临时目录下且目录名以
  `jiuwenswarm_skill_upload_` 开头，否则返回 `discarded: false` 且不产生任何删除副作用；
  目录内的文件与目录一律整体回收。
- 调用方：仅前端 `SkillPanel`。上传导入结束（成功、失败、用户取消覆盖均覆盖）后在 `finally`
  发起且不等待；浏览器直接退出等场景请求可能未送达，由 24h TTL 兜底。
- 为何是 POST：`/file-api` 静态服务层只实现 GET/POST/HEAD，`do_DELETE` 命中即 405，
  故沿用 POST 表达删除语义，而非新增 DELETE 路由。

## 验证

- `tests/unit_tests/test_app_web_skill_upload.py`：上传后可导入并回收、同名冲突保留与覆盖回收、
  越界路径拒绝、TTL 回收、悬空符号链接回收；discard 接口删除成功与重复调用幂等、64KB 超限 413
  且无删除副作用、非法 JSON 与缺 `path` 的 400、越界路径返回 `discarded: false` 且文件保留。
- 本地 default 实例冒烟：upload-temp 返回 `<tmp>/jiuwenswarm_skill_upload_<rand>/smoke.zip`，
  导入终态后文件与专属目录均被删除。

## 遗留

- `vite.config.ts` 开发中间件的上传实现仍写入 `<tmp>/jiuwenswarm_skill_upload/<ts>_<name>` 扁平布局，
  既不按前缀目录归属，也无 discard 与 TTL；`npm run dev` 下的上传残留需另行处理（仅开发期生效，
  不参与打包）。
- 浏览器直接退出、崩溃或网络中断时前端 discard 请求可能未送达，此时依赖 24h TTL 回收，
  不是即时清理。
- 部署侧若把 `FRONTEND_HOST` 改为非 localhost 地址，需要自行在入口增加鉴权。

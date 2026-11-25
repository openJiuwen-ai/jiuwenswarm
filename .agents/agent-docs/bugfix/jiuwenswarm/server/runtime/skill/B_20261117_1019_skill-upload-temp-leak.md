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
- `discard_skill_upload`：内部函数，解析符号链接后要求父目录就是系统临时目录且目录名带固定前缀，
  删除整目录，幂等；其余路径一律拒绝。当前仅由导入终态清理调用。
- `handle_skills_import_upload`：导入成功或终态失败后删除源 zip；同名冲突
  （`SKILL_ALREADY_EXISTS` / `SKILL_IMPORT_OVERWRITE_REQUIRED`）保留文件以支持覆盖确认后的二次导入。
- `cleanup_stale_skill_uploads`：启动后台线程与每次上传前各执行一次，按 24h TTL 回收；
  用 `lstat` 读 mtime，悬空符号链接同样能按 TTL 回收。
- 请求体大小早检：upload-temp 在读取 body 前校验 Content-Length，超限直接 413。
- 保持既有本地服务安全边界：默认绑定 `FRONTEND_HOST`（缺省 localhost），
  `/file-api` POST 统一拒绝 `Sec-Fetch-Site: cross-site`。

## 接口契约

`POST /file-api/skills/upload-temp`

- 请求：`multipart/form-data`，字段 `file`（zip，≤50MB；请求体上限 52MB）。
- 响应：`200 {"path": "<绝对路径>"}`；`413 {"code": "SKILL_FILE_TOO_LARGE", "message", "error"}`。
- 生命周期：文件保留到 `skills.import_upload` 终态或 24h TTL 到期；同名冲突待覆盖确认期间保留。

## 验证

- `tests/unit_tests/test_app_web_skill_upload.py`：上传后可导入并回收、同名冲突保留与覆盖回收、
  越界路径拒绝、TTL 回收、悬空符号链接回收。
- 本地 default 实例冒烟：upload-temp 返回 `<tmp>/jiuwenswarm_skill_upload_<rand>/smoke.zip`，
  导入终态后文件与专属目录均被删除。

## 遗留

- 前端 `SkillPanel` 的 finally 回收、`POST /file-api/skills/upload-discard` 接口及其 64KB
  请求体上限、`vite.config.ts` 开发中间件改造一并推迟到批次 2；在该批次完成前，
  用户取消覆盖或关闭页面后的残留只能等待 TTL，`npm run dev` 的上传仍写入旧扁平布局，
  不会被前缀 TTL 命中。
- 部署侧若把 `FRONTEND_HOST` 改为非 localhost 地址，需要自行在入口增加鉴权。

---
id: B_20261103_1112_raw-file-range-seek
title: /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/channels/web
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/app_web.py
  - jiuwenswarm/channels/web/frontend/vite.config.ts
  - tests/unit_tests/test_app_web_raw_file.py
designs: []
design_note: 根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有
  serve_file_chunks 实现），无独立设计。
---

# /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进

## 现象与影响

Issue #6015（0.2.9，design 模式，前端通用）：DesignerMaterialViewer 预览
视频时可以正常播放，但拖动进度条无法快进。后端 raw-file 端点忽略了
Range 请求：任何 seek 都返回 200 全量流，浏览器 video 元素拿不到 206
Partial Content，`seekable` 只覆盖已缓冲区间，拖动落点弹回——用户感知
即"进度条不可快进"。

## 关联设计与当前契约

DesignerMaterialViewer（`features/designer/DesignerMaterialViewer.tsx`）的
视频/音频用原生 `<video controls>`/`<audio controls>` 播放，src 指向
`designerAssetPreviewUrl` 生成的 `/file-api/raw-file?path=...`；三个
入口都依赖该端点：app_web（单机生产）、vite dev 中间件（开发态）、
gateway `serve_file_chunks`（企业版，已支持 Range/206/416）。本次补齐
前两个入口，与 gateway 行为对齐。

## 根因与修复决策

根因：app_web `_handle_file_api_get` 的 raw-file 分支与 vite.config.ts 的
`/file-api/raw-file` dev 中间件都不读 `Range` 头、不回 `Accept-Ranges`，
对任何请求固定 200 + 全量文件。

修复决策：两处均按 gateway `_parse_single_byte_range` 的语义补标准单区间
Range 支持——合法区间回 206 + `Content-Range` + `Accept-Ranges: bytes`
只写区间字节；非法区间（start 越界、end<start、multipart、非 bytes 单位）
回 416 + `Content-Range: bytes */total`；无 Range 请求回 200 +
`Accept-Ranges: bytes`。app_web 以 `seek(start)` + 按剩余量循环写chunk，
dev 中间件用 `fs.createReadStream(fullPath, { start, end })`。不支持的
multipart ranges 按既有 gateway 契约返回 416，浏览器会退化为整段重取，
不影响 seek。

拒绝的备选：仅在前端为 video 组件手写进度条（自定义 seek UI）——所有
`/file-api/raw-file` 消费方（ChatPanel 媒体、ArtifactsPanel FilePreview、
SkillPanel 图片、designer 预览）共享同一端点，且 audio 预览同样受益；
在端点层修一次即可，前端原生控件不动。

## 回归证据与适用范围

分支 `fix/issue-6015-video-seek`（基线 upstream/develop `b3188493e`），
Windows 本机 `uv run pytest`：

- `tests/unit_tests/test_app_web_raw_file.py` 15/15：新增
  `test_parse_single_byte_range_rejects_invalid_variants`（10 组解析
  变体）与 `test_raw_file_answers_byte_range_requests`（8 组参数化真实
  HTTP：无 Range 200 全量、全区间/开区间/闭区间/suffix 206 切片、
  start 越界/end<start/multipart 416，GET 与 HEAD 双方法验证
  Accept-Ranges、Content-Length、Content-Range）。
- `tests/unit_tests/test_agentos_file_transfer.py` 42/43，唯一失败
  `test_register_web_handlers_binds_container_file_client_for_agentos_router`
  在未修改的 develop 基线上同样失败（`AttributeError: WORKSPACE_TREE`），
  属存量问题，与本修复无关。
- 前端 `tsc -p tsconfig.node.json` 报错集与 stash 基线完全一致（4 个
  develop 存量错误），本次 vite.config.ts 改动未引入新类型错误。

未验证：真实浏览器拖动进度条的手工 E2E（需要完整 designer 会话栈）；
由 CI 流水线与合入后 issue 报告方复验。重验条件：design 模式预览
视频，拖动进度条可落到目标位置，network 面板可见 206 响应。

## 防错模式与资料同步

- "媒体可播但不可 seek"先查服务端 Range 支持，不要先改前端控件——
  `<video controls>` 的 seek 依赖端点 206 响应，200 全量流必然锁死进度条。
- 同一 `/file-api/raw-file` 契约现在有三个实现（app_web / vite dev /
  gateway serve_file_chunks），改行为必须三处同步，否则环境间行为分裂。
  gateway 已有 `_parse_single_byte_range` 参考实现，本次未抽公共包
  （app_web 是纯标准库 http.server，不引 FastAPI 依赖），但语义与
  注释已互相锚定。

## 遗留与后续

- `/file-api/file-content` 与 `/file-api/download` 仍无 Range 支持；
  两者分别是文本读取与附件下载，无 seek 需求，暂不扩大范围。
- `Cache-Control: no-store` 使浏览器无法跨请求复用媒体缓冲，seek 落点
  均需重新拉流；在局域网场景可接受，若后续报告 seek 后重复缓冲卡顿，
  可评估按路径哈希放开缓存。

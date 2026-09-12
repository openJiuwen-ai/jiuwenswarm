# 专家包 ZIP 导入

小艺 Work 的专家广场支持从前端导入当前 `xiaoyi_0.2.4.beta3` 契约的单专家包和专家团包。导入完成后，包会进入本机专家缓存，并立即出现在已安装专家列表中；同 ID 再次导入会更新已有版本。

## 使用方式

1. 打开“专家”中的专家广场。
2. 点击“导入 ZIP”，选择一个专家或专家团 ZIP。
3. 等待页面提示导入成功，再从专家列表查看或使用。

ZIP 可以平铺打包，也可以包含一个同名外层目录，但包内容根目录必须存在 `manifest.json`：

- 单专家：使用当前 beta3 的 `agentCard.id` 作为专家 ID。
- 专家团：`package_type` 为 `agent_group`，使用 `name` 作为专家团 ID。

旧试验包若只有 `package_type: agent_template`、没有 `agentCard`，需要先转换为当前 beta3 专家包格式。

## 处理链路

```text
浏览器选择 ZIP
  -> Web Gateway: expert.import
  -> 临时目录安全解压
  -> 复用专家包完整校验
  -> 按专家 ID 原子替换专家缓存
  -> ImportedExpertPackageSource
  -> expert.list / expert.load / 前端展示与使用
```

导入在 Web Gateway 本地终结，不再把 Base64 文件内容转发到内部 AgentServer WebSocket。这样既避开内部通道的 8 MB 消息上限，也避免大段 Base64 进入路由日志。AgentServer 仍保留同名处理入口，供直连调用和契约测试使用。

## 安全与资源限制

- ZIP 压缩文件最大 50 MB。
- ZIP 条目最多 5000 个，解压后总大小最大 200 MB。
- 拒绝绝对路径、`..` 路径穿越、符号链接、加密条目和重复条目。
- 先在缓存内的临时目录完成解压和完整校验，再替换目标目录；替换失败时恢复旧版本。
- 仅带 `.xiaoyi-import.json` 标记的缓存目录会作为用户导入包被枚举，避免把普通下载缓存误报为已导入资源。

## 验证范围

单元测试覆盖单专家导入、非法 Base64、ZipSlip 拒绝、列表可见性和上传内容日志脱敏。前端需同时通过 TypeScript 检查与生产构建；真实验收应从页面分别导入一个单专家包和一个专家团包，并确认成功提示、列表可见和缓存标记三者一致。

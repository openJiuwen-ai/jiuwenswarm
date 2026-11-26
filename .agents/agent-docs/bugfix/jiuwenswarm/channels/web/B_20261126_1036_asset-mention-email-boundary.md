---
id: B_20261126_1036_asset-mention-email-boundary
title: 素材 @ 引用在邮箱地址中误匹配
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/sessionAssets
files:
  - jiuwenswarm/channels/web/frontend/src/features/sessionAssets/assetReferences.ts
  - jiuwenswarm/channels/web/frontend/tests/sessionAssetReferences.test.mjs
designs: []
design_note: 前端文本解析边界缺陷，无独立设计方案，按 Issue #7059 处理。
---

# 素材 @ 引用在邮箱地址中误匹配

## 现象与影响

前端把用户消息里的 `@名称` 识别为会话素材引用，并在发送前追加「引用素材」说明。修复前只校验了右边界，未校验左边界：

```
输入：把报价发到 team@fox.com
findReferencedAssets        -> [fox]
withAssetReferenceNote      -> 原文 + "[引用素材] @fox = C:/a/fox.png (image)"
```

用户正文中只要出现与素材同名的邮箱前缀，就会被注入一段伪造的素材路径，并随消息发给 agent，可能触发错误的图片/文件引用。

## 根因

`findReferencedAssets` 用 `lowered.indexOf('@' + name)` 定位候选，只在匹配结尾判断「下一个字符不能是字母数字」，没有判断 `@` 左侧。邮箱地址中 `@` 左侧必然是本地部分（ASCII 字母/数字/下划线等），因此 `team@fox.com` 被当作 `@fox` 的合法引用。

## 修复决策

在匹配位置增加左边界判断：`@` 前一个字符若是 ASCII 字母、数字或下划线，视为嵌在邮箱地址中，不识别为引用。

- 左边界只拦 ASCII，保留中文紧邻 `@` 的引用能力（如 `用@狐狸。`），中文用户不加空格直接引用素材是既有用法，必须继续可用
- 与右边界规则对称，均使用 `WORD_CHAR`，避免出现两套字符集
- 只影响 `findReferencedAssets` 与 `withAssetReferenceNote`，不涉及路径拼接、接口或存储

## 回归与验证

- `npm run test:session-assets`：27 passed
- 新增用例覆盖：`team@fox.com`、`fox@example.com` 不产生引用；`withAssetReferenceNote` 原样返回；中文紧邻 `@` 仍可引用
- 既有前缀匹配用例（`@foxes` 不匹配 `@fox`）保持通过

## 遗留与限制

- 中文命名的邮箱本地部分（如 `狐狸@fox.com`）仍会误匹配，属已知取舍：纯文本层面无法区分「中文邮箱本地部分」与「中文紧邻引用」，且中文本地部分邮箱实际使用极少
- 若后续出现真实误报，可按右侧域名特征（`@名称` 后紧跟 `.com`/`.cn` 等常见 TLD）做启发式排除，而非收紧左边界

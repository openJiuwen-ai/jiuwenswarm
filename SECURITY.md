# 安全策略 (Security Policy)

感谢您帮助提升 jiuwenswarm 的安全性。本文档说明如何报告安全漏洞以及漏洞的处理流程。

## 支持版本

请以本仓库 **默认分支 / 当前发布标签** 为准进行安全评估与修复；仅维护中的版本线接受安全修复，已过时版本建议升级到最新版本。具体长期支持策略可由维护团队在 Issue 或 Release 说明中补充。

## 报告漏洞

请**勿先公开漏洞细节，邮件发至 [contact@openjiuwen.com](mailto:contact@openjiuwen.com)，我们将在 3 个工作日内确认，并按 90 天协调披露政策处理**。

请**勿在公开 Issue、讨论区或代码评论中提交未披露的漏洞细节**（包括可利用代码、PoC）。

按敏感程度选择报告渠道：

| 渠道 | 适用场景 | 入口 |
| ---- | -------- | ---- |
| 安全邮箱（推荐） | 可被利用的漏洞、敏感安全问题 | `contact@openjiuwen.com` |
| 私密渠道 | 可被利用的漏洞、敏感安全问题 | 通过 GitCode 仓库 `openJiuwen/jiuwenswarm` 的维护者私信渠道联系；若平台不提供私信，先开一个不含细节的 Issue 请求私密沟通，标题注明 `[security] jiuwenswarm` |
| Issue | 非敏感的安全加固建议、依赖组件风险讨论 | [https://gitcode.com/openJiuwen/jiuwenswarm/issues](https://gitcode.com/openJiuwen/jiuwenswarm/issues) |

### 报告内容要求

为便于快速定位与复现，私密报告请尽量包含：

1. **影响范围**：涉及的模块 / 文件 / 版本号（`jiuwenswarm` 版本或仓库分支）
2. **漏洞类型**：如命令注入、路径穿越、信息泄露、反序列化等
3. **复现步骤**：最小可复现的环境、配置与操作序列
4. **影响评估**：可能的攻击面与后果（机密性 / 完整性 / 可用性）
5. **修复建议**（可选）

## 响应时限

| 阶段 | 目标时限 |
| ---- | -------- |
| 确认收到报告 | 3 个工作日内 |
| 初步评估（是否为漏洞、严重等级） | 7 个工作日内 |
| 修复方案与计划 | 评估完成后 5 个工作日内 |

时限为尽力而为的目标，节假日可能顺延；超时未响应可发邮件催办，或通过 Issue 催办（不含漏洞细节）。

## 处理流程

1. **确认与评估**：维护者确认报告，评定风险等级（safe / low / medium / high / critical）
2. **修复开发**：在私有分支完成修复与回归测试
3. **发布**：随下一个 PATCH / MINOR 版本发布安全修复；critical 级别漏洞可发布紧急版本
4. **披露**：版本发布后，在 [发布页](https://gitcode.com/openJiuwen/jiuwenswarm/releases) 公开致谢（报告者同意的前提下）

## 披露政策

我们遵循 **90 天协调披露** 原则：

- 请勿先公开漏洞细节；修复发布前，请勿公开漏洞细节
- 我们将在 3 个工作日内确认收到报告，并在协调窗口内推进修复与披露安排
- 若报告后 90 天内未发布修复，报告者可自行决定是否公开披露（请提前 14 天告知维护者）

## 联系方式

- 安全邮箱：[contact@openjiuwen.com](mailto:contact@openjiuwen.com)
- 仓库：[https://gitcode.com/openJiuwen/jiuwenswarm](https://gitcode.com/openJiuwen/jiuwenswarm)
- 问题反馈：[https://gitcode.com/openJiuwen/jiuwenswarm/issues](https://gitcode.com/openJiuwen/jiuwenswarm/issues)

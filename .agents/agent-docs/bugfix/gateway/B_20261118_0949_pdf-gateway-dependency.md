---
id: B_20261118_0949_pdf-gateway-dependency
title: Issue 6122 的 PDF 附件修复依赖接入与双分支验证
type: B
author: Yoggaboomin
author_email: yutao131@huawei.com
designs: []
design_note: 延续用户确认的 Issue 6122 附件修复方案，本次仅接入已合入 Gateway 的修复并验证消费链路，无新增设计。
modules:
  - jiuwenswarm/server/runtime/attachments
  - jiuwenswarm/agents/harness/common/tools
  - openJiuwen/agent-gateway/agent_gateway/message_handler
files:
  - pyproject.toml
  - uv.lock
  - tests/unit_tests/gateway/test_pdf_attachment_prompt.py
---

# Issue 6122 的 PDF 附件修复依赖接入与双分支验证

## 现象与根因

Gateway PR 143 和 146 合入后，JiuwenSwarm 固定的 Git revision 决定默认安装是否包含修复，不能以 Gateway 分支已合入推定所有消费方都升级。

2026-11-18 核验上游：

- `develop` 基线 `c18fd2924e7426f6a4658717da80eb9bda0705d3` 已固定 Gateway `703da5cec987acaaab3557e90007965ca2644c96`，包含 PR 143 的实际合入提交 `6dc27141a`。本次无需更新 develop 的生产依赖或业务代码。
- `releases/0.2.9` 基线 `ea0c44968cf8893f6d0c4bf54fefb7d8b0903b9c` 仍固定 Gateway `4d82b28972b131599e97c0691e490819fd25a9b4`，尚无附件分类与预算修复，仍会将 PDF 当文本内联。

## 修复范围与决策

0.2.9 将 `pyproject.toml` 的直接依赖与 `tool.uv.sources` 两处 pin 同步到 Gateway release 的真实合入提交 `a7de5ef8637cf92161add07eb402c993586cc70c`，重新执行 `uv lock`。锁文件只改变 Gateway 的 source 与 requires-dist 两条记录，其他依赖不变。使用 release 修复避免把 Gateway develop 的其他功能引入稳定分支。

该 Gateway 提交相对原 pin 除本次 PDF 修复外，还包含已先行合入的 PR 148：Windows tasklist 编码修复。未修改 Gateway 的依赖声明，不另升级传递依赖。

新增消费方回归覆盖八附件、空格路径、PDF 工具发现与正文提取，以及结构化附件和手写路径的真实 `chat.send` 队列分发，断言发送给 AgentServer 的 query/content 一致且无 PDF 原始体。两个分支使用各自协议包的实际导入路径，不强行将 develop 的旧 e2a 导入用于 release。

## 验证与证据

证据目录：`/private/tmp/issue-6122-swarm-dependency-20261118-FTAWzM/`。macOS、Python 3.12.14，各分支使用独立 `.venv`，Gateway 从上游固定提交构建安装，非 editable 修复工作区。

- `uv lock --check --offline` 两分支均通过。0.2.9 的 `uv sync --locked` 在无关 Computer Use 包的 `rustc --version` 阶段停滞；只停止本任务进程后，以 `--no-install-package openjiuwen-computeruse` 安装其余锁定依赖。未验证该原生包的安装与运行，未修改其版本或锁定信息。develop 完整默认同步成功。
- Swarm 定向测试：两分支各 44 项通过，范围为 `test_pdf_attachment_prompt.py`、`test_pdf_tools.py`、`test_document_attachments.py`。
- 用 0.2.9 的旧 Gateway 运行新增五个用例，四项按预期因 PDF 被内联失败，一项 PDF 工具读取通过；切回锁定的新 Gateway 后全部通过。证据 `release-before-tests.txt` 与 `release-integration-tests.txt`。
- Gateway 合入代码在对应 Swarm 环境执行附件、消息处理和持久化回归：develop 309 项通过；release 加 Windows 编码回归共 310 项通过。对应 `gateway-develop-tests.txt`、`gateway-release-tests.txt`。
- 用户提供的 53 页 `2412.19437v2.pdf` 复制八份，共 15,098,928 字节，经实际文档持久化与 `chat.send` 队列，两分支的 `@路径` 输出 query 均为 2,367 UTF-8 字节，结构化附件输出均为 3,215 字节。全部保留八个引用，不含 PDF 头、流、替换乱码或正文标题；query/content 一致。
- 通过注册的 `read_pdf.invoke` 读取同一 PDF 第一页，两分支均返回 1,950 字符，包含 `DeepSeek-V3 Technical Report`。请求参数与结果见 `real-pdf-{release,develop}/` 的 `forwarded-*.json`、`read-pdf-page-1.txt`、`result.json`。
- 0.2.9 wheel 构建成功，实际 METADATA 的 Requires-Dist 已固定上述 Gateway 合入提交；构建记录 `release-build.txt`，产物 `release-wheel/`。

命令使用 `python -m pytest ... -o addopts= -o asyncio_mode=auto -o log_cli=false --tb=short -q`，不生成覆盖率产物，不执行格式化或静态检查。验证过程中上游前进一提交，已快进到以上两个最终基线；新提交仅修改个人上下文文件夹选择，不改变此处依赖、文档上传或 PDF 工具。

## 适用边界与交付

本轮未启动完整 Web 服务、未调用真实模型；证明默认固定依赖的 Gateway 转发内容与实际 PDF 工具兼容，不宣称本轮验证了模型自主选工具或完整报告生成。引用文本字节数不是完整模型请求 token 数。修复消除 PDF 二进制内联导致的输入膨胀，不保证连续读取所有正文永远不触及上下文上限。

交付目标为从用户 fork 向 JiuwenSwarm 的 `releases/0.2.9` 提交独立修复 PR；上游 0.2.9 在此依赖修改合入前仍使用旧 Gateway，develop 无需另起依赖修复 PR。若 Gateway pin、协议、上传路径或 PDF 工具改动，需要重验受影响链路。

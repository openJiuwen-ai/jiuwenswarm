---
id: F_20261114_0017_authorized-original-file-download
title: 授权附件原文件引用与 SHA-256 留痕实施
type: F
author: nypdmax
author_email: chengkunck@gmail.com
created: 2026-11-14
updated: 2026-11-16
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/server/runtime/gateway_adapter
files:
  - jiuwenswarm/common/verified_download_assets.py
  - jiuwenswarm/common/file_download_token.py
  - jiuwenswarm/agents/harness/common/tools/send_file_to_user.py
  - jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py
designs:
  - D-20261113-1809-authorized-original-file-download
---

# 授权附件原文件引用与 SHA-256 留痕实施

## 关联依据与实际范围

依据 [D-20261113-1809-authorized-original-file-download](../designs/active/D-20261113-1809-authorized-original-file-download/design.md) 的精简四文件方案，用户于 2026-11-14 明确授权实施、必要测试及独立评审，并要求不提交。当前契约见 [S_01_authorized-file-download](../specs/jiuwenswarm/common/S_01_authorized-file-download.md)。

基线为 `develop@ebf16caa544d30b69925e37865ce4ce36f079875`（tree `4279755d7a980eae82da6a31840a1608d2db0677`），实施候选为该基线上的工作区差异。四个生产文件新增 68 行、删除 34 行，合计 102 行；五个既有测试文件新增 201 行、删除 11 行，合计 212 行。未暂存或提交，既有其他文档改动保留。

## 实际决策及与设计关系

本次落实已批准精简稿，不另立登记、审计存储、重签或平台读取机制。原 sidecar 继续负责登记，原读取循环兼算摘要；显式引用选项让原路径和 `exp=0` 进入既有 token 生成器。生成器及八字段结构不改，验证器仍匹配登记；分块读取仅对引用使用当前大小。stage/commit/revoke/history 的授权、投递和异常顺序保持原有机制。

原文件清理不属于登记 Owner：引用源在私有缓存目录内时提前拒绝，stage 失败、revoke 和 prune 只清理引用登记；旧正期限快照仍复制和按 TTL 回收。SHA 时点沿用精简稿明确选择的交付阶段，审批发放点和 grant 均不改。没有改变已确认方案的采集时点、数据契约或消费者范围。

## 执行证据

候选完整差异及逐文件 SHA、锁定依赖来源见 [candidate.diff](../../../task-artifacts/authorized-original-file-implementation-20261114/candidate.diff) 与 [candidate-manifest.json](../../../task-artifacts/authorized-original-file-implementation-20261114/candidate-manifest.json)。差异 SHA-256 为 `ce43270a5b9f8628352464e7ee07995fb723548c5776d11234bd738906b988f7`。

| 2026-11-14 验证 | 实际结果 | 证据及限制 |
| --- | --- | --- |
| Owner/token/授权消费者/分块适配核心验证 | 114 passed | `core-tests.log`；随后纳入最终扩展运行，不累计计数。首次运行因自定义 basetemp 父目录缺失在 setup 失败，创建目录后重跑通过，原日志保留。 |
| 最终 Python 定向回归，11 个文件 | 234 passed，1 skipped | [validation.xml](../../../task-artifacts/authorized-original-file-implementation-20261114/validation.xml)、[validation.log](../../../task-artifacts/authorized-original-file-implementation-20261114/validation.log)；桌面模块因缺少可选 `webview` 依赖在收集阶段跳过。 |
| `npm run test:subagent-history` | 52 passed | 现有普通历史恢复兼容回归，无前端生产修改。 |
| `npm run test:team-history-panel` | 9 passed | 现有团队历史投影兼容回归，无浏览器 E2E。 |
| code-quality-check 及其聚焦架构升级 | PASS，无当前范围阻塞项 | [quality-review.md](../../../task-artifacts/authorized-original-file-implementation-20261114/quality-review.md)；只核对登记与原文件/旧副本生命周期，不扩大为全仓架构检查。 |

Python 在 macOS arm64、3.11.4、独立冻结环境运行，使用 `uv sync --frozen --python 3.11 --no-install-project --no-default-groups --group test`，未修改 pyproject 或 uv.lock；Gateway 与 core 使用锁文件原 revision。测试使用真实 Owner、token、历史 append/flush/load、WorkspaceFileAdapter 及 Web 下载处理器，Runtime push/网络边界按既有用例替身隔离，无真实模型调用。

最终命令的完整参数和路径见 [validation-command.txt](../../../task-artifacts/authorized-original-file-implementation-20261114/validation-command.txt)。关键运行选项为 `uv run --no-sync pytest -o addopts='-q --strict-markers --tb=short --asyncio-mode=auto'`，隔离 `UV_PROJECT_ENVIRONMENT`、缓存及 basetemp；前端使用仓库既有 npm 命令。按仓库约束未运行格式化或静态检查。

用户可见行为由新引用的真实 GET/HEAD/Range、重试及并发下载断言验证；预期字节及 SHA 由测试预先固定的数据独立计算。文件变长、变短和空文件采用当前大小；旧副本仍返回初始字节。副作用检查独立读取 sidecar、源权限和源字节；历史写入 flush 后重建 Owner/manager，从原历史 token 恢复初始 SHA，再修改/删除源文件，不重算或重签。无授权、授权不匹配、重放、跨登记目录、篡改、撤销和非法期限保持失败关闭。

## 验证结论与遗留

本次已检查并验证四文件增量及相关 Python 消费链，旧共享 token、普通模式、路径/目标授权与历史恢复回归通过。前端 61 项为原恢复逻辑兼容证据，新 SHA 持久恢复由后端新增用例直接断言；没有声称已完成端到端浏览器或真实 Windows 测试。

沿用设计中有意保留的边界：摘要不是审批决策时刻的快照；原文件可变可删；临时登记被清除会使下载失败，Windows 默认 PID 目录不保证换进程恢复，需要既有稳定目录配置；引用登记保留少量不过期元数据。不新增跨平台原子读、并发修改一致性或强审计保证。未在 Windows/Linux、真实 AgentOS 部署或真实模型环境执行；桌面原生下载 UI 未验证。

后续生产/协议/fixture 或锁定依赖发生相关变化时，须对受影响范围重验。

## 最终独立评审

2026-11-14，`independent-reviewer (/root/download_plan_review)` 对完整九文件差异、实际消费者、已发布 S/F 和上述证据独立检视，结论为 **Approve**，无当前范围阻塞的 Introduced/Exposed 问题；未独立重跑测试。详细结论见 [independent-review.md](../../../task-artifacts/authorized-original-file-implementation-20261114/independent-review.md)。候选文件哈希与 manifest 一致，技术差异在验证与评审后均未变；此后仅补本条真实评审记录。已完成授权范围内实施、必要验证和独立评审，未提交，设计保持 active，等待后续真实交付确认而不提前归档。

## 实施演进：2026-11-16 Windows 默认登记目录去除 PID

用户确认 PID 目录不合理后，在原四文件范围内继续完善，仅替换 `verified_download_assets.py` 的默认路径函数：无 getuid 时复用既有 `get_user_workspace_dir() / "download-assets"`；DATA_DIR/HOME 和显式 DOWNLOAD_ASSET_ROOT 继续原有配置机制。有 getuid 的平台维持旧目录，token、工具及登记生命周期均未变化。此前 2026-11-14 的 PID 限制记录作为历史保留，此后现行契约以同一 S 规约为准，D 第 7 节记录本次局部设计替代与真实方案评审。

本次生产增量新增 5 行、删除 2 行；既有 Owner 测试文件新增 105 行，六个新用例包含两组真实跨进程 stage/commit/token validate。没有增设迁移器、注册表、重签机制或新生产文件。旧 Windows PID 目录不扫描/迁移，已知目录可由显式配置继续选择；macOS/Linux 原路径保留，避免本次移动其已有登记。数据根配置必须在共享 Owner 首次导入前就绪，解析路径不创建登记目录，utils 的既有日志初始化副作用保持原行为。

候选完整九文件差异 SHA-256 为 `8d9ddc4dfc3cbed76d140eeb28b4fb9b2fd1eec42c1be0a178da9191437b7ec1`，仅本次两文件增量 SHA-256 为 `9c96965864c912acfb95e8472cdf74cc28b449f2f99a674e65b590a7c2f6ad36`。详见 [followup.diff](../../../task-artifacts/authorized-file-stable-root-20261116/followup.diff)、[candidate-manifest.json](../../../task-artifacts/authorized-file-stable-root-20261116/candidate-manifest.json)。其余七个源码/测试文件与原批准候选哈希一致。

同一冻结 Python 3.11.4/macOS arm64 环境下，Owner 首轮 30 passed；最终九文件定向回归 **197 passed，无跳过**，不累计两次数量。涵盖 root 选择、原文件引用、旧快照、token、授权、分块及 Web 下载/Range/重试；完整命令及 JUnit 见 [validation-command.txt](../../../task-artifacts/authorized-file-stable-root-20261116/validation-command.txt)、[regression.xml](../../../task-artifacts/authorized-file-stable-root-20261116/regression.xml)。跨进程用真实两个 PID、新 Owner 与原签名 token 读取同一 sidecar/字节，Windows 的无 getuid 分支在 macOS 模拟；未执行原生 Windows/Linux 测试、浏览器/模型 E2E，未重跑无改动的前端套件。

code-quality-check 核对完整增量及兼容边界，无当前范围阻塞项；本次路径选择不改变所有权、生命周期或集成结构，架构升级 **Not triggered**，沿用此前生命周期审查结果。方案独立评审与最终实施独立复核均为 **Approve**，无当前范围阻塞项；复核核对代码、哈希、六个新增用例与 JUnit，未另行重跑测试。最终结论见 [independent-review.md](../../../task-artifacts/authorized-file-stable-root-20261116/independent-review.md)。详见 [quality-review.md](../../../task-artifacts/authorized-file-stable-root-20261116/quality-review.md) 和 [plan-review.md](../../../task-artifacts/authorized-file-stable-root-20261116/plan-review.md)。仍未暂存、未提交，历史验证结果保留原候选限定。


### 2026-11-16 diff 评论：导入统一置于模块顶部

按用户明确要求，将 `get_user_workspace_dir` 从默认路径函数内移到模块顶部，只有该生产文件 2 行新增/2 行删除（含空行），其他候选文件哈希保持不变，不新增测试或改变 token/路径规则。现在各平台首次导入资产模块均加载 utils，沿用其既有日志初始化；未发现直接循环导入。此前 197 项回归仍限定于前一候选，本次更新候选重验 Owner、token 和 SendFile 消费者三文件，**45 passed，无跳过**，包括真实跨进程登记/token 恢复。源文件哈希、精确增量和 JUnit 见 [本次候选清单](../../../task-artifacts/authorized-file-stable-root-20261116/import-at-top/candidate-manifest.json)、[测试报告](../../../task-artifacts/authorized-file-stable-root-20261116/import-at-top/tests.xml)。独立只读复核结论 **Approve**，详见 [本次复核](../../../task-artifacts/authorized-file-stable-root-20261116/import-at-top/independent-review.md)；保持未暂存、未提交。


## 验证补充：2026-11-16 天翼云电脑原生 Windows 跨进程恢复

用户提供 `ssh windows-host` 后，在天翼云电脑 Windows 10.0.20348.4893、Python 3.11.16 AMD64 上完成原生补测。使用隔离目录 `C:/authdl-01a1d431-20261116`、独立 venv/data/config 与一次性测试文件；未覆盖既有安装、真实配置或会话。本次没有修改产品代码或锁文件，验证的是上一节模块顶部导入后的候选。上传后及测试后均核验全部 2,206 个源码文件，当前关键源码和锁文件也与执行候选一致，见 [测试后源码核验](../../../task-artifacts/authorized-download-windows-20261116/post-run-source-verification.json)。

实际未指定登记目录或签名密钥，也未使用平台模拟。七个顺序启动且退出的新进程分别为 stage/2420、restore/11296、grown/11384、shorter/4420、empty/7304、missing/10704、revoked/8952，cwd 各不相同；默认登记目录始终为隔离数据根下的 `download-assets`。stage 通过真实 Owner/manager 登记原文件、`exp=0` token 和 chat.file 历史后退出；随后进程通过真实历史 API 恢复同一 token 与首次 SHA，以持久密钥验签，再经 WorkspaceFileAdapter 分块读回当前字节。原文件、变长、变短和空文件大小依次为 23、51、1、0，各阶段校验当前大小、字节、EOF、越界偏移和会话绑定。源文件删除但登记仍在时拒绝 token；恢复测试源文件后撤销登记，确认源文件保留、登记消失、原 token 失效。全部恢复阶段历史 token 摘要及首次 SHA 一致，登记目录仅含 JSON 元数据，没有文件快照。

环境准备经历 registry 下载及构建依赖网络超时，业务原生补测首次运行即通过。最终复制既有 Windows 环境中 248 个版本匹配原锁的 registry 库，35,868 个库文件逐项核验 RECORD 摘要；使用原锁执行 `uv sync --frozen --no-install-project --no-default-groups --group test --no-build-isolation --inexact --default-index https://pypi.org/simple`。Pillow 12.3.0 的 Windows wheel 匹配原锁，额外保留 wheel 0.45.1 作为构建工具；五个 Git 依赖原生构建，实际 commit 均严格匹配原锁。复制未包含 console entrypoint，测试使用明确的 Python 路径与产品 API；未复用 macOS 二进制包。准备命令、文件摘要及环境来源见 [补测说明](../../../task-artifacts/authorized-download-windows-20261116/README.md) 和 [环境校验](../../../task-artifacts/authorized-download-windows-20261116/native-environment-check.json)。初始 ZIP/输入清单保留 prepared 时的事实，实际执行脚本在上传后更新并以独立摘要核对；产品源码未变化。

原生运行结果为 **7 个独立进程阶段全部通过，退出码 0**，见 [实际报告](../../../task-artifacts/authorized-download-windows-20261116/native-report.json)、[核验摘要](../../../task-artifacts/authorized-download-windows-20261116/native-verification-summary.json) 与 [执行命令](../../../task-artifacts/authorized-download-windows-20261116/native-driver-result.json)。本次独立只读复查结论为 **Approve**，无当前范围阻塞项，见 [补测独立复查](../../../task-artifacts/authorized-download-windows-20261116/independent-review.md)；评审未另行重跑测试。没有新的结构变更，沿用上一轮 code-quality-check 架构结论，不重复升级。此前 macOS 模拟和本地脚本自检仍保留各自限定，本节补齐原生 Windows API/存储层进程间恢复证据；整机重启、真实 AgentOS 部署及浏览器/模型全栈 E2E 未执行。仍未暂存、未提交，设计保持 active。


## 提交与发布分支迁移：2026-11-16

用户明确授权提交及推送独立修复分支，并要求迁移 releases/0.2.9 与 releases/0.2.8。为保留原 develop 工作区的其他未提交内容，从三个最新 upstream 基线分别创建隔离修复工作区。三个基线均仍有 600 秒快照交付路径；四生产文件及五测试文件的实际增删行逐行一致，共 426 行。0.2.8 仅在原授权 stage 块落下等价参数改动，保留其 runtime.host_services、仓内 Gateway 及各自既有模式分支。

| 目标基线 | 基线 SHA | 修复代码提交 | 本次回归 |
| --- | --- | --- | --- |
| develop | `227ae52605aa` | `5fe7d061df8d` | 240 passed，1 skipped |
| 0.2.9 | `3df504b0559d` | `ce24c0f4ba4a` | 240 passed，1 skipped |
| 0.2.8 | `357884f316c3` | `5bc9dfeab100` | 236 passed，1 skipped |

每个分支均用自己的原始 uv.lock 新建冻结环境，实际 Git 依赖 commit 逐一匹配该锁；未迁移或修改依赖文件。环境准备先遇到并行共享缓存锁超时，随后复用本机已有精确 Git 对象并顺序安装；离线尝试缺构建后端元数据，最终以原锁和 PyPI 索引完成安装。环境失败日志保留，三分支各自首次业务回归均通过。11 个相关 pytest 文件使用 macOS/Python 3.11.4，唯一跳过均为可选 webview 缺失；此前原生 Windows 结论仍仅覆盖其当时实测的依赖组合。

code-quality-check 未发现当前范围阻塞项，本次迁移没有新机制或结构变化，架构升级 Not triggered，沿用已完成的生命周期审查。迁移独立只读评审为 Approve，已核对三个完整差异、原消费者、JUnit、退出码、候选及锁摘要，未另行重跑测试。结果、命令、环境和评审见 [迁移测试摘要](../../../task-artifacts/authorized-download-delivery-20261116/test-summary.json)、[质量记录](../../../task-artifacts/authorized-download-delivery-20261116/quality-review.md) 和 [迁移独立评审](../../../task-artifacts/authorized-download-delivery-20261116/independent-review.md)。测试日志、配置、密钥、运行产物以及其他未提交任务内容均不纳入提交；以代码修复、现行契约、设计与验证记录分别组织提交，向独立修复分支推送。


## PR #10236 门禁与检视修复：2026-11-16

门禁初始评论报告 CodeCheck 与 UT 失败，其他扫描、Ruff、构建和 Issue 关联通过；评论本身未绑定可验证的执行 SHA，不把报告状态扩大为后续候选状态。已读取在线 CodeCheck 明细，唯一规则为 G.CTL.03（过期判断含 4 个布尔表达式，阈值 3），改为两个等价的嵌套判断；未运行本地格式化或静态检查。

检视指出 reference 登记使用原始绝对路径、校验使用 resolve 后路径，父目录别名可使历史 token 被拒绝。新增真实 token/Owner 重建用例在原候选失败，修复后通过：引用登记保存规范化路径，读取仍对未 resolve 的输入执行 no-follow/普通文件检查，继续拒绝文件本身为符号链接。独立复查进一步要求保持宿主已选定的精确交付路径；现将 asset 加入既有 owned_assets 后比较登记路径与该原路径，不等时在构建 token/push 前失败，复用 finally 撤销登记。授权消费后父目录重定向的负例先红后绿，确认没有 push/history、登记已撤销、原文件均保留。

UT 在线报告唯一失败为 ASR 配置优先级用例在存在加密 Provider 时把明文测试值解密为空。以公开 Provider 注入点可在原基线和 PR 复现相同断言（1 failed/3 passed）；无 Provider 时两侧均 4 passed。仅对该优先级用例 monkeypatch getter 返回 None，并将已有导入置于模块顶部，解密正负用例仍各自验证真实调用边界，ASR 生产代码不改。修复后同一 Provider 条件下 4 passed。此证据确认用例依赖未固定的进程状态，不声称已追出 CI 当次的 Provider 注册来源。

迁移前分别核对：两发布分支的缺陷 Owner 代码逐字节相同、存在相同的过期表达式及授权投递路径，因此迁移路径、授权 guard 和 CodeCheck 修复。0.2.9 还包含同一 ASR 用例及 owner 解密逻辑，迁移其测试隔离；0.2.8 均不存在，不迁该项。未新增 TTL 清理、下载时重算摘要或其他机制。

| 分支 | 最终本地回归 | 范围 |
| --- | --- | --- |
| develop | 246 passed，1 skipped | 12 个文件，含 ASR 配置用例 |
| 0.2.9 | 246 passed，1 skipped | 12 个文件，含 ASR 配置用例 |
| 0.2.8 | 238 passed，1 skipped | 11 个下载相关文件 |

唯一跳过为缺少可选 webview。故障定位还对照了原基线和 PR：common 目录各 1184 passed/68 skipped，全 unit 收集但只选 ASR 时各 2 passed；前缀执行两侧均在 4460 passed 后因 macOS sandbox 禁止 sysctl 中断，发生于 ASR 前，不能当作 ASR 基线同败证据。没有把这些结果宣称为 Linux 全量门禁通过。后续 CI 结果由对应新提交重新确认，未合并任何 PR。证据见 [本轮质量与范围判断](../../../task-artifacts/authorized-download-pr-feedback-20261116/quality-review.md)、[迁移必要性](../../../task-artifacts/authorized-download-pr-feedback-20261116/migration-necessity.json) 与 [本轮测试摘要](../../../task-artifacts/authorized-download-pr-feedback-20261116/followup-test-summary.json)。

本轮最终独立只读复核为 **Approve**，三个候选均无未解决阻塞项；核对最终差异、逐文件摘要及 JUnit，未另行重跑测试，详见 [本轮独立复核](../../../task-artifacts/authorized-download-pr-feedback-20261116/independent-review.md)。复核后仅追加本条真实结论，代码及测试不变。

---
id: D-20261113-1809-authorized-original-file-download
title: 授权附件改为原文件、不过期令牌与 SHA-256 留痕
slug: authorized-original-file-download
type: D
status: in-development
stage: active
author: nypdmax
author_email: chengkunck@gmail.com
reviewers:
  - independent-reviewer (/root/download_plan_review)
approved-date: 2026-11-14
created: 2026-11-13
updated: 2026-11-16
domain: security
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/common
  - jiuwenswarm/server/runtime/gateway_adapter
files:
  - jiuwenswarm/agents/harness/common/tools/send_file_to_user.py
  - jiuwenswarm/common/verified_download_assets.py
  - jiuwenswarm/common/file_download_token.py
  - jiuwenswarm/server/runtime/gateway_adapter/workspace_file_adapter.py
tags: [auto-permission, file-delivery, sha256]
scope: 最小取消文件快照与下载 TTL，复用现有登记、token 与历史，仅对四个生产文件作必要适配。
related: []
---

# 授权附件改为原文件、不过期令牌与 SHA-256 留痕

2026-11-14 管理更新：用户已明确授权按精简四文件方案实施、完成必要测试和独立评审，先不提交。下文“只制定方案、未授权”等表述保留为此前方案评审阶段的历史边界；当前阶段以前置元数据及本条确认为准。实际实施与验证只记录在关联 F，不回填本设计。

2026-11-16 设计修订：第 7 节局部替代 Windows 默认 PID 目录的旧限制；其他精简方案与既有审批保持有效。

## 1. 收敛目标与明确取舍

用户要求在“原文件＋不过期令牌＋SHA-256”的既定方向上进一步精简，只保留三项目标必需的改动，优先复用既有机制，并保持 token 结构。本稿替代上一稿的实施切片，仍只制定和评审方案，未授权改代码。

**最小方案：保留现有文件登记，将登记对象从文件副本改为原文件引用。** 登记中仍只有路径、摘要和已有元数据，不保存文件字节。继续复用原来的 stage/commit/revoke、token 生成/验证、下载通道和 chat.file 历史。

**SHA 时点明确收敛为：工具已消费本次路径/目标授权后、生成下载链接前，在现有 stage 工作线程中采集。** 它记录“本次获授权交付时”的文件指纹，沿用当前 delivery-time 行为，不能表述为审批决策瞬间的内容摘要。若后续要求审批发放瞬间摘要，需要恢复审批链改动，不属于本精简版本。

该采集时点是本稿交给用户确认的明确取舍，技术评审通过不等于用户已经确认实施。

不改 grant 数据结构、审批 rail 或异步调用链；不新增读取框架、Windows 原生句柄封装、token 字段/生成器/校验选项、RPC、前端投影或审计库。沿用现有文件类型、符号链接和平台可用的 O_NOFOLLOW 检查，不额外承诺父目录并发替换下的原子路径保证。企业 OBS、普通模式、上传/图片和旧快照继续原有行为。

## 2. 直接依据与复用理由

基线为本地 `develop@ebf16caa544d30b69925e37865ce4ce36f079875`，tree `4279755d7a980eae82da6a31840a1608d2db0677`；本地 origin/develop 同步，未联网更新。既有工作区文档修改不属于本任务，生产与测试代码未变；以下为源码核对，未执行测试。

| 已有机制/证据 | 本稿如何复用 |
| --- | --- |
| `send_file_to_user.py:573–589` 已在精确 grant 消费后，通过 asyncio.to_thread 执行 owner.stage；`:606–615,650–657` 已有提交、失败撤销；`:706–735` 已构造 token/payload。 | 保留整条流程和 try/finally，只让此次 stage 登记原文件引用、期限为 0，无需修改审批发放点或大段去缩进。 |
| `VerifiedDownloadAssetOwner.stage:74–117` 已计算 SHA、创建随机 asset_id 并原子写 sidecar；`_copy_verified_source:258–295` 一边分块读取一边摘要。 | 让读取函数支持“不写目标文件”，保留读取与摘要部分；不增加第二套签名或摘要服务。 |
| `is_active:139–175` 已将 token 的路径、ID、大小、摘要与当前登记目录中的记录匹配；`commit/revoke/prune` 已是唯一登记生命周期所有者。 | 保留登记核对，原文件引用只取消时间检查与副本范围限制，不能变成仅靠 exp=0 就放行。保留登记目录关联也比上一稿跳过登记更少改动；共享登记目录的进程仍可读取该登记，不把它称为独立进程身份认证。 |
| `file_download_token.py:119–147` 生成现有八字段；`:189–244` 最终调用 owner.is_active。 | 生成器不改，新增逻辑仅允许“受管 kind 的数值 exp=0”进入登记验证；其他用途和旧正 exp 保持原规则。 |
| `WorkspaceFileAdapter._handle_verified_download_chunk:571–670` 已使用 O_NOFOLLOW（平台支持时）、fstat 和分块上限。 | 对已验证的引用读取当前大小，其余校验/传输复用原实现，不建设平台读取组件。 |
| `app_web.py:1767–1838` 和 `SkillManager._import_local_from_download_token:5500–5565` 调用同一 token 验证入口；Gateway 精确钉 `54e5330…` 按现有 kind 透传。 | 登记仍存在、token 结构不变，这三个消费者无需新增模式参数或生产代码改造。 |
| history 保存完整 files/token；普通恢复保留 files，团队恢复保留含 token 的 URL，详见 `session_history.py:1580–1654`、`historyRestore.ts:1419–1428`、`teamHistoryPanelRestore.ts:639–647`。 | SHA 已在 token.digest，仍随原 history.jsonl 保存和恢复；不新增镜像字段或前端改动。 |

此前扩展审批发放点、传播 await、独立安全读取组件及改造全部消费者，超出了这次精简选择的必要范围，予以撤回。原文件引用所必需的撤销不删源文件、旧快照兼容和下载大小适配属于本次增量，必须保留；其他基线问题不扩大修复。

## 3. 四处必要改动

### 3.1 Owner 仅增加引用登记分支

给现有 stage 增加默认关闭的内部引用选项，原调用默认继续复制文件并要求正常期限。仅本次非企业授权交付传入引用选项和 expires_at=0；该模式下：

- 验证普通源文件，复用现有分块读取/摘要循环但不创建目标文件、不 fsync/chmod/删除源文件；返回的既有 VerifiedDownloadAsset 值对象中，sealed_path 承载原文件路径，expires_at=0。这个旧属性名仅为内部兼容，不意味着文件已封存。
- 原 sidecar 结构和原子写入不变，存放 asset_id、源路径、0、大小、摘要、state。不是文件快照，也不是新增审计存储。仅引用无需新启 sweeper；已有旧快照 sweeper 可继续工作。
- 保留 commit；stage 失败、revoke 和 prune 对引用只能删除登记，不能删除或修改源文件。为避免既有 orphan sweeper 的目录所有权混淆，引用模式在任何 prune 前拒绝位于 owner.root 内的源路径；该目录是旧快照私有缓存，不用于将旧缓存文件转成永久原文件链接。
- is_active 必须先/同时核对当前登记目录中的 sidecar：ID、路径、0、原始 size/digest、合法 state 均匹配且当前路径为普通文件才有效。仅对这种登记允许源路径位于 owner.root 外；当前文件内容/大小不与摘要重比。未登记或登记撤销后，即使 token 签名有效且 exp=0 也拒绝。
- prune 对引用不作 TTL 回收，非法引用最多清理 sidecar，绝不 unlink 其外部源文件；旧快照仍按原规则回收。旧正期限和引用两分支保持明确。

### 3.2 工具复用现有交付线程

send_file_to_user 继续先消费精确授权，再执行既有业务物化/去重、stage、构建 payload、push、commit/历史；只将该 stage 调用切换为引用登记、0 期限。SHA 自动来自现有工作线程中的读取，无需在 grant 中传摘要，无需改 before_tool/reviewer_execution。业务目录复制继续原行为，文件安全快照取消。

### 3.3 Token 只选择性解释已有字段

保持 `kind, asset_id, path, exp, size, digest, name, sid` 八字段、类型形态、签名算法和 URL。现有 generate_verified_asset_token/build_verified_asset_download_info 原样使用引用的值对象即可：path 是原路径，exp=0，digest 是交付阶段读取到的 SHA，asset_id 仍关联原登记。

验证器只为既有受管 kind 的合法数值 0（不接受 bool/空值/非有限数作为该标记）跳过时间比较，**仍调用 owner.is_active 核对登记**。旧正 exp、普通 token、上传和 Skill 图片不获得“0 即永久”的通用豁免。不新增全局免验选项或完全跳过资产登记，签名、会话及登记检查保持有效。

### 3.4 原分块读取使用当前大小

通过验证后，只对 exp=0 引用，令既有 claimed_size 工作变量取当前 fstat 大小，再复用原来的普通文件检查、offset/limit、os.read、返回 size/eof。旧快照仍校验其固定大小。Gateway 现有 HEAD/Range/流式读取因此无需改动；文件在下载期间再次变动继续原有失败/一致性限制，不新增锁或租约。

## 4. 持久化、兼容与边界

- **SHA 保存**：token.digest 同时存在原 sidecar 和原 `chat.file.files[].download_token`/URL 中。成功落盘后从 `<数据根>/agent/sessions/<会话>/history.jsonl` 的原 token 取回，刷新/重启/归档恢复不重算、不重签、不恢复 grant。同路径多次交付以各条原始记录为准，不把去重 UI 当完整审计。
- **登记恢复**：复用原 owner 的磁盘 sidecar 读取，不新增加载器或恢复机制。源文件可变可删；登记或历史被删除、写入未完成/失败时不能承诺记录或链接完整。默认登记目录仍是现有临时目录，系统删除它会使链接失效，SHA 历史仍可读。Windows 缺少 os.getuid 时该默认目录还包含 PID（`verified_download_assets.py:32–37`），所以换进程/重启也可能读不到旧登记；若要求 Windows 跨进程/重启保持下载，应复用现有 JIUWENSWARM_DOWNLOAD_ASSET_ROOT 指定稳定目录。本次不改默认目录或强制迁移而破坏旧附件兼容，验收需明确目录条件。
- **旧行为**：正 exp 快照保留旧复制、登记、TTL、撤销与固定大小校验；旧普通无 digest 历史不伪造摘要。旧客户端和 Gateway 结构不变，旧母仓服务仍会拒绝 exp=0，回退不保证新链接继续可用。
- **访问边界**：原 grant 路径/投递目标、IAM/用户实例路由、会话和 HMAC、登记目录关联仍保留。既有读取检查复用，不以本方案宣称新增跨平台原子路径或强审计保证；也不借“精简”跳过必要授权/登记。
- **元数据取舍**：不保存文件字节，但引用 JSON 登记不按 TTL 自动回收，会积累少量元数据；沿用 revoke 删除登记，不另建管理/清理机制。

## 5. 验证与估算

本次不运行功能测试。实施后优先复用现有 assets/token/send-consumer/gateway-adapter 与历史测试，新增或调整以下必要断言，不新增完整测试框架或扩大为全量模型 E2E：

| 场景 | 通过条件 |
| --- | --- |
| 引用创建、600 秒后及 owner 重建 | 原文件无复制/改权限，asset_id 只有 JSON 登记；exp=0，摘要正确；同一登记目录下重建 owner 后仍可下载。Windows 跨进程测试使用现有固定目录配置，同时如实覆盖默认 PID 目录的限制。 |
| 修改内容及大小、HEAD/GET/Range | digest 保留最初交付指纹，读取当前字节和大小，不因内容变化重审；旧正 exp 快照行为保持。 |
| stage/sidecar 写失败、revoke、prune、私有缓存源路径 | 原文件字节与权限不被删除/改写；登记可被撤销；缓存目录源路径提前拒绝，不触发误删。 |
| 无 grant、错路径/目标、未登记/跨登记目录/篡改 token | 仍失败关闭，不能仅靠 exp=0 读取文件或回退普通容器路径；原并发单次消费用例继续通过，不把共享同一登记目录误测成独立进程隔离。 |
| 共享 token 用途及异常 0 值 | 上传/图片/旧普通/旧快照保持原预期，非法类型不获得 0 期限豁免。 |
| 历史 flush 后清状态、刷新/恢复、源文件修改/删除 | 原记录 token/URL 中仍是原 SHA，不补算/覆盖，不恢复一次授权；沿用普通/团队恢复测试。 |

按新增＋删除行数估算，不含文档；没有编写实现 diff，数字不是实测或硬上限：

| 生产文件 | 预计变更行数 | 直接原因 |
| --- | ---: | --- |
| common/verified_download_assets.py | 90–150 | 引用分支、复用摘要读取、登记匹配与不删源文件。 |
| tools/send_file_to_user.py | 5–10 | 选择引用与 0 期限，调整相应生命周期说明。 |
| common/file_download_token.py | 10–20 | 受管已登记引用的 0 期限语义。 |
| WorkspaceFileAdapter | 15–40 | 引用读取当前大小和范围，保留旧分支。 |
| **生产小计** | **120–220** | **4 个既有文件，不新增生产文件。** |
| **测试** | **150–250** | **复用约 3–5 个既有测试文件，必要时增加少量用例。** |
| **合计** | **270–470** | **生产净新增约 40–120 行，具体以最终 diff 为准。** |

Owner 的清理分支是估算主要不确定性。独立评审认为初估生产 100–180 行、测试 150–250 行有可行性但偏紧；本表由主代理为清理分支留出余量，调整为生产 120–220 行，没有扩大四文件范围。实施若需要改变本稿的采集时点、token/登记契约或突破当前最小范围，应先回到方案评审，不通过删安全检查或压缩可读性凑行数。实现后仍须 code-quality-check 和 gated 最终评审；本稿技术评审通过不授权实施。

## 6. 评审与修订记录

精简稿独立评审结论 **Approve**，无当前范围阻塞项。当前 approved 仅表示实际技术评审通过；本次只修订方案、评审记录及索引，未修改生产/测试代码，未运行功能测试。

| 日期 | 参与者 | 主题与结果 |
| --- | --- | --- |
| 2026-11-13 | 用户 | 选择原文件、取消 TTL、SHA 留痕，要求保持 token 结构与恢复能力；本轮进一步要求最小改动、重新评审估算，先不改代码。 |
| 2026-11-13 | independent-reviewer（/root/download_plan_review） | 前两稿均 Approve；分别见 independent-review.md、independent-review-token-fields.md，输入快照均保留在本任务证据目录。 |
| 2026-11-13 | 主代理 | 精简为复用既有原文件引用登记和交付线程，撤回审批链、额外校验模式/消费者/平台框架改造；明确 SHA 交付时点，提交复审。 |
| 2026-11-14 | independent-reviewer（/root/download_plan_review） | **Approve**；四文件复用成立，清理不删源、登记 claims 和当前大小约束必须落实。补充既有 Windows PID 目录限制并修正“登记目录关联”用语；估算可行但偏紧，已为 Owner 清理留余量。详见本任务 independent-review-minimal-reference.md。 |

评审输入快照为本任务 `design-reviewed-input-minimal-reference.md`，SHA-256 `7f2c9a3b24c72883731d1a50cb0ac8a46c7a8fafcc4fb1d39d1beb937e2b74aa`。评审后仅补上述非阻塞说明、估算余量和真实管理状态，未改变核心实现方式。

## 7. 2026-11-16 Windows 默认登记目录修订

用户在确认默认目录使用 PID 后明确同意去掉此依赖。本节局部替代第 4 节中 Windows 默认目录保留 PID 的决定，其他 token、原文件、摘要时点、登记与清理契约继续有效。

- 保留 `JIUWENSWARM_DOWNLOAD_ASSET_ROOT` 的最高优先级；无 `os.getuid` 的平台复用 `common.utils.get_user_workspace_dir() / "download-assets"`，遵循既有 DATA_DIR/HOME 数据根配置，同一数据根跨进程定位相同登记。
- 有 getuid 的 macOS/Linux 维持原临时目录加 UID 路径，避免移动已有登记；此次只替换 PID 分支，不扩大为全平台目录迁移。
- 共享 Owner 在模块导入时固定根路径，数据根或显式覆盖须在进程启动/首次导入前设置。路径解析不创建登记目录，stage/prune 继续负责创建；首次加载既有 utils 可能初始化日志目录。
- 旧 Windows PID 目录不扫描或自动迁移；已知旧目录时可用既有显式覆盖读取。保留同一签名密钥与原文件仍是下载条件，不从历史恢复 grant 或重签 token。
- 预期验证：环境覆盖、UID 兼容、不同 PID 的路径稳定性、数据根隔离，以及两个新进程对引用/正期限资产的原 token 和登记读取。模拟 Windows 分支不冒充真实 Windows 验证。

2026-11-16，independent-reviewer（/root/download_plan_review）对上述有界方案给出 **Approve**，无当前范围阻塞项，并要求准确区分登记目录与 utils 的日志初始化；本节已采纳。实施与实际验证仍由关联 F 记录，不回填本设计。先不提交的授权边界持续有效。

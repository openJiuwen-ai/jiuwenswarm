---
id: B_20261116_1701_my-plugin-installed-only
title: 未安装插件草稿混入我的扩展并显示安装按钮
type: B
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/server/runtime
  - jiuwenswarm/channels/web/frontend
  - tests/unit_tests/agentserver
files:
  - jiuwenswarm/server/runtime/extension_package_manager.py
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx
  - tests/unit_tests/server/extensions/test_extension_package_hub.py
  - jiuwenswarm/channels/web/frontend/tests/pluginMineActions.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/pluginDetailInstall.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/pluginInstallConcurrency.test.mjs
  - tests/unit_tests/agentserver/test_paid_search_configuration.py
  - tests/unit_tests/agentserver/test_deep_adapter_empty_run_guard.py
designs: []
design_note: 用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。
---

# 未安装插件草稿混入我的扩展并显示安装按钮

## 现象与影响

“我的扩展 → 插件”出现 `[TODO: 中文展示名]` 卡片，右上角提供“安装”。
只读检查问题数据后确认：`local/css-style-editor/manifest.json` 存在，展示名仍是插件创建脚本的占位文字；
`marketplace.json` 只登记另外三个已安装插件，没有该包，`hub_state.json` 也没有其映射。
这是未安装本地草稿，不是前端模拟数据或 Hub 同名插件。

## 关联设计与当前契约

本次按用户确认的策略修复：我的插件仅代表已安装插件；安装入口在广场，使用入口在我的插件。
当前规则见 `S_01_plugin-marketplace`，位于
`.agents/agent-docs/specs/jiuwenswarm/server/runtime/S_01_plugin-marketplace.md`。
专家、专家团与连接器的原有归属规则不在此次变更范围内。

## 根因与修复决策

后端 `mine` 原条件为 `source == "local" or installed`，无安装记录的本地包也会返回。
前端我的卡片直接复用广场按钮矩阵，未安装状态显示“安装”；详情页同样只检查安装状态，不检查入口。

修复在三个边界落实：

1. 插件的 `mine` 只返回已安装包，保留 Hub 身份映射和旧 `local` 查询兼容语义。
2. 仅我的插件卡片显式采用“使用”操作，不传安装回调；未就绪时进入详情处理依赖。
3. 详情的安装按钮要求从广场进入且未安装；我的详情保留使用入口，但未安装或依赖未就绪时禁止使用。

拒绝的方案：删除用户草稿会损失开发数据；隐藏 TODO 字样无法排除其他未安装包；
把安装按钮仅改文案仍会调用安装；直接改变共享筛选器的所有调用会扩展到专家和专家团。
此次保留真实安装与连接状态，不以伪造已安装状态控制界面。

## 回归证据与适用范围

初始验证基线为 `origin/releases/0.2.9` 的 `12ae10245e9dd36476b51bba3ccabfe49736dfa0`。
develop 交付分支为独立 worktree 中的 `fix-my-plugin-actions`，按用户要求迁移到最新 `origin/develop`；
发布分支的独立适配见下文。
后端回归使用临时包目录和安装记录，前端回归使用真实组件与受控依赖，不修改问题数据或原工作区。

实施前对无安装记录、显式 `installed=false` 的本地草稿做失败对照，两例均会错误混入 `mine`；
已安装本地包和 Hub 来源对照保持正确。

修复后验证：

- `python -m pytest tests/unit_tests/server/extensions/test_extension_package_hub.py -k plugin_mine -o addopts='' -q`：
  5 passed，覆盖缺安装记录、明确未安装、已安装本地包与 Hub 安装态/资产身份；旧 `local` 仍返回草稿，
  `mine` 不访问远端 Hub，也不改变包文件。
- 后端 `test_extension_package_hub.py`、`test_extension_package_manager.py` 两文件：198 passed / 6 failed。
  对相同六例在独立进程内存加载基线模块后重跑，同样全部失败：五例为 Windows 扩展路径断言，
  一例为未指定编码的 GBK 解码失败。未扩修这些既有问题。
- `npm --cache node_modules/.cache/npm run test:plugin-mine-actions`：9 passed，验证真实列表和详情组件的
  渲染与点击，包含中英文、我的插件未安装缓存、安装就绪/依赖未就绪、广场安装和我的 MCP 连接。
  测试仅替换 store/API 边界，不用源码字符串断言代替交互。
- 既有 `equipmentMarketplace` 与 `pluginInstallConcurrency` 回归：5 passed。
- 已从真实组件测试输出的 DOM 生成独立静态 fixture，并使用系统浏览器无窗口截图核对“我的扩展”中的
  “使用”按钮。截图位于前端忽略缓存 `node_modules/.cache/plugin-mine-actions/my-plugin-use.png`；
  该图只证明组件操作展示，不是连接真实后端的完整桌面截图。
- 从问题目录只读复制四个 manifest、安装记录和 Hub 映射到临时数据根，调用真实
  `list_plugin_packages_with_hub({"filter": "mine"})`：基线返回四条（含未安装的 `css-style-editor`），
  修复后只返回三个已安装插件，包名及 Hub 资产映射保持。

环境为 Windows，Python 复用既有 `.venv`，`PYTHONPATH` 指向新 worktree；前端测试依赖使用本地副本。
测试收集前隔离用户配置与数据根，core 日志只写新 worktree。未修改用户问题目录、原工作区或其依赖。
未运行格式化、静态检查、真实模型调用或桌面应用端到端测试；组件行为通过不代表完整桌面端已验收。

## Develop 基线交付复验

develop 分支交付以 `origin/develop` 的 `dee1a566ec143c1e6550a63e5bca0964a873a94e` 为父提交，
仅移入本次修复、回归测试和对应文档。保留 develop 的插件替换流程、
`clearInstallPending(id, true)`、租户目录及其原有新增测试，没有整文件回退到发布分支。

- 新增组件行为用例 9 passed；equipmentMarketplace 4 passed；develop 的 pluginReplacement 6 passed。
- 后端两个相关文件使用 develop 锁定的 e2a `06a75d66`、core `2f4bedb9c` 本机源码缓存复验：
  216 passed / 6 failed，新增五项全过。将相同六个失败节点在独立进程内存加载 develop 原模块后重跑，
  全部同样失败，仍为五例 Windows 扩展路径断言及一例 GBK 解码；未修改原虚拟环境或扩修无关问题。
- 旧 pluginInstallConcurrency 用例在构建阶段失败：mock 缺少 develop 新增的 `PluginReplacementError` 导出。
  该测试及其导入的 store 均与 develop 相同，本次未扩修无关 mock。

## 检视与 CI 报告补充修复

按 G.EXP.04 将 `mine` 的复合条件推导式改为普通循环及条件分支；安装筛选、其他装备的本地草稿兼容、
条目顺序和每项最多加入一次的行为保持不变。

用户提供的 Linux/Python 3.11 报告对应 `c9b756b53`，包含九个失败：

- 八个图片 fallback notice 用例的无约束 `MagicMock` 自动生成假的 ReAct context 和历史游标，
  真实同步代码因此报 `HISTORY_RESTORE_CHANGED`。测试替身显式设置 `react_agent=None`，
  表达该 notice 场景没有持久上下文；不修改产品历史校验，也不吞异常。修复前八例失败，修复后八例通过。
- 付费搜索用例未隔离免费搜索开关，实际同时发生两次付费注册和三次免费注册。
  fixture 隔离 DDG/BING 开关，并参数化免费搜索开启/关闭，分别精确验证两种工具的注册次数与暴露状态；
  不改产品注册流程、不放宽为总次数下界。该文件修复后 27 passed。

联合运行以上两个 UT 文件及插件筛选五项回归，在外部免费搜索开关均为 true 的环境下得到 **40 passed**，
证明测试隔离及本次修复不依赖机器默认开关。未将用户 HTML 报告或运行日志加入仓库。
最新发布分支没有该图片 notice 测试，发布适配不引入 develop 专属功能或测试；付费搜索修正适用于两条分支。

## Releases/0.2.9 分支适配

发布分支 `fix-my-plugin-actions-0.2.9` 从再次 fetch 后的 `origin/releases/0.2.9`
`32c70070922baaafe7449523b65dce4c4fa26d6e` 创建，保留发布版本的私有 API、安装流程与依赖版本。
只移入插件列表/按钮修复、普通循环和适用的搜索 UT 修正；不引入 develop 的插件替换功能，
也不创建该版本不存在的图片 notice 测试文件。

发布分支前端验证：新增组件用例 9 passed，equipmentMarketplace 与安装并发用例合计 5 passed。
后端付费搜索完整文件与插件筛选五项联合验证 **32 passed / 64 deselected**，外部 DDG/BING 开关均为 true。
core 使用发布锁定的 `45d53dd1919` 本机 Git 源码副本；protocol 沿用原环境 `4c5f3e9`，
与发布锁定 `267c077` 存在环境差异，本次没有下载依赖或修改原虚拟环境，未据此声称全量 CI 已通过。

## 2026-11-17 release rebase

基线更新至 `ce4916ad5bfc6bc942aeb4504766338193f074ca`。上游也已加入关闭免费搜索的 fixture 循环，
冲突仅为同义实现的变量名差异；保留上游的 `flag` 循环，移除重复的 `name` 循环，
不改变付费搜索回归增强、插件筛选与按钮行为，也不恢复其他上游已删除的测试。
前端自动合并后的 package.json 相对新基线仍只增加本次测试脚本，14 项相关前端回归通过。
后端在外部 DDG/BING 均为 true 的环境下复验 **32 passed / 64 deselected**；使用锁定 core 的本机副本，
protocol 版本差异与首次发布验证相同，未修改用户环境，也未运行格式化或静态检查。

## 防错模式与资料同步

### MoA 清理 UT 的阶段等待修复（2026-11-17）

最新 CI 报告对应 develop 提交 `23e364875`，失败组合为
`test_moa_close_failure_preserves_owner_cleanup[asyncio-True-True]`。
失败发生在等待进入 MoA 关闭阶段的两秒超时，并非后续 owner 清理断言失败。
该等待还覆盖首次 dispatch 和延迟导入，不能作为实际清理耗时的上限。

测试中单独为进入故障注入阶段设置十秒有界预算，同时观察 consumer 完成和阶段事件；
consumer 提前异常直接传播，阶段等待任务在 finally 中取消并回收。
实际关闭/取消后的等待仍限两秒，故障注入位置、owner/permission/mailbox 清理断言及生产 finally 均未改变。

修复前两分支原文件各十八项本地通过；注入 2.1 秒慢启动后，旧两秒逻辑稳定失败。
develop 整文件修复后 **23 passed**，包含八种关闭/取消/启动组合及提前异常回归。
发布分支将相同测试修复移至 `c9316221e` 基线，整文件同样 **23 passed**，使用发布锁定 core 的本机副本；
protocol 版本差异保持前次记录。两分支的测试文件内容一致，未声称完整 CI 已复验通过。

安装归属必须来自持久化安装记录；发现包定义不能代替安装成功。
共享展示组件接收页面明确给定的操作，不从未就绪状态反推用户应执行安装。
已同步当前插件市场 spec 与 `docs/zh/原生插件运行时.md`、`docs/en/NativePluginRuntime.md`。

## 遗留与后续

不清理或迁移草稿，不改变创建、导入、安装事务和旧 `local` 接口。
未就绪插件的实际执行仍受已有后端门禁约束。

## Release PR 10312 检视补充（2026-11-17）

本节记录当时的恢复方案与验证；其中 `catalog` 广场草稿恢复方案已被下文“回退广场草稿恢复扩展”替代。
独立的测试写盘清理、使用操作约定及上游兼容修正仍有效。

SF-1 成立：create/import 先落盘未安装本地包，后续 install 失败、待连接流程取消或页面刷新后，
`mine` 与旧 `builtin+hub` 都不返回该包；同 ID 创建又被拒绝，导致用户无法从界面恢复。
本节补齐恢复路径，不替代前文“我的插件仅已安装、没有安装操作”的决策。

新增插件专用 `catalog` 查询，包含 builtin/Hub 与未安装 local，Web 广场列表和搜索统一使用它。
旧 `builtin+hub`、`local` 和其他装备规则保持。相同运行包名的独立 Hub 资产不会被本地草稿遮住。
安装成功后的本地卡片立即退出广场视图；显式卸载本地草稿成功后清卡片和详情缓存并返回广场，失败保留。
不自动删除或覆盖草稿，也不扩大创建/导入事务；恢复通过既有安装、卸载入口完成。

检视建议的“在我的插件展示待安装包仅提供删除”会重新混入未安装目录，因此未采用。
仅对 install 失败自动回滚也不能覆盖页面刷新中断，且不应删除已有用户草稿，故选择可重复发现的广场入口。

NTH-1 的 error+use 当前无生产入口；use 模式本就禁止安装类操作，补充 prop 约定并验证 error 时也不调用安装。
NTH-4 只涉及旧缓存/并发卸载下的未安装卡片，禁用实际使用与隐藏安装是必要门禁；本地详情仍有卸载操作，
不恢复我的安装按钮。另删除测试中的无断言价值 HTML 写盘，保留中英文按钮断言。

新增验证覆盖原 bug 的反向对照、广场查询与搜索、草稿安装后的缓存变化、本地卸载成功/失败和 Hub 保留行为。
在 `dc557bf5e` 的本次工作区差异上，完整 Hub 文件和 manager 列表/安装门禁相关回归 **88 passed**，
其中新增 catalog 用例六项，原 mine 五项保持通过；真实组件回归 **15 passed**，
equipmentMarketplace、pluginInstallConcurrency、marketSearchClient 合计 **10 passed**。
后端命令为 `python -m pytest -o addopts='' -o faulthandler_timeout=30 -p no:cacheprovider
tests/unit_tests/server/extensions/test_extension_package_hub.py
tests/unit_tests/server/extensions/test_extension_package_manager.py::TestListShowAndFileRead
tests/unit_tests/server/extensions/test_extension_package_manager.py::TestInstallPendingConnectorsGate -q --tb=short`。
前端命令为 `npm --cache node_modules/.cache/npm run test:plugin-mine-actions`，以及 esbuild 打包 helper 后的
`node --test tests/equipmentMarketplace.test.mjs tests/pluginInstallConcurrency.test.mjs tests/marketSearchClient.test.mjs`。
组件测试仍使用真实 UI 与受控 store/API，客户端测试保留真实请求路径，仅隔离无关技能安装 UI。

Python 复用既有虚拟环境，源码优先指向隔离 worktree，并追加本机缓存中的锁定 core `45d53dd1919`、
gateway `1119ea00f` 与 protocol `267c077`；本次 protocol 缓存已可用，与上文早期验证环境区别记录。
测试数据与日志隔离，未修改原工作区、用户插件目录或安装依赖；未运行格式化、静态检查或真实桌面端全链路。

### Release rebase 至 c1a79a57a（2026-11-17）

再次 fetch 后以 `origin/releases/0.2.9` 的 `c1a79a57a009f9ed39387e1c9f8101be6b757ebf` 为基线。
唯一文本冲突在 `equipmentMarketplace.ts`：同时保留上游 `isPluginBlockedInWorkMode` 与插件专用 `catalog` 重载。
其他文件核对自动合并结果，保留上游插件替换冲突、指纹确认、安装参数与取消语义，不回退新 API。

上游安装成功新增清空详情缓存；详情的直接安装和 MCP 续跑安装后均重新获取详情，避免停留在空加载壳。
广场成功续跑使用 `clearInstallPending(id, true)` 保留本次替换确认指纹，取消/失败仍清除确认。
旧并发安装测试补齐新增异常类和 i18n 边界，独立详情回归使用真实 store、API 和连接续跑观察缓存清空与恢复。

同一锁定依赖环境下，完整 Hub、manager 列表/安装门禁、付费搜索及 MoA 关闭测试合计 **162 passed**。
前端原操作矩阵、详情安装恢复、广场查询、删除、并发及上游替换确认回归合计 **39 passed**。
这些结果对应本次 rebase 工作区；未修改 develop 或原工作区，也未运行格式化、静态检查及完整桌面端验证。

### 回退广场草稿恢复扩展（2026-11-17）

用户再次确认本单仅交付“我的插件只显示已安装项、无安装按钮”，并授权回退额外的广场草稿恢复能力。
在既有 `c1a79a57a` 基线上局部撤销 `catalog` 筛选、Web 广场请求与搜索变化、草稿安装后隐藏和本地草稿删除缓存处理，
同时移除只服务这些规则的测试；不回退整个提交、上游插件替换功能或原 mine 过滤。
当前有效契约以 `S_01_plugin-marketplace` 为准，前文恢复方案和历史通过结果不再代表当前能力。

SF-1 指出的失败草稿无 UI 入口、同 ID 被占用的现象仍成立，保留为已知限制，后续单独确定恢复与清理规则。
未自动删除或覆盖用户文件，也未恢复我的插件安装按钮。
保留 HTML 调试写盘清理、error+use 操作约定及回归、MCP 重试保留替换确认、安装后详情刷新和相应真实 store 回归。
develop 分支不修改；本次只 amend 发布分支现有提交，不新增功能提交。

回退后在 `233164e25` 的本次工作区差异上复验：完整 Hub 与 manager 列表/安装门禁回归 **106 passed**，
包含原 mine 五项及上游替换测试；前端 mine 脚本 **16 passed**，并发安装 **1 passed**、
插件替换 **9 passed**、装备筛选 **4 passed**，共 **30 passed**。
Python 继续使用前述锁定 core/gateway/protocol 本机源码缓存；不安装依赖、不修改用户数据，未运行静态检查或格式化。

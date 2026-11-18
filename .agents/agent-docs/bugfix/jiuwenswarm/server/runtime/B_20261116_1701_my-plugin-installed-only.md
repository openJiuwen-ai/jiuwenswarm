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
交付分支为独立 worktree 中的 `fix-my-plugin-actions`，按用户要求迁移到最新 `origin/develop`。
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

develop 分支首次交付以 `origin/develop` 的 `dee1a566ec143c1e6550a63e5bca0964a873a94e` 为父提交，
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

## 后续 develop rebase

更新 origin 后，develop 修复分支与 `8f6ffb1194e0c8d4948080f795c28f7ea6a734ca` 只在搜索 UT 发生内容冲突。
上游已按付费工具对象过滤全局注册调用，避免后台路径串扰；保留该对象过滤、两次注册和 `skip_if_exists`
断言，同时保留本修复的 DDG/BING 环境隔离、免费开关参数化及局部 manager 的免费工具可见性断言。
不再精确统计全局免费注册次数，避免把同一类后台串扰重新引入测试；未修改产品注册流程。

合并后，原九项失败涉及的两个 UT 文件与插件筛选五项联合复验 **40 passed**，使用基线锁定的 e2a/core 缓存，
且外部 DDG/BING 均为 true。发布分支相对最新 `af98d14b7` 合并预演无冲突，按用户要求保持
`fe3c4a2db` 不做 rebase 或推送。

## 2026-11-17 develop 再次 rebase

基线更新至 `9dd7219fcfe77794086f764b7be67bedc331ebdb`，搜索 fixture 与上游新增的免费搜索隔离发生冲突。
保留上游的注释和 `delenv` 实现，不重复保留本分支原有的 `setenv` 循环；免费开关参数化、局部 manager
可见性断言及上游的付费工具注册身份过滤均保留。

上游同时已在图片 notice fixture 中 mock `sync_conversation_context`，所以移除本分支重复的
`react_agent=None` 两行；该测试文件最终与新基线相同，没有恢复上游删除或重组的其他测试。
合并后，在外部 DDG/BING 均为 true 的环境下，两个 UT 文件与插件筛选五项联合验证 **40 passed**。

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
修复后 develop 整文件 **23 passed**，包含八种关闭/取消/启动组合及提前异常回归。
这项修复只涉及测试同步，不以删除断言或吞掉关闭异常代替问题定位。

安装归属必须来自持久化安装记录；发现包定义不能代替安装成功。
共享展示组件接收页面明确给定的操作，不从未就绪状态反推用户应执行安装。
已同步当前插件市场 spec 与 `docs/zh/原生插件运行时.md`、`docs/en/NativePluginRuntime.md`。

## 遗留与后续

不清理或迁移草稿，不改变创建、导入、安装事务和旧 `local` 接口。
未就绪插件的实际执行仍受已有后端门禁约束。

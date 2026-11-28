---
id: B_20261128_0922_external-cli-switch-save-loading
title: 三方 CLI agent 开关保存往返期间仅置灰无处理中反馈，被读成状态延迟
type: B
author: yuhaiquan
author_email: 384127717@qq.com
designs: []
design_note: 由 GitCode issue #6823 直接驱动的前端交互反馈小修复，无独立 D；方案决策（乐观更新回退、loading 终选）与断言语义分界记录于本文。
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/components/ExternalCliAgentsSection.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ui/Switch/Switch.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ui/Switch/Switch.css
  - jiuwenswarm/channels/web/frontend/tests/externalCliInstallState.test.mjs
---

# 三方 CLI agent 开关保存往返期间仅置灰无处理中反馈，被读成状态延迟

## 现象与影响

三方 agent（Claude/Codex，内置 CLI 卡片）的启用开关点击后，先短暂置灰再变蓝，
测试反馈"状态反馈有延迟"（GitCode issue #6823，workswarm 0.2.9 每日构建，
develop/【通用】前端，录屏佐证）。功能正确性不受影响：保存最终都会落盘、失败会
报错回滚；问题纯在反馈形态——置灰（disabled 视觉语言）没有传达"保存中"，被读成
卡顿/延迟。

## 关联设计与当前契约

无独立 D。依据的当前契约：

- 开关是受控组件：`checked={enabled && installed}` 来自 `draftValues`（本地草稿，
  与持久化状态对齐），只有保存往返成功后由 `persistUpdates` 重写草稿才翻转颜色。
  该语义是刻意的——开关颜色等于真实持久化状态，保存失败会回滚，不假成功。
- 保存往返期间 `toggleAgentEnabled` 置 `committing[cliAgent]`，Switch 的
  `disabled` 包含该标记，置灰效果来自 `Switch.css` 的
  `.ui-switch:disabled { opacity: 0.56 }`。
- `committing` 窗口的真实时长不止 HTTP 往返：启用链路在保存前有探测校验
  （`validateAndCommitPath` 对自定义路径先探测、`resolveEnabledChoice` 无显式
  路径时逐条探测 path_histories），每次探测都是后端 spawn CLI `--version`
  子进程；后端保存时还同步执行 `ensure_codex/claude_dependency`（SDK 模块可用性
  检查）与 `refresh_external_cli_builtin_models`。写 config.yaml 本身是本地
  load/dump round trip，毫秒级，不是延迟主因。
- 内置模式且已安装时，`ConfigSourceProvider.save` 的乐观 `setConfig` 经
  ExternalCliSettings 的同步 effect 几乎即时翻转草稿；可见灰窗主要集中在探测类
  启用（自定义路径 / 无路径历史回退），探测发生在保存之前，整段期间开关都是灰的。

## 根因与修复决策

**根因（单一）：** 保存往返期间开关唯一的视觉反馈是 disabled 置灰，缺少
"处理中"的明确信号。置灰在用户心智里读作"不可用/坏了"，而不是"正在保存"。

**设计决策（两层）：**

- 第一层（通用组件契约）：`Switch` 组件新增可选 `loading` prop——轨道中央渲染
  `Loader2` spinner、隐藏滑块（`ui-switch--loading > span { opacity: 0 }`，并为
  滑块补 opacity 过渡）、`aria-busy` 标记、`loading` 隐含 `disabled` 锁定交互。
  spinner 颜色按轨道状态分档：OFF 灰轨道（启用主场景）用深色
  `--color-text-strong`，checked 蓝轨道用 `--color-text-inverse`，并以
  `.ui-switch--loading:disabled { opacity: 1 }` 抵消 disabled 的整块减淡。不传
  loading 时行为与原实现完全一致，既有使用方（此前仅 ExternalCliAgentsSection
  使用）零影响。
- 第二层（卡片接线）：`ExternalCliAgentsSection` 的内置 agent 卡片开关传
  `loading={committing[cliAgent]}`，保存往返（含启用前的探测校验）期间原地
  转圈，落定后 spinner 退场、开关按持久化结果变蓝或回 OFF。

> 检视回归（atomgit-bot Medium，PR !11141 第二轮）：初版 spinner 恒用
> `--color-text-inverse`（白），且 loading 隐含 disabled 触发既有
> `.ui-switch:disabled { opacity: 0.56 }` 整块减淡——启用场景（OFF
> `--color-switch-track-off` #c2c2c2 轨道，即 #6823 的原始场景）白 spinner
> 合成到白卡片上对比度 ~1.3:1，近乎不可见，处理中信号只在"关闭已启用 agent"
> 的蓝轨道场景成立，修复目标在主场景落空。修复 = spinner 基础色改
> `--color-text-strong`（#18181b，OFF 轨道对比度 ~10:1）、checked 态覆盖为
> inverse、`loading:disabled` 抵消减淡（与 `:disabled` 特异性同为 (0,2,0)，
> 靠源码顺序取胜）；测试以 Switch.css 源码正则钉住颜色分档与规则顺序——
> JSDOM 环境 esbuild 的 css loader 为 empty，真实可见性无法在 DOM 层断言，
> 只能源码钉住。

**备选与拒绝：**

- 「乐观翻转 enabled 草稿（点击立即变色、失败回滚）」已实施后被否决回退：初版
  对齐一键安装 `installNow` 的既有乐观模式（含三条回滚路径与 install running
  守卫，44/44 全绿），但用户评审否决——乐观方案让开关颜色先于持久化状态变化，
  与"开关颜色=真实持久化状态"的受控语义漂移；`installNow` 的乐观是安装场景
  特有（点击即代表安装意图，且其提交载荷强制 `force`），开关场景选择保守的
  "保留受控语义 + 明确的处理中反馈"。
- 「覆盖自定义（上传）agent 卡片开关」不在本次范围：自定义开关走 `onChange`
  立即翻草稿（本身即时反馈），无逐卡 commit 往返；其"快速双击存在重复提交
  窗口"属既有行为，非本修复引入，列为后续项。
- 「SystemPluginSwitch 等其它 pending 态开关接入 loading」跨模块（ConnectorMarket），
  范围外，后续统一接入。

**断言反转的理由（#6620 与开关的语义分界）：**

#6620 修复时在 `tests/externalCliInstallState.test.mjs` 留下全局断言
`doesNotMatch /loading={committing[cliAgent]}/`，本意是钉"**一键安装按钮**的
spinner 只认 `installInFlight`、不准跟卡片级 commit"（否则重启用已安装 agent
时安装按钮会像在重装转圈）。该不变式实际由同处保留的正向断言
`loading={installInFlight[cliAgent]}` 继续钉死，未放松。本修复中**开关**的
spinner 有意跟随卡片级 commit——开关转圈本身就是保存在途反馈——因此全局负断言
改为开关上的正向断言并加注释区分两个 spinner 的语义分界：按钮级动作 spinner
跟各自的 in-flight 标记，开关 spinner 跟卡片 commit。

> 检视勘误：AI 检视（CR-11141-01）将断言反转的来源记为"#10678 时期的约定"，
> 实际是 #6620（一键安装转圈）；#10678 管的是未安装锁死语义（`!installed`
> 禁用开关），与本断言无关。

## 回归证据与适用范围

修复提交 `01ae4afa19`（2026-11-27，分支 `dev_1127`，PR !11141，关联 issue
#6823）。

- 新增 `tests/externalCliInstallState.test.mjs` 用例「the enable switch shows a
  loading spinner while the save is in flight (#6823)」：挂起 commit 断言
  `ui-switch--loading` 类、`.ui-switch__spinner` 元素、`aria-busy="true"`、
  `disabled` 在位；resolve 后四者全部退场、开关恢复可点。
- 断言调整：上述全局负断言 → 开关正向断言（见前节），#6620 的安装按钮正向断言
  保持不变。
- `npm run test:external-cli-install-state`：42/42 通过（含既有 41 用例零回归；
  loading 用例内含 Switch.css 源码断言，钉住 spinner 颜色分档与
  `.ui-switch--loading:disabled` 抵消规则的源码顺序）。
- `npx tsc --noEmit`：0 错误。
- 探索期教训（未进入终版）：乐观方案期间曾发现，探测 stub 一律返回 missing 会
  因 !10678 的"未安装锁死"语义禁用开关，探测失败类用例必须用提交拒绝构造才能
  挂起；该坑对后续写开关相关测试有参考价值。
- 未验证：浏览器内真实子进程探测时长的端到端体感（依赖本机 CLI 响应速度）；
  spinner 在 OFF 轨道上的真实可辨识度（对比度按主题 token 值推算 ~10:1，未做
  浏览器截图验证）。转测时可在 0.2.x 环境复核"启用三方 agent 开关期间深色
  spinner 清晰可辨、无长时间无反馈灰窗"。

## 防错模式与资料同步

可复用模式：

1. **pending 态开关用 loading 而非纯置灰**：任何"点击触发异步保存/动作"的开关，
   在途期间应通过 `Switch` 的 `loading` prop 给出处理中反馈；置灰只表达"不可
   点击"，不表达"正在处理"。
2. **spinner 语义分界**：按钮级动作 spinner 跟各自的 in-flight 标记（如安装按钮
   只认 `installInFlight`），开关 spinner 跟卡片级 commit——为新的 in-flight
   状态接线时先确认反馈主体，避免 #6620 式的"借用别人的 loading"回归。

无需新增 spec：未变更协议或对外契约；`Switch` 的 `loading` 为向后兼容的可选
prop，组件 docstring 已定义语义。模式已由 `externalCliInstallState.test.mjs`
固化为回归断言。

## 遗留与后续

- 自定义（上传）agent 卡片开关的 committing 锁定与 loading 接入（含快速双击
  重复提交窗口的评估），属独立改进项。
- 仓内其它 pending 态开关（如 `ConnectorMarket/SystemPluginSwitch`）统一接入
  `loading`，消除同类"置灰无反馈"。
- 新增 loading 用例未覆盖 commit reject 路径的 spinner 退场（清位依赖
  `finally`）；如需钉住可补挂起-拒绝变体用例。

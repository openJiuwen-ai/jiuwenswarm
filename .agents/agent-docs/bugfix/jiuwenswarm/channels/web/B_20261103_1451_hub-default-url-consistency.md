---
id: B_20261103_1451_hub-default-url-consistency
title: Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/channels/web
  - jiuwenswarm/channels/tui
files:
  - jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts
  - jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs
  - docs/en/SwarmSkills.md
  - docs/zh/SwarmSkills.md
  - docs/en/Configuration.md
  - docs/zh/配置信息.md
designs: []
design_note: 纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。
---

# Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达

## 现象与影响

`teamskills.openjiuwen.com` 的 DNS 仍解析到 119.8.233.112，但该地址 80/443 均连接超时；
可访问的 Hub 是 `swarmskills.openjiuwen.com`。仓库里两套写法并存：

- 后端可达默认值：`hub_client.DEFAULT_HUB_BASE_URL`、`hub_oauth.py`、`desktop_app.py`、
  `electron/main.cjs`、`swarm_skill_hub/extension.py`、README。
- 仍写死不可达域名：
  - 前端回退 `TeamSkillsHubModal/index.tsx` `DEFAULT_TEAMSKILLS_HUB_BASE_URL`；
  - TUI `/teamskills` 的 config / publish / delete 帮助示例；
  - `docs/en|zh/SwarmSkills.md`、`docs/en/Configuration.md`、`docs/zh/配置信息.md`。

影响：`info` 请求失败时前端回退到不可达域名，弹窗内的 Hub 数据永久为空；按文档去取
Token 的用户打不开页面；TUI 帮助里的示例把用户导向同一个不可达地址。

更隐蔽的问题是注释与取值不一致：`TeamSkillsHubModal` 注释声称"与后端默认值一致"，
实际写的是旧域名，说明两处硬编码已经漂移且没有契约约束。

## 关联设计与当前契约

无独立 D。当前契约由后端默认值确定：

- `jiuwenswarm/server/runtime/marketplace/hub_client.py` `DEFAULT_HUB_BASE_URL =
  "https://swarmskills.openjiuwen.com"`，其余后端入口同值。
- 前端只保留一个失败回退常量，不回传 `market_url`（见
  `tests/skillHubEndpointContract.test.mjs` 对 marketplace/install 链路的约束）。

## 根因与修复决策

根因：Hub 域名从 `teamskills` 切换到 `swarmskills` 时只改了后端默认值与 README，
前端失败回退常量、TUI 帮助示例和四份文档没有同步，且缺少前后端常量一致性校验。

修复决策：

1. 前端回退常量改为 `https://swarmskills.openjiuwen.com`，与后端默认值一致；
2. TUI 三处 `--market-url` 示例、四份文档的全部旧域名替换为新域名；
3. 在既有 `skillHubEndpointContract.test.mjs` 中新增一条契约测试，直接读取前端常量与
   `hub_client.py` 的 `DEFAULT_HUB_BASE_URL` 并断言相等，避免再次漂移。

未改 `TEAM_SKILLS_HUB_BASE_URL` 环境变量名与 `teamskills_*` 配置键：它们是既有接口名，
重命名会破坏存量配置，不属于本次域名对齐范围。

备选与拒绝原因：

- 是否保留 `teamskills` 作为别名：需要基础设施把该 DNS 记录指回可用 IP，属运维侧动作，
  代码仓无法保证；本次只在文档/示例层统一到可用域名。
- 让前端从后端下发默认值：涉及新增接口与契约，超出该缺陷的最小修复范围，已由上述
  契约测试提供等效保护。

## 回归证据与适用范围

候选：分支 `fix/issue-6067-hub-domain`（基于 upstream develop `55923a834`）。
环境：本机 Windows + 仓库前端 `node_modules`。

- `npm run test:skill-hub-endpoint`：2 passed / 0 failed。新增用例
  `TeamSkillsHubModal fallback matches the server-side default Hub` 通过。
- `npx tsc --noEmit`：仅剩 `McpAppView.tsx` / `mcpAppContext.ts` 缺少
  `@modelcontextprotocol/ext-apps` 的既有报错（未触碰的文件，本地依赖缺失），
  本次改动文件无新增类型错误。
- 全仓检索 `teamskills.openjiuwen.com`：仅剩 `.gitignore` 之外的本机分诊产物，
  跟踪文件已无旧域名。

未验证/限制：

- 未在浏览器中实际触发 `info` 失败回退；回退路径逻辑未变，只替换常量值。
- TUI 帮助文案属字符串替换，未跑交互手测。

## 防错模式与资料同步

- 模式：默认值在前后端/文档各写一份时，必须有一条读取两侧源码并断言相等的契约测试，
  否则域名或地址迁移必然漏改。
- 已同步：中英文文档、TUI 示例、前端常量四处口径统一为
  `https://swarmskills.openjiuwen.com`。

## 遗留与后续

- 若后续把 `teamskills.openjiuwen.com` 指回可用 IP，需要同时更新文档说明两者等价；
  当前不作为代码仓承诺。

# 企业用户面登录解耦与认证模拟插件化

- 日期：2026-08-30
- 里程碑 / commit：登录通用化改造 / `e4b8d0cbe`
- 涉及模块：企业部署脚本、User Web、Identity/Manager 代理、前端认证入口、构建与测试

## 背景与动机

历史企业部署采用“Manager 管理面内嵌 User Web”架构：Manager Web 负责登录和上下文选择，并通过同源 `/chat/` 嵌入用户面。新交付边界调整为只交付 User Web、Gateway 和 Runtime，不再交付或维护 Manager；客户可以继续维护历史 Manager，也可以基于保留接口接入自己的身份认证系统。

原部署配置把 `USER_WEB_MODE=enterprise` 与 `IS_UP_MANAGER_WEB=true` 强绑定，导致不交付 Manager Web 时无法启动企业用户面。与此同时，当前客户侧认证服务地址和 Manager 业务接口尚未提供，联调阶段仍需使用现有 Identity/Manager，且需要一条无需真实认证、能直接填充用户、组织、组网和 Agent 三元组的调试路径。

若只在现有登录组件中增加条件分支，模拟身份数据会长期混入正式制品，客户无法完整剥离，也会扩大后续替换客户认证系统的改动面。因此本次同时完成部署解耦、认证 Provider 化和模拟插件构建期剥离。

## 方案（定案，与需求方确认）

1. **交付边界**：默认只部署 `GATEWAY WEB RUNTIME`，Manager 不属于默认交付组件；现有 Manager ↔ Runtime、Manager ↔ Gateway 交互接口不删除。
2. **产品模式保持原配置**：不新增 `enterprise-debug`，不改变 `USER_WEB_MODE` 的既有含义。`personal` 始终跳过企业登录；`enterprise` 进入企业登录和用户上下文流程。
3. **模拟认证使用独立配置**：新增 `LOGIN_AUTH_SIMULATE`，默认 `true`。`true` 使用调试认证；`false` 使用正式认证并依赖 Manager ID 认证服务。
4. **正式联调目标**：客户认证地址和 Manager 业务接口尚未提供前，正式模式默认连接当前集群内 `jiuwenclaw-identity` 与 `jiuwenclaw-manager-server`；后续只替换目标地址，不改变 User Web 调用路径。
5. **模拟能力可插拔、可剥离**：正式认证与模拟认证实现统一为 `EnterpriseAuthProvider`。模拟实现通过虚拟模块在构建期装配；客户构建不引用模拟目录，客户源码包也可直接删除该目录。
6. **配置防呆**：新增制品能力标志 `LOGIN_AUTH_SIMULATE_AVAILABLE`。若企业模式请求模拟认证但制品未包含模拟插件，构建、部署检查和 User Web 启动均明确失败。
7. **运维可观测性**：启动日志明确输出“登录认证模拟调试模式已开启”或“正式身份认证模式，依赖 manager ID 认证服务”；非法值、缺失目标、服务不可达和模式冲突使用可读中文提示。
8. **入口形态**：`personal`、`enterprise` 均暴露独立 User Web NodePort，不再要求 Manager Web 作为唯一入口；企业用户登录后由 User Web 内的小面板完成用户信息展示、组织/组网/Agent 切换和退出登录。

被否或暂缓方案：

- **只用运行时条件隐藏模拟逻辑**：否决。模拟默认身份仍会进入客户静态产物，无法真正剥离。
- **本次重新设计历史 Manager 内嵌兼容方案**：暂缓。需求方明确由后续工程师统一设计；本次只保证不删除接口、不恢复强依赖，并记录影响边界。

## 登录全过程

### personal 模式

1. User Web 读取 `USER_WEB_MODE=personal`。
2. `EnterpriseEntry` 直接渲染用户面，不检查企业 token，不访问 Identity 或 Manager。
3. `LOGIN_AUTH_SIMULATE` 不参与该流程；若配置为 `false`，启动日志提示该配置不会启用正式认证。

### enterprise 模拟模式

1. User Web 读取 `USER_WEB_MODE=enterprise`、`LOGIN_AUTH_SIMULATE=true`。
2. Provider 注册器加载可选的模拟认证插件。
3. 插件从 URL 查询参数读取可选的 `user_id/group_id/gateway_id/bot_id`；缺失值使用内置候选值。
4. User Web 建立企业上下文，展示用户面及用户小面板，不访问 Identity/Manager 用户目录接口。
5. 切换上下文仍通过统一 Provider 接口完成，退出登录回到用户面入口。

### enterprise 正式模式

1. User Web 读取 `USER_WEB_MODE=enterprise`、`LOGIN_AUTH_SIMULATE=false`。
2. Provider 注册器选择 Manager 正式认证 Provider。
3. 未发现本地 access token 时清理登录状态并跳转 `/auth`。该路径由 Manager Web 或客户统一入口提供，User Web 自身不包含登录页；若 User Web 的 SPA 回退已经落到 `/auth`，则停止重复跳转并提示用户改从统一入口访问。
4. 登录后依次通过同源代理获取：
   - `/idp/v1/auth/me`：当前用户；
   - `/idp/v1/auth/me/orgs`：授权组织；
   - `/manager-api/v1/user-console/gateways`：授权组网；
   - `/manager-api/v1/user-console/agents`：指定组织和组网下的 Agent。
5. URL 中的组织、组网和 Agent 仅作为优先候选；Provider 返回的授权结果仍是生效依据。若候选失效，自动选择第一个可用组合。
6. 401 统一清理登录状态并跳转登录页；网络、空授权或业务错误展示可读提示。
7. 用户小面板负责组织、组网、Agent 切换和注销；注销调用 Identity logout，失败时仍完成本地退出。

## 实现

### 认证 Provider 与前端入口

- `jiuwenswarm/channels/web/frontend/src/auth/types.ts`：定义 `EnterpriseAuthProvider` 和统一认证错误。
- `src/auth/manager/ManagerAuthProvider.ts`：承载 token、Identity/Manager 请求、用户目录加载及注销逻辑。
- `src/auth/simulate/SimulatedAuthProvider.ts`：集中存放调试身份和三元组候选值。
- `src/auth/providerRegistry.ts`：按 `LOGIN_AUTH_SIMULATE` 选择 Provider；插件缺失时拒绝继续。
- `src/auth/config.ts`：严格解析布尔配置，缺失时按确认方案默认 `true`。
- `src/EnterpriseEntry.tsx`：只依赖 Provider 协议，不再直接包含 Manager API 或模拟数据。

### 构建期插件剥离

- `vite.config.ts` 根据 `INCLUDE_LOGIN_AUTH_SIMULATE` / `VITE_LOGIN_AUTH_SIMULATE_AVAILABLE` 将虚拟模块绑定到模拟 Provider 或空适配器。
- `npm run build` 生成内部联调制品，默认包含模拟插件。
- `npm run build:customer` 生成客户正式制品，模拟 Provider 不进入 Rollup 模块图。
- `src/auth/simulateUnavailable.ts` 位于模拟目录之外，因此客户源码包删除整个 `src/auth/simulate/` 后仍可构建正式制品。

### 运行配置与代理

- `app_web.py` 将 `USER_WEB_MODE`、`LOGIN_AUTH_SIMULATE` 和 `LOGIN_AUTH_SIMULATE_AVAILABLE` 注入 HTML，并在启动阶段校验配置、打印模式和探测正式认证依赖。
- `USER_WEB_IDP_TARGET`、`USER_WEB_MANAGER_TARGET` 分别控制 Identity 和 Manager 业务接口目标；浏览器继续使用 `/idp`、`/manager-api` 同源路径。
- 正式模式服务探测中，401/403 表示服务可达；连接失败或 5xx 输出包含配置项、目标地址和错误原因的日志。

### 企业部署

- `deploy/enterprise/args_handler.sh`：默认模块从 `GATEWAY WEB MANAGER RUNTIME` 调整为 `GATEWAY WEB RUNTIME`。
- `check_handler.sh`：删除 enterprise 对 Manager Web 的强制依赖，增加布尔值、模式冲突、插件可用性和正式服务目标校验。
- `deploy.sh`：规范化模式，补充当前集群 Identity/Manager 默认目标并输出登录模式日志。
- `templates/web.template.yaml`：向 User Web 容器传递认证模式、插件能力和两个上游目标。
- `web_handler.sh`：两种产品模式都生成独立 User Web NodePort。
- `.env.example`、`README.md`：同步交付边界、参数语义、默认目标和客户构建说明。

## 配置矩阵

| USER_WEB_MODE | LOGIN_AUTH_SIMULATE | SIMULATE_AVAILABLE | 行为 |
|---|---:|---:|---|
| personal | true | true/false | 跳过企业认证，直接进入用户面 |
| personal | false | true/false | 仍跳过企业认证，并输出配置冲突告警 |
| enterprise | true | true | 使用可选模拟 Provider |
| enterprise | true | false | 拒绝构建、部署或启动，提示制品未包含插件 |
| enterprise | false | true/false | 使用正式 Manager/IDP Provider |

`LOGIN_AUTH_SIMULATE`、`LOGIN_AUTH_SIMULATE_AVAILABLE` 仅接受大小写规范化后的 `true` 或 `false`；其他值视为非法配置。

## 验证

- 前端认证单测：`npm run test:user-web-entry`，**10/10 通过**。覆盖 personal 跳过认证、enterprise 未登录跳转、正式上下文加载、模拟默认值、URL 三元组覆盖、布尔配置严格解析、`/auth` 自跳转保护、候选排序、Agent 回退和调试上下文。
- Python User Web 单测：`JIUWENSWARM_DATA_DIR=/private/tmp/jiuwenswarm-codex-auth-test jiuwenswarm/bin/python -m pytest -q tests/unit_tests/test_app_web_gateway_api_proxy.py`，**5/5 通过**。覆盖代理路由、运行配置注入及严格布尔解析。
- 内部制品：`npm run build` 构建成功，Vite 转换 **4559** 个模块。
- 客户制品：`npm run build:customer` 构建成功，Vite 转换 **4558** 个模块，比内部制品少模拟插件模块。
- 客户产物扫描：对 `dist` 检索 `debug-user`、`debug-group`、`debug-gateway`、`debug-agent`、对应显示名及模拟模式启动文案，**0 个文件命中**。
- 冲突配置：以 `USER_WEB_MODE=enterprise LOGIN_AUTH_SIMULATE=true INCLUDE_LOGIN_AUTH_SIMULATE=false` 执行 Vite 构建，配置加载阶段按预期失败，并输出“当前客户交付制品未包含登录认证模拟插件”。
- 企业部署 Shell 测试在当前 macOS 自带旧 Bash 环境受既有关联数组兼容问题阻塞；目标 Linux/Bash 环境需补跑 `deploy/enterprise/tests/test_identity_user_web_deploy.sh`。

## 兼容性与影响面

- `USER_WEB_MODE=personal` 行为不变，单机版继续绕过企业认证。
- `ENABLE_USER_WEB_EMBEDDING` 保留为旧配置兼容输入，但新部署应使用 `USER_WEB_MODE`。
- Manager Web 不再是 enterprise 的启动前置条件；`IS_UP_MANAGER_WEB` 只决定是否显式部署历史 Manager Web，不决定 User Web 登录模式。
- Manager、Runtime、Gateway 之间既有业务接口没有因本次认证 Provider 重构而删除或改名。
- 正式 Provider 当前仍遵循现有 Identity/Manager 接口契约，因此历史 Manager 内嵌 User Web 可继续通过同源代理访问，但“历史内嵌入口是否完整兼容新独立用户面导航”尚未在本次做端到端验收。
- 客户制品必须同时设置 `LOGIN_AUTH_SIMULATE_AVAILABLE=false` 与 `LOGIN_AUTH_SIMULATE=false`；前者描述制品能力，后者描述运行选择，两者不可互相替代。

## 开放问题与后续计划

1. 客户侧真实 ID 认证地址、Manager 业务接口地址和认证协议尚未提供；接入时实现新的正式 Provider 或替换代理目标，避免把客户特有逻辑写入 `EnterpriseEntry`。
2. 历史“Manager 管理面内嵌 User Web”的长期兼容适配由后续工程师设计；重点验证 `/chat/` 路由、token 共享、登录跳转、注销回跳和用户小面板的宿主关系。
3. 目标 Linux 环境补跑部署 Shell 测试及 Kubernetes 实际部署，记录 Pod 启动日志、NodePort 入口和两种登录模式的端到端结果。

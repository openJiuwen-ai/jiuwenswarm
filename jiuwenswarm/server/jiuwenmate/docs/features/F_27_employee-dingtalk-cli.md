# 员工钉钉 DWS CLI 应用身份接入

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | F_27 / employee-dingtalk-cli |
| 关联计划 | [JM-27](../designs/jiuwenmate-development-plan.md#jm-27-员工钉钉-dws-cli-接入扩展) |
| 方案状态 | 默认 App Token 工具、群文件操作和当前群共享日程已实现；群成员读取使用 App Token，真实钉钉联调仍待验收 |
| 最近修订日期 | 2026-10-29 |
| 契约 | [S_06 钉钉 DWS CLI 应用身份规约](../specs/S_06_employee-dingtalk-cli.md) |

## 1. 背景、目标与范围

JiuwenMate 的钉钉渠道已保存默认应用 `client_id` 与 `client_secret`。员工不需要在员工管理页单独登录。App Token 用于应用级 OpenAPI 和群成员查询首选路径；若钉钉拒绝群成员接口，后端复用服务主机上已有的共享 DWS 用户登录态查询名单，不启动登录流程。成员 `unionId` 映射和日历写入仍使用当前钉钉应用身份。

本次实现提供 DWS CLI 的应用级 Raw API (`dws api`)，并提供一个组合操作，将 DOCX 文件创建到当前群的文件空间；若群空间响应含根目录 dentry UUID，则使用新版存储 API 请求转换为在线 DOC。应用凭证自动取自钉钉渠道配置；员工不需要管理页配置或单独登录。常规 DWS 产品命令、用户数据和用户事件仍依赖 DWS 用户 OAuth；不提供 `dws auth login` 授权入口，也不宣称机器人身份可替代用户身份。

钉钉日程创建通过独立的 `create_calendar_event` 组合操作实现：发送者身份只来自可信钉钉私聊或群聊回调，后端以应用 Token 将其映射为 `unionId`，再调用日程创建 OpenAPI 写入发送者的主日历。此操作不要求员工单独登录；必须先在应用权限中开通 `Calendar.Event.Write`（日历应用中日程写权限）和用户映射所需的 `qyapi_get_member`。缺少权限时工具报告实际失败接口和权限标识，不再返回无来源的通用错误。

## 2. 设计约束与假设

- 钉钉官方 DWS CLI 的 `dws api` 支持以完整的 `DWS_CLIENT_ID` / `DWS_CLIENT_SECRET` 凭证对调用受支持的企业内部应用 OpenAPI；App Token 由 CLI 获取与缓存。该模式不需要 `dws auth login`。
- DWS 常规业务命令通过用户 OAuth 身份执行；`dws api` 仅能调用应用已开通权限的接口，且目标仅允许官方 `api.dingtalk.com` 与 `oapi.dingtalk.com` 的 HTTPS/443 地址。不能让模型提供另一应用凭证、Token 或任意主机。
- 应用身份优先来自服务端当前 `channels.dingtalk.apps` 配置中唯一的默认应用，所有员工共用该身份；若未配置默认应用，则只为明确绑定了唯一钉钉应用的员工使用其绑定身份，不回退到其他员工的机器人。凭证不得写入提示词、工具参数、命令行参数或日志，只注入后端启动的 CLI 子进程环境。
- 保留员工现有权限模式和 Bash 审批行为；DWS CLI 不是绕过执行权限的独立高权限入口。

## 3. 总体方案与取舍

实现本地 DWS CLI 运行时，优先复用运行时目录中与固定版本匹配的官方 CLI 二进制；二进制缺失时通过固定版本 `npx` 按需运行，不做 OS 全局安装。按员工和默认应用凭证版本设置私有 `DWS_CONFIG_DIR`，隔离 App Token 缓存；npm fallback 使用按版本隔离并加锁的缓存。运行时在每次工具调用时从默认应用配置动态取得凭证，只注入后端启动的 DWS CLI 子进程环境；不在员工管理页增加登录、授权、断开或启用开关。

选择应用身份 Raw API 是因为钉钉官方 CLI 支持 App Token 模式；排除全功能 DWS 用户命令，是因为它们仍需要用户 OAuth。排除复用主机 `~/.dws` 登录态，以免静默引入不透明的个人身份和共享用户权限。

## 4. 详细设计

### 4.1 绑定与运行环境

绑定解析优先匹配唯一启用的默认钉钉应用及完整凭证对；无默认应用时，只匹配唯一绑定到当前员工的钉钉应用。应用 ID 重复、绑定不唯一、凭证不完整或 Node/npm 不可用时，不向员工工具执行环境注入 DWS 应用凭证。配置变更后重新解析应用；应用 ID 或 Secret 变化会切换到新的私有 DWS 配置目录，不再选择旧 App Token 缓存。

官方 CLI 以固定版本通过 `npx --package` 按需运行，npm 缓存放在 JiuwenSwarm 私有数据目录中，不做 OS 全局安装。每名员工和凭证版本有独立 `DWS_CONFIG_DIR` 与 Home；CLI 的 App Token 缓存只在该目录中使用，应用 Secret 仅作为本次后端 CLI 子进程的环境变量。工具仅接受 `api` 命令，并拒绝非 HTTPS 钉钉域名、用户登录命令和凭证覆盖参数。

### 4.2 群文档创建

模型在钉钉群消息中选择 `operation=create_group_document` 并提供标题和可选纯文本正文。工具只使用已核验路由快照中的当前 `openConversationId`，以及该群事件来源中的 `senderStaffId`；后端通过钉钉通讯录 API 换取发送者 `unionId`，查询群文件空间并上传 DOCX。若群空间响应提供根目录 dentry UUID，则走新版存储 API 并请求以 `convertToOnlineDocTargetDocumentType=DOC` 转换为在线文档；否则走按 `spaceId` 上传的兼容流程，结果是群文件区里的 DOCX 文件。响应通过 `documentType` 明确区分 `online_doc` 和 `docx_file`，不把普通文件描述成在线文档。提交成功后再调用文件预览接口获取打开链接；若链接读取失败仍返回 `created=true` 和 `linkUnavailable=true`，避免模型重试并创建重复文件。结果同时提供群内文件 `dentryId` 与 UUID，后续更新使用 `dentryId` 定位文件。

模型更新群内文件时选择 `operation=edit_group_document` 并提供该文件的 `dentryId` 和完整正文。后端重新解析当前群空间，在该空间查询目标 dentry 并核对空间 ID、文件类型、UUID 和版本。普通 DOCX 使用原文件的父目录和文件名重新上传二进制内容，再以 `conflictStrategy=OVERWRITE` 提交为同一文件的新版本。DOCX 更新会替换全文，不提供块级或局部编辑；在线 DOC 和其他文件类型明确拒绝。提交前复查 dentry 版本；若上传期间文件版本已变化，则停止提交，避免覆盖并发编辑。签名 URL 与上传头仅在后端组合调用中使用，不返回给模型。只有提交响应仍指向原 dentry 时才报告更新成功。

### 4.3 日历日程创建

模型在钉钉私聊或群聊中选择 `operation=create_calendar_event`，提供标题、带 UTC 偏移的开始与结束时间及 IANA 时区。标题、开始时间、结束时间、时区缺失时不调用 API；结束不晚于开始、时区无效或时间与时区不一致时在本地拒绝。描述、提醒分钟数、参与者员工 ID 和钉钉视频会议链接为可选字段。参与者身份通过同一通讯录接口映射，最终只把 unionId 传给钉钉；创建者固定为当前可信消息发送者，日历固定为 `primary`。

组合流程先以 App Token 调用通讯录用户详情接口解析发送者 unionId，再 POST `/v1.0/calendar/users/{userId}/calendars/primary/events`。钉钉返回的事件 ID 是报告创建成功的必要条件；请求超时、无有效事件 ID 或调用结果不确定时停止自动重试，避免重复创建。缺少 `Calendar.Event.Write` 时，错误明确标出 `POST` 目标路径、权限标识和可读权限名称；缺少 `qyapi_get_member` 时，错误标出身份映射接口及所需通讯录权限。此特性不调用 `dws calendar` 用户 OAuth 命令，也不会要求员工授权页面。

当用户要求当前群全员参加同一个日程时，工具使用 `operation=create_calendar_event` 并设置 `include_group_members=true`。后端只从已核验的钉钉群路由取得 `openConversationId` 和发送者 `senderStaffId`，通过 App Token 调用 `POST /v1.0/im/innerGroups/members/query` 读取完整分页名单：请求携带 `openConversationId`、`userId`、`maxResults` 和整数 `nextToken`；响应成员位于 `list[].userId`，并用 `hasMore` 与整数 `nextToken` 判断是否完成。该请求格式与钉钉生成 SDK 的 `QueryInnerGroupMemberListRequest` 契约一致，不需要 DWS 用户登录。接口调用被钉钉拒绝时，保留上游错误码并报告失败接口，且不创建日程；不猜测控制台权限名称。宿主在群成员读取失败后会把最终消息替换为本次接口的确定结果，避免模型复述历史中的授权建议。名单或任一身份映射不完整时不创建；随后在发送者主日历创建一个事件并邀请其他群成员，超过单次参与人上限时分批补齐。已知参与人可通过 `attendee_staff_ids` 单独邀请，无需读取群名单。该选项在私聊不可用。原始 API 操作中的群成员查询路径会被拦截并引导回可信群日历操作，避免模型自行拼接群 ID。创建后添加参与人若部分失败，则返回已有 eventId 和已确认人数并停止重试，避免重复建日程。

创建需要钉钉应用具备通讯录用户详情读取、群文件空间读取、存储文件上传信息读取和存储文件写入权限；获取预览链接及验证更新目标还需要企业存储文件读取权限。应用还必须能访问该群。当前配置应用的只读实测已确认：`ConvFile.Space.Read` 开通后群空间查询不再返回权限拒绝；真实群调用须以当前应用的实际权限结果为准。`GET /v1.0/doc/workspaces` 返回的 `Document.Workspace.Read` 与群文件创建、DOCX 文件覆盖无关。DWS CLI 的权限错误处理会提取钉钉返回的 `requiredScopes` 和安全校验后的权限申请入口，不再把它们折叠为通用“请求失败”。创建或更新所需接口失败时，工具返回失败接口和钉钉权限错误，且同次执行不会重试写操作；只有预览链接接口失败时，保留创建成功结果并标记链接不可用。

### 4.4 执行权限和失败行为

DWS CLI 通过员工专属 `dingtalk_cli` 工具启动，argv 不经过 shell 拼接。应用凭证只进入该子进程环境，不进入 LLM 可见输入。命令错误时对模型返回去敏后的错误，不暴露环境变量、Secret 或 CLI 诊断原文；对于 DWS CLI JSON 错误，提取接口权限标识、受限的上游错误码和 HTTP 状态，以及经过域名校验的钉钉申请入口。未分类错误不得推断为权限不足或接口不支持。调用结果仅证明钉钉 API 响应，不推断应用拥有未开通的权限。

使用 `npx` 时，服务端按 CLI 版本使用独立 npm 缓存，并通过跨进程锁串行化共享缓存上的 CLI 调用；等待锁最多 5 秒，超时返回 `DINGTALK_CLI_CACHE_BUSY`。若找到相同固定版本的 CLI 二进制，则直接启动该二进制，避免每个 API 调用都经过 `npx`。版本隔离避免复用旧版本遗留的半成品包目录。npm 包目录重命名冲突（`ENOTEMPTY`）发生时，运行时在锁内清理本版本缓存中的损坏 CLI 包目录并重试一次；重试仍失败时才返回 `DINGTALK_CLI_CACHE_BUSY`。HTTP 403 映射为 `DINGTALK_CLI_API_FORBIDDEN`，其他结构化上游错误映射为 `DINGTALK_CLI_API_ERROR` 并只返回安全的错误码/HTTP 状态，不向模型暴露 CLI 原始输出。每次 DWS CLI API 子进程最多运行 30 秒；超时返回 `DINGTALK_CLI_TIMEOUT`。同一执行中首次出现 `DINGTALK_CLI_CACHE_BUSY`、`DINGTALK_CLI_API_FORBIDDEN`、`DINGTALK_CLI_API_ERROR`、`DINGTALK_CLI_EXECUTION_ERROR` 或 `DINGTALK_CLI_TIMEOUT` 后，工具返回 `stop_retrying: true`，且该停止状态在同次执行重建工具实例时仍有效；模型应直接说明错误，不改用 Bash 或其他工具探测网络。独立执行重新尝试，不共享失败状态。

### 4.5 页面与 RPC

不增加员工管理页 DWS 登录授权页面或相应的 `jiuwenmate.dingtalk_cli.auth.*` RPC。钉钉应用绑定仍是 DWS 应用身份的唯一配置入口；解绑、删除员工后清理对应 CLI 配置目录。

## 5. 影响面与实施

- 服务端新增默认 DWS CLI 运行时，并在每次执行时读取钉钉默认应用、按需启动 App Token API 工具。
- 从员工详情页、前端 API、服务端 RPC 中移除独立 DWS 登录授权流程。
- 所有员工共用钉钉默认应用身份；各员工的 DWS 配置与 App Token 缓存仍分别保存。
- 更新 JM-27、S_06 与中英文系统提示，记录无用户 OAuth 能力边界。

## 6. 验证设计与实际记录

| 场景 | 预期结果 | 实际结果 |
| --- | --- | --- |
| 默认钉钉应用有效时 | 所有员工自动获得 `dingtalk_cli` 工具；首次调用按需下载并运行 CLI，不要求登录或单独绑定 | 实现中 |
| 无默认应用时员工绑定唯一应用 | 只向该员工注入绑定应用；其他员工不可使用 | 新增回归测试通过 |
| 无默认应用且当前员工未绑定、重复 App ID 或凭证不完整 | 不注入 DWS 应用凭证 | 实现中 |
| App Token API 命令 | 仅由默认应用身份发起；限制官方 HTTPS/443 API 域名；不经 shell 拼接参数 | 组件测试通过，真实 API 待验收 |
| npm 缓存并发、API 调用超时或执行失败 | npx 启动使用跨进程缓存锁；损坏包目录在锁内清理并重试一次；单次子进程最多 30 秒；同一执行遇到运行失败后停止重复调用 | 缓存和权限错误分支有回归测试 |
| 当前群创建文档 | 使用可信群 ID 和发送者身份，经 App Token 查询群空间并创建文档；根 UUID 可用时请求在线 DOC 转换 | 替身 API 验证在线 DOC 和 DOCX 文件两条路径；真实权限与群写入仍待验收 |
| 更新当前群 DOCX 文件 | 先核验 dentry 属于当前群，再以同目录、同文件名和 `OVERWRITE` 写入完整正文；并发版本变化时中止 | 组件测试通过；真实钉钉写入仍待验收 |
| 私聊或群聊创建个人日程 | 以可信发送者为组织者，使用 App Token 写入其主日历；无有效返回 ID 时不宣称成功 | 组件测试覆盖请求体、身份来源和时间校验；真实应用需开通日历写和通讯录用户详情权限后验收 |
| 当前群全员共享一个日程 | 从可信当前群读取完整成员分页并映射全部 unionId；名单不完整时不创建；成功后返回 eventId 和已邀请人数 | 替身测试覆盖 InnerGroup API 的请求/分页字段、可信群路由及查询失败时不创建；真实应用权限与日历写入待验收 |
| 凭证轮换、解绑和删除员工 | 轮换时切换私有缓存目录；删除员工时清理其 CLI 运行目录 | 实现中 |
| 员工管理界面 | 不出现 DWS 单独登录、授权或启用页面；飞书登录界面不受影响 | 实现中 |
| CLI 缺失、Node/npm 缺失、安装失败、网络失败 | 不泄露凭证；错误不阻断普通对话；DWS CLI 能力保持不可用 | 实现中 |

## 7. 已知限制

- `dws api` 只覆盖钉钉企业内部应用 App Token 支持且已授权的 OpenAPI。常规 DWS 命令（包括基于当前用户权限的群搜索、文档和事件命令）仍须单独的用户 OAuth；本特性不提供这些命令的无登录替代实现。
- 过去对 `GET /v1.0/doc/workspaces` 的 403 只说明知识库工作空间接口未授权；它不是群文档接口，也不再作为群文档写入是否可用的判断依据。当前真实应用已成功取得 App Token，且 `ConvFile.Space.Read` 已开通；`qyapi_get_member` 仍未授权，阻断发送者身份映射。真实群写入仍待该权限开通后验收。
- DOCX 更新是整篇内容替换，保留同一群文件条目并提交新版本；不支持追加、局部编辑或并发合并。通过 `dws doc` 直接编辑在线文档块仍依赖用户 OAuth；本工具明确实现的 App Token 覆写路径不依赖该登录。
- 日历操作仅创建当前可信消息发送者主日历上的个人日程或共享日程；读取、更新、删除和闲忙查询未接入。钉钉应用需具备对应日历写入和通讯录用户详情能力。
- 当前群共享日程只创建一个共享事件，发送者为组织者、其他群成员为参与人；群成员读取依赖应用对 `innerGroups/members/query` 的实际访问权限。已知参与人的员工 ID 可显式传入并邀请；不支持为全体成员分别创建独立日程。
- CLI 包按需运行依赖部署环境的 Node.js/npm 和网络。自动化替身验证不代表真实钉钉应用权限和业务 API 已联通。

## 8. 设计依据

- [DWS CLI 中文 README](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/README_zh.md)：说明 `dws api` 的应用级 Token 模式与环境变量。
- [DWS CLI 关于机器人身份的官方 Issue](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/issues/866)：维护者区分用户 OAuth、机器人发消息和应用身份 Raw API；常规 DWS 命令不能由 Bot Token 替代。

## 9. 实施修订（2026-10-27）

真实群聊中，员工收到“新建钉钉群文档”后确实进入执行。14:10 的这次请求中，DWS CLI 启动遇到 npm `ENOTEMPTY`，模型随后改用 Bash 探测网络；Bash 触发智能审批，但审批提示未投递到群里，请求因此等待审批超时。14:26 才触发第二次执行并发出答复，延迟约 16 分钟；该答复误称无法获取 App Token，与后续真实 API 结果不符，文档没有创建。由此确认此前仅映射缓存错误并不足够：缓存损坏需在锁内修复并限次重试，且停止重试状态必须跨同一执行中的工具实例重建共享。现已实现这两点，并更新工具说明禁止在 DWS 调用失败后改用 Bash 探测；服务于 14:34 重启，140 项钉钉相关回归测试通过。

重启后以当前渠道绑定的应用执行只读 `GET /v1.0/doc/workspaces`，CLI 成功启动并取得钉钉明确错误：缺少 `Document.Workspace.Read`。因此当前应用的权限尚未对该接口生效，不能声称文档权限已验证；本次没有执行写请求，也没有创建 `test` 文档。钉钉官方 DWS Wiki 建议权限变更后回读权限状态、检查 `needsPublish`，并确认目标应用版本已进入 `RELEASE`；见 [官方 DWS 命令 Wiki](https://open.dingtalk.com/dingtalk-dev/wiki)。此前将 `POST /v1.0/doc/workspaces` 当作文档创建请求是错误的：该调用不是 `dws doc create`。当前员工工具只开放 App Token `dws api`，尚未验证可通过应用身份创建目标群的空文档；真实写入仍待确认正确接口、权限及群文档归属后验收。

随后使用当前渠道应用凭证单独核验认证与 API 授权：`POST /v1.0/oauth2/accessToken` 返回 HTTP 200，确认成功取得 App Token；用该 token 请求 `/v1.0/contact/users/me`、`/v1.0/microApp/allApps`、`/v1.0/doc/workspaces`、`/v2.0/wiki/nodes` 则分别返回 HTTP 403 `Forbidden.AccessDenied.AccessTokenPermissionDenied`，钉钉明确报告缺少 `Contact.User.Read`、`qyapi_get_microapp_list`、`Document.Workspace.Read`、`Wiki.Node.Read`。因此“无法获取 access_token”的判断不成立；当前配置对应的读取权限尚未对这些 API 生效。实际文档创建所需的写权限范围尚未核实，也未创建文档。此前 CLI 层把 403 和缺失 scope 压成泛化错误，现仅在匹配上述钉钉权限错误码时向模型返回受限的 scope 标识，不透传原始诊断、链接或凭证。npm 缓存并发锁等待上限 5 秒、CLI 子进程上限 30 秒，同一执行遇到缓存占用、403、调用错误或超时即停止重复调用。相关组件回归测试通过。

本次修订重新执行只读 `GET /v1.0/doc/workspaces`，仍收到缺少 `Document.Workspace.Read` 的 403；App Token 获取正常。该 scope 是 API 返回的机器标识，不能据此断言钉钉管理界面也用这个字符串作为权限名称。工具错误现会同时说明被拒绝的 HTTP 方法和接口路径，并解释权限用途；钉钉群请求会把已核验路由中的当前群 ID 和消息发送者 `senderStaffId` 提供给工具说明，不把 `senderStaffId` 当作 `operatorId`。未执行任何写请求，群文档创建与群授权仍待真实接口和权限验证。

本次后续修订纠正接口方向：旧版按 `spaceId` 提交接口没有在线文档转换字段，在线 DOC 转换应使用新版以父目录 dentry UUID 定位的接口。因此运行时在群空间响应含根 UUID 时走新版在线 DOC 转换，否则只创建 DOCX 群文件并通过 `documentType` 明示结果。上传签名 URL 在后端内存中使用，不进入模型返回值。三份针对性回归文件共 83 项通过；这些替身测试不代表当前钉钉应用权限已开通或真实群写入已经成功。`Document.Workspace.Read` 与群文件空间权限是不同权限，之前把它作为建群文档前置条件属于错误判断。

本次修订补齐已创建 DOCX 的更新能力。钉钉存储提交接口支持 `conflictStrategy=OVERWRITE`；工具会用当前群空间查询 dentry，保留原文件名和父目录，重新上传 DOCX 并提交覆盖，不把普通 DOCX 误当作在线文档正文。更新期间重新查询版本，检测到并发变化时在提交前失败关闭。新增测试覆盖同群文件覆盖参数和跨群 dentry 拒绝；这些替身测试验证调用边界，不等于已对用户空白文件执行写入或真实钉钉验收。用户文档保持未修改。

## 10. 实施修订（2026-10-28）

补齐应用 Token 日程创建组合操作。原工具只有 raw `api` 操作，员工收到建日程请求时由模型自行拼装身份查询和创建接口；CLI 对其中一步返回通用执行错误时，既无法稳定定位错误阶段，也缺少专门校验和防重复写入状态。现由后端使用可信私聊／群聊发送者标识映射 unionId，固定写入该用户的 `primary` 日历，并在工具结果缺少 `eventId` 时明确停止、不宣称成功。新增 `Calendar.Event.Write` 的权限用途映射；`qyapi_get_member` 与 `Calendar.Event.Write` 未开通时可分别指出身份映射权限或日历写权限。替身测试覆盖创建请求、必填字段、可信发送者和 raw 写 API 阻断；未使用真实应用执行日程写入，真实权限验收仍需在钉钉侧确认。

## 11. 实施修订（2026-10-29）

增加当前钉钉群成员共享日程选项，修复“添加全群成员”仍走个人日程接口、无法自动读取和邀请群成员的问题。工具通过 `include_group_members=true` 从可信群消息取群 ID，完整读取成员分页后映射员工 ID，再创建一个共享事件；成员读取或映射失败时不会提前创建。参与人数超过单次接口批量上限时继续分批邀请；中途失败会返回 eventId 与已确认数量并停止重建。当前只完成替身 API 验证，真实群成员读取及日历写入仍需使用当前应用权限验收。

## 12. 实施修订（2026-10-29）

复核后确认 `/v1.0/im/innerGroups/members/query` 是钉钉生成 SDK 对应的群成员查询接口；原实现错把响应当成 `userIds` 列表并将分页游标按字符串处理。现已按 SDK 契约携带 `openConversationId`、发送者 `userId`、`maxResults` 和整数 `nextToken`，并解析 `list[].userId` 及整数游标；替身测试覆盖两页名单和权限/执行失败时不创建日程。官方 SDK 请求字段见 [QueryInnerGroupMemberListRequest](https://javadoc.io/static/com.aliyun/dingtalk/2.1.39/com/aliyun/dingtalkim_1_0/models/QueryInnerGroupMemberListRequest.html)。真实应用权限及群日历写入仍待验收，没有执行真实钉钉写入。


## 13–15. 历史方案（已由第 17 节替代）

早期多次在 App Token 群成员查询和主机共享 DWS 用户登录态之间切换，遗留了重复的修订记录。真实运行确认 `Chat.OrgInnerGroup.Read` 不宜作为控制台权限名称提示，共享主机账号也不能代表员工独立授权。共享账号回退已经移除；当前以第 17 节员工 OAuth 路径及无授权时的应用路径为准。

## 16. 实施修订（2026-10-30）：员工级 OAuth 与在线群文档失败关闭

团队版员工详情页按钉钉连接展示 DWS 设备码授权状态，支持开始、轮询、取消及断开；服务端校验员工配置与连接键仍匹配，再以连接 agent ID 作为 scope。用户 profile 与应用 Token profile 分目录，并按连接和应用凭证版本隔离。授权 URL、用户码和有限账号资料经过 RPC 字段白名单；Access Token、Secret 与 CLI 路径不返回前端。在线群文档操作在未授权时失败关闭。

此前方案依赖群根目录 UUID 并请求 DOCX 转换；实测群空间 API 不返回根 UUID，`dentryId=0` 的查询失败，因此该方案未打通。修订后使用员工已授权用户 profile 的 `doc create` 创建 ALIDOC，并由 `drive info` 验证类型；随后按真实文档 UUID 定位存储实体，通过群文件链接发送 API 在可信当前群建立在线入口，分页读回群文件列表验证。该路径需要 `ConvFile.File.Send` 权限。后续步骤失败时保留已创建的文档链接，不再让模型把“已有文档但未进入群”表述为“没有创建任何文档”。

验证记录：真实授权账号已创建空白「test在线文档」，`drive info` 返回 `ALIDOC/adoc`，UUID 到存储实体映射成功。最初发送到群时缺少 `ConvFile.File.Send`；用户开通后，同一篇文档已成功发送并在目标群文件列表中读回。发送接口使用内部数字文件名的问题通过群文件入口重命名修复，再以员工用户 CLI 读回标题「test在线文档」和 `ALIDOC/adoc` 在线类型。当前相关 provider/tool 测试共 86 项通过。

早期仅完成构建时，授权和群写入尚未验收；上述验证记录已更新为真实设备码授权、在线文档创建及群文件读回结果。历史构建记录不替代实际业务验证。


## 17. 实施修订（2026-10-30）：员工账号创建全群会议

此前群成员回退读取主机共享 `~/.dws` 登录态，未接入员工 OAuth；实测共享登录数据不存在。员工 CLI 返回的名单包含 `openDingtalkId`，强当成企业 staffId 会导致通讯录映射失败。现全群会议优先复用该员工的独立授权和当前组织 profile，分页读取可信当前群，区分 userId/openDingTalkId 后由用户日历命令分别邀请。开放 ID 路径保留跨组织成员；群 ID 只能来自已验证平台路由，不通过姓名或不兼容 ID 命名空间猜测成员。无授权时仅保留原有应用路径，移除共享账号回退。

完整名单、有效身份和最多 500 人的用户 CLI 限制在写入前校验；写入只调用一次，缺少 eventId 或连接中断时禁止自动重建。实际测试群已通过员工授权读到 2 位成员，CLI `--dry-run` 预览保留了两位开放 ID，并明确返回 `executed=false`。本次未获得新会议标题和时间，因此未创建真实测试会议或给群成员发送日历邀请。


## 18. 钉钉实现清理与审查（2026-10-30）

审查覆盖 Gateway 钉钉配置、连接、文件服务、入站、定时投递兼容入口、员工 CLI/provider/runtime/tool、授权 RPC 与前端。两处 cron 路径是仍有调用的兼容转发，保留；service 层无人调用的独立群日历包装已移除，provider 内部实际使用的方法保留。

合并应用/用户 CLI 环境构造及用户命令响应处理，统一安装缓存策略和响应大小限制；去掉无读取变量、重复参数校验、无效下载标签和终生缓存权限拒绝的分支。补齐默认签名上传实例方法、文件配置透传、文本/附件共用投递回执分类、raw API 绝对 URL/省略 operation 的保护边界。

OAuth 断开前停止待完成授权；开始/取消/断开按员工串行，取消必须匹配员工 scope；CLI 状态失败保留 error，不伪报未绑定；多账号不再默认选第一项。前端按连接废弃过期响应并避免重叠轮询，移除连接后清理状态。更新 Secret 时未编辑 robotCode 则保留配置，明确清空仍可生效。

验证以本轮最终定向 pytest、前端行为测试、构建及只读真实登录/名单检查为准；重构期间不新建外部会议或文档。

最终验证：钉钉/附件/投递/cron/生产接线定向 pytest 为 240 项通过；前端 API 请求测试 31 项、Profile 页面行为测试 28 项通过（包含 3 个授权竞态回归）；`npm run build` 成功。完整 profile+workspace 联合脚本仍有既存 WorkspacePage 的 `import.meta.glob` Node 测试环境失败，未改非钉钉 Workspace 构建逻辑。真实只读检查确认已有员工授权保持 connected、RPC 不返回私有身份字段、当前测试群完整返回 2 人。


## 19. 日程标题更新（2026-10-30）

CLI 已支持日程更新，但员工工具原先只暴露创建，导致模型拿 callback staffId 拼接 Raw Calendar PATCH 路径并收到 404。HTTP 404 本身不足以判定 unionId 错误，因此不延续模型对原因的猜测。新增明确的 `update_calendar_event(event_id, title)` 组合操作，以该员工用户 OAuth 和固定组织 profile 读取、更新原事件并读回校验，禁止用重建代替重命名。Raw 日历 PUT/PATCH/DELETE 也纳入宿主操作保护。当前只支持标题更新，不开放删除。

真实验证：用户指定的 2026-10-31 16:00–17:00 原日程标题已从「会议」更新为「AI同事讨论」；eventId、起止时间、参与人、提醒、组织者、会议室和全天状态均与修改前一致，没有新建或删除日程。

## 20. 在线文档搜索、读取与编辑（2026-10-30）

状态：已实现并通过定向回归与临时文档真实联调。沿用本特性的员工独立 OAuth 与受控操作，不开放任意用户 CLI 或 shell；不修改其他平台与个人版。

### 背景、方案与取舍

已有创建操作能生成真实在线文档，但 `edit_group_document` 仅覆盖 DOCX，不能继续编辑在线对象。新增 `search_online_documents(query, cursor, limit)`、`read_online_document(node_id)` 和 `edit_online_document(node_id, edit_mode, content, old_text, new_text)`，分别调用固定版本 CLI 的 `doc +search/+fetch/+update`。搜索限定在线文字文档，返回真实候选 ID、链接和分页边界；不自动选择第一个同名候选。读取返回 Markdown 和稳定对象身份；编辑只接受明确对象，支持追加、唯一文本替换与明确的整篇覆盖，保留原 DOCX 操作。

选择受控操作而非复制飞书的完整 Bash CLI 入口，复用现有凭据隔离与错误控制，避免此次扩展无关产品能力。在线对象只使用当前员工的已授权 profile；模型不能传 Token、profile 或本地文件路径。正文经 stdin 传入，避免以 `@文件` 形式意外读取宿主文件。CLI shortcut 的结构化 `status` 契约与旧原子命令的 `success` 契约分别校验；不能因退出码为零而报告成功。

### 修改与验证边界

代码限定 `dingtalk_cli_local_provider.py`、`dingtalk_cli_runtime.py`、`dingtalk_cli_tool.py` 及其测试。修改后同步 S_06。覆盖未授权、分页、对象身份不匹配、非在线对象、编辑失败/部分成功、正文输入与员工隔离。编辑由 CLI 自带回读验证确认；结果不确定时停止，不能重放成功写入或新建替代原文档。

已执行只读探测：当前员工的 `doc +search` 成功返回 2 个同名候选；`doc +fetch` 成功读取先前测试在线文档及其群文件入口。两者使用 `status` 契约，顶层没有 `success=true`，因此旧用户命令包装不能直接复用成功判断。真实 `+update` 使用 `data.nodeId`、`data.verified` 与 `data.verification.verified`，不能按读取响应的 target 结构判断写入。

真实联调：创建未附加到群的临时在线文档，读取空正文，追加内容，唯一文本替换并回读，整篇覆盖并再次回读，全部符合预期。临时文档以同一节点 ID 清理，删除响应 `ok=true/outcome=success`。没有发送群消息或改动已有群文档。独立审查指出文本替换使用 argv 的跨平台长度限制，已把 old/new 上限分别限定为 4,000 字符，正文 stdin 上限仍为 100,000 字符。

最终定向验证：钉钉文件/入站、附件、cron、CLI provider/tool 及生产接线 308 项通过。未修改前端，因此本轮未重复前端构建。普通群消息摄入不属于上述已完成能力。

## 21. 普通群消息智能响应接入评估（2026-10-30）

状态：开发者先批准增量读取方案，接口核验确认提及元数据不可得后，又明确选择“先实现保守版本”。保守摄入、轮询与共享 IM 生命周期接线已实现，验证记录见本节末尾。

当前只注册机器人回调，群普通消息无法持续进入已存在的 adaptive 判断。官方 CLI 1.0.65 的指定群用户事件能达到 ready，但该版本及 1.0.66 文档仍声明过滤授权账号自己发送的消息，且未提供公开关闭参数；短时间 ready 且零事件不构成业务送达验收。

建议复用员工已有 OAuth，对已知、仍有访问权且开启智能响应的群做约 10 秒周期的有界增量历史读取，不强制新增监测账号。实测测试群返回用户和机器人消息，sendType 为 user/robot；历史 messageId 与 openMessageId 相同，且 8 条消息 ID 与既有机器人回调匹配。历史发送者 ID 为开放 ID，不能当作企业 staffId；返回字段没有可靠的结构化提及信息，自动执行前仍须核验这一边界。

最小候选修改：新增 `dingtalk_group_polling.py` 管理增量水位、限额和退避；在 `agent_ws_server.py` 共享 IM runtime 起停时管理该任务；runtime/provider 提供宿主专用的群历史读取；`dingtalk_inbound.py` 以独立历史入口接入已有群归档和调度，不伪造机器人回调。旧消息不重放，机器人消息只归档，跨来源统一 ID 去重，未知身份或提及元数据不冒充已知值。测试需覆盖本人/其他成员普通消息、@ 回调去重、其他提及、机器人回声、passive、解绑/撤权/停服。以上为候选设计，尚未执行这些验收。

批准后的只读核验：atomic `chat message list-by-ids` 的 JSON/raw 与 `chat +messages-mget` 均没有提供可靠的 atUsers/atDingtalkIds/@all 字段；atomic 保留上游原字段，因此不是简单的 CLI 投影丢失。`search-advanced` 只支持 at-me 和指定 at-ids 的正向过滤，不支持公开的任意提及、排除提及或 @all 通配；完整当前群成员与机器人名单也不能覆盖退出成员、移除机器人、@all 或索引延迟。不能以搜索未命中证明无提及。

本次 97 条测试群消息中，39 条 user、58 条 robot；33 条正文有字面 `@`，未发现结构化标记。开发者据此选择保守版本：非机器人且正文不含 `@` 的新增文字进入 adaptive，其余只归档并保留机器人回调。该方案依赖接收正文保留提及展示，不能宣称支持完整提及语义。

通用准备已完成宿主专用 `read_group_history`：只使用当前员工 OAuth，最多三页/150 条，校验精确群 ID、消息 ID 和时区时间边界，并返回账号摘要与完整性。分页读取的 CLI 契约不含顶层 success，已按其真实结构校验。新增五项边界回归，相关定向测试共 313 项通过；真实最近五分钟受限读取完成，返回零条。尚未接入轮询任务、调度器或模型工具，也未为这部分重启服务。实际样本中八条可与机器人回调对应的消息均含字面 @；八条用户文字不含 @。这只支持样本回放，不构成完整提及语义的保证。

### 保守版本实施与验证（2026-10-31）

上述 313 项和未接线状态为通用准备阶段记录。现已增加 `DingTalkGroupPoller`：约十秒间隔、全局每轮最多八次读取尝试，同 namespace/账号/群合并查询，当前账号、连接、模式和成员访问权重验；同账号首选读取员工缺席时只在有界预算内换其他已授权员工。状态只保存版本和时间边界，私有目录原子落盘，不保存正文或 Token。临时授权错误保持执行下限和进度，明确撤权则重置执行下限；旧记录不重新执行。

`DingTalkInboundHandler.handle_history_message` 使用独立入口，不把历史伪造成机器人回调；只归档机器人、含 ASCII/全角 @ 或观察下限之前的文字，普通新增文字复用现有参与判断、调度与机器人主动群投递。跨来源使用原消息 ID 和每员工 source_ref；历史不覆盖较权威的回调内容。开放身份明确标记，需 staffId 的日历/群文件/DOCX 工具阻断，授权账号的在线文档操作继续可用。

真实只读核验发现 `chat group bots` 返回 `result.bots[].robotCode`，能与配置 robot_code 精确匹配；不能拿 openBotId 替代。现读取历史前校验当前员工机器人在群，收到的实时 botCodes 再筛选同账号其他员工；确认缺席会撤销缓存成员权，避免机器人被踢后继续读取/执行。新回调恢复可见起点，迟到旧回调不恢复较新的离群状态。

最终审查补充：恢复离群访问不能使用回调接收时间，因为重试可能在离群后才到达。事件独立保留 SDK 的 `createAt` 平台时间，跨 Gateway/AgentServer 以 ISO 传输；只有有可靠平台时间、在接收前五分钟以内且晚于离群证据的回调才可恢复。缺少时间不恢复，延迟旧消息不推翻离群记录，未改变 core 状态或执行协议。

验证：钉钉/附件/cron/provider/tool/runtime/poller/生产接线共 425 项通过；前端 API 行为测试 31 项通过，`npm run build` 成功。团队版中英文智能响应提示同步为授权后定期检查无 @ 的新增文字。真实授权账号核验返回同一 reader 摘要、群中四个机器人、最近五分钟零条新消息；录制替身轮询完成一次读取且只写临时 checkpoint，没有创建 Request 或发送消息。真实群新消息自动判断/发送仍需用户发言验收，不能把只读检查写成已经完成该验收。

额外非钉钉共享接线回归为 56 项通过、2 项既存失败：飞书测试硬编码恢复渠道集合不符当前动态集合，以及 WeLink reconciliation 被当前 ChannelManager 渠道校验拒绝。使用 HEAD 的共享 IM 启停方法在独立测试进程复现同样两项失败，ChannelManager 文件亦与 HEAD 一致；未修改这些非钉钉行为。

English summary: Conservative DingTalk group ingestion now uses isolated employee OAuth, bounded incremental reads, live robotCode membership checks and persistent overlapping checkpoints. Only new human text without ASCII/full-width @ enters existing adaptive participation; replies retain the employee's configured bot. Complete mention semantics and a real user-triggered group send remain outside the verified evidence.

服务验证：已重启 Swarm，5173/18092/19001/19000 均监听，前端 HTTP 200；共享 IM runtime 已启动，0600 权限的轮询 checkpoint 在两次观测间持续前移。初次读取失败后由退避重试恢复；对“系统代理导致失败”的怀疑经代理/直连对照后未成立，未修改代理配置。没有发送真实测试群消息，真实参与和回复仍待用户发言观察。

## 22. 已观测上下文与参与判断兼容修复（2026-10-31）

用户真实请求 `req_60c10652009846899a026820ece7a976` 的最终事件为 failed，reason 为 `group history unavailable: known collection gap`，此前没有模型或工具执行事件。钉钉采集正确保留 archive.gap，但 core 的参与判断会直接拒绝 `GroupMessagesResult.status=incomplete`。原接线测试替换了 PA 执行，没有覆盖此真实判断入口，因此未发现这一兼容问题。

最小修复仅在宿主 `HostGroupChatPort.recent_messages` 对钉钉、无游标首屏、非空已知消息、且确切原因是 known collection gap 时返回 `found` 和明确的观测范围说明。它表示找到可读的已知消息，不宣称平台历史完整。归档 gap、空结果、未知游标、权限复查、后续分页和跨群搜索保留原语义。钉钉系统提示同步要求不能从缺失发言推断不存在、同意或全部成员已回复；不修改 core 协议或其他平台。

回归使用真实 core `judge_participation`、真实 HostGroupChatPort 与 SQLite 归档，只替换模型调用，确认先复现中断再通过，并验证归档仍 incomplete、未知游标仍 incomplete。新增首屏边界覆盖钉钉非空、钉钉空页、飞书非空三种情况。该真实请求的实际群记录只读验证返回 found、8 条已知消息和不完整警示；未重放请求或发送群消息。

扩展回归为 446 项通过、1 项既存失败：“仅保留最新100条”断言与当前上限5000不符；本次没有修改归档实现，该实现与 HEAD 完全一致。排除该既存失败后重跑钉钉文件/入站、附件、cron、provider/tool、生产接线、轮询/runtime 和群读取端口专项，结果为 446 passed、1 deselected。

运行验证：2026-10-31 09:23 重启 Swarm，新的 runtime worker 已启动共享 IM；5173/18092/19001/19000 均监听，前端 HTTP 200。轮询进度从 01:22:21 UTC 前移至 01:23:04、01:23:23 UTC，确认重启后继续读取。未重放已结束的失败请求或向真实群发送测试消息，实际群回复仍待新消息验收。

English summary: DingTalk participation may use a nonempty first-page view of known observations while the archive still records a collection gap. Empty, invalid-cursor, authorization, pagination and search results keep their completeness guarantees. The model is explicitly told that unobserved messages cannot prove absence or consent.

## 23. 机器人发送回执与群回读重复归档修复（2026-10-31）

用户展示的 09:24:56/09:24:58 回复在归档中确有两行：发送侧以 processQueryKey 为 ID、source_ref 为 delivery；用户历史读取以 messageId 为 ID、发送者为 botOpenDingTalkId。真实只读核验确认群机器人列表提供 robotCode → botOpenDingTalkId，但消息本身没有 processQueryKey，不能宣称两种 ID 有直接映射。

最小方案：宿主历史结果保留经过校验的机器人发送者映射；轮询仅在 complete 且无 hasMore 的完整有界读取后，针对本群归档合并同一确认机器人、相同非空正文、无附件、正常状态、平台时间与回执时间相差不超过十秒且双向候选唯一的两行。回执前后十秒必须完整落在本轮 since/query_started 窗口内，避免分批或未来候选未产生时错误归并。保留真实 messageId/平台时间及本地员工身份、reply_to，留下 delivery-echo 来源使发送归档重试不恢复旧回执行。重复正文、不同机器人/群、时间不符、映射缺失或歧义均不合并；不是平台级精确 ID 映射。旧游标引用被合并的回执时可以安全失效，不伪造历史完整性。当前增量窗口之前的旧记录需单独完整回读对应时间窗后修复，不能拿本轮完整状态冒充历史覆盖证明。

仅变更钉钉 provider/runtime/poller 和群归档内的钉钉分支，不改前端、core 或其他平台。回归先复现双记录，再覆盖历史先到/回执先到、重试、重复正文、身份/时间/附件边界。独立审查发现局部分页和不完整候选时间窗会提前绑定，已改为 complete 且无 hasMore 才归并，并要求回执和回读各自前后十秒完整覆盖。所有已知回执先参与反向唯一性计算，不能提前排除尚未满足窗口的回执。该反例测试先失败再通过；阶段专项 237 passed、1 项既存保留条数断言排除，最终扩大回归和真实记录修复见下。

最终验证：钉钉文件/入站、附件、cron、provider/tool、生产接线、poller/runtime、群端口、commit 和投递脱敏共 560 passed、1 deselected；排除项仍是 HEAD 保留5000条与旧测试预期100条不符的既存断言。独立复核未再发现本轮 P1/P2。

真实记录修复前以 SQLite backup 保存目标群记录，文件权限0600。随后用原员工 OAuth 完整回读 01:24:35 UTC 之后的历史，complete=true、hasMore=false、返回1条机器人消息；经原历史入口以禁止派发的执行下限归档，再按验证窗口归并。用户报告的一对 ID 由两行变一行，真实 messageId 保留，Request 总数不变、派发尝试为零；没有重发群消息。

服务已于09:38重启，共享 IM runtime 启动，四个服务端口正常、前端 HTTP 200；轮询 checkpoint 从01:37:06前移到01:38:26 UTC，无本轮群读取错误。使用页面同一 ChannelRecords.messages 读取模型确认该回复仅一条，sender 为系统架构工程师、own=true、时间为09:24:56。当前修改仅后端，复用已有前端构建。

English summary: Confirmed DingTalk delivery receipts and user-history bot echoes are merged only for verified bot identities and unique text/time matches within a completed read covering both candidate windows. Ambiguous, partial, attachment and other-platform records stay separate. The reported duplicate pair was repaired after a complete authorized read and backup, without dispatching or resending messages.

## 24. 用户授权客户端隔离与页面反馈修复（2026-10-31）

实际页面的学习小助手授权失败。最初可见错误是服务重启预热期间的 runtime queue is full；服务就绪后再次点击复现 CLI 未返回设备码。直接核对当前员工 CLI 进程输出确认服务端返回900103“应用不存在”：`_oauth_environment` 复用应用环境，错误地把机器人 AppKey/AppSecret 当作 CLI 用户设备授权客户端覆盖。只移除 OAuth 环境中的 DWS_CLIENT_ID/DWS_CLIENT_SECRET 后，同一隔离员工目录可取得授权链接和设备码；测试进程已终止，没有代用户完成授权。

最小修复：只在用户 OAuth 环境移除机器人客户端覆盖，保留员工隔离目录、已有登录态与机器人 App Token 环境；用户业务命令继续显式选择员工 profile。前端保持上下布局，标题改为“钉钉用户账号授权”，说明群消息读取、在线文档和日历用途；请求期间明确显示获取链接，返回后展示设备码及可点击授权页，后端保留可安全展示的失败类型，不透传原始日志或凭据。中英文文案同步。计划覆盖 OAuth/app 凭据分离、真实前端按钮的加载/链接/错误/轮询保留以及 build；验证尚未执行。

验证：provider/tool/runtime 后端245项通过；新环境隔离/错误脱敏两项单独重跑通过。前端员工配置页29项、API31项、语言资源6项通过，npm run build 成功。完整 test:jiuwenmate-profile 脚本另带的 workspace 测试因 Node 无 import.meta.glob 而加载失败，属于未改动的工作台打包环境问题；员工配置页已单独完整运行通过。另更新了上一轮普通群消息能力变更遗留的过期“钉钉只能@”文案断言。独立审查未发现本轮 P1/P2。真实已授权架构员工在新 OAuth 环境仍 connected，受限群读取成功、返回零条新消息；真实新授权页面验收待补充。

真实页面验证：09:49重启，worker 已 agent_ready，5173/18092/19001/19000 正常，前端 HTTP 200。刷新 Chrome 后进入学习小助手渠道页，账号授权新标题及用途说明可见；点击显示“正在获取授权链接…”，随后显示设备码、“打开钉钉授权页”及取消按钮，CLI 同时打开设备授权标签页。停止在用户确认之前，尚未完成该员工绑定；未共享另一员工凭据。实际页面截图留在临时验证目录，不提交设备码或授权链接。

English summary: Employee user OAuth now uses the CLI user authorization client instead of overriding it with bot AppKey/AppSecret. Bot App Token operations retain their credentials. The account authorization card explains group messages, online documents and calendars, displays loading and the device link, and retains the same operation's link during partial status polls. No user consent was completed automatically.

## 25. 已消费设备码与组织权限失败识别（2026-10-31）

用户在设备页看到 Invalid or expired user code。实际学习小助手授权进程日志表明设备码已授权成功，随后 CLI 因所选组织未授权该账号访问 CLI 数据而退出；pid 对应进程不存在，但没有 exit 文件。宿主仅检查 exit 文件和600秒期限，错误地保持 authorizing 并重用已消费设备码；真实状态查询已复现 authorizing。

最小修复：状态查询结合进程存活及 CLI 安全终态识别，不再返回已退出/过期操作的 URL、设备码或 operation_id；组织拒绝使用安全枚举 org_access_denied，保留失败证据以便重进页面仍能显示原因，重新发起时清理上次失败元数据。前端按枚举显示本地化原因和开放平台 CLI 访问管理入口，停止旧轮询并允许重新发起。复用原状态模型、员工隔离和授权流程，不自动开通组织权限或切换组织。回归覆盖已消费组织拒绝、异常退出、超时及页面清除旧码；计划验证尚未执行。

验证：后端 provider/tool/runtime/poller 298项通过，前端员工配置页30项和API31项通过，npm run build成功。三类终态的回归先复现 authorizing/not_connected 错误，再验证安全错误及旧URL/设备码清除；组织拒绝同时覆盖JSON转义消息。真实子进程验证存活、退出及PID创建时间不符时不判为当前操作；取消/断开只向匹配活进程发信号。独立审查发现终态保留PID后取消可能误杀复用进程，已按相同身份校验修复并补回归，复核无本轮P1/P2。

真实旧操作在修复构建期间被仍运行的旧服务按600秒期限清理，随后状态已是not_connected；未伪造或恢复该授权操作。组织拒绝的根因来自此前实际CLI日志，未代用户开通权限或反复申请新码。修复后的真实新授权及组织权限开通仍需用户执行，不把回归替身当成组织验收。

运行验证：10:04重启后新的worker已agent_ready，四个服务端口正常、前端HTTP200；未发起新的设备授权，以免用户在组织权限处理前再次消费设备码。已明确告知需组织管理员为该账号开放CLI访问，或选择已有权限的组织，待用户完成后继续真实授权验证。

管理入口核验：用户在首页找不到CLI管理，实际入口为“更多 → 基本信息 → CLI设置”，原CLI使用的直达地址为 `https://open-dev.dingtalk.com/fe/old#/developerSettings`，已在真实Chrome打开并核实。该组织DWS CLI总开关已经开启，“其他渠道”全员禁止，“cyc的系统架构工程师”全员可用；此前笼统称组织未开启不够准确，拒绝还涉及Agent渠道和人员范围。未更改任何权限。Swarm管理链接改为该直达地址，中英文名称对齐真实菜单，回归及build另记。

## 26. 登记员工专属CLI渠道后的授权客户端校正（2026-10-31）

用户自行开通后，真实CLI设置页新增“cyc的Agent学习小助手”、全员可用，其他渠道仍禁止。受限设备授权探测确认：同一学习员工应用凭据从此前900103“应用不存在”变为正常返回设备URL/code；探测进程已终止、临时浏览器页已关闭，不能复用该探测码。

这纠正§24的临时判断：AppKey/AppSecret可合法用于组织登记的自建OAuth客户端，并非一律不应注入。§24移除覆盖只证明内置CLI客户端能取得设备码，没有验证其组织Agent渠道许可；不能以该探测结果替代完整授权。当前用户明确开通员工专属渠道，应恢复当前配置应用完整凭据对作为OAuth客户端，继续保持员工user-oauth和AppToken目录隔离，不静默切换到被组织禁止的“其他渠道”。已有登录数据不删除或复制。对应环境隔离测试按实际契约校正，后续需使用新设备码经用户确认完成真实绑定与读取验收。

校正后provider/tool/runtime/poller 298项通过，独立复核未发现P1/P2。CLI设置直达链接的7项前端定向测试通过，npm run build成功；尚未把设备码探测写成用户授权完成。

真实验收：10:18重启后worker已agent_ready。经Swarm学习小助手渠道页发起正式授权，设备页明确显示该员工应用dinglk4vrnlzhfamib7n；用户随后确认“已完成”。后台runtime返回connected、账号常钰聪、组织openjiuwen harness组；员工隔离reader可用，受限读取测试群返回complete=true/hasMore=false、5条消息，机器人在群校验通过。员工adaptive已启用、存在一个active群访问。真实Chrome中的Swarm卡片显示“钉钉用户账号已绑定该员工”、正确账号组织及断开按钮。未代用户发送测试群消息、创建文档或日程；普通新消息自动回复不作为此次只读授权验收的通过项。

## 27. 共享读取时员工进度丢失的回归（2026-10-31）

用户真实消息“大家都自我介绍一下吧”（02:25:52 UTC）仅生成架构师Request，参与判断respond；学习员工没有Request，不能解释为模型ignore。已只读确认两员工enabled/adaptive、同群active且可见起点早于今天、allow_from为空；新实例发现同一账号群两位接收者。真实历史返回两机器人；临时归档加捕获scheduler的真实入站可向两员工accept。现网当时没有recipient/floor日志，尚不能精确还原缺席原因。

独立审查稳定复现共享水位缺陷：员工身份查询临时失败/退避时，floor保留但员工不在recipients；同账号另一员工仍读消息并推进群checkpoint。缺席超过重叠窗口后，即使恢复加入，消息也已在since之前。新授权还会因确认未绑定的指数退避延迟观察起点。不能把该复现直接当作真实现场唯一根因。

已实现：维持同账号同群共享有界读取，额外在当前poller内存保留各recipient已处理的时间边界；暂停接收者不推进其边界，恢复时用当前接收者最早边界回读，仍按原floor和每员工source_ref防止越权/重复执行。确认失去访问、解绑或账号变更时随floor清理，重启继续使用原“不自动执行停机旧消息”的语义；不新增跨层状态字段或核心协议。确认未授权按下一轮重查，暂时查询失败仍保留退避。补充接收者/floor及数量日志，不记录正文/Token。

验证：polling和production_wiring共102项通过，覆盖双员工暂停恢复、下一轮发现新授权、共享水位领先时的分页补读、撤权、重启和失败不前进；真实SQLite/scheduler/commit/outbox配合替代模型与网络，证明同一条“大家都自我介绍一下吧”为两员工生成独立Request、分别通过其robotCode投递，重复轮询不重复执行。provider/tool/runtime/group_chat_port另288项通过，1项既有100条保留上限测试与当前5000条实现不一致而排除，未改该行为。只读独立复核未发现新增阻断问题。首次授权成功到下一次发现之间仍存在原有观察起点窗口；不声称本地集成测试替代真实群重发验收。

运行状态：10:38:55新worker已agent_ready，四个服务端口可连接且前端HTTP200。10:39:00和10:39:04分别记录架构师与学习员工进入同一个群的观察，两者floor均为02:38:55 UTC。已请求用户发送新消息进行真实双员工验收；未回放旧Request、未代用户发群消息。

## 28. 钉钉授权账号与 AI 员工发言身份边界（2026-10-31）

现场证据：10:25:52的自我介绍请求由架构师生成了“我是……常钰聪”的错误回复。已发布AgentCard仍为系统架构工程师，system_prompt的职责也未变；入站来源及群归档均把常钰聪正确标为human，机器人回复标为agent，没有OAuth写入员工名称的证据。该执行仅调用new_context，没有读取OAuth身份工具。当前提示说明授权账号用于文档和日历，却没有明确区分授权账号、发言人和AI员工；模型发生身份混淆，错误自称还会进入后续群背景。

最小修复：ExecutionDriver复用本次已发布snapshot的AgentCard，将ID/name作为数据传给钉钉专用execution guidance。该提示明确OAuth只提供数据权限、消息发送者不等于员工、自我介绍使用已配置员工身份、历史错误自称不得沿用；不改OAuth归属、不硬编码真人姓名、不替换历史内容或清空会话，不修改其他渠道提示。该边界在参与判断及正式执行前一并进入system_prompt_addendum。

回归先红后绿：纯提示与真实scheduler上下文两项测试均先失败，修复后通过；tool、production_wiring、polling与session_scheduler共222项通过。独立只读复核未发现P1/P2。另用两员工当前实际配置模型做隔离调用，背景特意保留“我是……常钰聪”的错误自称，新指引下分别回答为系统架构AI同事和agent学习小助手，均未冒充真人；未提供工具、未写入真实Request、未投递群消息。11:57:50重启后的worker已agent_ready，四个服务端口正常、前端HTTP200；本次没有前端改动，复用既有dist。

真实群的10:56:10重发已分别产生两员工Request，均completed且钉钉投递回执delivered，这发生在本次身份补丁重启前；其中已出现用户纠正后的正确身份，不能把它当作补丁真实验收结果。

## 29. 直接在群文件空间创建在线文档（2026-10-31）

问题与原因：此前的组合流程先在个人目录创建，再调用convFile links/send、最后重命名群入口；发送动作按授权账号unionId产生真人消息，发送快照带源文档内部数字名。群文件入口改名不能消除这条历史消息。

验证设计与取舍：通用Storage CopyDentry受限探测返回HTTP500，未重试；随后完整读取群文件列表确认没有新增副本，不把500推断为不支持或权限问题。Storage空间元信息额外缺少Storage.Space.Read，但原生创建不依赖该权限。CLI drive info从已有群文档解析到群根目录后，doc create --folder/--workspace成功；进一步只传--workspace为可信群spaceId也成功，覆盖不依赖已有文件的创建路径。已创建两篇明确标记验证用途的在线文档，均回读为ALIDOC、正确群workspaceId与正确标题；生产组合函数的端到端验证见下文。

已实现：仅修改LocalDingTalkCliRuntime.create_group_document及其测试和钉钉工具说明，创建时明确--workspace为已核验当前群空间。保留原OAuth和群空间解析，删除个人创建后的links/send、群入口改名等有消息副作用的步骤；读回必须核对nodeId、在线类型、标题和workspaceId，并以queryDentryId确认存储空间，再核验群文件列表出现同一dentry。创建后失败继续保留真实URL并阻止重建，不回退个人消息、不上传DOCX。不改前端、个人版、其他渠道或agent-core。

验证：12项专项回归先红后绿，覆盖正常、第二页、类型/标题/node/workspace错配、存储位置错配、空ID、列表缺失与分页停滞；provider/tool/production_wiring/polling/group_runtime共364项通过。独立只读复核未发现P1/P2。修改后的完整函数实际创建《群文档创建修复验证》（nodeId mweZ92PV6pqKK0lotRpOLDdN8xEKBD6p），返回同一在线文档URL和群内dentryId 241479184224，群文件列表核验通过；从调用前时间读取群消息返回complete=true、hasMore=false、0条，未产生真人文件发送消息。两篇前序原生接口验证文档保留在群文件列表，不删除用户原有文档。

运行验收：常规停服因旧worker IPC不可达留下服务子进程，核对本仓Python模块和PID后定向停止残留并重新启动。14:26:02新worker已agent_ready；14:26:03两员工均恢复同群监听；5173/18092/19001/19000可连接，前端HTTP200。新创建流程已随新worker加载，无前端修改，复用已有dist。

English summary: DingTalk now creates an online document directly in the verified conversation file workspace instead of creating a personal document and sending its link as the authorized user. Creation is reported only after identity, type, title, workspace and group file visibility are verified. The live host workflow confirmed group-file visibility without generating a user-account message; 364 focused tests passed. The restarted runtime and both group observers are ready.


### 30. 授权完成后的页面反馈（2026-10-31）

用户要求删除重复授权步骤，并加快账号和组织信息显示。根因：页面仅每2.5秒检查进行中的授权，返回前台不触发检查，后台定时器还可能被浏览器延迟；正常授权期间始终展开设备链接和设备码，容易被误认为二次确认。只读实测两个现有绑定的后端状态检查约256ms和1193ms，均已包含账号和组织，本次先修复前端等待。

确认范围：仅修改团队版员工授权页面、对应中英文文案和回归测试；不改变OAuth身份、授权凭据或个人版。进行中每秒检查一次，页面重新获得焦点或变为可见时立即检查，同一连接仍只允许一个状态请求在途。将设备码及授权链接收进默认关闭的备用帮助区域，成功、失败或取消后不显示旧码和旧链接。保留手动打开入口以支持自动浏览器打开失败和远程部署。验证计划：返回页面自动显示账号与组织、慢请求不重叠、解绑和失败清理，运行授权页面测试及前端build。已实现：新增回归测试先在旧实现失败，修复后页面测试32项通过，npm run build通过（仅有现有大包提示）；本地HTTP入口与新构建的index及JavaScript一致，无需重启后端。未重新发起真人授权，不改动现有绑定。

English summary: Refresh pending DingTalk authorization immediately when the user returns to the page and every second while waiting, preserving the single in-flight request guard. Keep device-code instructions in a collapsed fallback rather than presenting a second authorization step. All 32 profile tests and the frontend production build passed. The running web server serves the new build. Existing user bindings were preserved; a new live authorization was not performed.


### 31. 授权完成与状态检查交错（2026-10-31）

现场：新增绑定员工的设备授权进程已退出，网页显示authorization_failed；同一员工、同一应用修订目录重新执行CLI状态及profile检查返回connected并含账号与组织。状态实现先查询登录态，再检查进程存活；若登录凭据恰在两步之间写入，旧的未登录结果与新的进程退出事实组合成误判。前端将error视为终态停止检查，使短暂误判留在页面。

最小修复：provider在判断操作结束前有界重读一次真实登录态，仅在CLI状态与唯一profile均验证后返回connected；仍保持真实失败、组织拒绝和过期判断，不凭成功文案推断已绑定。团队版页面对authorization_failed状态在返回前台时重新查询当前绑定，而不是再次发起授权；失败状态不持续高频轮询。修改provider、员工页面、各自回归测试及本规约；不改变凭据存储和OAuth流程，不动个人版。验证计划：交错完成、真正失败仍终止、前台恢复已绑定及现有授权回归，前端build后重启源码服务并检查RPC。完成证据：两种完成交错回归在旧实现均失败，修复后provider/tool/production/polling/group_runtime共366项通过，页面33项通过，前端build通过。源码服务重启后15:43:21 runtime worker agent_ready；通过5173网页WebSocket实际核验生活小助手、学习小助手和系统架构工程师三份授权均为connected，账号、组织均存在且无error_code。网页index和JavaScript与新构建一致；未重新登录或替换既有绑定。

English summary: A stale unauthenticated snapshot could be combined with a newly exited device-login process and incorrectly become a terminal failure. Revalidate persisted login once at that boundary and recheck a failed authorization when returning to the UI, without starting a second login. The two completion-race regressions failed before the fix; 366 backend tests, 33 profile tests and the production build passed afterwards. The restarted source service returned connected states with account and organization through the actual Web RPC for all three DingTalk employees, including the affected employee. No new authorization was initiated.


### 32. 已绑定账号的名称与组织展示（2026-10-31）

现场复现：生活小助手的当前CLI profile仅包含组织级资料，未给userName/userId且corpName退化为corpId，RPC仍为connected。前端将整个身份区域放在auth.account存在的条件下，导致连已返回的组织也被隐藏。使用同一员工OAuth环境执行只读contact +me已实际取得本人姓名和所属组织。

最小方案：保持已登录判定不变，仅在profile缺失账号/组织名称或以ID充当名称时，使用明确当前profile的contact +me补充展示字段；有界超时，拒绝与已知profile userId冲突的资料，只投影name与org，不向页面传播手机、邮箱等字段，不改变内部读群身份与权限。页面connected时始终独立显示两行，字段缺失提示暂不可用。验证：组织有值但账号缺失、当前账号名称补齐、身份冲突及资料查询失败，授权现有回归、前端build、重启后RPC及真实页面。验证完成：缺资料及ID退化的两个回归先在旧实现失败；授权综合回归370项通过，补充无效响应和超时用例后provider139项重新通过，前端页面34项通过，npm run build通过。源码服务重启后16:02:19 worker agent_ready；通过实际页面进入生活小助手渠道卡片，两个独立DOM节点已可见且显示真实账号名和真实组织名称。没有重新授权，也未改变群访问身份。

English summary: Organization-only CLI profiles can omit the account name, which previously hid the whole identity panel. Resolve missing display names through a bounded, scoped, read-only current-user lookup and render the account and organization independently. Login status and access decisions remain unchanged. The missing-identity regressions failed before the fix. The combined backend suite passed 370 cases; the provider suite passed 139 cases after adding malformed-response and timeout coverage. All 34 profile tests and the production build passed. After restarting the source service, the actual employee channel page visibly displayed both the real account name and organization, without reauthorization.

## 33. 可信请求的应用选择（2026-10-30）

DingTalkCliScope 新增可选 app_id，由宿主从已核验钉钉请求的 route snapshot 注入，不接受模型选择。存在明确 app_id 时按该应用唯一解析，禁用、缺失、重复或已绑定另一 Agent 均拒绝，不能回落到默认应用；群和发送者仍来自可信钉钉事件。

没有钉钉路由时，保留既有团队模式的默认应用／唯一应用／员工绑定选择顺序。Web 或飞书请求不会被伪造钉钉群和发送者身份，依赖当前群／发送者的组合操作仍受原限制。飞书 CLI 资料与 DWS App Token 分开管理，跨渠道归档搜索不依赖 CLI 互用。

新增回归：`test_dingtalk_cli_app_scope.py`；`test_dingtalk_cli_tool.py` 验证实际传入的应用 Scope。本轮没有启动登录、授权或真实平台写入。

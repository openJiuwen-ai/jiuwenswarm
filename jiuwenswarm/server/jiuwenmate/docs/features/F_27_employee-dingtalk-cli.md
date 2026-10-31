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


## 13. 实施修订（2026-10-29；已由第 14 节替代）

钉钉错误文本中的 `Chat.OrgInnerGroup.Read` 不能作为可在开发者后台搜索的权限名称，原先将其当作缺失权限提示是不准确的。群成员读取现改用官方 DWS `chat group members` 命令和服务级私有共享用户 profile；App Token 仍用于成员 `unionId` 映射及日历写入。共享登录未初始化、成员读取失败或名单不完整时均停止创建，不报告推测的权限名称。此项只需部署者初始化一次共享 DWS 登录，不增加员工登录页面。

验证：`test_dingtalk_cli_local_provider.py` 覆盖共享 profile、分页名单、未登录提示及失败时不创建日程；真实钉钉群成员读取与日历写入尚未联调。

## 14. 实施修订（2026-10-29；已由第 15 节替代）

移除群日程对服务级 DWS 用户 OAuth 的依赖，恢复由当前钉钉应用 App Token 调用 `POST /v1.0/im/innerGroups/members/query`。请求按官方生成 SDK 契约传递可信 `openConversationId`、发送者 `userId`、`maxResults` 和整数 `nextToken`，响应必须包含成员列表、`hasMore` 和整数分页游标；只有完整读取并核验发送者在群内后才继续映射参与人和创建日程。接口失败时保留去敏后的钉钉错误码，不再提示登录或猜权限名称。若该接口返回无法在开发者平台核实的 `Chat.OrgInnerGroup.Read`，只报告接口、错误码和“未创建日程”，不得让用户搜索或申请该权限。每条钉钉请求还会注入高优先级操作边界，明确 App Token 请求不涉及 DWS 登录；错误后禁止推断登录状态或提出登录、OAuth、设备码及权限申请方案。若用户希望继续，仅可询问其是否提供参会人 staffId 名单。

验证：`test_dingtalk_cli_local_provider.py` 的 App Token 分页、上游失败和畸形名单路径，以及 `test_dingtalk_cli_tool.py` 的工具说明回归通过。真实钉钉群成员读取及日历写入仍待当前服务联调；本次不执行授权登录或真实写入。

## 15. 实施修订（2026-10-29）

针对真实群成员接口持续返回 `DINGTALK_CLI_API_FORBIDDEN` 的情况，保留 App Token 查询作为首选；当钉钉明确拒绝该端点时，自动切换到官方 DWS `chat group members` 命令，复用服务主机上已经存在的共享 DWS 用户登录态。回退仅用于查询可信当前群的完整成员名单，后续身份映射及日历写入仍由应用 App Token 执行。代码不会启动登录、OAuth 或设备码流程；共享登录态不可用、查询失败、分页不完整或名单不含当前发送者时，均停止且不创建日程。更新了员工工具说明与设计规范，避免继续把 App Token 403 当作唯一可用的成员查询方式。

验证：`test_dingtalk_cli_local_provider.py` 59 项通过，覆盖 App Token 拒绝后的共享 DWS 回退及无可用会话时失败关闭；工具说明回归、`git diff --check` 与服务重启状态待本轮核实。真实群成员读取和日历写入尚未联调，不代表线上已创建日程。

## 15. 实施修订（2026-10-29）

针对真实群成员接口持续返回 `DINGTALK_CLI_API_FORBIDDEN` 的情况，保留 App Token 查询作为首选；当钉钉明确拒绝该端点时，自动切换到官方 DWS `chat group members` 命令，复用服务主机上已经存在的共享 DWS 用户登录态。回退仅用于查询可信当前群的完整成员名单，后续身份映射及日历写入仍由应用 App Token 执行。代码不会启动登录、OAuth 或设备码流程；共享登录态不可用、查询失败、分页不完整或名单不含当前发送者时，均停止且不创建日程。更新了员工工具说明与设计规范，避免继续把 App Token 403 当作唯一可用的成员查询方式。

验证：`test_dingtalk_cli_local_provider.py` 59 项通过，覆盖 App Token 拒绝后的共享 DWS 回退及无可用会话时失败关闭；工具说明回归、完整测试结果及服务重启状态待本轮核实。真实群成员读取和日历写入尚未联调，不代表线上已创建日程。


## 16. 可信请求的应用选择（2026-10-30）

DingTalkCliScope 新增可选 app_id，由宿主从已核验钉钉请求的 route snapshot 注入，不接受模型选择。存在明确 app_id 时按该应用唯一解析，禁用、缺失、重复或已绑定另一 Agent 均拒绝，不能回落到默认应用；群和发送者仍来自可信钉钉事件。

没有钉钉路由时，保留既有团队模式的默认应用／唯一应用／员工绑定选择顺序。Web 或飞书请求不会被伪造钉钉群和发送者身份，依赖当前群／发送者的组合操作仍受原限制。飞书 CLI 资料与 DWS App Token 分开管理，跨渠道归档搜索不依赖 CLI 互用。

新增回归：`test_dingtalk_cli_app_scope.py`；`test_dingtalk_cli_tool.py` 验证实际传入的应用 Scope。本轮没有启动登录、授权或真实平台写入。

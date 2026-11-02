# 钉钉 DWS CLI 应用身份接入规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_06 / employee-dingtalk-cli |
| 关联模块 | `dingtalk_cli_runtime.py`、`dingtalk_cli_local_provider.py`、`dingtalk_cli_tool.py`、`agent_manager.py`、`execution_driver.py` 与钉钉渠道绑定 |
| 最近一次修订日期 | 2026-10-30 |
| 关联 feature | [F_27 钉钉 DWS CLI 应用身份接入](../features/F_27_employee-dingtalk-cli.md) |
| 设计依据 | DWS CLI `dws api` 的 App Token 模式及 DingTalk InnerGroup 成员查询 SDK 契约 |

状态：默认 App Token 工具、员工连接级 DWS 用户授权、在线群文档创建、DOCX 更新、群日历创建和日程标题更新已实现。在线文档搜索、读取及追加/文本替换/覆盖已通过临时文档真实联调。用户命令只使用对应员工的独立 OAuth profile，不读取主机共享用户登录态。验证范围与已知限制见 F_27。

## 1. 身份与凭证

- 钉钉发言中的 AI 员工身份由本次 Request 的已发布配置确定。宿主在执行提示中提供该版本 AgentCard 的 ID 与名称，明确 OAuth 账号仅提供数据访问权限、消息发送者不等于员工；自我介绍沿用员工配置，不冒充授权账号本人或其他真人，也不沿用历史错误自称。文档创建者与日历发起人的平台账号归属仍保持真实 API 结果，不把账号归属变成员工人格。此提示只适用于钉钉路由。

- 运行上下文由 `(namespace_id, agent_id)` 确定；优先使用 JiuwenSwarm 当前 `channels.dingtalk.apps` 中唯一启用的默认应用，所有员工共用该应用。无默认应用时，只使用唯一绑定到当前员工的应用；应用配置只能从服务端读取，不接受工具参数指定应用 Secret 或 Token。
- 默认应用无效时，或当前员工没有唯一有效绑定、App ID 重复、凭证不完整时，不提供 CLI 工具，也不注入 DWS 环境。
- CLI 子进程通过 `DWS_CLIENT_ID`、`DWS_CLIENT_SECRET` 使用 `dws api` 应用 Token 模式；应用 Secret 不得写入 argv、日志或 LLM 输入。CLI App Token 缓存放入由员工与应用凭证版本共同确定的私有 `DWS_CONFIG_DIR`。
- 解绑、应用 Secret 轮换及员工删除须使旧运行时缓存失效。员工删除清理该员工整个 DWS CLI 私有目录，但不删除共享 CLI 二进制。

## 2. 能力和边界

- DWS CLI 以固定版本通过 `npx --package` 按需运行；应用 Token 能力无需员工单独登录，用户能力需要员工连接授权。
- 所有员工在有效默认应用存在时获得自动注入的 `dingtalk_cli` 工具；没有默认应用时，只有绑定了唯一有效钉钉应用的员工获得工具。员工无需单独登录，命令 argv 不经过 shell 拼接。
- 无员工 OAuth 的默认能力仅是 DWS CLI `api` 子命令所提供的应用级 Raw OpenAPI，且受应用已开通权限和 CLI 官方域名校验限制。
- 团队版员工详情页提供按钉钉连接隔离的 DWS 用户 OAuth 设备码授权；授权 HOME、配置与缓存按 `(namespace_id, connection agent_id, app credential revision)` 隔离。前端只接收状态、授权 URL、用户码和脱敏账号/组织信息。员工未完成授权时，在线群文档操作失败关闭。个人版不增加授权入口。此授权生命周期不开放给模型工具。
- 全群会议优先使用当前员工已授权且按连接隔离的 DWS 用户 profile，通过官方 `chat group members` 完整分页读取当前群名单。未授权时保留原 App Token 路径；不读取主机共享 DWS 登录态，不自动启动授权。
- 钉钉群执行中的工具上下文只从已保存的 `RequestRouteSnapshot` 读取当前群目标；机器人回调从 `group_message` 来源读取 `senderStaffId`，用户历史摄入则保留 `dingtalk-open:` 开放身份，不当作 staffId。私聊和非钉钉请求不得带入群上下文。`senderStaffId` 不等同于经验证的 `unionId` / `operatorId`。
- `create_calendar_event` 仅允许在可信钉钉 `group_message` 或 `direct_message` 中调用；创建者来自消息 source 的发送者 ID，后端通过通讯录 API 映射 unionId，日历固定为 `primary`。标题、带时区偏移的开始和结束时间、IANA 时区为必填；结束不晚于开始、时区格式错误或与时间不匹配时不得调用外部 API。描述、提醒、参与者及钉钉视频会议链接为可选项。
- 应用 Token 日历路径需 `Calendar.Event.Write`，映射发送者或参与人需 `qyapi_get_member`。已授权员工的全群会议走 DWS 用户日历能力，不依赖应用将开放 ID 转换为企业员工 ID。所有路径都禁止在创建结果不确定时自动重放。
- `create_calendar_event` 可选接受 `include_group_members=true`，且仅限可信钉钉群消息。群 ID 与消息来源由钉钉平台接入及已保存路由提供，模型不能指定其他群。员工已授权时，以该账号在主日历创建一个会议；完整名单按 `userId` 与 `openDingTalkId` 分开去重并分别传入 CLI，支持跨组织成员，不将开放 ID 当企业 staffId。用户 CLI 单次上限为 500 人，超过上限或分页未完成时不得创建。只有钉钉确认成功并返回 eventId 后才报告创建；超时或无事件 ID 时禁止自动重放。无员工授权时，应用路径仍核验群名单中的 senderStaffId 并映射 unionId，保留原有大群分批添加能力。
- 每条钉钉请求的系统提示需说明全群会议优先使用员工独立 OAuth profile，未授权时仅尝试应用路径。不得用共享主机账号代替员工身份，不得猜测不存在的权限或自行启动 OAuth。完整名单未取得或 `stop_retrying=true` 时停止本轮相关操作，不以部分名单创建会议。
- 创建群文档要求当前员工连接已完成用户 OAuth。群 ID 来自可信路由；身份映射使用已授权账号的 userId，而非任意消息发送者。查询可信当前群的文件空间后，通过员工隔离的用户 CLI `doc create --name <title> --workspace <group-space-id>` 直接创建真实 ALIDOC，不依赖已有群文件或根目录UUID。`drive info` 必须核对同一nodeId、标题、ALIDOC类型及workspaceId；`/v2.0/doc/dentries/{uuid}/queryDentryId` 必须确认dentryUuid和群spaceId均匹配，随后在当前群根目录文件列表核验同一dentryId才报告成功。创建流程不得调用convFile links/send或以授权账号补发文件消息，不再生成个人文档后转发，不上传普通DOCX冒充在线文档；正常结果仍由员工机器人回复。创建后任何后续失败均保留在线文档链接并禁止重新创建同名文档，不回退到有消息副作用的旧流程。
- `update_calendar_event` 仅允许来自可信钉钉消息，接受明确的原 `event_id` 和新 `title`，当前只修改标题。必须复用当前员工的用户 OAuth profile，先读取匹配日程，再以 CLI `calendar event update --id ... --title ...` 提交一次并读回；核验 eventId、标题及时间、参与人、提醒等原信息。已是目标标题时返回 `changed=false` 而不重复提交。未授权、读回失败、身份不匹配或修改结果不确定时停止，不重建或删除日程。Raw API 的 POST/PUT/PATCH/DELETE 日历写操作必须经此类宿主组合操作，不能用 callback staffId 直接拼 unionId 路径。
- 编辑群内 DOCX 必须提供先前创建结果中的 `dentryId` 和完整正文。后端通过可信当前群 ID 解析群存储空间，在该空间查询并校验目标 dentry；只允许更新同群普通 DOCX 文件。通过原 `parentId`、原文件名重新上传，并以 `conflictStrategy=OVERWRITE` 提交新版本；提交前复查原 dentry 版本，发现变化则停止写入。在线 DOC 和其他文件类型明确拒绝。更新失败不得重试或报告成功。
- 上传签名 URL 和请求头仅在后端组合流程内处理，不进入模型输入或输出。只允许 HTTPS 钉钉 API 与钉钉返回的受限 OSS 域名；不跟随上传重定向。
- 建群文档需要通讯录用户详情读取、群文件空间读取、企业存储文件上传信息读取和企业存储文件写入权限。读取预览链接和核验更新目标另需企业存储文件读取权限；预览读取失败不改变已提交文件的创建结果。不把 `Document.Workspace.Read` 当作群文件前置权限。
- 本规约的 DOCX 更新通过存储上传提交接口替换同一文件的完整二进制内容，保留同群 dentry 并生成新版本；不支持追加或局部合并。`dws doc` 的常规文档块编辑仍属于用户 OAuth 能力。本规约不宣称覆盖所有 DWS 文档命令。
- `search_online_documents(query, cursor?, limit=10)` 使用当前员工 OAuth 和 `doc +search --extensions adoc`；limit 为 1–30。返回候选 `nodeId/name/url/docType` 和 `complete/hasMore/nextCursor`；多候选不自动选取，未完整搜索时不得把空结果称为不存在。续页游标停滞或响应缺少集合/分页字段时停止。
- `read_online_document(node_id)` 接受真实在线对象 ID 或 `https://alidocs.dingtalk.com/i/nodes/<ID>`，不接受本地文件或其他域名。使用 `doc +fetch`，核对 target 和正文 nodeId 一致、正文读取成功且完整后返回标题与 Markdown。普通群文件入口的在线节点链接也可读取。
- `edit_online_document(node_id, edit_mode, content?, old_text?, new_text?)` 使用同一员工 profile 和 `doc +update`。`append/overwrite` 仅接受 content（最多 100,000 字符），正文从 stdin 传入，不将 `@文件` 字符串解释成宿主文件。`replace_text` 仅接受非空 old_text 和字符串 new_text（允许空值删除），两者分别最多 4,000 字符，避免跨平台命令行长度溢出。只有 `status=success`、`ok=true`、`complete=true`、匹配的 `data.nodeId`，以及 `data.verified` 和 `data.verification.verified` 均为 true 才报告更新成功。部分或不确定结果停止写入，不自动重放。
- 上述在线文档操作可在员工 Web 对话或钉钉会话中调用，以员工授权账号的可访问范围为准；不要求模型提供 staffId，不开放 profile、Token 或任意 CLI 命令。DOCX 的 `edit_group_document` 与在线编辑是不同操作；模型应先搜索/读取真实对象，明确追加可直接追加，只有用户要求整篇替换时才覆盖。

## 3. 生命周期

- 宿主专用 `group_history_identity(scope)` 只返回当前员工 OAuth profile 的 SHA256 摘要或未授权空值；授权查询异常不能误报未授权。`read_group_history(scope, conversation_id, since)` 最多三页/150 条，要求带时区的时间边界，返回 readerKey、messages、complete、hasMore、nextCursor、botCodes、botSenders，不开放为模型工具。botSenders 仅包含群机器人列表中无歧义的 robotCode → botOpenDingTalkId 映射；缺失或歧义身份不得推断，不能以 openBotId 替代。
- 读取消息前使用同一员工 profile 查询 `chat group bots`；以配置中精确员工绑定的 robot_code（缺省 client_id）匹配返回的 robotCode，不能使用默认应用或 openBotId 冒充。机器人缺席时不读取历史，查询失败或结构异常不得使用旧名单继续。轮询使用 botCodes 筛选同账号接收员工，确认缺席撤销缓存成员权。
- 共享 IM runtime 的群读取约十秒间隔，全局每轮最多八次读取尝试；按 namespace、readerKey、群合并，只有原有 active、adaptive、启用且精确连接有效的员工可参与。进度在本地私有目录原子保存，完整批次保留三十秒重叠，部分或未完成的处理不跳过进度。普通新文字的保守过滤与生命周期见 [IM 规约](S_03_im-direct-message-and-delivery.md#8-钉钉普通群文字的保守摄入)。
- 共享读取仍分别保留当前进程内每位员工的处理边界，取已重新核验接收者的最早边界读取；临时身份查询失败保留该员工边界，其他员工推进共享检查点不能消耗其待处理消息。部分批次按已确认分页边界前进；确认解绑、撤权或更换账号时清除对应边界，重启不回放停机旧消息。确认尚未授权时下一轮重新检查，查询异常保留退避；诊断日志仅记录员工、群、时间边界和数量。

- 每次执行时重新读取默认钉钉应用；优先复用运行时目录内与固定版本匹配的 CLI 二进制，缺失时由 `npx` 按需获取固定版本。
- npm 包缓存位于 JiuwenMate 私有数据目录；应用 Token 缓存及配置按员工和凭证版本隔离。无有效绑定时不构建 CLI 工具，也不向进程环境传入 `DWS_CLIENT_ID` / `DWS_CLIENT_SECRET`。
- 用户 OAuth 命令使用独立员工 user-oauth 目录及当前配置应用的完整客户端凭据对；该应用须在组织CLI设置登记并获准使用。AppKey/AppSecret可以同时作为原App Token和自建OAuth客户端的身份，二者Token目录与用户profile仍隔离；不能因应用未登记而静默改走组织禁止的内置客户端。设备授权失败只返回安全归类后的错误，不把原始 CLI 输出或凭据送到页面。前端称“钉钉用户账号授权”，覆盖群消息、文档和日历用途；同一进行中 operation 的轮询若暂缺链接/设备码，保留本次已返回的值，结束或变更 operation 时不沿用。
- 团队版授权等待页每秒检查进行中的操作，页面重新获得焦点或变为可见时立即检查；同一连接的状态请求不得重叠。设备码和授权链接默认收进备用帮助区域，不作为二次授权步骤；成功后同时显示后端返回的账号与组织并移除帮助区域。
- 已绑定状态独立展示账号名和所属组织；缺少单个字段不能隐藏另一个字段，未取得名称时提示暂不可用。CLI profile未提供名称或使用ID代替名称时，可在相同员工OAuth环境及显式当前profile下只读查询contact +me（5秒HTTP超时），只投影name/org；与已知profile userId冲突时丢弃补充结果，查询失败不撤销已验证的登录态，不向前端返回手机号、邮箱，也不据此改变群访问权限。
- CLI登录态查询与设备进程退出可能交错。在将操作结束判为authorization_failed前，有界重读一次登录态，仅真实登录状态和唯一profile均有效时返回connected；不得根据日志中的成功文案或退出码直接宣称已绑定。前端对authorization_failed在返回前台时重新查询状态，不自动重新登录、不保留旧设备码，也不为失败状态持续启动高频轮询。
- 用户确认设备码后仍可能因组织权限失败。操作进程已退出、超时或 CLI 明确失败时返回 error，不再返回该 operation 的设备码、链接或 operation_id；只允许 org_access_denied、authorization_expired、authorization_failed 三种安全 error_code。失败元数据保留到下次发起或取消，以便刷新后仍显示原因；取消、断开及超时终止只向匹配存活且创建时间与该 pid 文件一致的进程发送信号，旧 PID 不匹配时只清元数据。前端停止失败操作轮询，提示组织管理员的 CLI 访问管理入口，不自动开通权限或选择其他组织。
- 安装、准备或调用失败时，普通 JiuwenMate 对话仍可继续；DWS 能力不注入或不可用，错误日志与返回信息不得包含 Secret、Token 或原始敏感 CLI 输出。
- `npx` 使用按 CLI 版本隔离的共享 npm 缓存，并在该缓存上使用跨进程互斥，最多等待 5 秒；锁内遇到 npm 包目录 `ENOTEMPTY` 时清理本版本缓存中的损坏 CLI 包目录并重试一次，仍失败时返回 `DINGTALK_CLI_CACHE_BUSY`，不得传递原始 npm 输出。持锁后重新检查固定版本二进制，避免并发安装完成后仍调用旧的 npx 路径。若存在相同固定版本的已缓存 CLI 二进制，则直接执行。单次 CLI API 子进程最多运行 30 秒；超时返回 `DINGTALK_CLI_TIMEOUT`；识别到 HTTP 403 时返回 `DINGTALK_CLI_API_FORBIDDEN`，其他可识别的上游错误返回 `DINGTALK_CLI_API_ERROR` 及去敏错误码/状态。仅当错误码为 `Forbidden.AccessDenied.AccessTokenPermissionDenied` 时，可从 CLI 错误中提取格式受限的缺失权限标识供模型说明；不得传递原始 CLI 输出、链接、Secret 或 Token。群成员查询只允许通过可信群日历操作；原始 `operation=api` 中的群成员路径会被拦截且不调用 DWS。通用执行错误不得推断为缺少权限或接口不支持。同一工具执行首次遇到 `DINGTALK_CLI_CACHE_BUSY`、`DINGTALK_CLI_API_FORBIDDEN`、`DINGTALK_CLI_API_ERROR`、`DINGTALK_CLI_EXECUTION_ERROR` 或 `DINGTALK_CLI_TIMEOUT` 后返回 `stop_retrying: true`，该状态在同次执行重建工具实例时保持，阻止重复调用外部 API；模型应直接说明错误，不改用 Bash 或其他工具探测网络。新的执行独立重试。

## 4. 页面与 RPC

- 团队版员工详情页在每个钉钉连接中提供独立授权状态、设备码授权、轮询、取消和断开。RPC 请求包含员工配置 ID 与连接键；服务端必须确认该连接仍绑定到该员工，再以连接对应的 Agent ID 创建运行 scope。
- 钉钉渠道绑定是唯一应用凭证来源；配置变更只触发服务端运行时准备，不暴露 Secret 给员工配置 UI 以外的调用者。用户 OAuth profile 与 App Token 缓存分目录存储。

## 5. 验收不变量

1. 默认钉钉应用有效时，所有员工无需 OAuth 即可自动获得并使用 `dws api` 工具；需要在线群文档能力时，该员工必须完成自己连接对应的用户 OAuth；无默认应用时，只有精确绑定的员工可用。
2. 默认应用无效，或当前员工没有唯一有效绑定时，员工无法获得 DWS 应用凭证或 App Token 缓存。
3. 员工 A 的运行目录与员工 B 不同；旋转凭证后 A 会切换到新的 App Token 缓存目录，删除员工会清理其所有旧目录。
4. CLI 调用不可指定或覆盖应用凭证、Token、员工 scope 或非官方 API 主机；仅允许 `api.dingtalk.com` 和 `oapi.dingtalk.com` 的 HTTPS/443 地址。
5. 所有 DWS 子进程输出和异常处理不得把应用 Secret 或 Token 写入日志、用户可见结果或模型上下文。
6. DWS CLI 缺依赖、安装失败或 API 权限不足不阻断普通对话。
7. 钉钉群调用的工具说明使用路由快照提供当前群 ID，不使用模型从聊天文本推断的群 ID；普通私聊不会收到群信息。
8. 在线群 DOC 创建仅使用可信当前群和发送者身份，且要求员工连接 OAuth 已授权；签名上传信息不得泄露。只有提交成功、类型为在线文档且同一 dentry 出现在当前群文件列表时才报告成功。在线类型、目标群或群文件入口核验缺失时失败关闭且不得静默降级。
9. App Token 群文档接口的 403 应指出实际失败接口和对应用途，不把知识库工作空间权限当成群文档权限。
10. DOCX 更新只覆盖经当前群空间核验的目标 dentry，保留原文件名和父目录；并发版本变化或目标不属于该群时，提交前失败关闭。
11. 日历写入只以可信钉钉消息发送者为创建者；必填字段和本地时间校验通过后才调用 API；缺少日历或通讯录权限时错误能指出对应权限和 API，结果不确定时停止重试。
12. 群共享日程只以可信当前群成员名单为邀请对象；应用接口拒绝时仅复用已有 DWS 用户 profile 查询群成员，不自动授权；名单分页或 unionId 映射不完整时不创建；一个群请求只创建一个事件，参与人分批添加失败时不得重复创建。

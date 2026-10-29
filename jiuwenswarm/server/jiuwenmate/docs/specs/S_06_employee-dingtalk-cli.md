# 钉钉 DWS CLI 应用身份接入规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_06 / employee-dingtalk-cli |
| 关联模块 | `dingtalk_cli_runtime.py`、`dingtalk_cli_local_provider.py`、`dingtalk_cli_tool.py`、`agent_manager.py`、`execution_driver.py` 与钉钉渠道绑定 |
| 最近一次修订日期 | 2026-10-29 |
| 关联 feature | [F_27 钉钉 DWS CLI 应用身份接入](../features/F_27_employee-dingtalk-cli.md) |
| 设计依据 | DWS CLI `dws api` 的 App Token 模式及 DingTalk InnerGroup 成员查询 SDK 契约 |

状态：默认 App Token 工具、当前群文件操作及群日历创建已实现；群成员读取在 App Token 被拒绝时回退到服务主机已有的共享 DWS 用户登录态。真实钉钉联调待验收，验证记录见 F_27。

## 1. 身份与凭证

- 运行上下文由 `(namespace_id, agent_id)` 确定；优先使用 JiuwenSwarm 当前 `channels.dingtalk.apps` 中唯一启用的默认应用，所有员工共用该应用。无默认应用时，只使用唯一绑定到当前员工的应用；应用配置只能从服务端读取，不接受工具参数指定应用 Secret 或 Token。
- 默认应用无效时，或当前员工没有唯一有效绑定、App ID 重复、凭证不完整时，不提供 CLI 工具，也不注入 DWS 环境。
- CLI 子进程通过 `DWS_CLIENT_ID`、`DWS_CLIENT_SECRET` 使用 `dws api` 应用 Token 模式；应用 Secret 不得写入 argv、日志或 LLM 输入。CLI App Token 缓存放入由员工与应用凭证版本共同确定的私有 `DWS_CONFIG_DIR`。
- 解绑、应用 Secret 轮换及员工删除须使旧运行时缓存失效。员工删除清理该员工整个 DWS CLI 私有目录，但不删除共享 CLI 二进制。

## 2. 能力和边界

- DWS CLI 以固定版本通过 `npx --package` 按需运行；员工管理页面无需单独启用、登录、授权或断开。
- 所有员工在有效默认应用存在时获得自动注入的 `dingtalk_cli` 工具；没有默认应用时，只有绑定了唯一有效钉钉应用的员工获得工具。员工无需单独登录，命令 argv 不经过 shell 拼接。
- 无员工 OAuth 的默认能力仅是 DWS CLI `api` 子命令所提供的应用级 Raw OpenAPI，且受应用已开通权限和 CLI 官方域名校验限制。
- 不提供 `dws auth login`、用户 OAuth 或管理页授权入口。群成员查询先使用当前应用 App Token；若被拒绝，只复用服务主机上已有的共享 DWS 用户 profile 执行官方 `dws chat group members` 命令，不触发登录。其他常规 DWS 产品命令不属于本特性。
- 钉钉群执行中的工具上下文只从已保存的 `RequestRouteSnapshot` 读取当前群目标，并从当前 `group_message` 的来源读取 `senderStaffId`；私聊和非钉钉请求不得带入群上下文。`senderStaffId` 不等同于经验证的 `unionId` / `operatorId`。
- `create_calendar_event` 仅允许在可信钉钉 `group_message` 或 `direct_message` 中调用；创建者来自消息 source 的发送者 ID，后端通过通讯录 API 映射 unionId，日历固定为 `primary`。标题、带时区偏移的开始和结束时间、IANA 时区为必填；结束不晚于开始、时区格式错误或与时间不匹配时不得调用外部 API。描述、提醒、参与者及钉钉视频会议链接为可选项。
- 创建日历日程需 `Calendar.Event.Write`（日历应用中日程写权限）；映射当前发送者或参与人需 `qyapi_get_member`。使用应用 Token，不走员工 OAuth。缺少权限时返回对应失败 endpoint 与权限说明；无事件 ID 或调用状态不确定时返回 `stop_retrying=true`，不得自动重放或声称已创建。
- `create_calendar_event` 可选接受 `include_group_members=true`，且仅限可信钉钉群消息。群 ID 与发送者 ID 来自已核验路由快照和消息来源；先使用 App Token 调用 `POST /v1.0/im/innerGroups/members/query`，请求传递 `openConversationId`、发送者 `userId`、`maxResults` 和整数 `nextToken`，响应解析 `list[].userId`、`hasMore` 与整数 `nextToken`，直到完整读取名单。若该接口被拒绝，回退到服务主机已有的共享 DWS profile，通过官方 `dws chat group members` 命令查询并完整分页；不会发起登录。上游 API 错误只在回退也失败后返回；不得猜测权限名称或提示用户授权。名单不完整或参与人 unionId 映射失败时不得创建。后续参与人分批添加失败时保留 eventId、报告已确认数量并停止，禁止重建。私聊不可使用此选项。
- 每条钉钉请求的系统提示需说明群成员查询使用“应用接口，拒绝后复用已有共享 DWS 登录态”的回退策略；不得声称登录成功或自动发起授权。两种群成员读取路径都失败或 `stop_retrying=true` 时，只报告名单未完整取得及日程未创建；不得建议登录、OAuth、设备码或申请权限，不得以部分名单创建全群日程。宿主在候选最终消息提交前以代码覆盖为确定失败回复，并抑制该失败之后的流式回复；继续操作时只可询问用户是否提供参会人 staffId 名单。
- 创建群文档使用当前群文件空间，不调用知识库工作空间 API。通过当前消息发送者的 `senderStaffId` 查询 `unionId`，并用 App Token 查询群文件空间、上传 DOCX。只有群空间响应提供根目录 dentry UUID 时才用新版 API 请求 `convertToOnlineDoc`，并指定 `convertToOnlineDocTargetDocumentType=DOC`；否则创建群文件区 DOCX 文件。返回值以 `documentType` 区分在线 DOC 与 DOCX 文件；提交成功后另行请求预览链接，失败时保留创建成功并设置 `linkUnavailable`，防止重复创建。
- 编辑群内 DOCX 必须提供先前创建结果中的 `dentryId` 和完整正文。后端通过可信当前群 ID 解析群存储空间，在该空间查询并校验目标 dentry；只允许更新同群普通 DOCX 文件。通过原 `parentId`、原文件名重新上传，并以 `conflictStrategy=OVERWRITE` 提交新版本；提交前复查原 dentry 版本，发现变化则停止写入。在线 DOC 和其他文件类型明确拒绝。更新失败不得重试或报告成功。
- 上传签名 URL 和请求头仅在后端组合流程内处理，不进入模型输入或输出。只允许 HTTPS 钉钉 API 与钉钉返回的受限 OSS 域名；不跟随上传重定向。
- 建群文档需要通讯录用户详情读取、群文件空间读取、企业存储文件上传信息读取和企业存储文件写入权限。读取预览链接和核验更新目标另需企业存储文件读取权限；预览读取失败不改变已提交文件的创建结果。不把 `Document.Workspace.Read` 当作群文件前置权限。
- 本规约的 DOCX 更新通过存储上传提交接口替换同一文件的完整二进制内容，保留同群 dentry 并生成新版本；不支持追加或局部合并。`dws doc` 的常规文档块编辑仍属于用户 OAuth 能力。本规约不宣称覆盖所有 DWS 文档命令。

## 3. 生命周期

- 每次执行时重新读取默认钉钉应用；优先复用运行时目录内与固定版本匹配的 CLI 二进制，缺失时由 `npx` 按需获取固定版本。
- npm 包缓存位于 JiuwenMate 私有数据目录；应用 Token 缓存及配置按员工和凭证版本隔离。无有效绑定时不构建 CLI 工具，也不向进程环境传入 `DWS_CLIENT_ID` / `DWS_CLIENT_SECRET`。
- 安装、准备或调用失败时，普通 JiuwenMate 对话仍可继续；DWS 能力不注入或不可用，错误日志与返回信息不得包含 Secret、Token 或原始敏感 CLI 输出。
- `npx` 使用按 CLI 版本隔离的共享 npm 缓存，并在该缓存上使用跨进程互斥，最多等待 5 秒；锁内遇到 npm 包目录 `ENOTEMPTY` 时清理本版本缓存中的损坏 CLI 包目录并重试一次，仍失败时返回 `DINGTALK_CLI_CACHE_BUSY`，不得传递原始 npm 输出。持锁后重新检查固定版本二进制，避免并发安装完成后仍调用旧的 npx 路径。若存在相同固定版本的已缓存 CLI 二进制，则直接执行。单次 CLI API 子进程最多运行 30 秒；超时返回 `DINGTALK_CLI_TIMEOUT`；识别到 HTTP 403 时返回 `DINGTALK_CLI_API_FORBIDDEN`，其他可识别的上游错误返回 `DINGTALK_CLI_API_ERROR` 及去敏错误码/状态。仅当错误码为 `Forbidden.AccessDenied.AccessTokenPermissionDenied` 时，可从 CLI 错误中提取格式受限的缺失权限标识供模型说明；不得传递原始 CLI 输出、链接、Secret 或 Token。群成员查询只允许通过可信群日历操作；原始 `operation=api` 中的群成员路径会被拦截且不调用 DWS。通用执行错误不得推断为缺少权限或接口不支持。同一工具执行首次遇到 `DINGTALK_CLI_CACHE_BUSY`、`DINGTALK_CLI_API_FORBIDDEN`、`DINGTALK_CLI_API_ERROR`、`DINGTALK_CLI_EXECUTION_ERROR` 或 `DINGTALK_CLI_TIMEOUT` 后返回 `stop_retrying: true`，该状态在同次执行重建工具实例时保持，阻止重复调用外部 API；模型应直接说明错误，不改用 Bash 或其他工具探测网络。新的执行独立重试。

## 4. 页面与 RPC

- 不提供员工详情页 DWS OAuth 页面或状态/开始/轮询/取消/断开授权 RPC。
- 钉钉渠道绑定是唯一应用凭证来源；配置变更只触发服务端运行时准备，不暴露 Secret 给员工配置 UI 以外的调用者。

## 5. 验收不变量

1. 默认钉钉应用有效时，所有员工无需在员工管理页执行 OAuth 登录或单独绑定即可自动获得并使用 `dws api` 工具；无默认应用时，只有精确绑定的员工可用。
2. 默认应用无效，或当前员工没有唯一有效绑定时，员工无法获得 DWS 应用凭证或 App Token 缓存。
3. 员工 A 的运行目录与员工 B 不同；旋转凭证后 A 会切换到新的 App Token 缓存目录，删除员工会清理其所有旧目录。
4. CLI 调用不可指定或覆盖应用凭证、Token、员工 scope 或非官方 API 主机；仅允许 `api.dingtalk.com` 和 `oapi.dingtalk.com` 的 HTTPS/443 地址。
5. 所有 DWS 子进程输出和异常处理不得把应用 Secret 或 Token 写入日志、用户可见结果或模型上下文。
6. DWS CLI 缺依赖、安装失败或 API 权限不足不阻断普通对话。
7. 钉钉群调用的工具说明使用路由快照提供当前群 ID，不使用模型从聊天文本推断的群 ID；普通私聊不会收到群信息。
8. 在线群 DOC 创建仅使用可信当前群和发送者身份，签名上传信息不得泄露；只有提交成功并返回文档标识后才报告成功。
9. App Token 群文档接口的 403 应指出实际失败接口和对应用途，不把知识库工作空间权限当成群文档权限。
10. DOCX 更新只覆盖经当前群空间核验的目标 dentry，保留原文件名和父目录；并发版本变化或目标不属于该群时，提交前失败关闭。
11. 日历写入只以可信钉钉消息发送者为创建者；必填字段和本地时间校验通过后才调用 API；缺少日历或通讯录权限时错误能指出对应权限和 API，结果不确定时停止重试。
12. 群共享日程只以可信当前群成员名单为邀请对象；应用接口拒绝时仅复用已有 DWS 用户 profile 查询群成员，不自动授权；名单分页或 unionId 映射不完整时不创建；一个群请求只创建一个事件，参与人分批添加失败时不得重复创建。

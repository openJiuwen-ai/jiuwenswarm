# F_01 首 token 时延：请求路径 CPU 热点缓存

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_01 / ttft-cpu-hotspots |
| 关联模块 | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`、`jiuwenswarm/common/config.py`、`jiuwenswarm/common/utils.py`、`jiuwenswarm/agents/harness/common/prompt/shell_environment.py`、`jiuwenswarm/agents/harness/common/tools/multimodal_config.py`、`jiuwenswarm/agents/harness/team/a2x/client/transport.py` |
| 日期 | 2026-11-05 |
| 关联 spec | common [[S_08]] N1（合并配置缓存） |

## 背景

「收到对话请求 → 发起模型调用」这一段由 AgentServer 自身决定。在进程内构建真实会话实例、模型替换为本地模拟，
只在这一段按 5ms 间隔采样主线程调用栈（空等 IO 的样本不计）剖析：

| 场景 | 主线程忙碌占比 | 忙碌时间的构成 |
|---|---|---|
| 后续轮 | 约 59% | 同步文件系统调用约 86%（realpath / exists / stat / open），纯计算不到 3% |
| 首轮新会话 | 约 86% | 会话实例构建约一半；其中每个会话新建 SSL 上下文约 17% |

这些调用每轮得到的结果几乎不变，却都在事件循环线程上同步执行。本次只把「结果不变却每轮重算」的步骤改为缓存，
不改业务逻辑，也不改 IO 型耗时（网络请求、画像拉取等）。

## 改动

| # | 位置 | 每轮在做什么 | 改法 | 失效条件 |
|---|---|---|---|---|
| 1 | `shell_environment._build_shell_core` | 每次模型调用沿 PATH 查找 PowerShell / bash，检查 Git Bash 候选路径 | 按语言、平台与 PATH 等 8 个相关环境变量缓存提示词文本 | 相关环境变量变化即重探；另有 300s TTL，覆盖运行中新装 Git 的情况 |
| 2 | `utils._find_package_root`、`utils.get_builtin_skills_dir` | 每次定位内置技能目录都 realpath 本模块文件，再 stat 两次判断标准布局 | 前者 `lru_cache`；后者 `exists() and is_dir()` 合并为一次 `is_dir()`（等价），不缓存 | 包根目录进程内不变；标准布局目录每次检查，运行中消失照常回退 |
| 3 | `utils.get_builtin_skill_names` | 每轮列内置技能目录 | 按目录路径 + mtime 缓存 | 目录内增删子目录（mtime 变化） |
| 4 | `utils.get_default_project_session_workspace_dir` | 每轮 `mkdir(exist_ok=True)`，目录已在时也有一次失败的系统调用和异常 | 先 `is_dir()`，不存在才 mkdir | 行为不变（目录被删时下次照常创建） |
| 5 | `_deepresearch_artifact_output_dir` | 每次调用 `resolve()` | 绝对路径按输入缓存解析结果，上限 256 | 相对路径不缓存（依赖进程 cwd） |
| 6 | A2X 客户端 `AsyncHTTPTransport` / `HTTPTransport` | 每个会话实例新建 httpx 客户端，随之新建 SSL 上下文并加载信任材料 | 进程内共享一个按 httpx 默认方式创建的上下文，同步、异步两个传输类共用 | 按 `SSL_CERT_FILE` / `SSL_CERT_DIR` 的取值与文件戳（mtime + 大小）区分：bundle 原地轮换即重建；另有 300s 有效期 |
| 7 | `multimodal_config._raw_model_api_key` | 为读一个字段调用 `get_config_raw()`，深拷贝整棵配置树 | 改读 `get_merged_config_dict()` 的共享缓存（只读） | 底层缓存按 YAML 路径 + mtime + 大小失效 |
| 8 | `config.clear_config_cache(service_id, agent_id)` | ns 级清理顺带清掉合并配置缓存，每个会话构建加载 `.env` 都会触发双 YAML 重解析 | ns 级清理不再清合并配置缓存；合并缓存键改为与 YAML 解析缓存同口径的 (mtime_ns, size) | 合并配置只取决于两份 YAML，按路径 + mtime + 大小校验；全量清理分支照旧清空 |

与原逻辑的差异只在缓存窗口内：

- 第 1 项：进程运行中安装或卸载 Git / PowerShell 且 PATH 不变时，最多 300s 后提示词才反映变化。
- 第 5 项：工作目录是符号链接且运行中被改指向时，已缓存的解析结果不会跟着变。
- 第 6 项：改动前每个新客户端都重新读取信任材料。现在 `SSL_CERT_FILE` 指向的 bundle 轮换（路径不变、内容替换，含 K8s Secret 的符号链接切换）后，下一个新建客户端即用新上下文；`SSL_CERT_DIR` 目录内原地改写已有证书文件不改目录 mtime，最多 300s 后生效。两个变量都未设置时 httpx 使用 certifi 自带的 bundle（不读系统证书库），随安装包变化，需重启生效，与改动前相同。

## 未处理的热点

| 热点 | 原因 |
|---|---|
| openjiuwen `SkillUseRail` 每轮 3 遍扫描技能根目录（增量刷新、`before_invoke` 与 `before_model_call` 各算一次签名），签名不按启用名单过滤；Relay 场景 392 个目录启用 29 个，后续轮约 120ms | 在 agent-core 仓，由配套的 agent-core 改动处理（签名与增量刷新先按启用名单过滤） |
| openjiuwen `cwd.init_cwd` / `set_cwd` 的 realpath | 在 agent-core 仓；cwd rebind 是 harness [[S_20]] 的双模式契约 |
| `FileSkillGrantStore` 每轮加文件锁读 JSON；`resolve_session_subdir` 的 realpath | 前者涉及文件锁语义；后者是会话目录的符号链接与重解析点安全校验，缓存会让后来被替换的链接绕过校验 |
| 历史文件格式探测（`_peek_first_non_ws_char`）、todo / plan 状态文件读取 | 属读文件 IO，读的内容可能每轮变化 |
| 会话元数据每轮读 3 次（`_ensure_auto_team_binding_for_chat`、`_bind_runtime_cron_context`、code 模式准备，Relay 后续轮合计约 34ms） | 耗时几乎全在打开刚被改写的 metadata.json，属文件 IO。试过按 metadata.json / projects.json 文件戳记住结果，但 `last_message_at`、`message_count` 等字段每轮都会改写该文件，剖析中命中率为零，已撤回。要省掉这次读，只能依赖 first-write-wins 与跨进程写入时序的假设（session [[S_03]] N7），不在本次范围内 |

## 效果

指标均为「收到请求 → 发起模型调用」。

模拟环境（本地模拟模型，agent 模式；后续轮 60 轮，会话已有 8 轮历史；首轮 30 个新会话）：

| 场景 | 改前 P50 / 均值 | 改后 P50 / 均值 | 主线程忙碌采样（每轮） |
|---|---|---|---|
| 后续轮 | 115.9 / 119.1 ms | 92.7 / 96.7 ms | 10.1 → 6.4 |
| 首轮新会话 | 352.5 / 371.6 ms | 253.4 / 274.9 ms | 40.1 → 23.2 |

首轮的下降主要来自第 6 项（每会话 SSL 上下文，约 65ms）与第 1 项（shell 探测，约 38ms）。

Relay 实测（flash 模式，首轮为新会话第一条消息、不含服务启动后的第一个会话，后续轮为同一会话第 2~10 条）：

| 场景 | 改前 P50 / 均值 | 改后 P50 / 均值 |
|---|---|---|
| 新会话首轮 | 678 / 666 ms | 463 / 476 ms |
| 会话实例构建（`create_instance_ms`） | 204 / 213 ms | 141 / 150 ms |
| 后续轮 | 279 / 274 ms | 265 / 274 ms |

Relay 的后续轮几乎不走第 1 项的 shell 探测，主要耗时在 `SkillUseRail`（见「未处理的热点」），本次改动对它基本无收益。

## 验证

单测：`tests/unit_tests/agentserver/test_ttft_cpu_caches.py`、
`tests/unit_tests/common/test_utils_builtin_skill_cache.py`、
`tests/unit_tests/common/test_config_merged_cache_scope.py`、
`tests/unit_tests/agents/harness/common/prompt/test_shell_environment.py`。

真实环境对比日志：同一会话 `[latency] stage=0 name=recv` 到 `[MODEL_HTTP] begin` 的间隔（后续轮），
以及首轮 `session adapter created ... create_instance_ms`（`openjiuwen/server.log`）。

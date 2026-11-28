# 写盘类工具正文被误判为凭证探测

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 日期 | 2026-11-28 |
| 问题 | OA.05000090（会话 `officeclaw_6b830fdc214040450964239c`） |
| 范围 | `agents/harness/common/rails/execution_guard/stalled_access_circuit_rail.py`、对应单测 |

## 现象与根因

2026-11-19 15:07:57 与 15:11:37，同一会话连续两轮在数据迁移任务中失败，宿主错误码为
`OA.05000090`（未分类）。护栏日志为：

```
[StalledAccessCircuitRail] tripped session_id=officeclaw_6b83... tool=write_file
probes_in_window=3 denied_in_window=0
```

`denied_in_window=0` 说明没有任何 `AUTH_SESSION_TERMINATED`；三次“探测”全部来自
`write_file`。回放 `history.json` 确认命中的是写盘正文：

```
$排除文件 = @(..., 'Cookies','Cookies-journal','Login Data',
              'Login Data For Account','Local State','Web Data', ...)
```

即模型在生成“每日增量备份脚本 / 迁移交付文档”时，把浏览器凭证文件写进**排除清单**。
`_tool_args_text()` 把 `tool_args` 的所有文本叶子摊平后交给 `_CREDENTIAL_PROBE`，
`content` 与 `file_path` 同等对待，于是“写排除清单”被判成“读取凭证”。第 3 次命中后
`request_force_finish(result_type="error")` 产出 `chat.error`，帧上没有结构化 `code`，
Host 侧 `classifyError()` 落到 `unknown`，最终显示 `OA.05000090`。

## 决策与边界

**取证维度按工具语义切分。** `write_file` / `edit_file` 只取路径类参数
（`_WRITE_PATH_KEYS`：`file_path` / `path` / `filename` / `destination` 等），正文
`content` / `old_string` / `new_string` 不再参与判定。写盘正文是模型产出物，不是“要做什么”
的意图证据；把它当探测证据会把所有“写备份脚本 / 写交接文档”的正当任务判成违规。
参数无法解析为对象时返回空串，fail-open：解析失败不产生误熔断。

**“Login Data” 加右边界。** 最终形态 `Login Data(?![\w\\/]|\.txt\b)`，只排除两类：
紧跟 `\w` 或路径分隔符（`Login Database`、`LoginData`），以及明确是笔记/导出文本的
`.txt`（`Login Data.txt`）。右侧刻意**不排除** `-` 与 `.`：
`Login Data-journal` / `Login Data-wal` / `Login Data-shm` 是同一 SQLite 凭证库的回滚
日志与 WAL/SHM 页，`Login Data.bak` 是复制件，都属于取证面，必须继续命中；本次事故
正文本身就把 `Cookies-journal` 列为凭证产物。真正的凭证库路径（`...\Login Data`、
后接引号/空白/串尾）与 `Login Data For Account`（后接空格）照常命中。

> 取舍：`.txt` 例外是本正则相对旧子串匹配**唯一**的收窄，理由是「读笔记/导出文本」
> 这类良性场景；若后续认为 `Login Data.txt` 复本也应计入，去掉 `|\.txt\b` 即可回到
> 旧命中面的超集，无需其它改动。cookie 侧（`Network[\\/]Cookies`）本就不做右边界，
> 两侧车文件的覆盖现已对称。

**收尾带结构化码。** 鉴权分支仍以 `result_type="error"` 收尾（确实需要用户重新登录），
但追加 `code="CREDENTIAL_GUARD_AUTH_REQUIRED"`；探测分支的 `answer` 收尾追加
`code="CREDENTIAL_GUARD_BLOCKED"`。`stream_utils.build_chat_error_event` 已把
`code` 透传为 `payload.code`，Host 可据此分流，不再只能落到 `unknown`。
两个常量由 `__all__` 导出，便于宿主侧对同一份字面量做契约测试。

**刻意不改的部分。** 判定仍按窗口内次数累计（`_probe_at`），没有引入“目标路径去重”——
现有用例明确要求“同一命令重复 3 次仍熔断”，改口径会同时改变真实探测的拦截强度。
软拒绝（首次命中改写工具结果）保持原样；结果侧命中仍只做观测计数；其它工具的参数口径
不变；阈值、窗口、配置项均未改动。

## 验证与限制

在 Windows、Python 3.11.18 的仓内 `.venv` 上执行：

```
python -m pytest tests/unit_tests/agentserver/rails/test_stalled_access_circuit_rail.py \
  -o addopts="--asyncio-mode=auto" -q
```

30 passed（原 19 例 + 新增 11 例，无跳过）。新增用例：

- 事故回放：`write_file` 正文包含 `Cookies` / `Login Data` / `Local State` / `Web Data`，
  连续 5 次不熔断、不改写工具结果、`_probe_at` 为空。
- `edit_file` 的 `new_string` 包含凭证字样不计数。
- `write_file` 的 `file_path` 指向 `...\Default\Login Data` 仍计数并软拒绝。
- `Login Database.txt` / `Login Data.txt` 不再命中。
- `Login Data-journal` / `-wal` / `-shm` / `.bak` 仍计数并软拒绝，右边界收紧没有制造漏检。
- 鉴权分支 `force_finish` 载荷带 `CREDENTIAL_GUARD_AUTH_REQUIRED`。
- 探测分支 `force_finish` 载荷带 `CREDENTIAL_GUARD_BLOCKED`。

未覆盖：真实模型是否会在看到软拒绝后改走合规路径；Windows 打包产物的实际行为；
Host 侧 `OA.05002010` 映射（属 Relay 仓改动，需单独验收）。

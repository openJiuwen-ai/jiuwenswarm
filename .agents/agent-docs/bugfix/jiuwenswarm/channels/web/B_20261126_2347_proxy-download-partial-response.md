---
id: B_20261126_2347_proxy-download-partial-response
title: 代理下载残响应粘包与 AgentOS 下载整包缓存修复
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/app_web.py
  - tests/unit_tests/channels/web/test_web_file_download.py
designs: []
design_note: 对 /file-api 代理既有行为的局部修复（响应头写出后的失败处理与响应体转发方式），无跨模块契约变化，无独立设计。
---

# 代理下载残响应粘包与 AgentOS 下载整包缓存修复

## 现象与根因

基线为 develop 的 d02647d42。`/file-api` 下载链路上两个相互独立的缺陷：

1. **代理下载中途失败后在已发出的响应上追加第二条响应**（`_proxy_file_download`）：
   流式回传为"urlopen 成功 → send_response + 透传响应头 → end_headers →
   `shutil.copyfileobj` 拷贝 body"。拷贝中途抛错（上游 chunked 响应断开的
   IncompleteRead、下游写失败 BrokenPipeError、读超时等）落进统一的
   `except Exception → return False`，不区分响应头是否已写出。调用方
   （`_handle_file_download`）对 False 的处理是回退本地下载或
   `_write_json(503)`——在同一个 socket 上把第二个 HTTP 响应（本地文件重发的
   200/206 或 503 JSON）接在残 body 后面。浏览器按已声明的 Content-Length
   收文件：残 body + 第二条响应的字节粘成一个损坏文件，且失败被伪装成
   下载内容。`except HTTPError` 分支（urlopen 返回前抛、头未发）属正常
   错误透传，不受影响。
2. **AgentOS 下载整包进内存**（`_proxy_http`）：非流式路径
   `resp.read()` 把上游响应体一次性读入内存，无字节上限。AgentOS 模式带
   `user_id` 的下载分支调 `self._proxy_http()` 无参——下载路由不在
   `streaming`（SSE）与 extension 资产/传输路由的流式判定内，大文件
   下载（或多并发下载）把上游完整 body 缓存在 Gateway 进程内存，
   可 OOM。

## 当前契约与修复决策

- `_proxy_file_download` 增加响应头写出跟踪（`headers_sent`，置于
  `end_headers()` 之后——send_response/send_header 阶段只入 buffer，
  end_headers 才落 socket）。body 中途失败且 `headers_sent` 时：标记
  `self.close_connection = True` 并返回 True——响应已开始写出，截断本身
  就是客户端可见的失败信号，连接终止防止任何后续写入；返回 False 仅表示
  请求尚未发出或响应头尚未写出，调用方才可以安全回退。docstring 同步
  写清返回值语义。
- `_proxy_http` 新增仅关键字形参 `stream_response: bool = False`：
  为 True 时跳过 `resp.read()` 整包读，复用既有的 `read1` 分块转发循环
  （extension 资产/传输路由已在用），边收边转发不缓存。局部判定变量改名
  `should_stream` 避免与形参遮蔽。AgentOS 下载分支改为
  `self._proxy_http(stream_response=True)`。
- 流式转发分支的 body 中途失败对下载语义不再静默：`read1` 循环的
  `except (OSError, http.client.HTTPException)` 内，`stream_response=True`
  时记 `logger.warning("[file-api/download] 流式代理中断: …")` 并显式
  `self.close_connection = True`，与 `_proxy_file_download` 的"截断即
  失败信号"语义对齐（首轮将 AgentOS 下载并入该分支后，头已透传的 body
  中途失败由基线可观测的 502——失败发生在头写出前——变为静默吞掉，
  检视指出后补齐）。SSE/extension 流的断连保持静默：客户端断开属
  常态，不产生日志噪音。

## 拒绝的方案

- 按异常类型细分（IncompleteRead 归截断、BrokenPipeError 归下游断开）再
  决定是否回退：两种场景的客户端可见结果一致——Content-Length 已声明，
  任何后续写入都是粘包；下游断开后本地回退同样写不出去。头已发即统一
  终止连接，不做异常分型。
- body 失败后改写 trailing 错误响应或回调通知前端：同上，socket 上不存在
  "第二条响应"的合法写法；错误的可见性由下载器对截断的报错承担。
- `_proxy_http` 加字节上限、超限返回 502：把内存问题转成大文件必然失败的
  可用性问题；既有 read1 流式基建复用即可，不引入新的上限语义。
- 为流式转发另写 `_proxy_http_stream` 方法：与既有 `stream_sse`/`stream_body`
  布尔形参的先例不一致，且 90% 逻辑重复。

## 回归证据与适用范围

环境为 Windows、Python 3.11.9（worktree 独立 .venv，uv sync），测试
`--no-cov -p no:cacheprovider`（未运行格式化或静态检查）：

```text
uv run pytest tests/unit_tests/channels/web/test_web_file_download.py \
  tests/unit_tests/channels/web/test_app_web_extension_assets.py \
  tests/unit_tests/channels/web/test_web_enterprise_proxy.py   # 60 passed
uv run pytest tests/unit_tests/channels/web/test_app_web_proxy_overrides.py \
  tests/unit_tests/channel/test_trajectory_http.py             # 41 passed
```

新增 5 个用例，红绿双向：

- `test_download_proxy_mid_stream_failure_does_not_append_second_response`
  （下游 wfile 写入即失败）：基线红——调用方补写 503 第二条响应；修复绿
  （`close_connection` 标记 + 无第二条响应）。
- `test_agentos_download_proxy_streams_upstream_body`（上游分两段写、
  第一段写出后等下游确认才继续）：基线红——整包 `resp.read()` 与上游
  互等至客户端读超时；修复绿——首段在上游未写完时即到达，body 完整。
- `test_download_proxy_unreachable_upstream_still_reports_retryable_fallback`
  （上游网络级不可达）：基线/修复均绿，守住"头未发才回退"的既有契约。
- `test_agentos_download_proxy_mid_stream_upstream_reset_is_observable`
  （上游 SO_LINGER(0) RST 模拟"头已透传后 body 中途硬断开"，干净 EOF
  不抛错走不到该分支）：基线红——流式分支静默吞掉、无 warning 记录；
  修复绿——记 warning + 客户端恰好收到已转发的字节（短读可见，无第二
  条响应）。
- `test_agentos_download_proxy_upstream_truncation_appends_no_second_response`
  （上游声明 Content-Length 后部分写出即干净关闭）：守护用例，修复前后
  均绿——read1 循环按 EOF 正常结束，客户端恰好收到已转发字节、短读
  可见，响应流上不出现第二条 HTTP 响应（#7191 的粘包形态不在该路径
  复现）。

测试客户端用标准库 `http.client`：探针对照发现 httpx 0.28.1（h11）对
BaseHTTPRequestHandler 的 HTTP/1.0 响应按"连接关闭即响应结束"读体、
不信任 Content-Length，上游未写完即读超时——与本次修复无关的客户端
兼容性限制，仓库内既有 HTTP 端到端测试（extension assets）同样使用
http.client。

适用范围：`_proxy_http` 其余 9 处调用方（网关/API/资产路由）不传
`stream_response`，默认 False 行为不变；`_proxy_file_download` 的唯一
调用方回退分支仅在返回 False（头未发）时进入。HTTPError 透传、Range/416、
inline 沙箱等既有行为未改动。

## 资料同步与遗留

无用户文档触及（服务器内部失败处理与转发方式，不改变用法说明）。
遗留：`_proxy_file_download` 的静默截断（上游 Content-Length 未读满但
`copyfileobj` 无异常地读到 EOF，短读不抛错）仍按正常结束返回 True——
浏览器按 Content-Length 校验会报网络错误，属另一类可观测性缺口，
未纳入本批。`_proxy_http` 流式转发在上游未声明 Content-Length（chunked，
`Transfer-Encoding` 按 hop-by-hop 丢弃）时的中途截断同理：网关侧无预期
长度事实可比对，客户端以连接关闭定界可能把残 body 当作完整文件——
保证上游声明长度或透传分帧属更大改动，未纳入本批。HTTPError 透传分支
的内层 `except Exception: pass`（app_web.py:2734 附近）为基线既有行为，
本批未触及。

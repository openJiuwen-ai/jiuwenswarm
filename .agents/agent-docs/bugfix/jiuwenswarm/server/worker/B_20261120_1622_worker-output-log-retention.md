---
id: B_20261120_1622_worker-output-log-retention
title: 限制 Worker stderr 与历史诊断文件的磁盘累积
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/worker
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/worker/output_logs.py
  - jiuwenswarm/server/worker/main.py
  - jiuwenswarm/server/supervisor/worker_process.py
  - tests/unit_tests/agentserver/test_worker_output_logs.py
  - tests/unit_tests/agentserver/test_worker_entrypoint.py
designs: []
design_note: 用户明确要求参考现有日志规则为 worker-logs 增加轮转，属于现有输出链路的局部修复，无独立设计。
---

# Worker 输出与诊断日志保留修复记录

## 现象与影响

用户反馈 `.jiuwenswarm/run/worker-logs` 累积过大。开发基线为 develop `6911ee63236b29afdf5130e5e2d0789dd1eef094`。基线中 Supervisor 为每个新 Worker 创建独立 UUID 的 stderr 文件，以追加方式直接继承给子进程；文件不轮转，多次重启的旧输出和异常产生的 `supervisor-*.txt` 也未限制保留数量。用户实际目录大小和生产负载未采集，本次代码与运行证据来自隔离环境。

## 当前契约与修改边界

沿用 `docs/zh/runtime-worker-lifecycle.md` 的原始路由：stdout 继承启动进程输出，stderr 直接写文件，不将 Worker 的 INFO 转成 Supervisor WARNING，不重新引入 Front 管道转发。应用日志的当前规则由 `common/utils.py` 的 `_LOG_FILE_MAX_BYTES = 20 * 1024 * 1024`、`_LOG_FILE_BACKUP_COUNT = 20` 定义，Worker 复用这些常量。

`docs/zh/runtime-worker-lifecycle.md` 和英文对应文档维护当前有效契约；本记录说明实施取舍和证据。修复先在 develop 的独立工作区实现，再按用户要求先交付 `releases/0.2.9` PR；release 回移基线为 `60c0e4214b4bd622b46349bea93158390f168643`。未改动当前旧分析分支。

## 根因与修复决策

1. 在 Worker 内启动后台轮转线程，每 5 秒检查 stderr。采用现有应用日志的复制后截断方式，保留原始句柄，因此原生写入和 faulthandler 的缓存描述符继续指向有效文件，resident Worker 也不依赖 Front 存活。
2. 每份归档限制 20 MiB，突发输出拆分为多个归档，当前 Worker 最多保留 20 份。单次已有内容超过完整预算时只归档最新 400 MiB。当前文件在轮询期间可以暂时超过阈值，复制与截断之间的并发写入存在与现有策略相同的丢失窗口。
3. Windows 受控实验证明：普通 `open('ab')` 经 Popen 继承后丢失 CRT 的追加标志，截断之后子进程会从旧偏移写入，产生 NUL 空洞。使用 `CreateFileW(FILE_APPEND_DATA | FILE_READ_ATTRIBUTES)` 且不申请 `FILE_WRITE_DATA`，验证原生写入在截断后从新 EOF 继续。POSIX 保持 `O_APPEND`。
4. 启动时及每 60 秒按时间清理已关闭的旧 stderr、历史 stdout、归档，共享 20 个文件 / 400 MiB 预算。Supervisor 快照单独采用相同预算。保护最近 60 秒修改的文件及本用户进程打开的输出，包括继承 stderr 的工具子进程。无法检查进程或检查超时则保留无法确认关闭的基础日志，不凭文件年龄删除活跃 Worker 日志。
5. 旧文件扫描和轮转在业务循环之外执行；维护异常只告警，后台线程为 daemon，关闭最多等待 0.2 秒，不把磁盘卡顿加入 Worker 关闭期限。

### 实际评估过的备选

- 仅套用 `RotatingFileHandler`：只能处理经 logging Handler 的记录，覆盖不了原始 stderr、原生库和 faulthandler 写入，因此不采用。
- Front 接收 stdout/stderr 管道并转写：改变现有路由，引入对 Front 生命周期和消费速度的依赖，因此不采用。
- 重命名当前 stderr 后新开文件：继承者和 faulthandler 会继续使用原描述符，Windows 打开文件的重命名也有额外约束，因此不采用。
- 只限制单个 UUID 的归档：多次 Worker 重启和 Supervisor 诊断快照仍可持续增长，因此增加目录保留预算。

## 回归证据与适用范围

- Windows 受控继承句柄实验：原实现截断后内容为 `NUL * 7 + after`；追加访问句柄实现为 `after`，无空洞。
- develop 初轮证据：轮转与入口测试 18 passed；现有输出、诊断快照、日志冷加载和关闭期限回归 38 passed、4 skipped（平台条件）。
- 0.2.9 分支独立回归：53 passed、4 skipped，零失败。覆盖默认 20 MiB 阈值的真实 Worker main 入口、多次轮转、两种 transport 的真实子进程、原生堆栈、异常退出、Front 退出后的 resident 轮转、磁盘维护阻塞时的有界关闭、归档被占用时的继续轮转，以及活跃子进程文件保护。4 项跳过分别依赖 POSIX 信号或 Unix socket；该分支不存在 develop 的日志冷加载测试文件，未将它算入 release 结果。
- 测试使用 Windows 与隔离 Python 环境；从本机 0.2.9 运行环境读取已有依赖，测试工具安装在独立 venv，未更改运行中的环境。不能将这些结果解释为完整模型业务或打包产物验收。
- 运行记录：`D:/work_space/.codex-temp/worker-log-rotation-20261120/release029.xml`；develop 对照为 `rotation-final.xml`、`regression.xml`。源码、阈值、依赖或启动链变化后应重验相关路径。

## 资料同步与遗留

同步中英文运行生命周期和日志说明，并执行文档索引同步。未执行格式化与静态检查。

macOS/Linux 与安装包未进行真实运行验证；本次未部署到用户正在运行的服务，也未直接删除用户已有日志。进程访问受限时，无法证明已关闭的旧基础日志会被保守保留。

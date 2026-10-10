# Implementation Manifest（模块三内部实现清单）

模块二只负责说明“测什么”，不会提供不同论文方法的通用启动命令。模块三Agent完成具体方法、基线和指标代码后，必须在本次`run_dir/implementation-manifest.json`登记真实执行方式。执行引擎只运行清单中的命令，不会凭方法名称模拟结果。

## 1. 最小结构

```json
{
  "schema_version": "1.0.0",
  "execution_approved": true,
  "approval_type": "LLM_REVIEW",
  "approval_digest": "由后端写入的实现批准摘要",
  "approved_by": "experiment-agent",
  "reviewer_model": "实际审查模型名称",
  "review_digest": "第二阶段审查决定SHA-256",
  "reviewed_code_digest": "已审代码及清单SHA-256",
  "reviewed_smoke_digest": "已审冒烟证据SHA-256",
  "approved_at_utc": "ISO-8601时间",
  "datasets": {
    "dataset_name": "data/dataset_name"
  },
  "dataset_preparations": {
    "dataset_name": {
      "ready": true,
      "planning_aliases": ["模块二三列表中的明确别名"],
      "experiment_ids": ["exp1"],
      "command": [
        "python",
        "implementations/data/prepare.py",
        "--dataset",
        "{dataset_path}",
        "--metadata",
        "{metadata_path}"
      ],
      "cwd": ".",
      "env": {},
      "required_env": [],
      "marker_path": "data/dataset_name/.prepared.json",
      "notes": "实现DataPlan中声明的预处理步骤"
    }
  },
  "analysis_extensions": {},
  "implementations": {
    "method_name": {
      "ready": true,
      "command": [
        "python",
        "implementations/method_name/main.py",
        "--config",
        "{config_path}",
        "--metrics",
        "{metrics_path}"
      ],
      "cwd": ".",
      "env": {},
      "required_env": [],
      "metrics_path": "raw_results/{run_record_id}/metrics.json",
      "uses_gpu": false,
      "implementation_url": null,
      "revision": "release-or-source-version",
      "source_kind": "OPEN_SOURCE",
      "license": "Apache-2.0",
      "dependency_plan": ["package==1.2.3"],
      "smoke_test_command": ["python", "implementations/method_name/test_smoke.py"],
      "smoke_test_passed": true,
      "notes": "实现来源、适配内容和已完成的冒烟测试"
    }
  },
  "metrics": {
    "metric_name": {
      "verified": true,
      "definition_used": "本次采用的准确数学或评测定义",
      "implementation": "implementations/common/metrics.py:metric_name",
      "parameters": {},
      "library": null,
      "library_version": null,
      "direction": "MAXIMIZE",
      "unit": "score",
      "aggregation": "先按样本计算macro-F1",
      "seed_aggregation": "MEAN"
    }
  }
}
```

**数据文件发现规则（2026-09-17 补，起因：LongMemEval 文件无扩展名，5/5 方法读成空集，指标静默全 0）**：

- 数据集目录里的数据文件**不一定带 `.json` 后缀**：HuggingFace `resolve/` 下载回来的文件保留上游命名（如 `data/LongMemEval/0000-longmemeval_m`），内容是合法 JSON 但没有扩展名。
- 实现必须按"能否被 `json.load` 解析"来发现数据文件，而不是只按后缀筛选；目录下同时存在 `datasets.json`、`.download-complete.json` 等元数据文件，需要跳过。
- 语料或评测集解析为空时，实现必须**显式报错并非 0 退出**；返回码 0 + 全 0 指标会被当成"成功运行"计入分析，属于静默失败。

**`{config_path}` 与 `{metrics_path}` 的读写方向是硬契约（2026-09-17 补，起因：生成代码把 `--metrics` 当输入读，5/5 方法冒烟全崩）**：

- `--config {config_path}`：**输入**。只读 JSON，含 `experiment_id` / `dataset` / `dataset_path` / `method` / `metric_names` / `seed` / `parameters` / `split_strategy`。
- `--metrics {metrics_path}`：**输出**。实现必须把指标**写到**该路径（`raw_results/{run_record_id}/metrics.json`，运行前并不存在），并自行创建父目录。**不得**把它当输入文件读取——`json.load(open(args.metrics))` 会立刻 `FileNotFoundError`，冒烟必然以 exit code 1 失败。
- 输出 JSON 形状：`{"metrics": {"<metric_name>": {"value": <float>, "unit": "score", "split": "test"}}}`。`parse_metrics` 取 `payload["metrics"]`，`split` 缺省为 `"test"`。
- 冒烟测试必须在**不预写** `metrics.json` 的前提下验证"输出路径确实被写入"；预先写一个 `metrics.json` 会把该契约错误完全掩盖。

`aggregation`说明runner内部的样本级指标算法；`seed_aggregation`是机器读取的跨seed规则，当前只允许`MEAN`。若需要中位数或其他策略，必须先扩展执行器，不能靠说明文字中的关键词放行。

`scripts/main.py scaffold-manifest`生成的清单故意带`execution_approved=false`、`ready=false`、`verified=false`和`seed_aggregation=null`，只能作为待办清单。Implementation Builder登记实现后生成第一阶段审查上下文；根Agent提交结构化审查决定，但不能写批准字段。只有第一阶段批准、确定性门禁、真实冒烟和第二阶段批准全部通过，后端才写入上述审批元数据。不得手改审批字段；代码、命令、依赖、科学清单或冒烟证据变化会自动使两阶段审查失效。

`planning_aliases`仅用于把模块二三列矩阵的`variable`精确映射到实现键，跨实现必须唯一；`experiment_ids`在存在多个主实验时明确该实现所属实验。两者都不能由模块三猜测。`source_kind`取`EXISTING/BUNDLED/OPEN_SOURCE/GENERATED/CUSTOM`。固定开源来源必须同时登记revision与license。

## 2. 命令占位符

每个`command`必须是argv字符串数组，不使用shell字符串。支持以下占位符：

| 占位符 | 含义 |
|---|---|
| `{run_dir}` | 本次运行目录绝对路径，仅在进程启动时展开 |
| `{experiment_id}` | 模块二规划的实验ID |
| `{run_record_id}` | 单方法、单seed运行ID |
| `{dataset}` | 数据集名称 |
| `{dataset_path}` | `run_dir`内的数据路径 |
| `{metadata_path}` | 数据预处理命令读取的DataPlan元数据；仅用于`dataset_preparations` |
| `{marker_path}` | 数据预处理成功标记路径；仅用于`dataset_preparations` |
| `{context_path}` | 内含领域、实验、指标、发现和内置分析的JSON；仅用于`analysis_extensions` |
| `{output_path}` | 领域分析扩展必须写出的JSON；仅用于`analysis_extensions` |
| `{raw_metrics_path}` | seed级指标CSV；仅用于`analysis_extensions` |
| `{aggregated_metrics_path}` | 聚合指标CSV；仅用于`analysis_extensions` |
| `{method}` | 当前方法、基线或消融名称 |
| `{seed}` | 当前随机种子 |
| `{config_path}` | 执行引擎生成的单次运行配置 |
| `{raw_output_dir}` | 当前运行原始输出目录 |
| `{metrics_path}` | 当前运行必须写出的指标文件 |

`cwd`和`datasets`必须是相对`run_dir`的路径。实现代码、数据和运行产物因此可以一起复现，不把队员电脑绝对路径交给模块四。

如果`DataPlan.preprocessing_pipeline`不是`identity`、`none`等明确空操作，每个数据集都必须提供`dataset_preparations`。命令成功后执行器写入marker；预处理步骤或切分配置改变时marker失效并重新执行。不得只记录预处理名称而跳过实际处理。

每个实现的`dependency_plan`只能包含`package==exact.version`形式的精确版本。第一阶段根Agent审查通过后，后端把所有实现依赖去重写入`environment/requirements.lock`：`CURRENT`模式核对当前解释器，`VENV`模式在`environment/venv`创建本次运行专属环境。只有`allow_dependency_install=true`时才执行非交互安装；否则版本缺失或不一致直接`REPLAN`。最终解释器、锁文件摘要、安装动作和已验证版本写入`environment/deployment.json`，并纳入第二阶段审查及最终交付哈希。

## 3. 指标文件格式

每个命令返回码为0后，必须在`metrics_path`写出JSON或CSV。所有规划指标必须至少包含一条`split=test`记录。

简写JSON：

```json
{
  "metrics": {
    "macro_f1": 0.91,
    "latency_ms": {"value": 42.5, "unit": "ms", "split": "test"}
  }
}
```

记录式JSON：

```json
{
  "metrics": [
    {"name": "macro_f1", "value": 0.91, "unit": "score", "split": "test"}
  ]
}
```

CSV至少包含：

```text
name,value,unit,split
macro_f1,0.91,score,test
```

非有限值、缺失test指标、命令非0退出、超时或未生成指标文件都会使该次运行失败，并保留日志。执行引擎不会用预期结果补值。

## 4. 领域分析扩展

内置分析只对标量test指标提供描述统计和bootstrap区间。需要混淆矩阵、残差分析、生存分析、训练曲线、定性样例网格或其他领域方法时，在`analysis_extensions`登记实际分析脚本：

```json
{
  "analysis_extensions": {
    "classification-errors": {
      "ready": true,
      "required": true,
      "command": [
        "python",
        "implementations/analysis/classification_errors.py",
        "--context",
        "{context_path}",
        "--output",
        "{output_path}"
      ],
      "cwd": ".",
      "env": {},
      "required_env": [],
      "output_path": "analysis/extensions/classification-errors.json",
      "notes": "按类别计算错误分布并生成混淆矩阵候选"
    }
  }
}
```

扩展输出可以包含以下字段；没有的字段可省略：

```json
{
  "analysis_records": [],
  "visualization_data": [],
  "visualization_candidates": [],
  "tables": {},
  "figures": {},
  "table_artifacts": [],
  "figure_artifacts": [],
  "warnings": []
}
```

其中对象结构直接沿用`experiment-schemas.md`。扩展应从`context_path`读取真实`metric_record_ids`、`experiment_ids`和`finding_ids`，生成的新数据文件必须位于`run_dir`并使用相对路径。`tables`/`figures`与对应artifact名称必须成对，ID和名称不得与已有产物冲突。

`required=true`的扩展失败时，核心实验结果仍可形成，但模块状态降为`PARTIAL`；可选扩展失败只加入`warnings`。扩展不能修改内置指标或已有数字。

## 5. 方法实现职责

模块三Agent需要针对当前论文任务完成：

1. 把主方法组件实现为可执行代码。
2. 定位官方基线或可信复现；必要适配必须写入`notes`。
3. 实现并核对模块二指定的指标。
4. 让每个实现读取`config_path`中的数据集、seed、方法参数和输出目录。
5. 把代码路径、代码SHA-256、来源/revision/许可、固定依赖、最终argv、数据访问、资源需求、静态检查和风险写入第一阶段隔离审查上下文。
6. 根Agent审查通过后，由确定性后端做最长60秒、有限数据的最小冒烟测试，确认返回码、日志和真实test指标符合本契约；只有真实冒烟成功才可设置`ready=true`，指标契约校验才可设置`verified=true`。
7. 把冒烟日志、真实指标、代码摘要和冒烟摘要写入第二阶段审查上下文；配置、日志和原始指标文件都记录SHA-256，变化即失效。根Agent只能提交决定，最终批准字段由后端验证并写入。

算法本身随领域和论文变化，不应写进通用执行引擎。通用执行引擎负责一致的门禁、运行、证据链、分析、候选可视化和模块交接。

`execution_approved`是由确定性后端绑定双阶段独立LLM审查后形成的执行门禁，不是操作系统级沙箱。LLM生成代码先由受限写入工具保存，不会自动执行；第一阶段审查也不能直接放行正式实验。获批命令必须使用可信代码和最小权限环境；批准后若代码、命令、依赖、清单或冒烟结果发生变化，ExecutionAgent自动清空审批并退回`CODE_REVIEW_REQUIRED`。

## 6. 结构化代码Proposal

Agent模式的`write_generated`可直接提交对象，CLI仍可通过文件提交。Proposal必须包含`method`、`main.py`、`README.md`、`requirements.txt`、`source_url`、`revision`、`license`、固定版本`dependency_plan`、`required_env`、`uses_gpu`和`notes`；可选`test_smoke.py`与`config.example.json`。只允许这些文件名，单文件不得超过256 KiB。`method`必须在模块二冻结计划内；工具拒绝路径穿越、符号链接、内联密钥、未固定依赖以及可信`run_dir`之外的写入。

## 7. 密钥和敏感配置

- 普通非敏感变量可以放`env`。
- 访问令牌只把变量名放入`required_env`，真实值由运行环境提供。
- 不在`command`、`env`、日志、配置或输出中填写密钥值。
- 缺少`required_env`时在执行前返回`REPLAN`。

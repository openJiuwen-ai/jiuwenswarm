# executor 注入 python_cmd：skill_code 子进程一律用引擎当前解释器

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_04 / python-cmd-injection |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/executor.py`（`_merge_env_config_to_inputs`） |
| 归档日期 | 2026-10-23 |
| 关联 spec | S_01_execution-pipeline.md（10 步序列步 1 / 修改检查单 9）、S_04_node-contract.md（inputs 约定 + python_cmd 使用契约） |

## 背景与要解决的问题

docx-craft 的 `pipeline_init.py` P0 等 skill_code 需要拉起 python 子进程运行
python-docx 等依赖，但技能侧拿不到"正确的解释器"：

1. **`import sys` 被 validator 拒绝**（denied_import_exact，[[S_03]] 安全
   模型）——技能代码无法自行取 `sys.executable`；
2. **探测系统 PATH 上的 python 是错配**——PATH 命中的通常是裸系统解释器，
   不含 jiuwenclaw venv / 打包形态（embeddable python）下安装的技能依赖，
   是"依赖缺失"类运行期故障的直接来源。

注入键已有先例：`skill_root` / `skill_name` 走 executor
`_merge_env_config_to_inputs` 进 inputs，skill 侧按约定读取。

## 决策

executor 侧在 `_merge_env_config_to_inputs` 追加第三个注入键 `python_cmd`：

- **取值**：`sys.executable`——引擎当前解释器（jiuwenclaw venv / 打包后
  embeddable python），技能依赖装在哪它就指向哪；
- **守卫**：`not str(merged.get("python_cmd") or "").strip()`——缺省 /
  空白才注入；显式非空值保留（resume 持久化值不覆盖）。与 skill_root /
  skill_name 的 "not in merged" 同向但更强：空串也视为未设置；
- **技能侧契约**（固化为 [[S_04]]）：需要子进程的 skill_code 一律
  `inputs["python_cmd"]`；禁止 import sys、禁止探测 PATH；
- 注入走 debug 日志，便于部署形态（venv / embeddable）排查。

## 拒绝的方案

**门面符号（`skill_turbo_runtime.python_cmd`）**——把解释器路径作为门面
常量暴露：需递增 CONTRACT_VERSION + 同步 validator 白名单（[[S_05]] 契约
变更流程），而 `sys.executable` 是进程级常量，inputs 注入与 skill_root /
skill_name 同通道、零门面面扩张。拒绝。

**放开 skill 侧 `import sys`**——denied_import_exact 名单是 [[S_03]] 安全
模型契约，为一个路径键开洞不值；且放开 import 也解决不了行为层问题（技能
仍可能不用 executable 而去 PATH 上找别的 python）。

## 已知接受面

- `python_cmd` 把引擎进程解释器路径暴露进 inputs——skill_code 本就运行在
  同一进程，信息增量有限；接受。
- 空白字符串视为未设置：显式传 `""` 的调用方意图不明确，按"要默认值"处理。
- 作用面是 inputs 注入，非门面符号——不递增 CONTRACT_VERSION，旧技能侧仓
  零感知（不读该键即无行为变化）。

## 测试

`tests/unit_tests/server/runtime/skill_turbo/test_executor_python_cmd_injection.py`
（4 用例）：缺省注入 `sys.executable` / 空白串（`""` / `"  "`）视为未设置 /
显式非空保留（resume 持久化值不覆盖）/ 三注入键（skill_root / skill_name /
python_cmd）共存回归。

## Follow-up（不在本次范围）

- 技能侧仓 TURBO_CODE_SPEC 同步"子进程解释器契约"条目（正交体系，技能
  侧仓另行落地）。

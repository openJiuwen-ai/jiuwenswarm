---
name: ascend-moe-optimizer-auto-trace
description: >
  为昇腾算子在源码中接入 TRACE_POINT 与 MoeTracing，串通 trace_preprocessor、profiling tensor、point_map.json、
  save_profiling_data 与 trace_collector 生成 Chrome trace。强调门禁 G1–G5：全链路预处理与 OPP、profiling 为数据输出最后一位、
  整条编译与示例脚本联调、落盘路径在 spawn 前 resolve。遵循函数级粒度与就地扩展，禁止另注册 xxx_profiling 类第二入口，
  保持原 Op 与 torch.ops 名称及签名不变。在用户提到算子打点、Profiling、Chrome trace、MoeTracing，或将结论写入本 skill 时读取。
---

# 昇腾算子自动打点

## Agent 速查（执行本 skill 时先读）

**红线**：用户未明确说「只要改源码里的 TRACE / 不要 GM / 不要改 Op 输出与 pybind」时，**禁止只改 `op_kernel` 或只插桩不交联调脚本**。须满足下表 **G1–G5**；任一缺失须在回复中写明「未完成项 + 后续风险」，不得宣称已闭环。

| 门禁 | 必须满足 |
|------|----------|
| **G1 预处理** | 团队 **`compile_ascend_proj.sh`（或等价）** 已接入 **`trace_preprocessor.py` hook**；**当次编译**在构建树生成 **`point_map.json`**，且与**当前运行的 OPP/核**同源 |
| **G2 输出位次** | **`profiling_data` 为全部 Tensor「数据输出」中的最后一个**（主输出 `1…N`，再第 `N+1` 路 profiling）。**`op_host` / infer / tiling（若描述输出）/ 类 `Init` / `__global__` / `aclnnInner_*` / 手写 `pregen/.../aclnn_*` / `EXEC_NPU_CMD` 实参** 顺序一致；禁止只改其中一层 |
| **G3 编译** | 用项目**常用整条命令**跑通 **OPP**（及若有的 **pybind whl**）。**不等于**仅通过 `validate_trace_points.py` / `check_compile_safety.py` |
| **G4 联调与后处理** | 在既有 **`examples/*_sample.py` 和/或 `test_*.py`** 中：**设备同步**（如 `torch_npu.npu.synchronize`）→ **`trace_utils.save_profiling_data`**；若生成 Chrome：调用 **`trace_collector.py`**，且 **`point_map.json` 满足 G1**。**不得**「算子已多一路输出，但脚本仍按旧 arity 解包且从不落盘」 |
| **G5 落盘路径** | 传给 `save_profiling_data` / `trace_collector` 的 **`profiling_dir`、`chrome_trace`、`point_map`**：在 **`multiprocessing.spawn` 或等价并行之前** 一律 **`Path(...).expanduser().resolve()` 为绝对路径**。相对路径在 `save_profiling_data` 内会拼到 **`trace_utils.py` 所在目录**，与 shell cwd 不一致 → 易出现 **No rank\*.pt** |

**模式 A / B（与步骤 7 一致）**：**A** = `profiling_data` **OPTIONAL**，Python 侧可不增返回值个数；**B** = 同一 `torch.ops` 名，**返回值最后一项**为 profiling。**用户要落盘 / Chrome** 时优先 **B** 或在 sample 中显式接 optional 内核参数；OpDef **REQUIRED** 时禁止用 nullptr 规避。

**阅读顺序**：本段门禁 → 下文「目标」与「全链路操作性定义」→ **必须执行的流程 1–7** → **[reference.md](reference.md)**。

---

## 目标

根据自然语言需求，为目标算子生成可落地的算子侧打点代码。

边界约束：
- 本 skill **负责** 算子代码插桩 + profiling 数据采集/解析工具链的完整闭环。
- 本 skill **不修改** 算子的业务逻辑（matmul、通信等功能代码），仅新增 profiling 相关代码。
- 本 skill **需要支持** 在仅有算子代码时，自动补齐打点所需工程脚本、编译接入、以及从 profiling tensor 到 Chrome Trace JSON 的完整处理链路。
- **就地改造、少增文件**：优先改现有编译脚本、示例与 UT；避免平行维护新 `sh`、新 `run_*`、新整文件测试副本（细则见步骤 6–7 与下表）。
- **同一算子、同一接口名**：profiling 视为对**原算子**的增强，**禁止**再注册名为 **`xxx_profiling`**、**`*_with_profiling`** 或任何「看起来像另一个算子」的 **Op / `torch.ops` 入口**；**算子在图与 Python 侧的注册名保持不变**（若工程允许 arity +1，仅在**同一**名下多返回 profiling 张量；输入形参名与顺序也尽量不变，新增输出走既有扩展约定而非改名分叉）。

**默认交付标准（本 skill 执行时按此闭环，除非用户明确只要「仅插桩、不要 GM」）**：
- **算子侧**：在 `*_base.h` 中 **`ENABLE_MOE_PROFILING` 默认为 `1`**（关闭设备侧写入改为 `0` 并**重编核**；禁止依赖「不向设备传 profiling 张量」规避，与 REQUIRED 契约一致时尤其如此）；**`profiling_data`（或工程约定的同名输出）与主输出同级**（OpDef / infer / pybind / 核形参与 `Init` 顺序一致），核入口栈 buffer、`SetMoeProfilePtr`、**GM 写回**齐全。
- **`profiling_data` 在「数据输出」中的位置（易执行错、须写死）**：凡本 skill 走 **模式 B / REQUIRED**、或用户要求 **可采集 GM profiling** 时，**在所有与 GE/设备绑定的输出列表里，`profiling_data` 必须是最后一个 Output**（主输出 `1…N` 在前，**第 N+1 个且仅最后一个**为 profiling）。**Infer / tiling 中该输出的索引、`aclnnInner_*` 与手写 `pregen/.../aclnn_*.cpp` 形参顺序、`EXEC_NPU_CMD` 实参、`__global__`/`Init` 的 GM 槽位**须与同序；**workspace / tiling 缓冲等非 Tensor 输出**若与 Tensor 输出混排，以**该算子工程既有约定**为准，但 **profiling 张量不得插在主输出中间**。禁止只改 `op_host` 而漏改 infer/pregen/pybind/核入口任一处导致「看似编过、运行时错槽」。
- **编译**：在团队实际使用的 **`compile_ascend_proj.sh`（或等价）** 中已部署 **`trace_preprocessor.py`** hook（`# TRACE_PREPROCESSOR_HOOK_START/END`）；本仓库 UMDK 路径为 **`umdk/build/cam/comm_operator/compile_ascend_proj.sh`**，工具链脚本与 skill **`scripts/`** 对齐（可用 `bootstrap_trace_toolchain.py` 同步）。
- **测试**：在既有 **`*_sample.py` / `test_*.py`** 上扩展——**返回值 arity** 与 **`torch.ops` 解包**兼容多一路 profiling；**`torch_npu.npu.synchronize`（或等价）后**再落盘；可选 **`--point_map` + `trace_collector.py`** 生成 Chrome trace（具体 CLI 以目标仓库已存在的示例脚本为准）。

**用户用语与默认范围（避免只做「半套」）**  
- 用户仅说 **「打点 / 插桩 / trace / profiling / 性能点位」** 且**未**写明 **「只要改源码里的 TRACE_POINT 字符串、不要改 Op 输出 / 不要 GM / 不要动 pybind」** 等缩范围指令时，**一律按上文「默认交付标准」执行全链路**（算子 + profiling 张量绑定 + 编译预处理 + 示例或 UT 解包）。  
- 仅当用户**明确**缩小范围（例如「只加点位、本迭代不接 profiling 输出」）时，才可省略 GM / Op 变更，并应在回复中说明后续补齐项与风险。

**「全链路」操作性定义（避免只改少数文件就交差）**  
以下视为**同一交付物**，缺任一项即属半套（须在回复中列出未完成项）：**①** 编译管线中的 **`trace_preprocessor.py` hook**（生成与当次 OPP 一致的 `point_map.json`）；**②** `op_host` / **infer** / **tiling（若有输出描述）** 与 **核 `Init`/`__global__`** 的输出顺序一致，且 **profiling 为最后一路数据输出**（见上条）；**③** **`aclnnInner_*` 与手写 `pregen/.../aclnn_*` 对齐**；**④** **pybind** 多路返回或 `EXEC_NPU_CMD` 与之一致；**⑤** 既有 **`examples/*_sample.py` 或 `test_*.py`**：在 **`torch_npu.npu.synchronize`（或等价）之后** 调用 **`save_profiling_data`**，且父进程或文档可 **`trace_collector.py` → `chrome_trace.json`**（与 **`point_map.json` 同源**）。**仅 kernel 内 `TRACE_POINT` + 工具链脚本存在，但 sample/UT 仍不解包、不落盘、不接 collector —— 不算完成本 skill 默认交付。**

**推荐执行顺序（与下方步骤编号对应）**：扫描与规划（1→2→3）→ 插桩（4）→ 静态校验（5）→ 部署工具链与编译接入（6）→ Profile 测试脚本分叉（7，可与 6 并行准备，但须在 pybind/算子已暴露 profiling 输出之后才有意义）。

## Skill 自维护（元规则）

与本 skill 范围相关的讨论（排障、形状、ABI、profiling 与主路径关系等）若得出 **可复用、非一次性** 的结论，**应在同一会话或用户确认后写回本仓库 skill**，避免经验只留在聊天记录里。

- **写哪里**：默认编辑本目录下的 `SKILL.md`（与 `reference.md` 同级；本仓库示例路径见 `reference.md` 文首）；过长细节写入 `reference.md` 并保持链接。
- **写什么**：短条目、可执行检查项、易错的「不要 / 必须」、与代码路径/常量名的对应；**不要**整段粘贴 plog 或冗长堆栈。
- **本仓库 UMDK 与 Skill 同步**：若修改本 skill **`scripts/`** 下的 `trace_preprocessor.py`、`trace_utils.py`、`trace_save.py`、`trace_collector.py`、`validate_trace_points.py`、`check_compile_safety.py`、`inspect_rank_pt.py`、`bootstrap_trace_toolchain.py`，应**同步更新** **`umdk/build/cam/comm_operator/`** 下同名文件（若仓库内另有**对照/金标树**（本仓常见为并行目录下的 `build/cam/comm_operator/`），应与之对齐或文档说明有意差异）。批量同步：`python3 <skill_root>/scripts/bootstrap_trace_toolchain.py --build-dir umdk/build/cam/comm_operator`（`<skill_root>` 为含本 `SKILL.md` 的目录；从仓库根代入 `jiuwenswarm/resources/agent/workspace/skills/ascend-moe-optimizer-auto-trace/`）。
- **何时写**：用户明确要求「记成规则 / 写进 skill」时必做；若新结论 **修正** skill 里旧表述（例如 optional vs REQUIRED），应直接改原文并保持一致性。
- **触发词**：用户说「记录规则」「经验更新到 skill」「探讨的结论落盘」等，按本条执行。

**近期已并入本 skill 的探讨结论（示例索引，便于检索）**

| 主题 | 要点 |
|------|------|
| **Agent 门禁 G1–G5** | 文首 **「Agent 速查」**；默认交付先逐条满足，回复对照 **「输出约定」** 声明；**G5** 与 `save_profiling_data` 相对路径陷阱见 [reference.md](reference.md)「常见陷阱」。 |
| **`point_map.json` 与 Chrome 解析** | 必须与**当前已安装 OPP/核**为**同一次** `trace_preprocessor` 产物；路径填**真实文件**（勿用 `/path/to/...` 占位）。Host 落盘 profiling 须在 **NPU `synchronize`（或等价）之后**。`skipped_no_mapping` 高而 `rank*.pt` 非空 ⇒ **映射与二进制不一致**，非「没打点」。详见 [reference.md](reference.md) 末尾相关小结。 |
| profiling 输出地位（示例：多输出算子） | 若采用独立 `profiling_data`：**与主输出同级**绑定（OpDef/pybind/核 `__global__`/`Init` 顺序一致）；REQUIRED 时禁止向设备传空 profiling；关设备侧写入用宏 + 重编核。若工程选择「复用既有 GM / optional」须与图语义一致，**勿混用**两种绑定。 |
| 核写回与 host 可见性 | 设备写 profiling GM 后，若 host 读数异常或陈旧，可按平台补充 cache 一致性操作（如 **`DataCacheCleanAndInvalid`** 等），以目标 CANN/AscendC 文档为准。 |
| 混合核入口同步 | 1C2V 等场景下，若在 `SetMoeProfilePtr` 前后或首条 `MoeTracing` 前出现边界异常，可按算子语义在 AIC/AIV 间补 **CrossCore 屏障**，避免 trace 与执行顺序错位。 |
| **大块实现 / `#include` 子树（易漏检）** | 入口 **`op_kernel/<入口>.h`** 往往只调度；**真正耗时的 matmul / epilogue / 通信 / 分核 `operator()`** 常在 **`gemm/`、`kernel/`、`epilogue/`、`raw_distributed/` 等子目录头文件**中。必须从入口 **递归扫全 `op_kernel/`**，对这些翻译单元打点；**禁止**只改入口壳子。自检：对目标算子目录 **`grep -E 'MoeTracing|TRACE_POINT' .../op_kernel`**，长耗时路径上应有与「打点密度」匹配的命中。若仓库另有**参考树**（如 `*_trace/`、legacy 目录），可对照查漏，**交工以当前构建所用源码树为准**。 |
| 编译接入形态 | **改造已有编译脚本**，用标记块插入 `trace_preprocessor.py`；**不**新增平行「专用编译 sh」作为唯一入口。工具链优先放在与 `compile_*.sh` 同目录的可提交路径；`bootstrap` / `apply_trace_scaffold` 仅在其他仓无副本或一次性接入时使用。 |
| 就地改造与文件数量 | **尽量少新建文件**：在既有 `*_sample.py`、`compile_*.sh`、`test_<op>.py` 上扩展；工具链与预处理脚本优先与现有 build 目录同仓提交。 |
| 算子命名与接口 | **禁止**单独算子名 **`xxx_profiling`** / **`*_with_profiling`**（及同类变体）；**保持原算子注册名与 `torch.ops` 名不变**，profiling 为同算子改造（多一路输出时用 **同一 Op 名** + 文档化的返回值扩展，而非第二个算子）。 |
| `MIX_AIC_1_2_SLOTS_PER_GROUP` | `1 + GetSubBlockNum()`，本任务 1C2V 下常数为 `1 + 2`；Infer 中拆成 `MIX_AIC_1_2_SUBBLOCK_NUM` 与 `1 + …` 避免魔法数 `3`。 |
| `MAX_INFER_GETBLOCKNUM_UB = 128` | Infer 无 `GetBlockNum()`；为防低估 profiling GM；运行时常见 24 与上界无关；宁可略大占 GM，不可估小。 |
| **默认全链路 / `ENABLE_MOE_PROFILING`** | 交工默认含 **profiling 输出（或与工程一致的绑定方式）+ 预处理 hook + 示例或 UT 解包**；设备侧宏默认 **`1`**。**Infer 与动态输出**：若主输出行数/形状依赖运行时计数、infer 难以与 tiling 一致，可**仅对 `profiling_data` 在 infer 中强制 shape/dtype**，其余输出仍由图或 tiling 推导（须在工程内验证 GE/运行时无冲突）；此为**工程权衡**，非所有算子必需。 |

## 输入

- 目标算子路径，例如 `src/.../op_kernel/<op>.h`（或仓库约定的 `ascend_kernels/<op>/` 根目录）。
- **自然语言需求**：若未显式缩小范围，默认按 **「默认交付标准」** 与 **「用户用语与默认范围」** 执行（见文首）。
- 打点风格：`MoeTracing(TRACE_POINT("label", "B/E"))` 或带上下文 `MoeTracing(TRACE_POINT("label", "B/E"), extraId, index)`。
- 约束条件：
  - 函数级粒度（见 [reference.md](reference.md)「打点密度与均匀性要求」）
  - 根节点名称固定为 `processing`
  - 最大深度为 7（实际按语义需要决定，不要人为卡在浅层）
  - 对深层或低价值调用链执行智能合并

## 插桩覆盖必达清单（交工前自检）

以下与具体算子目录结构无关；**不得**只改「最外层调度头文件 / 单文件入口」即视为完成插桩。

1. **Kernel 入口**：`op_kernel` 下实际参与编译的 device 入口（通常为 `*.cpp` 中的 `__global__` / `__aicore__` 函数）——含 profiling 栈 buffer、与 GM 写回等与本 skill 约定一致的逻辑时，必须接入且与 `op_host` 参数个数一致。
2. **入口头文件 + 递归 `#include` 可达的全部实现**：在**该算子** `op_kernel/`（含任意子目录）内，凡实现 **AIC / AIV 分核主流程阶段**的翻译单元（含模板 `operator()<AscendC::AIC>` / `operator()<AscendC::AIV>`、分核 `Process`、通信、epilogue、与入口链路上的大块计算/融合逻辑等），**均须具备与语义匹配的 B/E 点位**；仅最外层已打点、**深层实现头文件未打点**视为未完成。易漏检形态：**入口头只做转发**，大块逻辑在子目录头文件中——须 **逐层 `#include` 跟到底**，不得以「文件名像数学库」为由跳过（见上表 **大块实现 / `#include` 子树**）。
3. **`op_host` / `infer` / pybind**：profiling 输出、形状推导、Python 解包 arity 等按本 skill 其他章节执行；凡在 OpDef 中将 **`profiling_data`（或等价名）标为 `REQUIRED`** 的算子，均须满足下文 **「profiling_data 与主输出同等工程地位」** 全条（禁止 nullptr optional、核 `__global__` 与类 `Init` / `aclnn` 形参顺序一致等）。
4. **密度门槛**：见 [reference.md](reference.md)「打点密度与均匀性要求」——**按每种核类型（AIC、AIV）分别**核对可见语义标签数；未达标时优先在「大块实现」内补**阶段边界**（见步骤 4 与 [reference.md](reference.md)「常见陷阱」），而不是在入口重复堆叠同义点位。

## 必须执行的流程

1. **扫描目标代码**
   - 从入口文件出发，**递归跟随 `#include` 进入同算子目录下的所有头文件**，直到遍历完整个算子内部代码树。不能只看入口 `.h`，必须读取其直接或间接包含的所有实现文件。
   - 识别主流程阶段与函数边界；特别关注 **模板实例化调用链**：如果入口函数调用了模板类并最终执行 `operator()()`，该 `operator()` 同样属于主流程阶段边界，必须跟进到对应头文件。
   - 将 **`#include` 拉起的、参与编译的** 所有子目录头文件列入待打点清单；对 **子目录中文件名含 `workspace` / `kernel` / `gemm` / `epilogue` 等大块实现** 尤须逐文件打开核对（与上条「易漏检」一致），**不得**因模板深或行数多而跳过。
   - 识别 **AIC / AIV 分核执行路径**：如果算子使用混合核（1C2V 等），AIC 分支和 AIV 分支各自是独立的主流程，需要分别打点。
   - 对于 1C2V 等模式，**必须检查 `operator()<AIV>()` 内部是否存在角色分工**（如 send core / recv core / compute core / share quant core）。不同 AIV 核可能通过 `aivIdx` 或 `GetSubBlockIdx()` 走完全不同的分支，每种角色的主要工作阶段都需要独立打点。
   - 尽量保留已存在且合法的点位。

2. **构建打点树**
   - L1 必须是 `processing`。
   - L2 至 L7 必须来源于当前算子真实语义（不要把 `dispatch/combine` 当作全局默认词）；合并规则见步骤 3，语义需要时用到 L6/L7 是正常的。
   - 对 AIC/AIV 分核执行路径，分别用 `<phase> aic` / `<phase> aiv` 作为 L2/L3 区分。
   - 对 expert group 循环、stage 循环等带索引的重复结构，打点时必须传递索引参数（见 [reference.md](reference.md)「MoeTracing 运行时规格」）。

3. **应用智能合并规则**
   - 超过 7 层的调用，折叠到最近的 L7 祖先节点。
   - 对无同步/无通信边界的薄封装函数与 helper 进行合并。
   - 对热点语义（`wait`、`sync`、`send`、`recv`、`copy`、`quant`、`dequant`）保留独立点位。

4. **插入代码**
   - 使用稳定命名的 `B/E` 成对点位。
   - 保证 begin/end 词法嵌套正确。
   - **"最内层循环"指 tile 级别的矩阵计算循环（如 matmul 块内沿 K 的迭代、细粒度 epilogue tile 循环），不要在其中打点**。但 expert group 循环、stage 循环属于阶段边界，必须在循环体入口/出口打点。
   - 区分「阶段边界」与「tile 内层」——**同一头文件里可能同时存在二者，不得以目录名或文件名猜测并整文件跳过**：
     - ✅ 需要打点：分核主流程的 **`operator()<AIC>` / `operator()<AIV>`（或等价的分核入口）** 的整体阶段边界；expert / stage 等**粗粒度**循环体上的入口与出口；AIC↔AIV 同步与等待；独立语义的 epilogue、通信、dispatch/combine 子阶段等。
     - ❌ 不要打点：块内 matmul/epilogue **单次 tile** 的内层搬运与沿 K 的紧循环、孤立单次 `DataCopy` 等无独立阶段语义的位置。
     - **判断标准**：若某函数/入口是 **本分核上某一整段业务的调度或阶段边界**（典型为分核 `operator()`、或等价的大阶段入口），则打点；若仅为 **单次 tile 或单次微内核调用的内层实现**，则不打点。文件名、子目录名**不作为**是否跳过的依据。

5. **校验**
   - 对改动文件运行 `scripts/validate_trace_points.py`，检查点位命名与 B/E 配对。
   - 若仓库内**同一算子存在多套源码树**（例如金标目录与产品目录），建议**对每一套各自的 `op_kernel`（或等价目录）各跑一遍**上述脚本，避免分叉漂移。
   - 运行 `scripts/check_compile_safety.py <operator_dir>`，静态检查插桩是否会引入编译错误。此脚本检查：花括号平衡、预处理指令配对（`#if`/`#endif`）、MoeTracing 头文件可达性、TRACE_POINT 参数语法、变量作用域、profiling guard 闭合、kernel 参数与 op_host 注册的一致性。
   - **步骤 5 的定位**：主要覆盖**算子源码树内**的常见静态错误；**不能**替代完整 OPP / `cust_opapi` / pybind 工程编译。例如 **`aclnnInner_*`（自动生成）与仓库内手写 `pregen/.../aclnn_*.cpp` 签名不一致**、`EXEC_NPU_CMD` 宏对参数左值的要求、CPack 安装路径缺失等，脚本未必能检出。
   - 如果校验失败，修正问题后重新运行。两个脚本都通过后，**仍须**用目标仓库的 **`build.sh` / `compile_ascend_proj.sh`（或 CI 等价命令）跑通一次完整编译**作为最终门禁（见 [reference.md](reference.md)「编译与打包门禁」）。

6. **部署工具链并接入编译（必须执行，不可跳过）**
   - 此步骤不是可选的"缺省场景"，而是打点流程的必要组成部分。即使插桩代码已正确插入，如果工具链脚本未部署、预处理未接入编译，打点数据无法采集和解析。
   - **少新文件、改已有入口（优先原则）**：**不要**为打点单独再维护一条「新的编译 `sh`」或平行入口，替代团队已在用的命令。正确做法是：在**现有** `compile_ascend_proj.sh`（或 CI 调用的等价脚本）里，于 `copy_ops`/源码拷入构建树之后、`./build.sh` 之前，插入**一段**预处理调用，并用 `# TRACE_PREPROCESSOR_HOOK_START` / `# TRACE_PREPROCESSOR_HOOK_END` 包裹，便于幂等与审查。日常编译仍只跑**原**命令；`apply_trace_scaffold.sh` 仅是**一次性接入助手**（跑完 bootstrap + patch + verify），**不是**长期编译入口。
   - **工具链放哪**：若仓库已把 `trace_preprocessor.py` / `trace_utils.py` / `trace_collector.py` 等与编译脚本放在**同一可提交目录**（例如本仓库 `umdk/build/cam/comm_operator/`），hook 内用 `dirname "${BASH_SOURCE[0]}"` 解析到的目录调用即可，**无需**再 `bootstrap` 复制一份到别处，避免重复文件与路径漂移。仅当目标仓**没有**可提交的副本、且不希望把 `.py` 纳入版本库时，才用 `bootstrap_trace_toolchain.py` 拷到指定 `build_dir`。
   - **发现 build 目录**：在项目中搜索编译脚本（如 `compile*.sh`、`build*.sh`、`Makefile`、`CMakeLists.txt`），定位算子的 build 目录。常见位置如 `build/`、`scripts/` 等，不要假设目录名称。
   - **部署脚本（按需）**：无仓内副本时，运行 `bootstrap_trace_toolchain.py` 将下列脚本复制到目标 build 目录：`trace_preprocessor.py`、`trace_utils.py`、`trace_save.py`、`trace_collector.py`、`validate_trace_points.py`、`check_compile_safety.py`、`inspect_rank_pt.py`（以脚本内 `TOOLCHAIN_FILES` 为准）。
   - **接入编译**：运行 `patch_build_pipeline.py` 在**现有**编译脚本中注入预处理 hook；anchor 不匹配时，**手工**在同一脚本、同一相对顺序插入命令并加 `# TRACE_PREPROCESSOR_HOOK_START` / `END` 标记。
   - **校验部署**：运行 `verify_trace_scaffold.py` 确认脚本文件存在且编译 hook 已就位。
   - 不覆盖用户已有脚本；已存在时只做缺失补齐或可控更新。
   - **完整编译门禁**：工具链部署完成后，必须在实际使用的环境（容器 / CI / 本机）中执行**与团队一致的一条完整编译**（含算子包与 pybind，若项目如此组织）。仅「预处理成功」或仅步骤 5 通过，**不等于**产物可安装、可 import。常见工程问题见 [reference.md](reference.md)「编译与打包门禁」。

7. **Profile 测试脚本分叉（默认交付的组成部分；非「有空再做」）**
   - 与本段相关的交付门禁：**G4**（同步后落盘、collector 与 point_map 同源）、**G5**（`profiling_dir` 等 **`resolve()`**）。不满足则默认交付不完整。
   - **Python 面两种模式（勿混为一谈）**：
     - **模式 A（保持原返回值个数）**：图 / `op_host` 注册 **OPTIONAL** `profiling_data`（或等价名）时，公开 pybind 可仍只返回原先主输出；在 C++ 里通过 `aclnn*GetWorkspaceSize` 向 Inner 传入**空 optional / nullptr** 表示本次不采 profiling。原 UT、原 `torch.ops` arity **不变**。**注意**：一旦某算子在 OpDef 中将 `profiling_data` 标为 **REQUIRED**，则**禁止**再使用该 nullptr 路径，否则图语义、GE 绑定与设备参数不一致。
     - **模式 B（同一算子名、返回值 arity +1）**：在 **Op 注册名 / `torch.ops` 名与输入签名均不变** 的前提下，仅在**同一**算子名上扩展返回值（多一路 `profiling_data`）。**禁止**新增 **`xxx_profiling`**、**`*_with_profiling`** 等第二套算子或第二套 `torch.ops` 名（那是「另一个算子」，与本原则冲突）。调用方用 `..., _ = op(...)` 忽略最后一项即可保持业务逻辑不变；落盘与 Chrome 在**团队已有或本 skill 扩展的** `*_sample.py` 中用 **`--profiling_dir`**（写 `rank*.pt`）、可选 **`--point_map`** + **`--chrome_trace`**（spawn 结束后 **`subprocess`** 调 `trace_collector.py`）完成，避免再增 `run_*` / `*_profile.py` 整文件。
   - **多主输出算子：`profiling_data` 与主输出同等工程地位（REQUIRED 时强制契约）**  
     打点 / profiling 的 **GM 输出** 必须与**该算子全部主输出**在图与绑定上**同级**，不得单独做成「可选旁路」导致向设备传 `nullptr` 或与主输出参数生命周期不一致。设主输出共 **N** 路，profiling 为第 **N+1** 路 GM 输出（具体枚举名以 `op_host` 为准）。实现检查清单：
     1. **`op_host` OpDef**（`op_host/<op>.cpp` 或团队等价路径）：`Output("profiling_data")` 使用 **`ParamType(REQUIRED)`**，与主输出同级。
     2. **InferShape / InferDataType**（`op_host/<op>_infer.cpp` 等）：对 profiling 输出索引做与主输出相同的 **nullptr 门禁**；**始终**设置其维度与 dtype，不得依赖「可选输出可能不存在」分支。
     3. **pybind**（`pybind/<op>.cpp` 等）：**始终**分配并向 `aclnn<OpName>` / `EXEC_NPU_CMD` 传入 profiling 的 `at::Tensor`（与主输出同为实张量）。**禁止**用 `c10::nullopt`、环境变量等方式向设备侧传入「空 profiling GM」以规避绑定。
     4. **设备类 `Init`**（`op_kernel/<入口>.h`）：GM 形参顺序为 **主输出 1…N，再 `profiling_data`，再 `workspace`/tiling 等**——须与 OpDef / `aclnn` 一致（具体是否紧挨 workspace 以该算子既有约定为准，但**不得**与核入口乱序）。
     5. **`__global__` 核函数入口**（`op_kernel/<op>.cpp` 等）：与 OpDef / `Init` **同序**；改序后必须 **全量重编算子包 / OPP** 并做一次运行验证（plog 参数槽与 DFX），避免与旧二进制混用导致错参。
     6. **关闭设备侧 trace 写入**：通过 **`ENABLE_MOE_PROFILING`**（在 `<op>_base.h` 或团队等价 base 头）与**重编核**控制核内是否写入；**不要**依赖「不传 profiling 张量」——在 REQUIRED 契约下该做法非法且易与参数槽位/调试结论混淆。
   - **目的**：历史脚本若只解包前 N 个主输出，需在升级后改为多解包一位（可用 `_` 丢弃）；专门采集脚本显式接收 profiling 张量并 `save_profiling_data`。
   - **禁止**：为适配 profiling 在 **profile 用途之外** 把 `trace_utils` 硬塞进核心数值 UT 的主路径。原 UT 仍以数值断言为主；若必须兼容旧 arity，可在调用处用 `*head, _ = op(...)` 或固定长度解包。
   - **推荐（少新文件）**：在**原有** `examples/<op>_sample.py` 或团队 driver（非 pytest）中扩展：对 **`torch.ops...<原算子名>(...)`** 使用 `len(outs)` 分支，向 `forward` 返回元组**末尾**附带 `profiling`（或 `None`）；`__main__` 增加 profiling / trace 相关 CLI；子进程内 `save_profiling_data`，父进程在 `mp.spawn(..., join=True)` 之后可用 `subprocess` 调用 `trace_collector.py`。**算子名与接口名不变**；**不要**注册 **`xxx_profiling`** / **`xxx_with_profiling`**。若仅有 pytest UT、无 sample，再在**同一份** `test_<op>.py` 里增加辅助函数（仍优于新建整文件副本）。
   - **命名与位置**：优先改现有 `*_sample.py` / 团队已有 driver；确需 pytest 专用断言时再在同一目录的 `test_<op>.py` 内加函数，避免另建 `test_<op>_profile.py` 除非团队明确要求分拆文件。
   - **必改内容**：
     - 对主入口 `torch.ops.<lib>.<op>(...)` 在 **`len(outs)`** 上兼容「旧 arity / 新 arity（多一路 profiling）」；最后一项为 profiling 时参与落盘。
     - 封装算子的 `nn.Module` 的 `_apply_ops` 若把 profiling 传到 `forward`，下游解包须与元组长度一致；数值对拍仍只比较主输出，可用 `_` 忽略 profiling。
     - **SmallOps / 对照路径**：baseline 不返回 profiling 时保持原元组长度不变；带 profiling 的路径在对比时只对主输出子集 `assert_close`。
   - **与工具链对接**：`build/.../trace_utils.py` 的 `save_profiling_data`；**模式**为：若设 **`--profiling_dir`**，在 **`torch_npu.npu.synchronize`（或等价）之后** 再 `save_profiling_data`；`__main__` 在 **`--profiling_dir` 且 `--point_map`** 时用 **`subprocess`** 调用 **`trace_collector.py`** 写 **`chrome_trace.json`**（输出路径可用 **`--chrome_trace`**）。**本仓库**可在 `umdk/src/cam/examples/` 下查找已接入上述 CLI 的 sample 作参照（文件名随算子而变）。
   - **无 NPU 静态校验**：可在 sample 或 UT 中增加 **`--trace_checks`**（或等价入口），内部调用 `validate_trace_points.py` 与 `check_compile_safety.py`，脚本路径优先解析到仓内已提交的 `comm_operator` 工具链目录。
   - **`trace_utils` 导入**：将含 `trace_utils.py` 的目录加入 `sys.path` 后再 `import`；目录不存在时打印提示并跳过（见 sample 实现）。
   - **环境说明**：`save_profiling_data` 的 `base_h_path` 指向 `<op>_base.h`（`ENABLE_MOE_PROFILING` / `PROF_SIZE_PER_CORE`）；sample 默认尝试仓库内相对路径。
   - **pytest**：无单独 `test_*_profile.py` 时，在 **`test_<op>.py`** 内增加无 NPU 的校验函数即可。


## 命名规则

- 通用根标签固定为 `processing`。
- 阶段标签必须从当前算子语义中提取。
- 标签采用 **空格分隔的层级路径**，前缀表示所属阶段，后缀表示具体子阶段。例如 `"dispatch-phase1 aic"` 表示「dispatch-phase1」主阶段下 AIC 分支。
- 名称描述"做什么"，不要过度绑定实现细节。
- 在语义不变时，尽量保持命名稳定。

示例（名称仅示意，须与当前算子真实阶段一致）：
- `processing`
- `dispatch-phase1`
- `dispatch-phase1 aic`、`dispatch-phase1 aiv`
- `dispatch-phase1 moe-process`（带 groupIdx）
- `dispatch-phase1 wait-token`（带 groupIdx）
- `combine-phase block-epilogue waiting`（带 stageId）
- `combine-phase block-epilogue calc`（带 stageId）
- `combine-phase combine-send`、`combine-phase combine-recv`

## 详细参考

以下已移至 [reference.md](reference.md)：MoeTracing 模板与缓冲区、Profiling 搬运规格、infer 与 pybind 对齐、编译与打包门禁、打点密度、`trace.json` 四步流程、`point_map` 契约、固定脚本一览与示例命令、常见陷阱。

执行本 skill 时以门禁与上文「必须执行的流程」为准；需要完整样板代码或大表时展开 `reference.md`。

## 输出约定

完成后回复中**必须**包含：

**门禁对照（默认范围）**  
- 用 **G1–G5** 逐条声明 **已满足 / 未满足**；未满足须写原因与用户需补动作。

**技术与结果**  
- 插桩修改的文件列表（含 **`op_kernel/` 子树**，不仅是入口壳子）。
- 最终点位层级（L1 为 `processing`；合并关系可简述）。
- `validate_trace_points.py` 与 `check_compile_safety.py` 结果（或说明为何目标仓未跑）。
- **全链路改动摘要**：至少列出 **`op_host` / infer / tiling / 核入口 / pregen `aclnn_*` / pybind** 中是否已对齐 **G2**（profiling 最后一路、顺序一致）。
- 工具链：hook 所在脚本、`point_map.json` 典型路径形态；若 bootstrap 了哪些文件到 build 目录。
- **步骤 7**：改动的 **`examples/*_sample.py` / `test_*.py` 路径**；是否 **`synchronize` → `save_profiling_data`**；Chrome 是否 **`trace_collector` + 同源 point_map**；**路径是否已 `resolve()`**（G5）。
- **UMDK**：wheel 路径 **`umdk/output/cam/comm_operator/dist/`**、安装命令；**`libcam.so`** / **返回值个数** 见 [reference.md](reference.md)「编译与打包门禁」。
- 生成 **`chrome_trace.json`** 的命令行示例（参数用真实形态，避免 `/path/to` 占位误导）。

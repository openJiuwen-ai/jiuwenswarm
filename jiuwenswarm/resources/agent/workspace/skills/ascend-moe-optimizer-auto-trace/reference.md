# 昇腾算子自动打点 · 详细参考

本文件是 [SKILL.md](SKILL.md) 的延伸：MoeTracing 与 Profiling 规格、编译门禁、打点密度、`trace.json` 与 `point_map` 契约、常见陷阱及固定脚本清单。门禁 G1–G5 与「必须执行的流程」仍以 SKILL 正文为准。

**步骤编号**：下文出现的「步骤 1–7」「步骤 5」「步骤 6」「步骤 7」等，若无特别声明，一律指 **[SKILL.md](SKILL.md)** 中「必须执行的流程」的同名步骤。

## Skill 根目录与本仓库路径

下文中的 `<skill_root>` 表示与本 `SKILL.md` 同级目录。**本仓库**（workspace 根下常见相对路径）示例：`jiuwenswarm/resources/agent/workspace/skills/ascend-moe-optimizer-auto-trace/`。

## MoeTracing 运行时规格

MoeTracing 不是简单的空宏。当项目的 base 头文件中缺少 MoeTracing 定义时，必须按以下规格在算子已有的 `_base.h` 文件中补齐（不要新建单独的头文件）。

### 宏定义

```cpp
#define ENABLE_MOE_PROFILING 1
#define PROF_SIZE_PER_CORE 2048
#define ENABLE_MOE_PROFILING_BARRIER true
```

### per-core profiling buffer 指针

每个核拥有独立的 profiling buffer，通过 block-local 指针访问：

```cpp
__BLOCK_LOCAL__ __inline__ int64_t* g_moeProfilePtr;

__aicore__ inline int64_t* GetMoeProfilePtr(uint32_t idx = 0)
{
    return &g_moeProfilePtr[idx];
}
```

如果算子存在 AIC/AIV 分核编译（`SPLIT_CORE_CUBE` / `SPLIT_CORE_VEC`），需要为每种核类型声明独立的指针变量（`g_moeProfilePtrCube` / `g_moeProfilePtrVec`），并在 `GetMoeProfilePtr()` 中根据编译宏选择。

### MoeTracing 函数实现

MoeTracing 是 **模板函数**，不是宏。模板参数 `sync` 控制是否在记录前插入 `PipeBarrier<PIPE_ALL>()`：

```cpp
template <bool sync = ENABLE_MOE_PROFILING_BARRIER>
__aicore__ inline void MoeTracingWithCycle(int64_t data, int64_t cycle)
{
#if ENABLE_MOE_PROFILING
    if constexpr (sync) {
        AscendC::PipeBarrier<PIPE_ALL>();
    }
    int64_t *profileData = GetMoeProfilePtr();
    profileData[profileData[0]++] = data;
    profileData[PROF_SIZE_PER_CORE - profileData[0]] = cycle;
#endif
}
```

Buffer 布局：`profileData[0]` 是写入索引，**正向写 point_id 数据，反向写 cycle 时间戳**。

### 三种调用重载

```cpp
// 基础调用：记录 point_id + 当前 cycle
template <bool sync = ENABLE_MOE_PROFILING_BARRIER>
__aicore__ inline void MoeTracing(int64_t data)
{
    MoeTracingWithCycle<sync>(data, AscendC::GetSystemCycle());
}

// 带索引：将 index 编码到 data 高 32 位，用于区分不同 expert group / stage
template <bool sync = ENABLE_MOE_PROFILING_BARRIER>
__aicore__ inline void MoeTracing(int64_t data, uint32_t index)
{
    MoeTracing<sync>(data | (int64_t)(((uint64_t)index) << 32));
}

// 带 extraId + index：用于同时传递 stageId 和循环索引
template <bool sync = ENABLE_MOE_PROFILING_BARRIER>
__aicore__ inline void MoeTracing(int64_t data, uint32_t extraId, uint32_t index)
{
    MoeTracing<sync>(data, (extraId | (index << 8)));
}
```

### 调用示例

```cpp
// 基础打点（前缀/后缀随算子语义命名，下为示意）
MoeTracing(TRACE_POINT("dispatch-phase1 aic", "B"));

// 带 groupIdx（区分不同 expert / tile 组）
MoeTracing(TRACE_POINT("dispatch-phase1 moe-process", "B"), 0, groupIdx);

// 强制 barrier 后再记录（覆盖默认 sync 参数）
MoeTracing<true>(TRACE_POINT("combine-phase combine-barrier-all", "E"));
```

命名规则与标签示例见 [SKILL.md](SKILL.md)「命名规则」，此处不重复。

## Profiling 数据搬运规格

打点数据写入 per-core 栈上 buffer 后，需要一条完整链路将其搬到 Host 侧。本 skill 要求在算子框架上**显式新增一个 profiling 输出 tensor**，而不是复用已有输入 tensor 的 GM 地址。**默认交付（G2）**：该输出在 **Op 注册的所有 Tensor 输出中排在最后**（第 `N+1` 路）。**ParamType** 可为 OPTIONAL（模式 A）或 REQUIRED（模式 B / 强制采数）；下文代码片段用 OPTIONAL 仅为示意语法，**位次规则不因 OPTIONAL/REQUIRED 改变**。Python 侧 **「图多一路 optional」 vs 「返回值多一项」** 见 [SKILL.md](SKILL.md) 步骤 7。

### 1. 算子框架层：新增 profiling 输出（在既有 output 之后多注册一个）

在 `op_host` 算子定义中在**全部主输出之后**再注册 profiling（示意可为 OPTIONAL，实际以工程与模式为准）：

```cpp
// op_host/<op>.cpp — 算子注册
this->Output("profiling_data")
    .ParamType(OPTIONAL)
    .DataType({ge::DT_INT64, ge::DT_INT64, ge::DT_INT64, ge::DT_INT64})
    .Format({ge::FORMAT_ND, ge::FORMAT_ND, ge::FORMAT_ND, ge::FORMAT_ND})
    .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND, ge::FORMAT_ND, ge::FORMAT_ND});
```

在 `op_host/<op>_infer.cpp` 中推导 shape：

**`totalCores` 必须等于 AIC 核数 + AIV 核数，不能只取其中一种。** kernel 侧 buffer 布局为：AIC 核写 `[0, aicNum)` 区间，AIV 核写 `[aicNum, aicNum + aivNum)` 区间。如果 `totalCores` 只取了 AIC 数量，AIV 核的写入会越界。

**禁止硬编码核数。** 不同硬件型号的核数不同（如 24C+48V=72、20C+40V=60），硬编码任何具体数字都会在其他型号上出错。正确做法按优先级：

1. **tiling 函数**（推荐）：通过 `platform_ascendc::PlatformAscendC(context.GetPlatformInfo())` 获取 `GetCoreNumAic()` 和 `GetCoreNumAiv()`，已有的 tiling 流程通常已包含此逻辑。
2. **kernel 入口**：通过 `AscendC::GetBlockNum()` 获取实际 AIC 核数，AIV 核数 = `GetBlockNum() * GetSubBlockNum()`（1C2V 下 SubBlockNum=2）。
3. **infer / pybind 侧**：`InferShapeContext` 没有平台查询 API，**不能在 infer 里读到实机的 `GetBlockNum()`**，只能写一个**对运行时 `GetBlockNum()` 的上界**来定 profiling 一维长度。对 **`KERNEL_TYPE_MIX_AIC_1_2`**，kernel 里逻辑槽数约为 **`GetBlockNum() * (1 + GetSubBlockNum())`**（常见 1C2V：`SubBlockNum=2` ⇒ **每路 AIC 组对应 3 个槽**）。这与「物理上有多少颗 Cube」不是同一个数：例如单卡 **24 Cube + 48 Vector** 时，若运行时 `GetBlockNum()` 为 24，则只需 **72** 个槽；历史上若误把「槽数上界」当成「只有 Cube 数」、且该数 **小于** `3 * GetBlockNum()`，AIV 侧 `(GetBlockNum() + GetBlockIdx())` 才可能越界。工程内常量如 **`MAX_INFER_GETBLOCKNUM_UB`** 是对 **`GetBlockNum()` 的 infer 上界约定**，须与 **pybind 分配的元素个数**一致，**不是**从硅片规格直接读出的核数；常见写法 `MAX_PROFILING_CORE_SLOTS = MAX_INFER_GETBLOCKNUM_UB * MIX_AIC_1_2_SLOTS_PER_GROUP`（系数随核类型而变）。

```cpp
// op_host/<op>_infer.cpp — infer 上界（命名与工程内已有算子对齐即可）
constexpr uint32_t MAX_INFER_GETBLOCKNUM_UB = 128;
constexpr uint32_t MIX_AIC_1_2_SLOTS_PER_GROUP = 3;
constexpr uint32_t MAX_PROFILING_CORE_SLOTS = MAX_INFER_GETBLOCKNUM_UB * MIX_AIC_1_2_SLOTS_PER_GROUP;
gert::Shape *profilingShape = context->GetOutputShape(OUTPUT_PROFILING_DATA);
profilingShape->SetDimNum(1);
profilingShape->SetDim(0, MAX_PROFILING_CORE_SLOTS * PROF_SIZE_PER_CORE);
context->SetOutputDataType(OUTPUT_PROFILING_DATA, ge::DT_INT64);
```

```cpp
// pybind — 模式 B：元素个数与 infer 完全一致；每槽 PROF_SIZE_PER_CORE 如 2048
constexpr int64_t kProfilingElems = static_cast<int64_t>(MAX_PROFILING_CORE_SLOTS) * PROF_SIZE_PER_CORE;
at::Tensor profilingData = at::zeros({kProfilingElems}, opts.dtype(at::kLong));
return {mainOut0, mainOut1, profilingData};  // 主输出个数因算子而异；须与 infer 元素个数一致
```

### 1.1 `aclnn` 外层包装与 `aclnnInner_*`（自动生成）的签名对齐

在 `op_host` 中**增加、删除或调整任一 Output（含 OPTIONAL）** 后，工具链生成的 **`build_out/autogen/aclnnInner_<Op>*.h/.cpp`** 中 `aclnnInner<Op>GetWorkspaceSize` / `aclnnInner<Op>` 的参数列表会随之变化。

若仓库中另有**手写维护**的对外封装（常见于 `pregen/build_out/autogen/aclnn_<op>.h`、`aclnn_<op>.cpp`，或等价路径），其形参顺序与类型必须与 **当前** `aclnnInner_*` **逐参一致**（含 optional profiling 的 `const aclTensor*` 等），否则 **`cust_opapi` 等目标会在完整编译阶段才报错**，`check_compile_safety.py` 未必覆盖。

**交工前自检**：改完 `op_host` / `infer` 后，打开最新一次 msopgen 或编译产物中的 `aclnnInner_*` 声明，与 `pregen/.../aclnn_*.h` 中 `aclnn<Op>GetWorkspaceSize` 对比；外层实现应只做薄转发（含将 optional 原样传入 Inner）。

```cpp
// 示意：Inner 已含 profilingDataOutOptional 时，外层必须多传一格再接到 workspaceSize
return aclnnInnerMyOpGetWorkspaceSize(/* ... */, lastMainOutputOut,
    profilingDataOutOptional, workspaceSize, executor);
```

### 2. Kernel 入口（`.cpp`）：buffer 初始化 + 搬出

在 kernel 入口函数中，算子执行**前后**分别处理 profiling buffer：

**执行前**——在栈上分配 buffer、初始化写索引和起始时间戳、设置指针：

```cpp
#if ENABLE_MOE_PROFILING
    int64_t profData[PROF_SIZE_PER_CORE];
    profData[0] = 1;
    profData[PROF_SIZE_PER_CORE - 1] = AscendC::GetSystemCycle();
    SetMoeProfilePtr(&profData[0]);
#endif
```

**执行后**——将栈上 buffer 逐条写到 profiling output tensor 的 GM 地址：

```cpp
#if ENABLE_MOE_PROFILING
    AscendC::GlobalTensor<int64_t> profGlobal;
    profGlobal.SetGlobalBuffer((__gm__ int64_t *)(profiling_data));
    // AIC 核写前半段，AIV 核写后半段
    AscendC::GlobalTensor<int64_t> coreGlobal;
    if (g_coreType == AscendC::AIC) {
        coreGlobal = profGlobal[AscendC::GetBlockIdx() * PROF_SIZE_PER_CORE];
    } else {
        coreGlobal = profGlobal[(AscendC::GetBlockNum() + AscendC::GetBlockIdx()) * PROF_SIZE_PER_CORE];
    }
    for (unsigned i = 0; i < profData[0]; ++i) {
        coreGlobal(i) = profData[i];
        coreGlobal(PROF_SIZE_PER_CORE - i - 1) = profData[PROF_SIZE_PER_CORE - i - 1];
    }
    // DataCacheCleanAndInvalid 确保 host 可读
#endif
```

辅助函数 `SetMoeProfilePtr` 的定义放在 `.cpp` 入口文件中，根据分核编译宏选择正确的 block-local 指针：

```cpp
__aicore__ inline void SetMoeProfilePtr(int64_t *profilePtr)
{
#if __CCE_AICORE__ == 220 || defined(__DAV_C310__) || defined(__DAV_310R6__)
#ifdef SPLIT_CORE_CUBE
    g_moeProfilePtrCube = profilePtr;
#elif defined(SPLIT_CORE_VEC)
    g_moeProfilePtrVec = profilePtr;
#else
    g_moeProfilePtr = profilePtr;
#endif
#else
    g_moeProfilePtr = profilePtr;
#endif
}
```

### 3. Host 侧（Python）：读取 + 保存

`trace_utils.py` 中 `save_profiling_data` 需要：
- 从 `_base.h` 读取 `PROF_SIZE_PER_CORE` 与 `ENABLE_MOE_PROFILING`（若实现里提供 `base_h_path` 参数，**新算子应传入当前算子的 `<op>_base.h` 绝对路径**，避免工具链内写死的相对路径仍指向示例算子）。
- 将 profiling tensor reshape 为 `(total_cores, PROF_SIZE_PER_CORE)`；`get_core_num_list()` 等若仍为示例中的硬编码或环境变量，需与目标硬件/tiling 一致，否则分组索引会越界或切分错误。
- 按 AIC/AIV 核类型分组（考虑 1C2V 映射）
- 保存为 `rank{id}.pt` 供后续解析工具使用

```python
from pathlib import Path
import trace_utils

profiling = profiling_data.cpu()
out_dir = str(Path("./prof_out").resolve())  # 第三参为输出目录，须绝对路径（见 G5）
op_base_h = Path("/repo/.../src/.../<op>_base.h").resolve()
trace_utils.save_profiling_data(profiling, rank_id, out_dir, base_h_path=str(op_base_h) if op_base_h.is_file() else None)
```

### 3.1 Pybind：`EXEC_NPU_CMD` 与 optional 参数的左值约束

若 pybind 通过 `EXEC_NPU_CMD(aclnnXxx, ...)` 调用 `aclnnXxxGetWorkspaceSize`，宏内部通常会对实参做 `ConvertTypes(...)` 一类展开，**要求可绑定到非 const 左值引用**（具体以项目内 `pytorch_npu_helper.hpp` 为准）。

因此向 `aclnn*GetWorkspaceSize` 多传一个 **optional profiling tensor** 时：

- **禁止**写成 `c10::optional<at::Tensor>()` 等**纯右值**直接塞进宏参数列表（典型编译错误：无法绑定到 `optional&`）。
- **应**在宏外声明具名变量，例如 `c10::optional<at::Tensor> profilingDataOptional;`（默认不采），再传入 `EXEC_NPU_CMD(..., profilingDataOptional)`；若本次要采 profiling，则先对该变量赋值再调用。

模式 A（见 [SKILL.md](SKILL.md) 步骤 7）下常用「空 optional + 原 return 个数不变」；模式 B 再与「多返回一个 `at::Tensor`」的 pybind 示意配合。

## 编译与打包门禁（工程侧）

本节与打点语义无关，但为「[SKILL.md](SKILL.md) 步骤 6 + 完整编译」中反复出现的工程问题；不同仓库脚本名可能不同，以实际 `compile*.sh` / `build.sh` 为准。

- **CANN / msopgen 须在 PATH 中**：`msopgen`、`ccec` 等通常依赖 `source ${ASCEND_HOME_PATH}/bin/setenv.bash`（或项目规定的 setenv）。在 **docker exec 非登录 shell**、CI 裸 `bash -lc` 等场景下，若编译脚本先调用 `msopgen` 再 source，会导致 **`msopgen: command not found`**；应在**首次**调用 `msopgen` **之前**注入环境（由项目统一改 `compile_ascend_proj.sh` 等，或由执行者在同一 shell 中先 source）。
- **源码属主与构建用户**：`msopgen` 可能对输入 JSON 做「当前用户须为文件 owner」校验。容器内若以 **root** 编译、仓库挂载为普通用户属主，会报错；应以与挂载卷**一致的用户**（如 `docker exec --user <uid>:<gid>`）执行编译，或按团队规范在镜像内对齐属主。
- **`AddCustom.json` 与 `msopgen`（UMDK 实践）**：`msopgen gen -i .../AddCustom.json` 可能报 **`You are not the owner of path ...`**。本仓库在 **`umdk/build/cam/comm_operator/compile_ascend_proj.sh`** 中于 **`msopgen` 之前** 对 **`./ascend_kernels/AddCustom.json`** 尝试 **`chown $(id -u):$(id -g)`**，失败则 **`sudo chown`**。若以 **root** 成功 `chown`，该文件在工作区可能变为 **root 属主**；若希望挂载卷仍归开发者，优先 **`docker exec -u <与卷一致的 uid>`** 跑整条编译，或事后 **`chown` 回开发用户**。
- **`build_out` 清理与占位目录**：部分 msopgen 工程的 `build.sh` 会对 `build_out` 做 `rm -rf build_out/*` 后再 `cmake --build`。若 CPack / `cmake_install.cmake` 仍引用 **`op_kernel/binary/config/`** 等路径，而工具链未生成该目录，会在 **package** 阶段失败；可在 **`--target binary` 之后、`package`（或等价）之前** 由项目脚本 `mkdir -p` 占位（空目录即可），具体路径以生成工程为准。
- **门禁顺序**：工具链部署（[SKILL.md](SKILL.md) 步骤 6）→ **完整编译通过**（算子包 + 若有的 pybind wheel）→ 再视情况跑 [SKILL.md](SKILL.md) 步骤 7 / 设备侧 UT。勿将「仅 validate / check_compile_safety 通过」误认为已满足交付。

### UMDK `comm_operator`：pybind whl 标准产物路径（勿默认写 `/tmp`）

与 **`umdk/build/cam/comm_operator/build_pybind.sh`** 一致，wheel 输出目录为 **`${MODULE_BUILD_OUT_PATH}/dist`**，即仓库内：

- **`umdk/output/cam/comm_operator/dist/`** — 成功构建后在此生成 **`umdk_cam_op_lib-*.whl`**。

**推荐命令**（在 **`umdk/build/cam`** 下，仅编 pybind、不跑算子 `msopgen`）：

```bash
./build.sh comm_operator -p
```

安装：

```bash
pip install --force-reinstall umdk/output/cam/comm_operator/dist/umdk_cam_op_lib-*.whl
```

手工执行 `python3 setup.py bdist_wheel` 时，**`--dist-dir` 应指向上述 `dist`（可先 `mkdir -p`）**，**不要**随意写到 **`/tmp`**，以免与 CI、文档和归档路径脱节。

算子 OPP **`.run`** 由 **`compile_ascend_proj.sh`** 等完整算子链路生成，通常落在 **`umdk/output/cam/comm_operator/run/`**（如 **`CAM_ascend910_93_debian_aarch64.run`**，SOC 名随 `-c` 变化）。**whl 与 `.run` 需分别安装**；仅升级 whl 而不升级已装 OPP 时，注意版本是否匹配。

### `import umdk_cam_op_lib`：`libcam.so` 与 Ascend / 驱动库

部分环境打出的 **`umdk_cam_op_lib*.so`** 在 ELF **`DT_NEEDED`** 中会依赖 **`libcam.so`**（CAM host 侧产物）。若运行时 **`LD_LIBRARY_PATH`** 未包含其所在目录，会报 **`ImportError: libcam.so: cannot open shared object file`**。

- 将含 **`libcam.so`** 的目录加入 **`LD_LIBRARY_PATH`**（常见为各团队 **`comm_operator` host 编译输出目录**，例如部分树布局下的 **`umdk/src/cam/comm_operator/build`**，以实际产物为准）。
- **本仓库部分示例**在模块加载时调用 **`_prepend_cam_op_native_lib_path()`** 一类辅助：支持环境变量 **`UMDK_CAM_NATIVE_LIB_DIR`**，并在 **`import torch_npu` / `import umdk_cam_op_lib` 之前** 写入 **`LD_LIBRARY_PATH`**（同一进程内、在扩展被 `dlopen` 前生效）；其它仓库按既有 driver 方式处理依赖路径即可。
- **`torch_npu`** 另需 Ascend CANN **`.../aarch64-linux/lib64`** 及 **`/usr/local/Ascend/driver/lib64`**（及常见子路径 **`.../driver/lib64/driver`**）等；**`docker exec` 非登录 shell** 若未继承镜像登录环境，需显式 **`export LD_LIBRARY_PATH=...`** 或与 **`${ASCEND_HOME_PATH}/bin/setenv.bash`** 一致。

### Python / `torch.ops`：模式 B 下返回值个数升级

- **模式 B**：pybind 在**同一算子名**上较旧版 **多返回一路 profiling tensor**（ arity = 原主输出数 + 1）。
- **旧 whl** 仍为旧 arity 时，若写死新长度解包会报错。处理：**重装**与当前 `pybind` / `op_host` 一致的 whl；或在调用处对 **`len(outs)`** 分支兼容（见 [SKILL.md](SKILL.md) 步骤 7 与团队 sample），并在 **rank0** 提示需升级 whl。

### 端到端 profiling + Chrome（UMDK 可参考；其它仓替换为各自的 sample/driver）

- **通用约定**（路径、sync、`point_map`）见上文 **「point_map 与 Chrome 解析契约（通用）」**。
- **本仓库**：在 **`umdk/src/cam/examples/`** 下选择**已接入 profiling** 的 `*_sample.py`（非 pytest；常与数值对拍同文件）；典型 CLI：**`--profiling_dir`**（输出目录，内含 `rank*.pt`）、**`--point_map`**（**真实路径**的 `point_map.json`，与当次编译 OPP 同源）、可选 **`--chrome_trace`**。`trace_utils` / `trace_collector` 由 sample 内嵌路径解析到 **`umdk/build/cam/comm_operator`**；`save_profiling_data` 的 **`base_h`** 指向对应 **`<op>_base.h`**。具体默认路径以该 sample 文件头注释为准。
- **`MOE_USE_1C2V=1`** 时 **`trace_utils.get_core_num_list()`** 为 **`[24,24,24]`**，否则常见为 **`[24,48]`**；与硬件/核映射解读需一致。

## 打点密度与均匀性要求

- **目标标签数（按核类型分别统计）**：对 **AIC 与 AIV 各自**，在「该核实际会执行到的代码路径」上，应能观察到大约 **15～20 个不同的语义阶段名**（即互不相同的 `TRACE_POINT` 标签字符串个数，**不是**全算子 AIC+AIV 混在一起凑总数）。过少（例如某一核类型上**少于 10 个**）不利于看子阶段瓶颈；过多（例如**多于 30 个**）易占满 buffer 且 trace 难读。
- **均匀性**：按**当前算子**的真实主阶段划分（名称随算子语义而定，如 dispatch、多段 matmul、combine、量化等），各主阶段下的子标签数量应**大致均衡**。若某一主阶段已有多个子点位，而另一主阶段在对应核上仍只有入口/出口两点，说明后者打点不足，应深入该阶段所在实现（含分核 `operator()` 内部）补充子阶段。
- **"函数级粒度"的正确理解**：指每个有独立语义的阶段函数（如 `SendCoreFunc`、`RecvCoreFunc`、`CompCoreFunc`、`UpdateAndCleanInfo`），不是仅限于调用链第一层入口的 `operator()`。`operator()` 内部如果有多个语义明确的子函数调用，每个都应该有独立的 B/E 点位。
- **二级拆分**：即使一个子函数已经有了 B/E 点位（如 `某阶段 aiv send`），如果其内部仍有语义可分离的子阶段（如 count-prep vs token-DMA、spin-wait vs data-copy），也应在函数内进一步拆子标签（如 `… aiv send-count` + `… aiv send-token`）。典型的可拆分模式包括：
  - **count/status 广播** vs **payload 数据搬运**（dispatch、recv）
  - **spin-wait/polling** vs **实际计算或搬运**（recv-count、group-wait）
  - **shared expert** vs **routed expert** 的独立执行路径
  - **metadata load**（index/scale DataCopyPad）vs **per-token reduce 循环**（combine local-copy）
- **AIV 角色分工**：对于 1C2V 等混合核模式，`operator()<AIV>()` 内部可能通过 `aivIdx`、`GetSubBlockIdx()` 或角色标志（`isSendCore`、`isRecvCore`、`isCompCore`）将不同 AIV 核分配到不同工作路径。每种角色的主要工作阶段都需要独立打点，让 trace 中能区分各类 AIV 核的时间分布。
- **多变体对齐**：如果同一算子有多个 kernel 变体（如 deep-fuse vs shallow-dispatch），所有变体的 AIV `operator()` 都应该有相似粒度的子阶段标签。不能一个变体有 8 个子标签而另一个只有入口/出口。
- **自检方法**：打点完成后，**分别**列出 AIC 与 AIV 在各自可达路径上出现的**不同**标签名集合并计数。若某一核类型明显低于上述量级，或某一主业务阶段在该核上仍只有一对 B/E 而无子阶段，则须继续补充（优先大块实现头文件中的阶段边界，见 [SKILL.md](SKILL.md)「插桩覆盖必达清单」与「必须执行的流程」步骤 4）。

## 容量与扰动约束

- 每核 profiling buffer 容量有限（`PROF_SIZE_PER_CORE`），禁止默认高密度铺点。
- 不要默认给每个小 helper 或最内层循环都加点。
- 优先保证可读性与稳定定位瓶颈能力，而不是追求全覆盖。

## 常见陷阱（快速自检）

- **因「看起来像数学库/大块计算实现」而整文件跳过**：子目录或文件名**不能**作为免打点依据；凡含 **分核 `operator()<AIC/AIV>`（或等价阶段入口）** 且属于主流程的实现头文件，必须与 tile 内层区分并打点（见 [SKILL.md](SKILL.md) 步骤 4）。
- **大块实现头未打点**：主耗时往往在 **`#include` 子树**的 workspace / kernel / gemm / epilogue 模板 **`operator()`** 内；仅打外层调度头会导致 trace 看不到真实子阶段——属**高频遗漏**，交工前按 [SKILL.md](SKILL.md) 自维护表中 **「大块实现 / `#include` 子树」** 与 `grep` 自检；若仓库有对照树可 diff，**交工以当前构建树为准**。
- **`aclnnInner_*` 已变、手写 `pregen/.../aclnn_*.cpp` 未改**：`op_host` 增删 output 后 Inner 签名已更新，外层仍少传 / 错传 `profilingDataOptional` 等参数 → **`cust_opapi` 编译失败**；见上文「Profiling 数据搬运规格」小节 1.1 交工前自检。
- **`EXEC_NPU_CMD` 传入 `optional` 临时量**：见上文「Profiling 数据搬运规格」小节 3.1，须使用具名 `c10::optional<at::Tensor>` 变量。
- **infer/pybind 硬编码核数**：用安全上界或动态逻辑；与 kernel 侧 per-core 写入区间一致。
- **Python 解包 arity**：仅在使用**模式 B**（[SKILL.md](SKILL.md) 步骤 7）时 fusion / profile 脚本比原先多接一个 profiling 张量；原 UT 不解包改时拷贝为 `test_<op>_profile.py`。模式 A 下原 UT arity 不变。
- **`trace_utils` 静默不落盘**：`_base.h` 路径不对或 `ENABLE_MOE_PROFILING` 为 0；优先检查 `base_h_path` 与宏。
- **`save_profiling_data` 的相对路径与 cwd 不一致（高频误导）**：`trace_utils.save_profiling_data(..., output_dir)` 若 **`output_dir` 为相对路径**，实现会拼到 **`trace_utils.py` 所在目录**（常为 `build/cam/comm_operator`），**不是** shell 的当前工作目录。表现为：日志里 `Saved: .../comm_operator/.../rank*.pt`，而 `trace_collector` 或用户在 **`examples/`** 下传的 `./prof_out` 为空 → **No rank\*.pt**。**修复**：sample/driver 在 spawn 前将 **`profiling_dir` / `chrome_trace` / `point_map` 设为 `Path(...).resolve()` 绝对路径**，或调用方始终传绝对路径。
- **工具链未部署仍以为能出 trace**：[SKILL.md](SKILL.md) 步骤 6 未完成则没有预处理后的 `point_map.json` 与可复现的 point_id。
- **`point_map` 路径错误或占位符**：`load_mapping` 为空 → 全记录跳过；**错用旧工程 / 他机拷贝的 `point_map.json`** → `skipped_no_mapping` 极高，见上文「point_map 与 Chrome 解析契约」。
- **sync 前落盘 profiling**：见上文「Host 侧何时保存 profiling tensor」；与 map 错配症状不同（前者常表现为 pt 空或 counter≤1，后者 pt 正常但 decode 全跳过）。
- **未跑完整编译即认为可交付**：[SKILL.md](SKILL.md) 步骤 5 与静态脚本不覆盖 autogen / pybind / CPack 全链路；须满足上文「编译与打包门禁」。

## trace.json 生成流程

打点数据的完整处理链路（从设备到可视化）分 4 步：

### Step 1: 预处理（编译前）

`trace_preprocessor.py` 扫描源码，将 `TRACE_POINT("label", "B/E")` 替换为唯一整数 point_id，生成 `point_map.json`：

```bash
python <skill_root>/scripts/trace_preprocessor.py <operator_src_dir> <output_dir> --modify
```

输出 `point_map.json` 格式：
```json
{
  "points": {
    "1": {"label": "processing", "event_type": "B", "file": "...", "line": 415, "event_id": 1},
    "2": {"label": "dispatch-phase1 aic", "event_type": "B", ...}
  }
}
```

### Step 2: 运行算子采集 profiling tensor

算子执行后，Host 侧获取 profiling output tensor（通常为**最后一个** output，即比插桩前多出来的那一个），调用 `trace_utils.save_profiling_data` 拆分保存：

```python
import trace_utils
from pathlib import Path

profiling = profiling_data.cpu()
out_dir = str(Path("./prof_out").resolve())  # 须绝对路径；勿传未 resolve 的 "./xxx"
op_base_h = Path("/abs/or/repo/path/to/<op>_base.h").resolve()
trace_utils.save_profiling_data(profiling, rank_id, out_dir, base_h_path=str(op_base_h) if op_base_h.is_file() else None)
```

也可离线保存：
```bash
python <skill_root>/scripts/trace_save.py raw_profiling.pt --rank 0 --output profiling_data
```

### Step 3: 生成 Chrome Trace JSON

`trace_collector.py` 读取所有 `rank*.pt` + `point_map.json`，解析 64 位组合 ID（低 32 位 point_id + 高 32 位 extra_id），配对 B/E 事件，生成 Chrome Trace 格式：

```bash
python <skill_root>/scripts/trace_collector.py profiling_data point_map.json -o chrome_trace.json
```

支持参数：
- `--clock-divisor 50.0`：时钟频率 MHz（cycle → us 换算）
- `--extra-mode seq`：extra_id 解析模式（`seq`=高 24 位序号+低 8 位 extra，`legacy`=整体使用）
- `--depth 0`：区间深度过滤（0=全部，1=仅叶子，2=叶子+父层）

### Step 4: 可视化

在 Chrome 浏览器打开 `chrome://tracing`，加载生成的 `chrome_trace.json`。
每个 rank 对应一个 process，每个核（AIC/AIV × core_id）对应一个 thread。

### point_map 与 Chrome 解析契约（通用）

本节与**具体算子名**无关；任意昇腾算子只要走 `TRACE_POINT` → 预处理器 → 设备写整型 ID → `trace_collector` 解码，均适用。

#### `point_map.json` 是什么、生成在哪里

- `trace_preprocessor.py` 扫描**参与当次编译的那份源码树**（常为 msopgen `copy_ops` 之后的生成目录），将 `TRACE_POINT("label","B"|"E")` 替换为**唯一整数 point_id**，并在**输出目录**（CLI 第二参，常与该生成工程根目录相同）写出 **`point_map.json`**（结构一般为 `{"points": {"1": {"label", "event_type", "file", "line", "event_id"}, ...}}`）。
- **设备侧写入的是预处理后的 point_id**；Host 侧用 JSON **按字符串键**（如 `"149"`）查 `event_type` / `label`。因此：
  - **解码用的 `point_map.json` 必须与当前运行的内核/OPP 来自同一次预处理 + 同一次编译**。换了一份源码再跑 preprocess、或拷贝了别台机器的 map、或只重装 whl 不重编算子，都会导致 **ID 对不上**。
- **典型落点**（形态因仓库而异，勿背死路径）：`<…>/build_tmp/<…>/<msopgen_project_name>/point_map.json`，与编译日志里预处理 hook 所操作的目录一致。在目标环境用 `find … -name point_map.json` 或查 `compile_*` 里 `trace_preprocessor` 的第二参数最可靠。

#### 使用时的路径（常见误操作）

- 传给 `trace_collector.py` 的第二个参数、或各仓 sample/driver 里的 `--point_map`，必须是 **`os.path` 上真实存在的文件**。
- 文档、注释里的 **`<repo>/...`、`build_tmp/.../point_map.json` 仅表示目录形态**；**禁止**把字面量 **`/path/to/...`** 当作参数——会表现为 `point_map` 加载失败、`point_map keys: 0`、或 `load_mapping` 返回空，进而 **全部记录被跳过**。
- 建议在调用前做 **`Path(path).is_file()`** 检查并给出清晰错误（各仓 sample 可按需加入）。

#### Host 侧何时保存 profiling tensor（与设备可见性）

- 设备把 profiling 写入 GM 后，若在 **未完成队列同步 / 设备到 Host 可见** 时就在 Python 里读张量并 `save`，可能读到**全零或计数不更新**的缓冲，`trace_collector` 解析条数为 0。
- **通用做法**：在算子执行返回后、落盘前调用 **`torch_npu.npu.synchronize(device_id)`**（或项目规定的等价同步），再 `cpu()` / `save_profiling_data`。具体插入点因框架而异（例如在 `forward` 外、`synchronize` 之后再写盘）。

#### 如何判断是「映射错了」还是「没采到数」

- 先用 **`inspect_rank_pt.py`**（见下表，与 `comm_operator` 同目录提交）检查 `rank*.pt`：各分组 tensor 的 **非零比例**、**`tensor[core,0]` 计数（counter）**；若大量核 **`counter > 1`**，说明 per-core 上有有效记录，**问题不在设备打点**。
- 再跑 **`trace_collector.py`**：看 **`otherData.skipped_no_mapping`**。若其值 **接近原始记录总数**，而 `point_map` 键数正常，多为 **base_point_id 与 JSON 键不一致**（错 map / 旧 map）。
- 工具在 stderr 打印的 **`diagnose:`** 行：`unique base_point_id in rank*.pt`、`point_map keys`、**`intersection`**。**`intersection` 为 0** 且两侧都非空时，可断定 **point_map 与当前内核不是一套**；应改指向**本次编译生成目录**中的 `point_map.json` 并重新生成 trace。
- **勿与「sync 时机」混淆**：全零 pt → 先查同步；pt 有数据、仅 Chrome 空且 **`skipped_no_mapping` 高** → 先查 **map 路径与版本**。

## 固定脚本

路径规范：
- 文档中的**编译/校验命令示例**优先使用**相对路径**（便于换机复现），**不依赖** Cursor 专属绝对路径。
- **例外（必守）**：`save_profiling_data`、`trace_collector`、sample 的 **`--profiling_dir` / `--chrome_trace` / `--point_map`** 在代码里须 **`resolve()` 成绝对路径**（见 **G5**）。勿在示例里暗示「相对路径一定相对当前 shell」。

### 本仓库 UMDK：`build/cam/comm_operator` 与 Skill 脚本的关系

本 skill 的 **`scripts/`** 下列出了**完整**工具集；若日常只引用 `<skill_root>/scripts/...` 而**不在算子编译目录提交副本**，会出现「文档里有很多脚本、工程里用不上」的割裂。

**本仓库约定**：

- **`umdk/build/cam/comm_operator/`**（与 `compile_ascend_proj.sh`、`build.sh` 同目录）应提交与 **编译预处理、profiling 落盘、Chrome 解析、插桩校验**直接相关的脚本；若本仓库另有对照树，**布局与其 `build/cam/comm_operator/` 对齐**，避免工具链分叉。
- **同名脚本以 Skill 为规范源**；修改行为时优先改 Skill 下文件，再**同步拷贝**到 `umdk/build/cam/comm_operator/`（或合并差异后两边一致）。

| 文件（`umdk/build/cam/comm_operator/`） | 作用 |
|----------------------------------------|------|
| `trace_preprocessor.py` | `TRACE_POINT` → `point_id`，生成 `point_map.json`（**`compile_ascend_proj.sh`** 中 hook 调用） |
| `trace_utils.py` | `save_profiling_data`、从 `*_base.h` 读 `PROF_SIZE_PER_CORE` 等 |
| `trace_save.py` | 离线原始 `.pt` → 按核拆分输出目录 |
| `trace_collector.py` | `profiling_data` 目录 + `point_map.json` → `chrome_trace.json`（stderr 含 `diagnose:` 与 `skipped_no_mapping` 提示） |
| `inspect_rank_pt.py` | 快速查看 `rank*.pt` 形状、非零、每核 counter，判断 pt 是否有有效 profiling（**不依赖**具体算子名） |
| `validate_trace_points.py` | [SKILL.md](SKILL.md) 步骤 5：标签与 B/E 配对 |
| `check_compile_safety.py` | [SKILL.md](SKILL.md) 步骤 5：静态安全检查 |
| `bootstrap_trace_toolchain.py` | 将上表所列 `TOOLCHAIN_FILES` 从**本脚本所在目录**同步到 ``--build-dir``（幂等；``--dry-run`` / ``--list``）；**规范源**与 Skill ``scripts/`` 同名文件一致 |
| `compile_ascend_proj.sh` / `build.sh` / `build_pybind.sh` / `set_conf.py` | 既有构建与预处理 hook |

**仅保留在 Skill 目录、一般不提交到 UMDK `comm_operator` 的脚手架**（新仓库一次性接入、草稿插桩）：`patch_build_pipeline.py`、`verify_trace_scaffold.py`、`apply_trace_scaffold.sh`、`generate_instrumentation_plan.py`、`instrument_operator.py`。**`bootstrap_trace_toolchain.py`** 在 **Skill 与 `umdk/build/cam/comm_operator/` 各有一份**，修改后应两边对齐。本仓库已对 **`compile_ascend_proj.sh`** 做预处理接入，一般**不必**再对 UMDK 跑 `apply_trace_scaffold`；给其他仓接入时仍从 Skill 路径执行。

**UMDK 内同步 Skill 工具链到本目录（示例）**：

```bash
# 从仓库根执行：用 Skill 目录为源，刷新 umdk/build/cam/comm_operator 下各脚本
python3 <skill_root>/scripts/bootstrap_trace_toolchain.py \
  --build-dir umdk/build/cam/comm_operator
```

**本仓库推荐调用方式（任选其一）**：

```bash
# 与工程同目录的副本（推荐；与对照树 layout 一致更佳）
cd umdk/build/cam/comm_operator
python3 validate_trace_points.py ../../../src/cam/comm_operator/ascend_kernels/<op>/op_kernel
python3 trace_collector.py <profiling_out_dir> <path/to/point_map.json> -o chrome_trace.json

# 或显式使用 Skill 路径（与下表「命令示例」一致）
python3 <skill_root>/scripts/validate_trace_points.py ...
```

**与 [SKILL.md](SKILL.md)「必须执行的流程」步骤对应（检索用）**：

| 步骤 | 脚本或产物 |
|------|----------------|
| 1–4 辅助（可选） | `generate_instrumentation_plan.py`、`instrument_operator.py` — 规划/草稿插桩，不能替代人工审查 |
| **5 校验** | `validate_trace_points.py`、`check_compile_safety.py`（**不替代**完整 OPP/pybind 编译；见 [SKILL.md](SKILL.md) 步骤 5 说明与上文「编译与打包门禁」） |
| **6 工具链 + 编译接入** | **首选**：仓内已有 `trace_*.py` 时只改现有 `compile_*.sh` 注入 hook（见 [SKILL.md](SKILL.md) 步骤 6）。**按需**：`bootstrap_trace_toolchain.py` → `patch_build_pipeline.py` 或手工 hook → `verify_trace_scaffold.py`；一次性 `apply_trace_scaffold.sh`。编译前在构建树拷贝目录跑 `trace_preprocessor.py ... --modify`，生成 `point_map.json`。**通过后须跑通目标仓库完整 `build.sh` / `compile_ascend_proj.sh`（或 CI 等价）** |
| **6（本仓库 UMDK 已接入）** | `umdk/build/cam/comm_operator/trace_preprocessor.py` 与 **`compile_ascend_proj.sh`** 内 **`# TRACE_PREPROCESSOR_HOOK_START/END`**：在 `copy_ops` 之后、`set_conf.py` 之前，对 **`${MODULE_BUILD_PATH}/${proj_name}`** 执行预处理（**只改当次 msopgen 生成树**，仓内 `src` 源码仍保留 `TRACE_POINT` 字符串）；`point_map.json` 落在该生成树根目录。脚本缺失时打印 WARNING 并跳过。 |
| 运行后解析 | `trace_save.py`（离线 `.pt`）、`trace_collector.py`（→ `chrome_trace.json`）— 见上文「trace.json 生成流程」 |
| **7 Profile UT / 联调** | 扩展既有 **`examples/*_sample.py`** / **`test_<op>.py`**：profiling 落盘、可选 Chrome、可选 **`--trace_checks`**；数值 UT 与 profile 入口分离（少增 `*_profile.py`）。详解见 [SKILL.md](SKILL.md) 步骤 7；落盘后经 `trace_collector` 的流程另见上文「trace.json 生成流程」「端到端 profiling + Chrome」。 |

- 生成打点草案（函数树 + 合并决策）：
  - `python <skill_root>/scripts/generate_instrumentation_plan.py --root <operator_dir> --entry <entry_function>`
- 根据函数边界自动写入打点代码：
  - `python <skill_root>/scripts/instrument_operator.py --target <operator_src_file_or_dir> --root-label processing`
- 校验点位命名与 B/E 配对：
  - `python <skill_root>/scripts/validate_trace_points.py <file_or_dir>`
- 静态编译安全检查（花括号平衡、预处理配对、头文件可达、参数一致性等）：
  - `python <skill_root>/scripts/check_compile_safety.py <operator_dir>`
  - 加 `--strict` 将 warnings 也视为错误
- 预处理（编译前替换 TRACE_POINT 为整数 ID）：
  - `python <skill_root>/scripts/trace_preprocessor.py <operator_src_dir> <output_dir> --modify`
- 保存 profiling tensor（离线）：
  - `python <skill_root>/scripts/trace_save.py <raw_pt_file> --rank <rank_id> --output <profiling_data_dir>`
- 生成 Chrome Trace JSON：
  - `python <skill_root>/scripts/trace_collector.py <profiling_data_dir> <point_map.json> -o chrome_trace.json`
- 部署工具链脚本到 build 目录：
  - `python <skill_root>/scripts/bootstrap_trace_toolchain.py --build-dir <build_module_dir>`
- 对编译脚本打补丁并接入预处理（幂等）：
  - `python <skill_root>/scripts/patch_build_pipeline.py --compile-script <compile_script_path> --preprocessor-cmd "<cmd>"`
- 校验工具链与编译接入是否就绪：
  - `python <skill_root>/scripts/verify_trace_scaffold.py --build-dir <build_module_dir> --compile-script <compile_script_path>`
- 一键执行"部署工具链 + 编译接入 + 校验"：
  - `bash <skill_root>/scripts/apply_trace_scaffold.sh <skill_root> <build_module_dir> <compile_script_path>`

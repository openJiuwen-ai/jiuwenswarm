# HarmonyOS 上安装 DeepResearch（openjiuwen_deepsearch）完整记录

> 适用环境：HarmonyOS (HongMeng Kernel, aarch64, musl libc)，HNP Python 3.12.8，
> 仓库 `officeClaw/jiuwenswarm`，venv `.venv`（`relay-claw/vendor/jiuwenclaw` 是本仓库的符号链接，
> App sidecar 与命令行共用同一套环境）。

## 背景：为什么 pip 装不上

鸿蒙机器的 pip 只接受 `harmonyos_aarch64` 平台标签（无 manylinux/musllinux），而
`openjiuwen_deepsearch` 依赖的 `pypdfium2`、`pandas` 等含原生扩展的包在 PyPI 上只有
musllinux wheel。此外鸿蒙内核强制 **ELF 代码签名**：没有 `.codesign` 段的 `.so`
`dlopen` 时直接 `Permission denied`。

本次安装解决了三层问题，最终 DeepResearch 的 4 个工具
（`deepresearch_stream` / `deepresearch_prepare_rewrite` / `deepresearch_commit_rewrite`
/ `deepresearch_generate_rewrite_html`）已全部注册进 AgentServer。

## 推荐：一键安装脚本（跨机器）

```sh
cd ~/officeClaw/jiuwenswarm
sh scripts/install-deepsearch-ohos.sh
```

`scripts/install-deepsearch-ohos.sh` 把下述全部步骤自动化，且**幂等可重跑**：

- 纯 Python 依赖优先从 `wheels/deepsearch-deps/` 离线装（16 个锁定版本的 wheel），
  缺失时回落清华镜像；
- `openjiuwen_deepsearch` 本体优先装 `wheels/openjiuwen_deepsearch-0.2.0-py3-none-any.whl`，
  其次本地源码 `.cache/deepsearch-src/`，最后才 git 克隆；
- 原生包（numpy/pypdfium2/Pillow/pandas）三级兜底：
  ① 仓库内 `wheels/*harmonyos_aarch64.whl` 直接装；
  ② 装不上（跨机器代码签名不被接受）→ 用**本机** binary-sign-tool 重签后再装；
  ③ 还不行 → 从 `wheels/musl-sources/`（或镜像下载）的 musl wheel 现场转换
  （同余修复 + DT_NEEDED libpython + 签打包库 + 改标签）；
- pymilvus 平台桩自动写入 site-packages；
- skill 目录软链自动创建；
- 最后在**空 LD_LIBRARY_PATH** 下跑完整验证（8 模块导入 + pandas/pypdfium 功能测试
  + 4 工具注册）。

**跨机器迁移（git 克隆）**：在新机器上先 `git lfs install`，再克隆本仓库与 relay-claw
（skill 脚本在里面），先执行 `scripts/install-ohos-agentserver.sh` 建好基础 venv，
再跑上面的脚本即可。签名由新机器本地完成，不依赖旧机器的密钥。

> **Git LFS**：gitcode 单文件限 10 MiB，两个超限的 pandas wheel 已走 LFS（`.gitattributes`）：
> `wheels/pandas-2.3.1-cp312-cp312-harmonyos_aarch64.whl`（12.2 MiB）与
> `wheels/musl-sources/pandas-2.3.1-cp312-cp312-musllinux_1_2_aarch64.whl`（12.8 MiB）。
> 拉取机器需装 git-lfs（`git lfs version` 验证）才能取到真实内容；未装时这两个文件是
> ~130 字节的指针文本，安装脚本会自动识别（zip 魔数 "PK" 检查）并改走镜像在线转换
> （此时需要网络，其余包仍离线）。以后新增 >10 MiB 的文件时同样处理：
> `git lfs track '<路径>' && git add --renormalize '<路径>'`。

## 三层问题的解法

### 1. ELF 签名与段布局（binary-sign-tool 的坑）

- 签名工具：`/data/service/hnp/bin/binary-sign-tool sign -selfSign 1 -inFile X -outFile Y
  -signAlg SHA256withECDSA -keyAlias default`
- **坑 A**：签名工具会按 section 表重排段布局，把后续 LOAD 段挪到
  `align8(上一段文件尾)`，一旦破坏 `p_offset ≡ p_vaddr (mod 4096)` 同余关系，加载即 SIGSEGV。
  **解法**：签名前把上一段最后一个 section 的 `sh_size` 延长（吸收全零 padding），
  使签名工具的落点恰好等于下一段的原始 offset。
- **坑 B**：改已签名的文件会失效，任何补丁必须在签名**前**做。
- **坑 C**：签名工具需要 section 表存在（不能 strip）。

### 2. 外来 musl 扩展缺 libpython 依赖

OHOS 的 musl 加载器 dlopen 扩展时不搜索主程序依赖链，而 HNP python 主程序
不导出 `Py_*` 符号（符号在 `libpython3.12.so.1.0` 里）。外来 musl 扩展必须显式
`DT_NEEDED libpython3.12.so.1.0`。

**解法（弱符号名复用）**：把 `.dynstr` 中链接器垃圾弱符号
（`_ITM_deregisterTMCloneTable` 等，运行时永不查找）的名字字符串覆盖为
`libpython3.12.so.1.0`，再把 `.dynamic` 里一个空闲槽位（DT_FINI/DT_FLAGS_1）改写成
DT_NEEDED 指向它。偏移是正常范围内正数，加载器完全接受。

### 3. Alpine 哈希命名的 C++ 运行时（自包含方案）

pandas 的 musl wheel 里本来就把 `libstdc++-1f1a71be.so.6.0.33` /
`libgcc_s-69c45f16.so.1`（cibuildwheel 哈希名）打包在 `pandas.libs/` 目录，
且扩展自带 `DT_RPATH=$ORIGIN/../../../pandas.libs` 自行解析——**不需要改 DT_NEEDED
名字，也不需要 ~/usr/local/lib**。早期方案（改名 + 系统目录部署 + LD_LIBRARY_PATH）
已废弃：App sidecar 不一定继承环境变量，而 RPATH 是 wheel 自包含的，任何进程
环境下都能加载。

**唯一要做的**：把 `pandas.libs/` 里的库（版本化的 `.so.6.0.33` 文件名）逐个签名
（转换脚本的 so 收集正则已覆盖 `\.so(\.\d+)*$`）。

## 一键工具

`scripts/ohos/ohos-musl-wheel-convert.py` 封装了以上全部手术：

```sh
# 整个 wheel 转换（改名扩展后缀 + 同余预修 + DT_NEEDED libpython + 签名 + 改标签 + 重打 RECORD）
.venv/bin/python scripts/ohos/ohos-musl-wheel-convert.py \
    /path/to/foo-cp312-cp312-musllinux_1_2_aarch64.whl \
    --rename-needed "libstdc++-1f1a71be.so.6.0.33=libstdc++.so.6" \
    --rename-needed "libgcc_s-69c45f16.so.1=libgcc_s.so.1"

# 单个 .so 就地签名（比如 Alpine 的 libstdc++/libgcc_s）
.venv/bin/python scripts/ohos/ohos-musl-wheel-convert.py --so /path/to/libstdc++.so.6.0.33
```

## 完整安装步骤（手动方式，脚本内部即此流程）

```sh
cd ~/officeClaw/jiuwenswarm

# 0. 纯 Python 依赖（优先离线 wheels/deepsearch-deps/，全部 --no-deps）
pip install --no-index --find-links=wheels/deepsearch-deps --no-deps \
    jinja2==3.1.6 json-repair==0.58.0 networkx==3.4.2 pyvis==0.3.2 \
    aiolimiter==1.1.0 tldextract==5.3.2 requests-file==3.0.1 \
    python-dateutil==2.9.0.post0 pytz==2026.3.post1 openpyxl==3.1.5 \
    et-xmlfile==2.0.0 jsonpickle==4.1.2 ipython==9.17.1 traitlets==5.16.1 \
    prompt_toolkit==3.0.53 wcwidth==0.8.2

# 1. openjiuwen_deepsearch 本体（wheel 在 wheels/ 里，避免连带装 matplotlib/seaborn）
pip install --no-deps wheels/openjiuwen_deepsearch-0.2.0-py3-none-any.whl

# 2. 原生包：pypdfium2 + pandas（离线 harmonyos wheel 直接装；跨机器时脚本自动重签
#    或从 wheels/musl-sources/ 的 musl wheel 现场转换）
pip install --no-deps wheels/pypdfium2-4.30.0-py3-none-harmonyos_aarch64.whl
pip install --no-deps wheels/pandas-2.3.1-cp312-cp312-harmonyos_aarch64.whl
# 手动转换 musl 源（如需要）：
.venv/bin/python scripts/ohos/ohos-musl-wheel-convert.py \
    wheels/musl-sources/pandas-2.3.1-cp312-cp312-musllinux_1_2_aarch64.whl
# （pandas.libs/ 里的哈希名 C++ 库由脚本自动签名，RPATH 自包含，无需系统部署）

# 3. Pillow（内部 OHOS 构建，PyPI 无 musl wheel，必须复用仓库内 wheel 重签）
pip install --no-deps wheels/Pillow-12.2.0-cp312-cp312-harmonyos_aarch64.whl

# 4. pymilvus 桩（真 pymilvus 依赖 grpcio 原生库，鸿蒙上不可能；
#    deepsearch 只在用 Milvus 向量检索时才真正调用，桩满足模块级导入即可）
#    由安装脚本自动写入（见下方说明）

# 5. skill 目录兜底链接（_resolve_skill_root 的 cwd fallback）
ln -sfn ~/officeClaw/relay-claw/office-claw-skills ~/officeClaw/jiuwenswarm/office-claw-skills

# 6. 重启 AgentServer（工具模块不支持热重载）
sh scripts/start-ohos-agentserver.sh 18092
```

### pymilvus 桩说明

`.venv/lib/python3.12/site-packages/pymilvus/` 提供 `MilvusClient`、`AsyncMilvusClient`、
`AnnSearchRequest`、`RRFRanker`、`WeightedRanker`、`Collection`、`CollectionSchema`、
`Function`、`FunctionType`、`DataType`、`MilvusException`、`connections`、`utility`，
以及 `pymilvus.client.search_result.SearchResult`、`pymilvus.client.types.LoadState`、
`pymilvus.milvus_client.IndexParams`。所有类在实例化时抛 `NotImplementedError`
（说明"鸿蒙平台 Milvus 检索不可用"），满足 openjiuwen / deepsearch 的模块级导入。

## 验证结果（2026-09-03，一键脚本端到端复测）

- 模拟全新机器（卸载 18 个包 + 删 pymilvus 桩 + 删 skill 软链）后跑
  `sh scripts/install-deepsearch-ohos.sh` → 全部通过；
- 隐藏 pandas 离线 wheel 再跑一次 → 兜底路径生效（musl wheel 现场转换 + 本机签名）
  → 同样全部通过；
- 8 模块导入链全部 OK（report_style.service、agent_factory、workflow、
  task_manager、tools、deepsearch_tools、rewrite_tools、styled_html_export），
  且在**空 LD_LIBRARY_PATH** 下验证（模拟 App sidecar 无环境变量的最严苛场景）；
- `get_deepresearch_tools()` → 4 个工具：deepresearch_stream、
  deepresearch_prepare_rewrite、deepresearch_commit_rewrite、deepresearch_generate_rewrite_html；
- pandas 2.3.1 功能测试通过（rolling C++ 路径、to_excel）；
- pypdfium2 4.30.0 通过（PdfDocument.new + new_page）。

## 已知降级

- `matplotlib` / `seaborn` 未装：只在图表生成沙箱子进程里用到，DeepResearch 主流程
  不受影响；如需要，可用同样的转换流程处理 musl wheel。
- Milvus 向量检索不可用（pymilvus 桩）：deepsearch 的 browsecompplus 索引/检索
  功能受限，其余功能正常。

## 已知环境限制：系统终端无法加载 hmdfs .so（2026-09-04 排查记录）

**现象**：在系统终端（Terminal App）里运行安装脚本，所有原生包（连基础环境的
`pydantic_core` 也是）`dlopen` 一律 `Permission denied`；而 App(AgentServer) 内
DeepResearch 一切正常，AgentServer 会话（hishell 链）里 import 也正常。

**结论**：这是 hmdfs 任务级执行限制（挂载项 `hmmac=use_task`）——**哪些进程上下文
允许 mmap-exec 文件系统上的 .so 与文件本身无关**（签名、权限位、文件内容均已排除）。
终端里"功能测试失败 ≠ 安装失败"：pip 写入完全正常，装好的包在 App 里可用。

**脚本对策**：预检阶段用"金丝雀"探测（尝试 dlopen site-packages 里已有的 .so）；
全部被拒则进入受限模式——安装照常、以 pip 元数据判定是否已装、跳过功能验证，
最后提示在 App 内调用一次 DeepResearch 完成验证。脚本在两种终端里都能正确收敛，
无需区分使用场景。

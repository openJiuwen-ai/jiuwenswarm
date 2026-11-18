---
id: B_20261118_1150_memory-frozen-registration
title: 修复冻结后端中 JiuwenMemory 动态实现漏包
type: B
author: ToNy_St4rk
author_email: dengjiahao9@huawei.com
modules:
  - scripts
files:
  - scripts/jiuwenswarm.spec
  - scripts/memory_bundle.py
  - scripts/verify_memory_bundle.py
  - scripts/build-exe.ps1
  - scripts/build-exe.bat
  - scripts/build-electron-exe.ps1
  - scripts/build-macos.sh
  - scripts/build-electron-exe.sh
  - tests/unit_tests/scripts/test_memory_bundle.py
designs: []
design_note: 按已确认的发布包故障进行局部打包修复，不改变 Memory 的公共接口或运行时配置契约。
---

# JiuwenMemory 动态实现漏包修复记录

## 现象与影响

2026-11-18 安装的 WorkSwarm 0.2.9 在导入 AgentMemoryMemoryProvider 时，报缺少
`jiuwen_memory.common.security.authentication.authentication_impl.memory_key_store`，
随后 JiuwenMemoryRail 构建失败，内置记忆停用，视频工具也无法注册。
此前对旧安装包的只读排查确认其 Memory 模块来自 0.2.1，且含视频工具实现；故障不能解释为 Memory 版本过旧。

## 关联设计与当前契约

没有独立 D。依据锁定的 JiuwenMemory 0.2.1 的 Producer 注册与 adapter 默认 SDK 配置，
修复 Swarm 的 PyInstaller 收集范围。Memory 底层实现与长期静态导入改造仍由 Memory 仓库维护。

## 根因与修复决策

Memory 的 bootstrap、实现包通过 import_required/import_optional 调用 importlib.import_module，
PyInstaller 无法从字符串识别完整依赖图。认证包在导入阶段强制加载 memory_key_store、
api_key_authenticator、trusted_authenticator 等实现，缺失会在 SDK 装配前直接阻断 Provider 导入。
其他存储、摄取、检索和视频实现也依赖动态注册，因此仅补三个认证模块不充分。

新增 scripts/memory_bundle.py，集中声明 API/config/security、构建/控制/摄取/检索、
视频 normalizer 和 Swarm adapter 的运行时包，以及默认 SDK 使用的轻量公共实现和本地存储实现。
由共用 spec 合入 hiddenimports，包扫描失败直接阻断构建。未整包收集 Memory，
避免额外引入可选的 BGE、HanLP、spaCy、Milvus、PostgreSQL、Redis、Elasticsearch 实现。
已有其他组件自行依赖这些第三方包的行为不属于本修复范围。

新增冻结校验脚本，经正式 backend 的脚本入口在临时 DATA_DIR/HOME 中运行。
实际构建两个 Provider：普通 SDK 配置应暴露普通记忆工具且无视频工具；有效视频配置应额外暴露
agent_memory_video_submit、agent_memory_video_status。初始化失败、schema 缺失都使构建失败，
各路径均 shutdown。移除继承的 JIUWEN_MEMORY_/EMBED_ 环境变量，使用临时 markdown root 和虚构模型配置，
失败时额外直接保存 traceback，防止第三方导入替换 stderr/异常钩子后丢失诊断。
不发送模型请求、不读真实视频、不访问用户记忆库。四个平台包装入口接入门禁；Windows Electron 的
ReuseBackend 也执行校验，FrontendOnly 保持原有跳过后端验证的语义。

## 回归证据与适用范围

- 基线：upstream/releases/0.2.9，1ffd0363e88f94bfe890a432774c227a47ed5da4；候选为本记录 files 中的差异。
- Windows / Python 3.11.16 / Memory 0.2.1 发行缓存；其他运行时依赖复用已有开发环境，非完整 uv.lock 重建。
- 真实 Provider 初始化后，普通配置返回 4 个工具，视频配置返回 6 个工具，均完成 shutdown。
- 定向 pytest：test_memory_bundle.py、test_gateway_adapter_bundle.py、test_cli_channel_bundle.py，20 passed。
  命令为 python -m pytest -o addopts='' 加上述三个路径 -q；禁用默认覆盖率产物。
  初次复用依赖时两个既有 Gateway 子进程测试因 pywin32 搜索路径缺失失败；补齐该环境路径后全部通过。
- PyInstaller 实际扫描 policy 得到 334 个唯一模块，包含上述三个缺失认证模块。
- 独立只读复查未发现明确新缺陷；未执行格式化或静态检查。
- 定向 PyInstaller 冻结包已实际运行 verify_memory_bundle.py，普通/视频 SDK 初始化和工具声明检查通过，exit=0。首次冻结运行另暴露 fs_impl 顶层强制动态导入 encrypted_fs_store；最终清单补入加密 FS/KV 包装器。定向重打包复用首次依赖分析 TOC 并追加两模块，其静态依赖已在原图内；这不是正式 spec 的完整发布构建。
- 尚未生成或安装完整 WorkSwarm 发布安装器。

## 防错模式与资料同步

发布内置插件不能只证明源码 import 成功；字符串注册必须显式进入冻结依赖图，
并由安装产物的脚本入口验证初始化和最终工具声明。实现和门禁清单在 scripts 中集中维护，
该模式由本故障记录及新增回归用例留存，不扩展为无关模块的通用检查。
文档通过 sync_documents.py 同步元数据及索引。

## 遗留与后续

门禁为离线装配和工具声明检查，watch_document=False；不证明 watchdog 启动、实际记忆读写、
视频摄取、ffmpeg/ffprobe、ASR/VLM 推理或三平台完整安装包可用。完整 Release 需重建后端并验证，
旧安装包不会因本仓源码修改自动修复。Memory 长期应评估将启动必需模块改为静态导入，并另行发布。


## 检视与静态报告修订（2026-11-18）

- 静态报告两项 G.EXP.04：将跨多行的模块清单生成器改为完整字面 tuple，将环境过滤字典推导改为普通 for；33 项模块内容、顺序及包根逐项比较不变，环境过滤回归通过。未运行格式化或静态检查工具，专有检查器结果仍需 CI 复核。
- 检视第 1/2/4 条属于同一漏项：补齐 build-exe.bat 的 Memory 门禁，通过父 Python launcher 隔离用户目录，并在非零返回时立即退出。
- 新增五个桌面构建入口的门禁覆盖检查，并在 Windows 实际执行 batch 门禁片段，用 uv 替身验证成功继续、失败阻断和含空格路径传参；不运行完整安装器构建。
- 检视第 3 条已按当前两分支核对：pyproject.toml 均为 JiuwenMemory>=0.2.1，uv.lock 均锁定 0.2.1。该意见对应先前旧候选，当前无需再修改依赖。
- develop 的 Memory 与 Gateway 回归共 22 passed。独立只读复查确认常量及环境过滤语义不变，未发现行为缺陷；随后调整新增测试的导入顺序与参数换行。

- release 分支的 Memory、Gateway 与 CLI 打包回归共 23 passed，uv lock --check --offline 通过；完整安装器未重建。

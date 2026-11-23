---
id: B_20261121_0108_pypi-update-version-baseline
title: 更新检查用打包源版本比较、并展示非最新版本的 whl，导致每日构建显示可升级到更旧的版本
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/updater.py
  - jiuwenswarm/common/version_source.py
  - tests/unit_tests/common/test_updater_release_metadata.py
  - tests/unit_tests/test_updater_install_mode.py
designs: []
design_note: 仅修正更新检查的版本基线取值与 PyPI 源对最新版本文件的归属判定，不改变更新源配置、安装方式与前端展示契约；无独立设计。
---

# 更新检查用打包源版本比较、并展示非最新版本的 whl

## 现象与根因

#6849：手工安装每日构建的 `workswarm-0.2.9-py3-none-any.whl`（当天 11/19
构建）后，应用内更新面板显示：

- 当前版本 `0.2.9`；
- 最新版本 `0.2.9b3`，其下展示的升级文件却是
  `workswarm-0.2.5-py3-none-any.whl`；
- 发布时间 `2026/11/13`，并给出可升级/安装入口。

而更新源 `https://mirrors.aliyun.com/pypi/simple/workswarm/` 上最新的分发包
就是 `0.2.9b3`（11/13 上传），比已安装的 `0.2.9` 更旧。

两处口径不一致：

1. `UpdaterService._check` 用打包时写入源码的 `__version__` 作为「当前版本」
   参与比较，而面板展示的 `current_version` 取自分发元数据
   `get_runtime_version()`（提交 577ec9485 引入）。每日构建的分发元数据版本
   （`0.2.9`）与源码版本可能不同，因此 `_is_newer_version` 会把更旧的
   `0.2.9b3` 判成可升级。
2. `PyPIVersionSource.fetch_latest` 把 simple 索引里的 **全部** whl 都当作本次
   `release` 的 assets，而 simple 索引按上传顺序列出项目所有历史产物；
   `_resolve_pip_asset` 取其中第一个 whl，于是展示的是最旧的 `0.2.5`。
   另外发布时间用 `latest_version in filename` 子串匹配，`0.2.9` 会命中
   `workswarm-0.2.9b3-...`，可能取到错误的时间戳。

## 修复决策

- `_check` 统一以 `get_runtime_version()`（与 `current_version` 展示一致）作为
  比较基线，并把它传给 `fetch_latest` 用于桌面端按版本查时间戳。
- 评审补充：每日构建的分发版本可能带构建时间后缀
  （`0.2.9.beta5.dev20261119`、`0.2.9+local`）。`_detect_prerelease_type`
  按固定顺序返回最先命中的标记，会把这类版本判成 `dev`（rank 0），反而低于
  同基线的旧 beta。新增 `strip_build_suffix`，在比较前剥离结尾的
  `.dev<8位日期>` / `.post<8位日期>` / `+local` 段，桌面端按 tag 匹配也使用
  归一化后的版本；展示的 `current_version` 仍保留原始分发版本。
- `PyPIVersionSource.fetch_latest` 在解析文件名版本时记录「文件名 → 版本」，
  `release.assets` 只保留版本号与 `latest_version` **完全相等**的文件；时间戳
  也从该文件的上传时间取得，不再使用子串匹配。
- 评审第二轮补充：归一化必须对称。`_check` 保留原始
  `current_version` 传给 `_is_newer_version`，由
  `is_newer_distribution_version` 对候选与已安装两侧统一剥离构建后缀后比较；
  两侧归一化后的发布版本相同时，只有**双方都带日期构建后缀**才按 build 日期
  比较新旧，避免每日构建通道永远判定「已是最新」。`PyPIVersionSource`
  选取最新版本也改用 `distribution_sort_key`：发布版本相同时干净发布优先，
  同一条目下的多个日期构建按日期取最新，避免 `max` 在等键时取到最旧条目。
- 评审第三轮补充：构建日期可能只出现在 PEP 440 local 段
  （`0.2.9+local.20261119`），而 `_BUILD_DATE_PATTERN` 原先只认 `dev`/`post`
  段。此时两侧归一化后同为 `0.2.9`、日期都取不到，「双方都带日期」规则不生效，
  每日构建通道恒判「已是最新」。现让 `_BUILD_DATE_PATTERN` 同时识别
  `+local.<8位日期>` / `local.<8位日期>`，并让 `PyPIVersionSource` 解析文件名
  时保留 PEP 440 local 段（此前版本正则会在 `+` 处截断或整条文件名匹配失败，
  local 日期在源头即丢失）。
- 不改变 pip 安装命令（仍为 `pip install --upgrade <package>`）、更新源配置与
  前端对 `latest_version` / `published_at` 的展示契约：更新源上最新发布版本
  依旧如实展示，只是状态正确落到「已是最新版本」，且可升级时才展示与
  `latest_version` 对应的 whl。

## 验证

`tests/unit_tests/common/test_updater_release_metadata.py`：

- `test_pypi_source_keeps_only_latest_version_files`：索引同时包含 `0.2.5`、
  `0.2.9b3`、`0.2.9` 时，`assets` 仅 `workswarm-0.2.9-py3-none-any.whl`，
  `published_at` 取自 `0.2.9`（验证子串匹配缺陷已消除）。
- `test_pip_check_compares_installed_build_version`：已安装 `0.2.9` 时
  `has_update=False`、`matched_asset=""`；已安装 `0.2.8` 时 `has_update=True`
  且 `matched_asset` 为 `0.2.9b3` 对应的 whl。
- 带构建后缀的已安装版本（`0.2.9.dev20261119`、`0.2.9.beta5.dev20261119`）
  面对索引中的旧 `0.2.9b3` 时 `has_update=False`；而
  `0.2.9.beta1.dev20261119` 仍能识别到 `0.2.9b3` 是升级。
- `test_strip_build_suffix_keeps_release_version`：覆盖 `.dev<日期>`、`.post<日期>`、
  `+local`、多段后缀，以及 `0.2.9b3` / `0.2.9.beta5` / `0.2.9.dev1` 保持不变。
- `test_pip_check_orders_daily_builds_by_build_date`：更新源同时存在
  `0.2.9.beta5.dev20261119`、`0.2.9.beta5.dev20261120` 与旧 `0.2.9b3` 时，
  已安装 1119 能升级到 1120；已安装 1120 或更新的 1121 不会回退；
  `0.2.9.beta4.dev20261119` 仍能升级到更新的 beta5 构建；
  `0.2.9.dev20261119` 归一化后高于同基线 beta5 构建，不误报升级。

- `test_build_date_suffix_reads_dev_post_and_local_segments`：`.dev<日期>`、
  `.post<日期>`、`+local.<日期>`、`local.<日期>` 都能取到构建日期；
  `+local`（无日期）、`+local.abc`、`.dev1`、干净版本返回 0。
- `test_pip_check_orders_local_daily_builds_by_build_date`：更新源只有
  `0.2.9+local.20261119` 与 `0.2.9+local.20261120` 时，已安装 1119 能升级到
  1120；已安装 1120/1121 不回退；干净 `0.2.9` 与同日构建等价；
  `0.2.8` 仍能升级到 local 构建。

`tests/unit_tests/test_updater_install_mode.py` 中依赖 `updater.__version__`
的用例改为注入 `updater.get_runtime_version`。

三个测试文件共 105 个用例全部通过（含本轮新增定向用例）。

## 遗留

- 每日构建产物发布在 OBS `daily-whl/<version>/<date>.zip`，不在默认 PyPI
  镜像上；面板展示的「最新版本/发布时间」始终是当前更新源的最新发布。
  若要在每日构建通道内检查，需要把 `updater.pypi_mirror` 指向该通道的索引。
- 仅当更新源上存在比已安装分发版本更新的版本时才会展示升级文件，本次不再
  出现「显示更旧 whl」的组合。

# 插件体检台（Plugin Doctor）

在 Web 界面上对「应用插件（Application Plugin）」做**只读**静态体检，把宿主的真实加载语义
逐条对成可复核结论——每条结论都带宿主源码依据（文件:行号）。

- 插件 id：`plugin-doctor`（`package_type: application`，自带 `extension.py` 与 iframe 页面）
- 入口：扩展 → 应用插件 → 「插件体检」
- 交付物：本插件目录 + `tests/unit_tests/extensions/test_plugin_doctor_*.py`
- **依据基线**：规则表里的行号校准于 `openJiuwen/jiuwenswarm` 的 `develop`（`c4440433`）；
  宿主重构后行号会漂移，改动时请同步 `_RULE_DEFS` 里的 `evidence`。

## 为什么需要

宿主的扩展机制里有不少「只声明不校验」「失败只记日志」的地方，插件作者很难自查：

| 现象 | 宿主实现 | 后果 |
| --- | --- | --- |
| `min_jiuwenswarm_version` 写高了 | 只在 `sdk/base.py:79` 读入，宿主不做任何比较 | 不满足版本也可能照常加载，行为说不准 |
| `config_schema` 写错了 | 只存进元数据（`sdk/base.py:81`），无消费点 | 以为会被校验，实际无任何效果 |
| `requires_transport: "true"` | `manager.py:122` 用 `is True` 判定 | 字符串真值不算，扩展被跳过 |
| 仅清单插件（无 `extension.py`） | id 为空 / 没有 frontend / render_mode 非 iframe / position 非整数都会抛 ValueError | manager 只 `logger.error`，插件静默消失 |
| 扩展目录多套了一层 | 只扫搜索目录的一层子目录（`loader.py:53-57`） | 插件根本不会被发现 |
| 没有 `extension.yaml` | 读元数据抛 FileNotFoundError（`sdk/base.py:64-68`） | `/api/application-plugins` 整体 500 |

## 安全边界

- **只读**：只读取目录结构、`extension.yaml` 与 `extension.py` 的源码文本；不导入、不执行被测插件。
- **不执行命令、不联网**：无 subprocess、无网络调用；iframe 页面也不加载任何外部资源。
- **不越界**：`plugin.doctor.inspect` 只接受搜索目录之内的路径，其余返回 `PATH_NOT_ALLOWED`。
- 单次扫描最多 200 个插件（`MAX_PLUGINS`），畸形目录再多也不会拖死面板。

## 使用

界面路径：扩展 → 应用插件 → 插件体检。页面提供健康分、插件表、按级别过滤的结论明细、
单目录体检与 JSON / Markdown 导出。

本地 RPC（均为 `local_only`，前缀 `plugin.doctor.`）：

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `report` | `{search_paths?, refresh?}` | 全量体检报告（分数、插件表、结论明细） |
| `inspect` | `{path}` | 单个扩展目录的体检结果 |
| `rules` | – | 规则目录 |
| `paths` | – | 当前搜索目录、用户插件目录、宿主版本 |
| `export` | `{format: json\|markdown}` | 可直接贴进 MR / 工单的报告文本 |

搜索目录与宿主 `extensions/manager.py` 保持一致：`<data-dir>/application_plugins` +
配置里的 `extensions.extension_dirs`（`;` 分隔）+ 包内 `jiuwenswarm/extensions`；
`report` 也可以显式传 `search_paths` 覆盖。

## 规则目录（31 条）

错误 = 会加载失败或接口报错；警告 = 大概率不符合预期；提示 = 知会一声。
「宿主依据」都可以在仓库里打开对照。

| 规则 | 级别 | 说明 | 宿主依据 |
| --- | --- | --- | --- |
| PD001 | 错误 | 目录不是扩展 | `jiuwenswarm/extensions/loader.py:29-30` |
| PD002 | 错误 | 清单不是普通文件 | `jiuwenswarm/extensions/loader.py:19-21,167-174` |
| PD003 | 错误 | 清单 YAML 无法解析 | `jiuwenswarm/extensions/loader.py:171-174; jiuwenswarm/extensions/manager.py:136-137` |
| PD004 | 错误 | 清单根节点不是映射 | `jiuwenswarm/extensions/loader.py:71; jiuwenswarm/extensions/sdk/base.py:71-74` |
| PD005 | 错误 | PyYAML 不可用 | `jiuwenswarm/extensions/loader.py:175-176` |
| PD006 | 错误 | id 为空 | `jiuwenswarm/extensions/loader.py:73-74; jiuwenswarm/extensions/registry.py:144-148` |
| PD007 | 错误 | 仅清单插件未声明前端页 | `jiuwenswarm/extensions/loader.py:71-78` |
| PD008 | 错误 | 仅清单插件用了非 iframe 渲染模式 | `jiuwenswarm/extensions/sdk/application_plugin.py:133-137` |
| PD009 | 错误 | 前端入口为空 | `jiuwenswarm/extensions/sdk/application_plugin.py:138-140` |
| PD010 | 错误 | 前端 position 不是整数 | `jiuwenswarm/extensions/sdk/application_plugin.py:144-147` |
| PD011 | 错误 | 缺少 extension.py 且不是 application 包 | `jiuwenswarm/extensions/loader.py:70-83,146-152` |
| PD012 | 错误 | extension.py 存在语法错误 | `jiuwenswarm/extensions/loader.py:146-164` |
| PD013 | 警告 | 入口脚本未导出 register_extensions | `jiuwenswarm/extensions/loader.py:85-99` |
| PD014 | 错误 | register_extensions 不是 async | `jiuwenswarm/extensions/loader.py:85-86` |
| PD015 | 错误 | 前端入口文件不存在 | `jiuwenswarm/extensions/sdk/application_plugin.py:105-110; jiuwenswarm/gateway/extensions/application_host.py:96-110` |
| PD016 | 错误 | plugin_id 重复 | `jiuwenswarm/extensions/registry.py:140-151` |
| PD017 | 警告 | 清单未声明 id | `jiuwenswarm/extensions/registry.py:144-148` |
| PD018 | 警告 | 类属性 plugin_id 与清单 id 不一致 | `jiuwenswarm/extensions/registry.py:144-148; jiuwenswarm/extensions/sdk/base.py:73-92` |
| PD019 | 警告 | 元数据字段缺失 | `jiuwenswarm/extensions/sdk/base.py:73-92` |
| PD020 | 警告 | min_jiuwenswarm_version 高于当前宿主 | `jiuwenswarm/extensions/types.py:15; jiuwenswarm/extensions/sdk/base.py:79` |
| PD021 | 警告 | 声明的依赖未安装 | `jiuwenswarm/extensions/loader.py:101-144` |
| PD022 | 警告 | requires_transport 不是布尔真值 | `jiuwenswarm/extensions/manager.py:120-123` |
| PD023 | 警告 | 入口脚本的导入方式在按路径加载时会失败 | `jiuwenswarm/extensions/loader.py:146-164` |
| PD024 | 警告 | 扩展被套在更深一层目录里 | `jiuwenswarm/extensions/loader.py:47-58` |
| PD025 | 提示 | permissions 只做透传 | `jiuwenswarm/gateway/extensions/application_host.py:35,50; jiuwenswarm/channels/web/frontend/src/applicationPlugins/ApplicationPluginOutlet.tsx:66-70` |
| PD026 | 提示 | 前端贡献无法静态判定 | `jiuwenswarm/extensions/sdk/application_plugin.py:102-110` |
| PD027 | 提示 | version 不是数字版本号 | `jiuwenswarm/extensions/sdk/base.py:76` |
| PD028 | 警告 | permissions 含 iframe 白名单外的值 | `jiuwenswarm/channels/web/frontend/src/applicationPlugins/ApplicationPluginOutlet.tsx:66-70` |
| PD029 | 提示 | 没有前端贡献 | `jiuwenswarm/gateway/extensions/application_host.py:30-44` |
| PD030 | 提示 | config_schema 不是映射 | `jiuwenswarm/extensions/sdk/base.py:81` |
| PD031 | 错误 | 缺少 extension.yaml（宿主读元数据会抛 FileNotFoundError） | `jiuwenswarm/extensions/sdk/base.py:64-68; jiuwenswarm/gateway/extensions/application_host.py:27-29` |

## 本地自测

```bash
python -m pytest \
    tests/unit_tests/extensions/test_plugin_doctor_engine.py \
    tests/unit_tests/extensions/test_plugin_doctor_plugin.py \
    tests/unit_tests/extensions/test_plugin_doctor_loader_contract.py
```

- `test_plugin_doctor_engine.py`：31 条规则每条一个最小畸形插件复现，外加合法写法的反向断言
  （防止规则退化成噪声），以及「依据文件存在、行号在文件范围内」「README 规则表与代码一致」
  两条护栏。
- `test_plugin_doctor_plugin.py`：RPC 契约——参数校验、路径越界拒绝、缓存、导出、宿主禁用态。
- `test_plugin_doctor_loader_contract.py`：用**真实** `ExtensionLoader` + `ExtensionRegistry`
  + FastAPI 资产路由跑端到端，把「引擎说宿主会这样」的断言钉在真实实现上（不 mock loader）。

## 已知边界

- 不做动态分析：动态构造的 `FrontendContribution` 只能报 `PD026`，离线无法核对入口文件。
- 不检查插件业务逻辑与运行时行为（依赖是否真能装上、RPC 处理是否正确等）。
- 宿主版本取不到时（既无 dist-info 也无相邻 `pyproject.toml`）跳过 `PD020`，不臆测。
- 依据行号随宿主重构漂移：测试只保证「依据文件存在、行号在文件范围内」，行号本身需人工复核。

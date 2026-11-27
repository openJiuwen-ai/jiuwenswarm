# openjiuwen-skillsdispatch

`openjiuwen-skillsdispatch` 是 openJiuwen 的技能分发能力包，用于基于 skill/plugin 目录构建 Capability Tree 索引。

## 目录结构

```text
dispatch/
├── build.py
├── indexing/
│   ├── catalog/
│   ├── io/
│   ├── scanners/
│   ├── tree/
│   └── workflows/
├── shared/
├── pyproject.toml
└── README.md
```

## 核心能力

- `indexing.scanners`：扫描 skill/plugin 目录。
- `indexing.tree`：基于 LLM 或 fallback 逻辑构建 Capability Tree。
- `indexing.workflows`：封装索引构建工作流，输出 tree、catalog、manifest 等产物。
- `shared`：提供 Rich 兼容层和 S3/OBS 存储工具。

## 安装

本地开发验证：

```bash
cd marketplace/dispatch
python -m pip install -e .
```

发布到 pip 源后安装：

```bash
python -m pip install openjiuwen-skillsdispatch
```

## SDK 使用

```python
from indexing.workflows.artifacts import BuildConfig, BuildMethod
from indexing.workflows.index_builder import IndexBuilder

config = BuildConfig(
    method=BuildMethod.TREE,
    llm_model="your-model",
    llm_api_key="your-api-key",
    llm_base_url="your-base-url",
)

IndexBuilder.build(
    item_paths=["/path/to/skill-or-plugin"],
    output_dir="/path/to/output-index",
    item_type="skill",
    config=config,
)
```

如果没有配置 LLM，默认允许走 fallback tree，用于本地 smoke test 和基础构建链路验证。真实树构建效果仍建议配置 LLM。

## 构建产物

构建完成后，输出目录通常包含：

- `manifest.json`
- `tree_index.yaml`
- `tree_index.html`
- `catalog.jsonl`

## 构建 wheel

在 `marketplace/dispatch` 目录下执行：

```bash
python -m pip wheel . --no-deps --no-build-isolation -w dist
```

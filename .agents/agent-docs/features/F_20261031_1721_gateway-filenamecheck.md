---
id: F_20261031_1721_gateway-filenamecheck
title: Gateway 4.2 文件名校验引用归位
type: feature
author: ruiqiang2
author_email: renruiqiang@huawei.com
modules:
  - gateway/channel_manager/web
files:
  - gateway/channel_manager/web/enterprise_files.py
  - tests/unit_tests/gateway/test_enterprise_files.py
designs: []
design_note: 按用户确认的 4.2 工作项收口已有共享实现，无独立设计。
---

# Gateway 4.2 文件名校验引用归位

基线为 develop `222df1638`，实施分支为 `gateway-dep-4.2-filenamecheck`。
工作项来源：`/Users/richard/Documents/work/notes/dev-stable_standalone/2026-10-30-develop-gateway-dependency-analysis.md` 的 4.2。

## 实施决策

基线已将纯校验实现下沉到 `gateway.common.attachment_safety`，Server 的
`upload_storage` 已从此处重新导出。企业文件路由改为直接引用同一实现，清除
Gateway 对 Server 的这处反向依赖。Server 兼容导出保留，清洗算法、上传接口和租户路径构造不变。
测试也直接引用 Gateway 的 OBS 实现，并扩展真实 HTTP 上传入口的特殊文件名用例。

## 验证

使用主检出已有 Python 3.14 虚拟环境，测试运行数据写入 `/private/tmp/gateway-filenamecheck-test`。
初次运行因默认日志目录不可写而在收集阶段退出；指定测试数据目录后通过。

```bash
JIUWENSWARM_DATA_DIR=/private/tmp/gateway-filenamecheck-test /Users/richard/Documents/aitools/jiuwen/repos/jiuwenswarm/.venv/bin/python -m pytest -o addopts='' tests/unit_tests/gateway/test_upload_storage.py tests/unit_tests/gateway/test_enterprise_files.py --asyncio-mode=auto -q
```

结果：55 passed。覆盖 Unicode、路径穿越、保留名、空名、双向控制字符、字节截断、
实际上传参数及租户作用域清理。OBS 使用测试替身，未访问真实对象存储。
另在全新 Python 进程阻断所有 `jiuwenswarm` 导入，成功导入企业文件路由并执行中文文件名清洗。
该证据仅验证本模块导入自包含，不代表完整独立安装或部署验收。

未改变对外契约，无对应 spec 变更；本次范围没有已知遗留。未提交、推送或创建 PR。

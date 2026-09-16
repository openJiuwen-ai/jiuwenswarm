---
name: static-check
description: >-
  Fix jiuwenswarm / openJiuwen static-check (pylint-style) findings such as
  G.FMT.07 wrong-import-order, G.CLS.11 protected-access, and G.CLS.07
  add-staticmethod-or-classmethod-decorator. Use when the user pastes CI
  静态检查结果、提到 G.FMT / G.CLS、pylint、wrong-import-order、protected-access，
  or asks to fix static analysis / lint gate on Python changes.
---

# jiuwenswarm 静态检查修复

收到 CI / 门禁静态检查报错时：按规则改代码，不要用大面积 `# pylint: disable` 糊弄（除非规则明确不适用且无更优写法）。

## 导入顺序 — G.FMT.07 (`wrong-import-order`)

顺序必须是：

1. `from __future__ import annotations`（若有）
2. **标准库**（如 `logging` / `os` / `typing`）
3. **第三方库**
4. **本应用模块**（`jiuwenswarm.*` 等）

组与组之间空一行。不要把 `jiuwenswarm...` 插在标准库之前。

```python
# ✅
from __future__ import annotations

import logging
import os
from typing import Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.schema.agent import AgentRequest

# ❌ 应用 import 夹在标准库前
from jiuwenswarm.edition import is_enterprise
import logging
from typing import Any
```

## 受保护成员 — G.CLS.11 (`protected-access`)

不要在类外（或无关客户端代码里）读写他方对象的 `_xxx` 属性。

可选改法（按优先级）：

1. 改用**公开**标记名（无前导 `_`），并用 `getattr` / `setattr`
2. 状态放到**本模块**变量（如 `set[int]` 记录已 patch 的 `id(cls)`）
3. 确属 monkey-patch 且无法改名时，对**单行**加 disable，并注释原因

```python
# ✅ monkey-patch 幂等标记
FLAG = "jiuwenswarm_runner_perf_patched"
if getattr(impl_cls, FLAG, False):
    return
setattr(impl_cls, FLAG, True)

# ❌
impl_cls._jiuwenswarm_runner_perf_patched = True
```

访问**本模块自己**定义的 `_HELPER` 函数/常量一般没问题；规则针对的是「别人类上的 protected 成员」。

## 无实例访问的方法 — G.CLS.07 (`add-staticmethod-or-classmethod-decorator`)

方法体不用 `self` / `cls` 时：

- 加 `@staticmethod`，去掉 `self` 参数；或
- 需要类对象时用 `@classmethod` + `cls`

调用方可继续写 `self.foo(...)`（对 staticmethod 合法）。

```python
# ✅
@staticmethod
def _set_rail_sys_operation(rail: Any, sysop: SysOperation) -> bool:
    ...

# ❌ 签名有 self 但从未使用
def _set_rail_sys_operation(self, rail: Any, sysop: SysOperation) -> bool:
    ...
```

## 工作流

1. 逐条对照报错：`文件:行` + 规则 ID / 消息
2. 优先结构性修复（调换 import、加装饰器、公开标记），少 disable
3. 改完后对触及文件做语法/相关 UT 冒烟
4. 回复里用短列表说明「规则 → 改法」，方便审阅

## 常见误报处理

| 场景 | 建议 |
|------|------|
| 给第三方/上游类打 patch | 公开 flag 名 + `setattr`，勿设 `_private` 到客户类上 |
| `getattr(obj, "_prepare_agent")` 读上游私有 API | 若门禁已报，改为 `getattr(obj, name)` 且 `name` 为变量；仍报再单行 disable 并注明上游 API |
| 仅类型检查需要的 import | 可用 `typing.TYPE_CHECKING` 推迟，但仍遵守组间顺序 |

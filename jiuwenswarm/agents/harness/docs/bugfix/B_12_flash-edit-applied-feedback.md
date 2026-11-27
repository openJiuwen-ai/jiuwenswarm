# Flash 重复提交已应用编辑时补充核实提示

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 日期 | 2026-11-27 |
| 问题 | BUG20261125416319 的 Flash 同类场景 |
| 范围 | `flash/tools/flash_edit_tool.py`、对应单测、core 依赖锁 |

## 现象与根因

成功编辑后再次提交原参数，旧文本已经不存在，工具返回失败。原单记录为 Code 模式，模型持续重复编辑直至循环护栏中止；core 开发人员随后在 Flash 验证中发现，core 新增的“可能已应用，请读取核实”提示没有生效。

`FlashEditFileTool` 继承 `EditFileTool.invoke` 的每文件锁，但覆写了 `_invoke_unlocked`。父类通过 `self` 调用编辑方法时进入 Flash 实现，因此父类匹配失败分支的加固不会自动执行。Flash 在对应位置仍直接拼接 `old_string not found`。

## 决策与边界

Flash 在普通匹配与引号容错都失败后，调用 core 提供的 `_old_string_not_found_error`，传入清洗后的旧新文本、当前内容快照和现有 `where`。父类 helper 判断新文本存在且与旧文本相似时，返回带位置的可能已应用提示及 `read_file` 建议；仍保持 `success=False`。

helper 是纯文本判断，不增加读取、写入或跨调用状态。多 hunk 中任一项失败仍在写回之前返回，保留批量原子性。单元素 hunks 继续使用 `old_string`，多元素使用 `hunks[i]`。编辑锁、缓存、CAS、换行和 JSONL history 机制沿用既有行为。

实际 core 必须包含 [agent-core !4013](https://gitcode.com/openJiuwen/agent-core/pull/4013)。本次只将 `uv.lock` 的 core 解析提交更新到含该修复的 `dev-stable` 合并提交；新旧提交的 `pyproject.toml` 相同，其他锁定包不变。

未复制 helper，避免维护两套相似度、防误报与引号兼容规则；未将“新文本存在”直接判为成功，也未跳过失败 hunk 继续写入，因为启发式提示不能证明整项编辑完成。未修改循环阈值、模型配置或 Relay。

## 验证与限制

Linux、Python 3.13.15，本地 editable core 与更新后的锁定提交一致。通过工具公开 `invoke` 使用真实临时文件验证：

- 新增 10 个用例；修改前 6 个提示用例失败、4 个防误报用例通过；修改后编辑测试文件共 36 项通过。
- 成功后原参数重试覆盖缓存／全量读取、传统参数／单元素 hunks；确认提示含位置与读取建议，文件和历史不变，无额外写入，缓存分支无额外读取。
- 负例覆盖泛化新串、空新串、缩短编辑和旧新文本均缺失；另验证多 hunk 失败时不应用已规划修改，以及直／弯引号重试。
- 与 `test_flash_file_guard.py`、`test_flash_adapter_tool_face.py` 组合运行共 77 项通过。现有环境缺少锁定的 `openjiuwen-protocol`，首次组合收集失败；在临时目录安装该锁定版本并加入测试 `PYTHONPATH` 后通过，未更改共享虚拟环境。
- `uv lock --check --offline` 通过。

未启动产品实例，未验证现场模型是否采纳提示、原 Code 场景闭合或 Windows 目标构建。工具反馈改善不能代替上述产品验收。core 私有 helper 后续升级时，需保留 Flash 真实编辑与重试回归以发现签名或语义漂移。

# Python 代码质量

适用于 `tools/`、`rails/`、`skills/<name>/scripts/` 下的 Python 源文件。

## 语言与风格

- Python 3.11+
- 使用项目现有命名和缩进风格
- 新增公共函数必须有类型注解和 docstring
- 不添加不必要的注释

## import 纪律

- 每个 import 语句必须在代码中有明确的调用位置
- 禁止保留未使用的 import（ruff F401 会报错）
- 禁止为了「可能的未来使用」而提前导入
- 自查：确认代码中每个导入的模块/类都被实际调用

## 文件编码（Windows 必守）

- 写入文件：`Path.write_text(content, encoding="utf-8")`
- 读取文件：`Path.read_text(encoding="utf-8")`
- 打开文件：`open(path, encoding="utf-8")`
- 禁止省略 `encoding` 参数；Windows 默认 GBK 会导致中文乱码

## 生成后自检

对每个新生成或修改的 `.py`：

```bash
python -c "import ast; ast.parse(open(r'<file>', encoding='utf-8').read())"
ruff check <file>
ruff format --check <file>
```

生成的代码必须能通过 `ruff check` 和 `ruff format --check`。

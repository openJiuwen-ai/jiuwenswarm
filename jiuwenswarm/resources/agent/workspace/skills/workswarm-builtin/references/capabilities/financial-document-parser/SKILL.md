---
name: financial-document-parser
description: Extract and analyze data from invoices, receipts, bank statements, and financial documents. Categorize expenses, track recurring charges, and generate expense reports. Use when user provides financial PDFs or images.
---

# Financial Document Parser

解析财务文档（发票、收据、银行对账单）并提取结构化数据。

## 核心脚本

本 skill 包含一个可复用的 Python 脚本：`financial_parser.py`

### 依赖安装

```bash
pip install pdfplumber

# 可选：OCR 支持（用于扫描版 PDF 和图片）
pip install pdf2image pytesseract
# 还需要安装 tesseract-ocr 系统包
# Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-chi-sim
```

### 命令行用法

```bash
# 解析 PDF 发票，输出 Markdown 报告
python financial_parser.py invoice.pdf

# 输出 JSON 格式
python financial_parser.py invoice.pdf --format json

# 导出为 CSV
python financial_parser.py invoice.pdf --format csv

# 解析图片收据
python financial_parser.py receipt.jpg

# 解析 CSV 银行对账单
python financial_parser.py statement.csv

# 完整输出（Markdown + JSON）
python financial_parser.py invoice.pdf --format all
```

### Python API 用法

```python
from financial_parser import FinancialParser

# 解析文档
parser = FinancialParser("/path/to/invoice.pdf")
doc = parser.parse()

# 获取结构化数据
print(doc.doc_type)      # Invoice, Receipt, Statement
print(doc.total)         # 总金额
print(doc.line_items)    # 明细项目列表

# 导出
print(parser.to_markdown())  # Markdown 报告
print(parser.to_json())      # JSON 数据
parser.to_csv("output.csv")  # CSV 文件
```

## When to Use This Skill

当用户：
- 提供发票、收据或银行对账单文件
- 要求 "解析这张发票" 或 "提取收据数据"
- 需要费用分类
- 想要追踪消费模式
- 要求生成费用报告
- 提供 PDF 或图片格式的财务文档

## 执行流程

1. **确认文件路径** - 获取用户提供的文件路径
2. **运行解析脚本** - 使用 Bash 工具执行：
   ```bash
   python financial_parser.py <文件路径> --format all
   ```
3. **展示结果** - 将解析结果展示给用户
4. **按需导出** - 如用户需要，导出 CSV 或 JSON

## 支持的文档类型

| 类型 | 格式 | 提取内容 |
|------|------|----------|
| 发票 | PDF | 发票号、日期、供应商、明细、税额、总额 |
| 收据 | PDF/图片 | 商户、日期、商品、金额 |
| 银行对账单 | PDF/CSV | 交易明细、余额、费用 |
| 信用卡账单 | PDF | 交易记录、还款信息 |

## 费用分类

脚本自动将费用分类为：
- **Software**: 软件、订阅、云服务
- **Office**: 办公用品、打印
- **Travel**: 差旅、机票、酒店
- **Meals**: 餐饮、外卖
- **Utilities**: 水电、网费
- **Marketing**: 广告、推广
- **Professional**: 咨询、法律、会计
- **Equipment**: 设备、硬件
- **Other**: 其他

## 输出示例

```markdown
# 财务文档分析报告

## 文档信息
- **类型**: Invoice
- **日期**: 2025-01-15
- **单据号**: INV-2025-0042
- **供应商**: 某某科技有限公司
- **总金额**: ¥12,580.00

## 明细项目
| 描述 | 数量 | 单价 | 金额 | 类别 |
|------|------|------|------|------|
| 云服务器年费 | 1 | ¥9,800.00 | ¥9,800.00 | Software |
| 技术支持服务 | 1 | ¥2,000.00 | ¥2,000.00 | Professional |

## 财务汇总
- **小计**: ¥11,150.94
- **税额**: ¥1,429.06
- **总计**: ¥12,580.00

## 费用分类
| 类别 | 金额 |
|------|------|
| Software | ¥9,800.00 |
| Professional | ¥2,000.00 |

## 洞察
- ✓ 最大支出类别: Software (¥9,800.00)
- ✓ 可抵扣税额: ¥1,429.06

## 需关注项
- ⚠ 大额交易: 云服务器年费 (¥9,800.00)
```

## 批量处理

处理多个文件：

```bash
# 批量解析目录下所有 PDF
for f in /path/to/invoices/*.pdf; do
  python financial_parser.py "$f" --format csv
done
```

## 注意事项

- 保持金额精确，不要四舍五入
- 敏感信息（账号）会自动脱敏
- 如果文本提取失败，会自动尝试 OCR
- 支持中英文混合文档

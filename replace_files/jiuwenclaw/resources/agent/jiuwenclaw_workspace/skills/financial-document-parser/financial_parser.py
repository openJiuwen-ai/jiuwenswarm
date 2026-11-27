#!/usr/bin/env python3
"""
Financial Document Parser - 财务文档解析工具
支持解析 PDF 发票、收据、银行对账单等财务文档
"""

import argparse
import json
import csv
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

# PDF 解析依赖
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    from pdf2image import convert_from_path
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


@dataclass
class LineItem:
    """单行项目"""
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0
    category: str = "Other"


@dataclass
class FinancialDocument:
    """财务文档数据结构"""
    doc_type: str = "Unknown"  # Invoice, Receipt, Statement
    doc_number: str = ""
    date: str = ""
    due_date: str = ""
    vendor_name: str = ""
    vendor_address: str = ""
    client_name: str = ""
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    currency: str = "CNY"
    payment_method: str = ""
    line_items: list = field(default_factory=list)
    raw_text: str = ""
    insights: list = field(default_factory=list)
    flags: list = field(default_factory=list)


class FinancialParser:
    """财务文档解析器"""

    # 费用分类关键词
    CATEGORY_KEYWORDS = {
        "Software": ["软件", "订阅", "云服务", "saas", "adobe", "microsoft", "github", "slack"],
        "Office": ["办公", "文具", "打印", "复印", "办公用品"],
        "Travel": ["差旅", "机票", "火车", "酒店", "住宿", "交通", "出租车", "滴滴"],
        "Meals": ["餐饮", "餐费", "午餐", "晚餐", "外卖", "美团", "饿了么"],
        "Utilities": ["水电", "电费", "水费", "网费", "电话费", "宽带"],
        "Marketing": ["广告", "推广", "营销", "市场"],
        "Professional": ["咨询", "法律", "会计", "审计", "顾问"],
        "Equipment": ["设备", "电脑", "硬件", "服务器"],
    }

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.doc = FinancialDocument()

    def parse(self) -> FinancialDocument:
        """解析文档"""
        if not self.file_path.exists():
            raise FileNotFoundError(f"文件不存在: {self.file_path}")

        suffix = self.file_path.suffix.lower()

        if suffix == ".pdf":
            self._parse_pdf()
        elif suffix in [".png", ".jpg", ".jpeg"]:
            self._parse_image()
        elif suffix == ".csv":
            self._parse_csv()
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")

        # 后处理
        self._detect_doc_type()
        self._categorize_items()
        self._generate_insights()

        return self.doc

    def _parse_pdf(self):
        """解析 PDF 文件"""
        if not HAS_PDFPLUMBER:
            raise ImportError("需要安装 pdfplumber: pip install pdfplumber")

        text_content = []
        tables = []

        with pdfplumber.open(self.file_path) as pdf:
            for page in pdf.pages:
                # 提取文本
                text = page.extract_text()
                if text:
                    text_content.append(text)

                # 提取表格
                page_tables = page.extract_tables()
                if page_tables:
                    tables.extend(page_tables)

        self.doc.raw_text = "\n".join(text_content)

        # 如果文本提取失败，尝试 OCR
        if not self.doc.raw_text.strip() and HAS_OCR:
            self._ocr_pdf()

        # 解析提取的内容
        self._extract_fields_from_text()
        self._extract_items_from_tables(tables)

    def _ocr_pdf(self):
        """使用 OCR 处理扫描版 PDF"""
        if not HAS_OCR:
            return

        images = convert_from_path(self.file_path)
        text_parts = []

        for img in images:
            text = pytesseract.image_to_string(img, lang='chi_sim+eng')
            text_parts.append(text)

        self.doc.raw_text = "\n".join(text_parts)
        self._extract_fields_from_text()

    def _parse_image(self):
        """解析图片文件"""
        if not HAS_OCR:
            raise ImportError("需要安装 OCR 依赖: pip install pdf2image pytesseract")

        from PIL import Image
        img = Image.open(self.file_path)
        self.doc.raw_text = pytesseract.image_to_string(img, lang='chi_sim+eng')
        self._extract_fields_from_text()

    def _parse_csv(self):
        """解析 CSV 银行对账单"""
        with open(self.file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                item = LineItem(
                    description=row.get('描述', row.get('description', row.get('摘要', ''))),
                    total=self._parse_amount(row.get('金额', row.get('amount', '0')))
                )
                self.doc.line_items.append(item)

        self.doc.doc_type = "Statement"
        self.doc.total = sum(item.total for item in self.doc.line_items)

    def _extract_fields_from_text(self):
        """从文本中提取字段"""
        import re
        text = self.doc.raw_text
        lines = text.split('\n')

        # 提取发票号 - 支持多种格式
        invoice_patterns = [
            r'Invoice\s+number\s+([A-Z0-9][\w\-\x00]+)',
            r'Invoice\s*(?:no\.?|#)[:\s]*([A-Z0-9][\w\-]+)',
            r'发票号[码]?[：:]\s*(\S+)',
            r'票号[：:]\s*(\S+)',
        ]
        for pattern in invoice_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                self.doc.doc_number = match.group(1).strip()
                break

        # 提取供应商名称
        vendor_patterns = [
            r'([A-Z][A-Za-z0-9\s&]+(?:GmbH|LLC|Inc|Ltd|Co\.|Corp|Corporation))',
            r'From[:\s]+([^\n]+)',
            r'供应商[：:]\s*([^\n]+)',
            r'销售方[：:]\s*([^\n]+)',
        ]
        for pattern in vendor_patterns:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                vendor = match.group(1).strip()
                # 清理前缀
                for prefix in ['Invoice ', 'Receipt ']:
                    if vendor.startswith(prefix):
                        vendor = vendor[len(prefix):]
                self.doc.vendor_name = vendor.strip()
                break

        # 提取日期 - 支持多种格式
        date_patterns = [
            r'(?:Date\s*(?:of\s*issue)?|Issue\s*date)[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
            r'(?:Date\s*(?:of\s*issue)?|Issue\s*date)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{4})',
            r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)',
            r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',
            r'([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                self.doc.date = match.group(1).strip()
                break

        # 提取货币类型
        if 'USD' in text or '$' in text:
            self.doc.currency = 'USD'
        elif 'EUR' in text or '€' in text:
            self.doc.currency = 'EUR'
        elif '¥' in text or '￥' in text or 'CNY' in text or 'RMB' in text:
            self.doc.currency = 'CNY'

        # 提取金额 - 支持多种格式
        amount_patterns = [
            r'(?:Amount\s*due|Total\s*due)[:\s]*[\$€¥￥]?\s*([\d,]+\.?\d*)',
            r'Total[:\s]+[\$€¥￥]?\s*([\d,]+\.?\d*)',
            r'合计[：:]\s*[¥￥]?\s*([\d,]+\.?\d*)',
            r'总[计额][：:]\s*[¥￥]?\s*([\d,]+\.?\d*)',
            r'[\$€]\s*([\d,]+\.?\d*)\s*(?:USD|EUR)?(?:\s+due)?',
        ]
        for pattern in amount_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                self.doc.total = self._parse_amount(match.group(1))
                break

        # 提取小计
        subtotal_patterns = [
            r'Subtotal[:\s]+[\$€¥￥]?\s*([\d,]+\.?\d*)',
            r'小计[：:]\s*[¥￥]?\s*([\d,]+\.?\d*)',
        ]
        for pattern in subtotal_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                self.doc.subtotal = self._parse_amount(match.group(1))
                break

        # 提取税额
        tax_patterns = [
            r'(?:Tax|VAT)[:\s]+[\$€¥￥]?\s*([\d,]+\.?\d*)',
            r'税[额款][：:]\s*[¥￥]?\s*([\d,]+\.?\d*)',
        ]
        for pattern in tax_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                self.doc.tax = self._parse_amount(match.group(1))
                break

        # 从文本中提取行项目（如果表格提取失败）
        self._extract_items_from_text(text)

        # 计算小计（如果未提取到）
        if self.doc.total and self.doc.tax and not self.doc.subtotal:
            self.doc.subtotal = self.doc.total - self.doc.tax
        elif self.doc.total and not self.doc.subtotal:
            self.doc.subtotal = self.doc.total

    def _extract_items_from_text(self, text: str):
        """从文本中提取行项目"""
        import re
        # 匹配类似 "Description Qty Unit price Amount" 后的行
        # 例如: "SEC API (per API Key) 1 $55.00 $55.00"
        item_pattern = r'([A-Za-z][^\n$€¥]+?)\s+(\d+)\s+[\$€¥]?([\d,]+\.?\d*)\s+[\$€¥]?([\d,]+\.?\d*)'

        matches = re.findall(item_pattern, text)
        for match in matches:
            desc, qty, unit_price, total = match
            # 过滤掉表头行
            if any(kw in desc.lower() for kw in ['description', 'qty', 'quantity', 'unit', 'amount', 'subtotal', 'total']):
                continue
            item = LineItem(
                description=desc.strip(),
                quantity=self._parse_amount(qty),
                unit_price=self._parse_amount(unit_price),
                total=self._parse_amount(total),
            )
            if item.total > 0:
                self.doc.line_items.append(item)

    def _extract_items_from_tables(self, tables: list):
        """从表格中提取行项目"""
        for table in tables:
            if not table or len(table) < 2:
                continue

            # 尝试识别表头
            header = table[0]
            if not header:
                continue

            # 查找关键列
            desc_col = None
            qty_col = None
            price_col = None
            total_col = None

            for i, cell in enumerate(header):
                if not cell:
                    continue
                cell_lower = str(cell).lower()
                if any(k in cell_lower for k in ['名称', '描述', '项目', 'description', 'item']):
                    desc_col = i
                elif any(k in cell_lower for k in ['数量', 'qty', 'quantity']):
                    qty_col = i
                elif any(k in cell_lower for k in ['单价', 'price', 'unit']):
                    price_col = i
                elif any(k in cell_lower for k in ['金额', '合计', 'amount', 'total']):
                    total_col = i

            # 提取数据行
            for row in table[1:]:
                if not row or not any(row):
                    continue

                item = LineItem(
                    description=str(row[desc_col]) if desc_col is not None and desc_col < len(row) else "",
                    quantity=self._parse_amount(row[qty_col]) if qty_col is not None and qty_col < len(row) else 1.0,
                    unit_price=self._parse_amount(row[price_col]) if price_col is not None and price_col < len(row) else 0.0,
                    total=self._parse_amount(row[total_col]) if total_col is not None and total_col < len(row) else 0.0,
                )

                if item.description or item.total:
                    self.doc.line_items.append(item)

    def _parse_amount(self, value) -> float:
        """解析金额字符串"""
        if not value:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)

        # 清理字符串
        s = str(value).replace(',', '').replace('¥', '').replace('￥', '').replace('$', '').strip()
        try:
            return float(s)
        except ValueError:
            return 0.0

    def _detect_doc_type(self):
        """检测文档类型"""
        text = self.doc.raw_text.lower()

        if any(k in text for k in ['发票', 'invoice', '增值税']):
            self.doc.doc_type = "Invoice"
        elif any(k in text for k in ['收据', 'receipt', '小票']):
            self.doc.doc_type = "Receipt"
        elif any(k in text for k in ['对账单', 'statement', '账单', '交易明细']):
            self.doc.doc_type = "Statement"
        elif any(k in text for k in ['报销', 'expense']):
            self.doc.doc_type = "Expense Report"

    def _categorize_items(self):
        """对行项目进行分类"""
        for item in self.doc.line_items:
            desc_lower = item.description.lower()

            for category, keywords in self.CATEGORY_KEYWORDS.items():
                if any(kw in desc_lower for kw in keywords):
                    item.category = category
                    break

    def _generate_insights(self):
        """生成洞察"""
        # 按类别汇总
        category_totals = {}
        for item in self.doc.line_items:
            cat = item.category
            category_totals[cat] = category_totals.get(cat, 0) + item.total

        if category_totals:
            top_category = max(category_totals.keys(), key=lambda k: category_totals[k])
            self.doc.insights.append(f"最大支出类别: {top_category} (¥{category_totals[top_category]:.2f})")

        # 检测大额交易
        for item in self.doc.line_items:
            if item.total > 10000:
                self.doc.flags.append(f"大额交易: {item.description} (¥{item.total:.2f})")

        # 税务相关
        if self.doc.tax > 0:
            self.doc.insights.append(f"可抵扣税额: ¥{self.doc.tax:.2f}")

    def to_dict(self) -> dict:
        """转换为字典"""
        result = asdict(self.doc)
        result['line_items'] = [asdict(item) for item in self.doc.line_items]
        return result

    def to_json(self) -> str:
        """转换为 JSON"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_csv(self, output_path: Optional[str] = None) -> str:
        """导出为 CSV"""
        final_path: str = output_path if output_path else str(self.file_path.with_suffix('.csv'))

        with open(final_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['日期', '供应商', '描述', '类别', '金额', '可抵税'])

            for item in self.doc.line_items:
                writer.writerow([
                    self.doc.date,
                    self.doc.vendor_name,
                    item.description,
                    item.category,
                    item.total,
                    'Yes' if item.category != 'Other' else 'No'
                ])

        return final_path

    def to_markdown(self) -> str:
        """生成 Markdown 报告"""
        lines = [
            "# 财务文档分析报告",
            "",
            "## 文档信息",
            f"- **类型**: {self.doc.doc_type}",
            f"- **日期**: {self.doc.date or '未识别'}",
            f"- **单据号**: {self.doc.doc_number or '未识别'}",
            f"- **供应商**: {self.doc.vendor_name or '未识别'}",
            f"- **总金额**: ¥{self.doc.total:,.2f}",
            "",
        ]

        if self.doc.line_items:
            lines.extend([
                "## 明细项目",
                "| 描述 | 数量 | 单价 | 金额 | 类别 |",
                "|------|------|------|------|------|",
            ])
            for item in self.doc.line_items:
                lines.append(
                    f"| {item.description[:30]} | {item.quantity} | ¥{item.unit_price:.2f} | ¥{item.total:.2f} | {item.category} |"
                )
            lines.append("")

        lines.extend([
            "## 财务汇总",
            f"- **小计**: ¥{self.doc.subtotal:,.2f}",
            f"- **税额**: ¥{self.doc.tax:,.2f}",
            f"- **总计**: ¥{self.doc.total:,.2f}",
            "",
        ])

        # 按类别汇总
        category_totals = {}
        for item in self.doc.line_items:
            cat = item.category
            category_totals[cat] = category_totals.get(cat, 0) + item.total

        if category_totals:
            lines.extend([
                "## 费用分类",
                "| 类别 | 金额 |",
                "|------|------|",
            ])
            for cat, total in sorted(category_totals.items(), key=lambda x: -x[1]):
                lines.append(f"| {cat} | ¥{total:,.2f} |")
            lines.append("")

        if self.doc.insights:
            lines.extend(["## 洞察", ""])
            for insight in self.doc.insights:
                lines.append(f"- ✓ {insight}")
            lines.append("")

        if self.doc.flags:
            lines.extend(["## 需关注项", ""])
            for flag in self.doc.flags:
                lines.append(f"- ⚠ {flag}")
            lines.append("")

        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='财务文档解析工具 - 解析发票、收据、银行对账单',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s invoice.pdf                    # 解析 PDF 并输出 Markdown
  %(prog)s invoice.pdf --format json      # 输出 JSON 格式
  %(prog)s invoice.pdf --format csv       # 导出为 CSV
  %(prog)s receipt.jpg                    # 解析图片收据
  %(prog)s statement.csv                  # 解析 CSV 对账单
        """
    )

    parser.add_argument('file', help='要解析的文件路径 (PDF/图片/CSV)')
    parser.add_argument('--format', '-f', choices=['markdown', 'json', 'csv', 'all'],
                        default='markdown', help='输出格式 (默认: markdown)')
    parser.add_argument('--output', '-o', help='输出文件路径 (仅用于 csv 格式)')
    parser.add_argument('--quiet', '-q', action='store_true', help='静默模式，只输出结果')

    args = parser.parse_args()

    if not args.quiet:
        print(f"正在解析: {args.file}", file=sys.stderr)

    try:
        parser_obj = FinancialParser(args.file)
        doc = parser_obj.parse()

        if args.format == 'json':
            print(parser_obj.to_json())
        elif args.format == 'csv':
            csv_path = parser_obj.to_csv(args.output)
            if not args.quiet:
                print(f"已导出到: {csv_path}", file=sys.stderr)
        elif args.format == 'all':
            print(parser_obj.to_markdown())
            print("\n---\n")
            print("## JSON 数据")
            print("```json")
            print(parser_obj.to_json())
            print("```")
        else:
            print(parser_obj.to_markdown())

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

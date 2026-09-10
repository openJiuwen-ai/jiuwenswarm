---
# ═══════════════════════════════════════════════════════════════════════════════
# CLAUDE OFFICE SKILL - Enhanced Metadata v2.0
# ═══════════════════════════════════════════════════════════════════════════════

# Basic Information
name: pdf-extraction
description: "Extract text, tables, and metadata from PDFs using pdfplumber"
version: "1.0"
author: claude-office-skills
license: MIT

# Categorization
category: pdf
tags:
  - pdf
  - extraction
  - text
  - tables
department: All

# AI Model Compatibility
models:
  recommended:
    - claude-sonnet-4
    - claude-opus-4
  compatible:
    - claude-3-5-sonnet
    - gpt-4
    - gpt-4o

# MCP Tools Integration
mcp:
  server: office-mcp
  tools:
    - extract_text_from_pdf
    - extract_tables_from_pdf

# Skill Capabilities
capabilities:
  - text_extraction
  - table_extraction
  - data_mining

# Language Support
languages:
  - en
  - zh
---

# PDF Extraction Skill

## Overview

This skill enables precise extraction of text, tables, and metadata from PDF documents using **pdfplumber** - the go-to library for PDF data extraction. Unlike basic PDF readers, pdfplumber provides detailed character-level positioning, accurate table detection, and visual debugging.

## How to Use

1. Provide the PDF file you want to extract from
2. Specify what you need: text, tables, images, or metadata
3. I'll generate pdfplumber code and execute it

**Example prompts:**
- "Extract all tables from this financial report"
- "Get text from pages 5-10 of this document"
- "Find and extract the invoice total from this PDF"
- "Convert this PDF table to CSV/Excel"

## Domain Knowledge

### pdfplumber Fundamentals

```python
import pdfplumber

# Open PDF
with pdfplumber.open('document.pdf') as pdf:
    # Access pages
    first_page = pdf.pages[0]
    
    # Document metadata
    print(pdf.metadata)
    
    # Number of pages
    print(len(pdf.pages))
```

### PDF Structure
```
PDF Document
├── metadata (title, author, creation date)
├── pages[]
│   ├── chars (individual characters with position)
│   ├── words (grouped characters)
│   ├── lines (horizontal/vertical lines)
│   ├── rects (rectangles)
│   ├── curves (bezier curves)
│   └── images (embedded images)
└── outline (bookmarks/TOC)
```

### Text Extraction

#### Basic Text
```python
with pdfplumber.open('document.pdf') as pdf:
    # Single page
    text = pdf.pages[0].extract_text()
    
    # All pages
    full_text = ''
    for page in pdf.pages:
        full_text += page.extract_text() or ''
```

#### Advanced Text Options
```python
# With layout preservation
text = page.extract_text(
    x_tolerance=3,      # Horizontal tolerance for grouping
    y_tolerance=3,      # Vertical tolerance
    layout=True,        # Preserve layout
    x_density=7.25,     # Chars per unit width
    y_density=13        # Chars per unit height
)

# Extract words with positions
words = page.extract_words(
    x_tolerance=3,
    y_tolerance=3,
    keep_blank_chars=False,
    use_text_flow=False
)

# Each word includes: text, x0, top, x1, bottom, etc.
for word in words:
    print(f"{word['text']} at ({word['x0']}, {word['top']})")
```

#### Character-Level Access
```python
# Get all characters
chars = page.chars

for char in chars:
    print(f"'{char['text']}' at ({char['x0']}, {char['top']})")
    print(f"  Font: {char['fontname']}, Size: {char['size']}")
```

### Table Extraction

#### Basic Table Extraction
```python
with pdfplumber.open('report.pdf') as pdf:
    page = pdf.pages[0]
    
    # Extract all tables
    tables = page.extract_tables()
    
    for i, table in enumerate(tables):
        print(f"Table {i+1}:")
        for row in table:
            print(row)
```

#### Advanced Table Settings
```python
# Custom table detection
table_settings = {
    "vertical_strategy": "lines",      # or "text", "explicit"
    "horizontal_strategy": "lines",
    "explicit_vertical_lines": [],     # Custom line positions
    "explicit_horizontal_lines": [],
    "snap_tolerance": 3,
    "snap_x_tolerance": 3,
    "snap_y_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length": 3,
    "min_words_vertical": 3,
    "min_words_horizontal": 1,
    "intersection_tolerance": 3,
    "text_tolerance": 3,
    "text_x_tolerance": 3,
    "text_y_tolerance": 3,
}

tables = page.extract_tables(table_settings)
```

#### Table Finding
```python
# Find tables (without extracting)
table_finder = page.find_tables()

for table in table_finder:
    print(f"Table at: {table.bbox}")  # (x0, top, x1, bottom)
    
    # Extract specific table
    data = table.extract()
```

### Visual Debugging

```python
# Create visual debug image
im = page.to_image(resolution=150)

# Draw detected objects
im.draw_rects(page.chars)        # Character bounding boxes
im.draw_rects(page.words)        # Word bounding boxes
im.draw_lines(page.lines)        # Lines
im.draw_rects(page.rects)        # Rectangles

# Save debug image
im.save('debug.png')

# Debug tables
im.reset()
im.debug_tablefinder()
im.save('table_debug.png')
```

### Cropping and Filtering

#### Crop to Region
```python
# Define bounding box (x0, top, x1, bottom)
bbox = (0, 0, 300, 200)

# Crop page
cropped = page.crop(bbox)

# Extract from cropped area
text = cropped.extract_text()
tables = cropped.extract_tables()
```

#### Filter by Position
```python
# Filter characters by region
def within_bbox(obj, bbox):
    x0, top, x1, bottom = bbox
    return (obj['x0'] >= x0 and obj['x1'] <= x1 and
            obj['top'] >= top and obj['bottom'] <= bottom)

bbox = (100, 100, 400, 300)
filtered_chars = [c for c in page.chars if within_bbox(c, bbox)]
```

#### Filter by Font
```python
# Get text by font
def extract_by_font(page, font_name):
    chars = [c for c in page.chars if font_name in c['fontname']]
    return ''.join(c['text'] for c in chars)

# Extract bold text (often "Bold" in font name)
bold_text = extract_by_font(page, 'Bold')

# Extract by size
large_chars = [c for c in page.chars if c['size'] > 14]
```

### Metadata and Structure

```python
with pdfplumber.open('document.pdf') as pdf:
    # Document metadata
    meta = pdf.metadata
    print(f"Title: {meta.get('Title')}")
    print(f"Author: {meta.get('Author')}")
    print(f"Created: {meta.get('CreationDate')}")
    
    # Page info
    for i, page in enumerate(pdf.pages):
        print(f"Page {i+1}: {page.width} x {page.height}")
        print(f"  Rotation: {page.rotation}")
```

## Best Practices

1. **Debug Visually**: Use `to_image()` to understand PDF structure
2. **Tune Table Settings**: Adjust tolerances for your specific PDF
3. **Handle Scanned PDFs**: Use OCR first (this skill is for native text)
4. **Process Page by Page**: For large PDFs, avoid loading all at once
5. **Check for Text**: Some PDFs are images - verify text exists

## Common Patterns

### Extract All Tables to DataFrames
```python
import pandas as pd

def pdf_tables_to_dataframes(pdf_path):
    """Extract all tables from PDF as pandas DataFrames."""
    dfs = []
    
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            
            for j, table in enumerate(tables):
                if table and len(table) > 1:
                    # First row as header
                    df = pd.DataFrame(table[1:], columns=table[0])
                    df['_page'] = i + 1
                    df['_table'] = j + 1
                    dfs.append(df)
    
    return dfs
```

### Extract Specific Region
```python
def extract_invoice_amount(pdf_path):
    """Extract amount from typical invoice layout."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        
        # Search for "Total" and get nearby numbers
        words = page.extract_words()
        
        for i, word in enumerate(words):
            if 'total' in word['text'].lower():
                # Look at next few words
                for next_word in words[i+1:i+5]:
                    text = next_word['text'].replace(',', '').replace('$', '')
                    try:
                        return float(text)
                    except ValueError:
                        continue
    
    return None
```

### Multi-column Layout
```python
def extract_columns(page, num_columns=2):
    """Extract text from multi-column layout."""
    width = page.width
    col_width = width / num_columns
    
    columns = []
    for i in range(num_columns):
        x0 = i * col_width
        x1 = (i + 1) * col_width
        
        cropped = page.crop((x0, 0, x1, page.height))
        columns.append(cropped.extract_text())
    
    return columns
```

## Examples

### Example 1: Financial Report Table Extraction
```python
import pdfplumber
import pandas as pd

def extract_financial_tables(pdf_path):
    """Extract tables from financial report and save to Excel."""
    
    with pdfplumber.open(pdf_path) as pdf:
        all_tables = []
        
        for page_num, page in enumerate(pdf.pages):
            # Debug: save table visualization
            im = page.to_image()
            im.debug_tablefinder()
            im.save(f'debug_page_{page_num+1}.png')
            
            # Extract tables
            tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 5,
            })
            
            for table in tables:
                if table and len(table) > 1:
                    # Clean data
                    clean_table = []
                    for row in table:
                        clean_row = [cell.strip() if cell else '' for cell in row]
                        clean_table.append(clean_row)
                    
                    df = pd.DataFrame(clean_table[1:], columns=clean_table[0])
                    df['Source Page'] = page_num + 1
                    all_tables.append(df)
        
        # Save to Excel with multiple sheets
        with pd.ExcelWriter('extracted_tables.xlsx') as writer:
            for i, df in enumerate(all_tables):
                df.to_excel(writer, sheet_name=f'Table_{i+1}', index=False)
        
        return all_tables

tables = extract_financial_tables('annual_report.pdf')
print(f"Extracted {len(tables)} tables")
```

### Example 2: Invoice Data Extraction
```python
import pdfplumber
import re
from datetime import datetime

def extract_invoice_data(pdf_path):
    """Extract structured data from invoice PDF."""
    
    data = {
        'invoice_number': None,
        'date': None,
        'total': None,
        'line_items': []
    }
    
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text()
        
        # Extract invoice number
        inv_match = re.search(r'Invoice\s*#?\s*:?\s*(\w+)', text, re.IGNORECASE)
        if inv_match:
            data['invoice_number'] = inv_match.group(1)
        
        # Extract date
        date_match = re.search(r'Date\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', text)
        if date_match:
            data['date'] = date_match.group(1)
        
        # Extract total
        total_match = re.search(r'Total\s*:?\s*\$?([\d,]+\.?\d*)', text, re.IGNORECASE)
        if total_match:
            data['total'] = float(total_match.group(1).replace(',', ''))
        
        # Extract line items from table
        tables = page.extract_tables()
        for table in tables:
            if table and any('description' in str(row).lower() for row in table[:2]):
                # Found line items table
                for row in table[1:]:  # Skip header
                    if row and len(row) >= 3:
                        data['line_items'].append({
                            'description': row[0],
                            'quantity': row[1] if len(row) > 1 else None,
                            'amount': row[-1]
                        })
    
    return data

invoice = extract_invoice_data('invoice.pdf')
print(f"Invoice #{invoice['invoice_number']}")
print(f"Total: ${invoice['total']}")
```

### Example 3: Resume/CV Parser
```python
import pdfplumber

def parse_resume(pdf_path):
    """Extract structured sections from resume."""
    
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ''
        for page in pdf.pages:
            full_text += (page.extract_text() or '') + '\n'
        
        # Common resume sections
        sections = {
            'contact': '',
            'summary': '',
            'experience': '',
            'education': '',
            'skills': ''
        }
        
        # Split by common headers
        import re
        section_patterns = {
            'summary': r'(summary|objective|profile)',
            'experience': r'(experience|employment|work history)',
            'education': r'(education|academic)',
            'skills': r'(skills|competencies|technical)'
        }
        
        lines = full_text.split('\n')
        current_section = 'contact'
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check if line is a section header
            for section, pattern in section_patterns.items():
                if re.match(pattern, line_lower):
                    current_section = section
                    break
            
            sections[current_section] += line + '\n'
        
        return sections

resume = parse_resume('resume.pdf')
print("Skills:", resume['skills'])
```

## Limitations

- Cannot extract from scanned/image PDFs (use OCR first)
- Complex layouts may need manual tuning
- Some PDF encryption types not supported
- Embedded fonts may affect text extraction
- No direct PDF editing capability

## Installation

```bash
pip install pdfplumber

# For image debugging (optional)
pip install Pillow
```

## Resources

- [pdfplumber Documentation](https://github.com/jsvine/pdfplumber)
- [Table Extraction Guide](https://github.com/jsvine/pdfplumber#extracting-tables)
- [Visual Debugging](https://github.com/jsvine/pdfplumber#visual-debugging)

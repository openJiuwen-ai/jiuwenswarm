# layout.json 约定（MVP）

```json
{
  "font_path": "/System/Library/Fonts/PingFang.ttc",
  "blocks": [
    {
      "id": "title",
      "text": "主标题文案",
      "x": 80,
      "y": 200,
      "font_size": 72,
      "fill": "#FFFFFF",
      "stroke_fill": "#000000",
      "stroke_width": 2,
      "max_width": 920,
      "align": "center",
      "line_gap": 12
    },
    {
      "id": "body",
      "text": "正文……",
      "x": 80,
      "y": 1200,
      "font_size": 36,
      "fill": "#F5F5F5",
      "max_width": 920,
      "align": "left"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `font_path` | 全局默认字体；block 可覆盖 |
| `blocks[].text` | 必须与 `copy.md` 对应段一致 |
| `x,y` | 左上角像素 |
| `max_width` | 换行宽度；`align` 为 center/right 时相对此宽度对齐 |
| `stroke_*` | 可选描边，提高 OCR 对比度 |

`blocks[].text` 应从定稿 `copy.md` 拷贝，不要在 layout 里另写一套文案。

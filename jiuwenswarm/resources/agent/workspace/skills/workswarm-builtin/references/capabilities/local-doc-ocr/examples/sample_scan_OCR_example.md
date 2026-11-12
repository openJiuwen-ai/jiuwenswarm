# OCR 识别结果（本地离线 · RapidOCR）— 示例输出（示意）

> 来源：examples/sample_scan.pdf
> 引擎：RapidOCR（纯本地 ONNX，不联网、不上传）
> 共 1 页/图

---

## 第 1 页

安全生产检查记录表

检查日期：2026 年 7 月 24 日

检查单位：安全环保公司安全生产部

受检单位：某作业区

检查人员：李昂 / 王强

检查结论：总体受控，个别隐患待整改

隐患清单：

1 消防通道堆放杂物 2026-07-31

2 配电箱未上锁 2026-07-28

3 安全帽佩戴不规范 2026-07-26

备注：以上隐患须按期闭环，逾期升级督办。

〔印章：安全环保公司〕

---

⚠️ 说明：以上为**示意效果**，实际输出以你运行 `python scripts/ocr_offline.py examples/sample_scan.png` 的结果为准。

- 引擎 A 对印刷体识别率较高；表格单元格文字顺序可能交错（本示例做了顺排，真实输出可能跨列跳动），需要规整表格请走「路线 B：多模态读图」。
- 红色印章区域引擎 A 可能识别不清或漏识，建议用路线 B 以〔印章：可见文字〕标注。

运行命令（首次会自动装好依赖）：

```
python scripts/ocr_offline.py examples/sample_scan.png
python scripts/ocr_offline.py examples/sample_scan.pdf
```

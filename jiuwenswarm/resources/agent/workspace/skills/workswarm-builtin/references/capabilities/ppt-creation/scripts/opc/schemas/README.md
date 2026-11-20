# OOXML 校验用 XSD

本目录是 Office Open XML 的官方 schema 文档，供 `../validate.py` 做打包前校验。

## 来源

| 目录 | 标准 | 出处 |
|---|---|---|
| `ISO-IEC29500-4_2016/` | ISO/IEC 29500-4:2016（ECMA-376 第 4 部分，Transitional Migration Features） | ISO/IEC 与 ECMA 公开发布 |
| `ecma/fouth-edition/` | ECMA-376 第 4 版，OPC（Open Packaging Conventions）部分 | ECMA International 公开发布 |
| `mce/`、`microsoft/` | Markup Compatibility 与 Microsoft 扩展 schema | 随上述标准一并发布 |

这些是公开的国际标准文档，可从 ECMA International
（<https://ecma-international.org/publications-and-standards/standards/ecma-376/>）
免费下载，随本 skill 分发仅为免去使用者自行拼装的麻烦。

## 使用方式

`validate.py` 按 XML **根元素的命名空间**选择 schema，不按文件路径——
`ppt/charts/chart1.xml` 的根元素属于 drawingml/chart 命名空间，
拿 `pml.xsd` 校验它只会报出无意义的错误。映射表见 `validate.py` 的 `NS_TO_XSD`。

`docProps/` 不做 schema 校验：`opc-coreProperties.xsd` 会 import Dublin Core 的
XSD，而 ECMA 只以 URL 引用、并未随标准分发；且这些元数据不影响 PowerPoint 打开文件。

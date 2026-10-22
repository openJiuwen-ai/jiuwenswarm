# 前端开发规则

## 适用范围

本文件适用于 `frontend/` 内新增或修改的代码；修改现有文件时只约束本次触及的代码。不要仅因这些规则而扫描或迁移整个仓库；大规模旧代码整理须另立任务。

## 组件归属与复用

- 新增界面控件前，先查看 `src/components/ui/index.ts`、相关业务领域的现有组件及其调用方式。行为和语义一致时复用或扩展已有实现，不复制一份外观相似的控件。
- 区分基础控件（如 `Switch`、`Input`）、通用组合组件（如 `PageToolbar`、`FilePreviewPanel`）、公共业务组件和特性私有组件。`src/components/ui/` 同时包含前两类；`src/components/` 的目录名本身不代表组件是公共能力。
- 业务状态和领域决策由对应业务模块负责，不放进基础控件。多个地方引用同一组件也不足以单独构成抽成公共业务组件的理由；先确认它们共享同一业务语义和接口。
- 修改 `src/components/ui/` 内的组件时，遵守该目录的 `AGENTS.md`。

## 特性规则入口

- 因设置页需求而修改代码时，先阅读并遵守 `src/features/settings/AGENTS.md`；这也包括为该需求修改的目录外业务组件，例如 `src/components/PersonalContext/SettingsPanel.tsx`。普通布尔、选择、文本设置须接入前端 Settings 契约，并使用声明式设置项和 `SettingItemRenderer`；具体规则由设置页文件维护。仅为其他特性修改通用 UI 组件时，不必套用设置页专属布局规则。
- 其他特性如有更深层的 `AGENTS.md`，修改对应目录时一并遵守；不要把特性专属规则复制到本文件。

## 浏览器兼容与格式

- Chrome/Chromium 107 是最低支持版本；新增或修改的 HTML、CSS、JavaScript 须在 Chrome 107 及更新版本正确运行和渲染。
- 遵守 `.prettierrc.cjs`，不要引入与 Prettier 冲突的格式规则。

## 国际化

- 新增或修改的用户可见文案，包括提示、占位符、错误信息和无障碍名称，须在中文和英文语言资源中完整提供。

## 颜色

- 业务组件、页面及其样式不得写死产品颜色，包括十六进制、`rgb()` / `rgba()` 和 Tailwind 色板类。
- 使用现有语义主题变量：CSS 使用 `var(--color-*)`；Tailwind 使用 `bg-accent`、`text-text-link`、`text-warn` 等语义类。
- 具体颜色值只在主题变量文件中定义；新变量按用途命名，不按色相命名。
- A2UI、Mermaid、图表配色、标志、多色插画、图片资源、SVG 遮罩/裁切/透明占位，以及不可避免的浏览器或第三方常量，可以保留具体颜色值。上述例外不适用于产品界面的背景、边框、按钮、文字、状态或交互反馈。

## SVG

- 新增或修改的静态单色 UI SVG，须在可见路径上使用 `fill="currentColor"` 和/或 `stroke="currentColor"`；通过 SVGR 作为 React 组件导入，例如 `import SettingsIcon from '../../assets/sidebar/config.svg?react';`。
- 新增或修改的内联 SVG 使用 `currentColor`；不必仅为满足本规则就抽成单独资源。不要通过 `<img>` 加载需要随主题变色的 SVG。
- 内联 SVG 中的裁剪路径、遮罩、渐变或滤镜等 ID 引用，须使用 React `useId()` 为每个组件实例生成独立 ID，并同步更新定义及所有引用。不得仅靠文件名前缀隔离；无实际作用的裁剪、遮罩及定义应移除。
- 标志、多色插画、图片资源及无须跟随主题的 SVG 可以保留原有颜色并使用 `<img>`。

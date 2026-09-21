# UI primitive source provenance

English | [中文](PROVENANCE.zh.md)

The files in this directory are copies of the modules required by the trajectory viewer from `packages/client/ui-primitives/src` at DeepSeek Harness commit `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`. They are used under the MIT license and are maintained locally so this repository has no runtime, build, workspace, or source-path dependency on DeepSeek Harness.

The copied set contains Tooltip, Menu, JSON tree, Markdown parsing and rendering, syntax highlighting, KaTeX rendering, icons, and their CSS. The JiuwenSwarm copy adds trajectory-local portal theme propagation and moves concrete presentation colors into scoped theme tokens. `index.ts` deliberately exports only the primitives consumed by this feature.

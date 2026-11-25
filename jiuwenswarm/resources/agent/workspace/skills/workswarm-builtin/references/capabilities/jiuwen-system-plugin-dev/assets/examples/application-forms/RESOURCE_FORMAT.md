# 形态资源包 v1

这是应用形态插件自己的资源格式，不是 Gateway 扩展包格式。资源包只提供沙箱前端内容，不能注册 Python、Node、宿主 Ops 或原生 IPC。宿主接口沿用 S_11 的会话快照和受控动作。

ZIP 根目录必须有 `presentation.json`，以及它引用的 HTML 页面、预览图和素材：

```json
{
  "schemaVersion": 1,
  "id": "my-capsule",
  "version": "1.0.0",
  "name": {"zh": "我的胶囊", "en": "My capsule"},
  "description": {"zh": "会话胶囊", "en": "Conversation capsules"},
  "entry": "index.html",
  "preview": "preview.svg",
  "window": {"width": 390, "height": 132},
  "config": {},
  "settings": [{
    "key": "appearance", "kind": "select",
    "title": {"zh": "颜色", "en": "Appearance"},
    "default": "dark",
    "options": [{"value": "dark", "label": {"zh": "深色", "en": "Dark"}}, {"value": "light", "label": {"zh": "浅色", "en": "Light"}}]
  }]
}
```

- ID 为 1～64 个小写字母、数字或 `-`，首字符为字母。版本使用三段数字。名称、说明、设置标题和选项标签均提供非空 zh/en，各项不超过 1024 UTF-8 字节。
- entry 是包内 `.html` 文件；preview 是包内 PNG、JPEG、WebP、GIF 或 SVG。相对路径禁止绝对路径、反斜杠、空段、`.`、`..` 和控制字符。
- window 使用 S_17 的宽高范围。config 是有限 JSON 对象；最终传给 ApplicationSurface 的配置仍受 16 KiB 限制；config 自身最多 8 KiB。整个设置视图也必须满足 S_17 的深度、节点数与帧大小预算，超限的导入或修改会在保存前拒绝。
- settings 最多 32 项，key 唯一。支持 `select`（字符串枚举）、`switch`（布尔）、`image`（通过宿主导入、默认空字符串）和 `input`（字符串，必须给出 1～1000 的 maxLength）；default 必须通过对应校验。设置按资源 ID 分别保存在插件存储中。页面从 `config.settings` 读取确认保存的值，`config.assetBase` 指向本资源的受管素材目录。
- ZIP 最多 64 MiB，解压内容合计最多 128 MiB、1000 个文件；不允许符号链接、加密文件或忽略大小写后的重复路径。导入时完整校验再发布。同 ID 重复导入明确拒绝，先移除原资源再导入。
- 只有蜜蜂是默认资源，形象与动态固定，无设置项；胶囊须导入完整 ZIP；终端实验暂不发布。导入资源最多 15 个。设置页管理导入、移除和资源设置；页面右下角悬浮按钮的“切换应用形态”菜单列出已发布资源，选择后进入。设置保存和导入均不自动收起工作台。
- 导入内容放在插件数据目录的 `public/resources/<SHA-256>/`，URL 不可变。移除资源撤销当前连接的候选；历史文件保留至插件卸载，避免破坏其他已打开连接的素材。
- 沙箱页面应先监听 `workswarm.presentation` v1 消息，再发送 ready。不得伪造任务进度，消息只发送到明确 conversationKey；未知结果由用户核对，不重发。

胶囊参考 `session-accordion.html` 的设计：三条一页，单项风琴展开，翻页不改变发送目标。其他桌宠通过完整资源包添加；图片可以作为导入资源自行声明的设置，不是默认蜜蜂的替换入口。

## 插件统一切换音效

`config.transitionSound` 与 `config.settings`、`config.assetBase` 一样由资源管理器注入，不能由资源 manifest 的 config 覆盖。它包含：

- `enabled`：布尔值，是否启用。
- `volume`：0～1 的有限数值，0 表示静音。
- `compact`、`workspace`：已发布的受管音频 URL，分别用于进入形态与返回工作台。

资源自行播放音频；宿主不解释此配置。示例中的 `transition-sound.js` 提供 `configure`、`prepare`、`play`、`stop`、`dispose`。动画准备阶段预加载，在真实 `transition-start` 时调用 `play(direction, durationMs)`，播放器依据解码后的时长安排采样在终点结束。相同 transition ID 不重复播放；`state` 更新只更新配置。无动画进入只播放一次，返回前等待实际声音结束；静音不等待。页面卸载时调用 `dispose`。

偏好以 `transition_sound_preferences` 独立存储，默认 `{schemaVersion: 1, enabled: true, preset: "whoosh", volume: 25}`；音量是 0～100 的 5% 整数步长。资源自身设置继续按资源 ID 保存。既有资源保持原字节和 URL，接入音效需要作者更新资源代码。

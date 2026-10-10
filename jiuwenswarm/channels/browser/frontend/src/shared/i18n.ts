/**
 * Lightweight i18n for the side panel UI.
 *
 * Provides a `t(key)` lookup with English as the default and Simplified Chinese
 * as the primary alternate (the extension targets researchers/analysts in both
 * markets). Locale is auto-detected from the browser at startup.
 *
 * NOTE: this is infrastructure + partial coverage. Only the user-facing strings
 * added during the UX pass are localised so far; the remaining hardcoded strings
 * are a follow-up.
 */

type Dict = Record<string, string>;

const EN: Dict = {
  "empty.waiting": "Waiting for server…",
  "empty.ready": "Ready to chat",
  "empty.title.waiting": "Waiting for server",
  "empty.title.ready": "Start a conversation",
  "conn.lost": "Lost connection to JiuwenSwarm",
  "conn.retry": "Retry",
  "conn.reconnecting": "Reconnecting…",
  "tool.highlight": "Highlighting a passage on the page…",
  "tool.scroll": "Scrolling to a section…",
  "tool.fill": "Filling in a form…",
  "tool.screenshot": "Taking a screenshot…",
  "tool.open": "Opening a link in a new tab…",
  "tool.read": "Reading a page…",
  "tool.pin": "Pinning the current page…",
  "tool.selection": "Reading your selection…",
  "tool.default": "Agent is working on the page…",
  "sug.summarize": "Summarize this page",
  "sug.compare": "Compare the pinned pages",
  "toast.pinned": "Pinned — I can now answer about this page",
  "toast.unpinned": "Page unpinned",
  "toast.undo": "Undo",
  "toast.restored": "Page restored",
  "err.websocket":
    "Couldn't reach the JiuwenSwarm server. Check that it's running, then click Retry.",
  "err.extraction":
    "This page couldn't be read. Some pages (bank portals, Chrome internal pages, JS-only apps) block extraction. Try again after the page fully loads.",
  "err.timeout": "The request timed out. Try asking again.",
  "session.required.pin": "Create a session first to pin pages into it.",
  "session.required.ask": "Create a session first to ask JiuwenSwarm.",
  "cta.create.session": "+ Create a session",
  "tour.1.title": "Meet JiuwenSwarm",
  "tour.1.body":
    "An AI agent that lives beside every page you read. Pin pages into a session, then ask questions across them.",
  "tour.2.title": "Pin a page",
  "tour.2.body":
    "Click 📌 Pin page (or press Ctrl+Shift+P) to add the current tab to your session. Pinned pages appear as chips below.",
  "tour.3.title": "Ask across your pages",
  "tour.3.body":
    "Type a question and press Enter. The agent reads all pinned pages and can even highlight, scroll, and fill forms on the page.",
  // Static UI (data-i18n)
  "session.noSession": "No session",
  "session.pickerTitle": "Click to switch session",
  "actions.exportJson": "⬇ Export JSON",
  "actions.exportMd": "⬇ Export Markdown",
  "actions.rename": "✏️ Rename session…",
  "rename.prompt": "Session name:",
  "actions.tour": "? Getting-started tour",
  "actions.pinAll": "📌 Pin all open tabs",
  "actions.search": "🔍 Search pinned pages",
  "actions.reader": "👁 Agent's view",
  "actions.privacy": "🔒 Privacy",
  "reader.back": "← Back",
  "reader.loading": "Reading page…",
  "reader.note": "This is the text JiuwenSwarm reads from this page.",
  "reader.error": "Couldn't read the active page. Try pinning it first, or open the page and try again.",
  "search.title": "Search pinned pages",
  "search.placeholder": "Search title, text, notes…",
  "search.close": "Close",
  "search.noResults": "No matches.",
  "privacy.title": "Privacy",
  "privacy.close": "Close",
  "privacy.body": "This extension sends page content to the JiuwenSwarm server you configure, which forwards it to your chosen LLM provider. The extension contacts no other service. Sessions are stored on your server; uninstalling the extension deletes only locally stored data.",
  "toast.pinAll": "Pinning all open tabs to this session…",
  "offline.cached": "(cached — server offline)",
  "pin.pin": "📌 Pin page",
  "pin.pinTitle": "Pin this page to the active session (Ctrl+Shift+P)",
  "nf.placeholder": "Session name…",
  "nf.create": "Create",
  "nf.cancel": "Cancel",
  "chat.placeholder": "Ask JiuwenSwarm about this page…",
  "chat.sendTitle": "Send (Enter)",
  "chat.stopTitle": "Stop generating",
  "tour.back": "Back",
  "tour.next": "Next",
  "tour.gotit": "Got it",
  "tour.skip": "Skip",
  "msg.copy": "Copy",
  "msg.sources": "Sources",
  "chip.unpin": "Unpin",
  "chip.retry": "Retry extraction",
  "chip.rePdf": "Re-extract (requires server)",
  "chip.moveEarlier": "Move earlier",
  "chip.moveLater": "Move later",
  "chip.previewHint": "Click to preview",
  "popup.openPanel": "Open panel",
  "popup.settings": "⚙ Settings",
  "popup.connected": "Connected to server",
  "popup.notConnected": "Not connected",
  "popup.serverNotReachable": "Server not reachable",
  "popup.activeSession": "Active session",
  "popup.connecting": "Connecting…",
  "popup.none": "None",
  "popup.pagesPinned": "pages pinned",
  "options.titleSuffix": "— Settings",
  "options.serverSection": "Server connection",
  "options.host": "Host",
  "options.port": "Port",
  "options.behavior": "Behavior",
  "options.autoExtract": "Auto-extract page context when panel opens",
  "options.autoSummarize": "Ask for a short summary when a page is pinned",
  "options.save": "Save settings",
  "options.saved": "Saved ✓",
  "options.portError": "Port must be between 1 and 65535.",
};

const ZH: Dict = {
  "empty.waiting": "正在连接服务器…",
  "empty.ready": "可以开始对话",
  "empty.title.waiting": "正在连接服务器",
  "empty.title.ready": "开始对话",
  "conn.lost": "与 JiuwenSwarm 连接已断开",
  "conn.retry": "重试",
  "conn.reconnecting": "正在重连…",
  "tool.highlight": "正在页面中高亮段落…",
  "tool.scroll": "正在滚动到相应位置…",
  "tool.fill": "正在填写表单…",
  "tool.screenshot": "正在截图…",
  "tool.open": "正在新标签页打开链接…",
  "tool.read": "正在阅读页面…",
  "tool.pin": "正在固定当前页面…",
  "tool.selection": "正在读取你的选中内容…",
  "tool.default": "代理正在页面中操作…",
  "sug.summarize": "总结此页面",
  "sug.compare": "比较已固定的页面",
  "toast.pinned": "已固定——现在可以就该页面提问",
  "toast.unpinned": "已取消固定",
  "toast.undo": "撤销",
  "toast.restored": "已恢复",
  "err.websocket": "无法连接 JiuwenSwarm 服务器。请确认其正在运行，然后点击重试。",
  "err.extraction": "无法读取此页面。部分页面（银行门户、Chrome 内部页面、仅 JS 渲染的应用）会阻止提取，请在页面加载完成后重试。",
  "err.timeout": "请求超时，请重试。",
  "session.required.pin": "请先创建会话，再将页面固定到其中。",
  "session.required.ask": "请先创建会话，再向 JiuwenSwarm 提问。",
  "cta.create.session": "+ 创建会话",
  "tour.1.title": "认识 JiuwenSwarm",
  "tour.1.body": "一个在你阅读的每个页面旁常驻的 AI 代理。把页面固定到一个会话中，然后跨页面提问。",
  "tour.2.title": "固定一个页面",
  "tour.2.body": "点击 📌 固定页面（或按 Ctrl+Shift+P）将当前标签页加入会话。固定后的页面会以标签形式出现在下方。",
  "tour.3.title": "跨页面提问",
  "tour.3.body": "输入问题并按回车。代理会读取所有固定的页面，甚至可以在页面上高亮、滚动和填写表单。",
  // Static UI (data-i18n)
  "session.noSession": "无会话",
  "session.pickerTitle": "点击切换会话",
  "actions.exportJson": "⬇ 导出 JSON",
  "actions.exportMd": "⬇ 导出 Markdown",
  "actions.rename": "✏️ 重命名会话…",
  "rename.prompt": "会话名称：",
  "actions.tour": "? 新手引导",
  "actions.pinAll": "📌 固定所有打开的标签页",
  "actions.search": "🔍 搜索已固定的页面",
  "actions.reader": "👁 代理视角",
  "actions.privacy": "🔒 隐私",
  "reader.back": "← 返回",
  "reader.loading": "正在读取页面…",
  "reader.note": "这是 JiuwenSwarm 从此页面读取的文本。",
  "reader.error": "无法读取当前页面。请先固定该页面，或打开页面后重试。",
  "search.title": "搜索已固定的页面",
  "search.placeholder": "搜索标题、正文、笔记…",
  "search.close": "关闭",
  "search.noResults": "无匹配结果。",
  "privacy.title": "隐私",
  "privacy.close": "关闭",
  "privacy.body": "此扩展会将页面内容发送到你配置的 JiuwenSwarm 服务器，由该服务器转发给你选择的大模型提供商。本扩展不会联系其他任何服务。会话存储在你的服务器上；卸载扩展只会删除本地存储的数据。",
  "toast.pinAll": "正在将打开的标签页固定到本会话…",
  "offline.cached": "（已缓存——服务器离线）",
  "pin.pin": "📌 固定页面",
  "pin.pinTitle": "将当前页面固定到会话（Ctrl+Shift+P）",
  "nf.placeholder": "会话名称…",
  "nf.create": "创建",
  "nf.cancel": "取消",
  "chat.placeholder": "就当前页面或已固定的页面提问…",
  "chat.sendTitle": "发送（回车）",
  "chat.stopTitle": "停止生成",
  "tour.back": "上一步",
  "tour.next": "下一步",
  "tour.gotit": "知道了",
  "tour.skip": "跳过",
  "msg.copy": "复制",
  "msg.sources": "来源",
  "chip.unpin": "取消固定",
  "chip.retry": "重试提取",
  "chip.rePdf": "重新提取（需要服务器）",
  "chip.moveEarlier": "移到更前",
  "chip.moveLater": "移到更后",
  "chip.previewHint": "点击预览",
  "popup.openPanel": "打开面板",
  "popup.settings": "⚙ 设置",
  "popup.connected": "已连接到服务器",
  "popup.notConnected": "未连接",
  "popup.serverNotReachable": "服务器不可达",
  "popup.activeSession": "当前会话",
  "popup.connecting": "正在连接…",
  "popup.none": "无",
  "popup.pagesPinned": "个页面已固定",
  "options.titleSuffix": "— 设置",
  "options.serverSection": "服务器连接",
  "options.host": "主机",
  "options.port": "端口",
  "options.behavior": "行为",
  "options.autoExtract": "面板打开时自动提取页面内容",
  "options.autoSummarize": "固定页面时请求简短摘要",
  "options.save": "保存设置",
  "options.saved": "已保存 ✓",
  "options.portError": "端口必须在 1 到 65535 之间。",
};

let _dict: Dict = EN;

export function initI18n(): void {
  const lang = (navigator.language || "en").toLowerCase();
  if (lang.startsWith("zh")) _dict = ZH;
  else _dict = EN;
}

export function t(key: string): string {
  return _dict[key] ?? EN[key] ?? key;
}

/**
 * Localise all elements carrying `data-i18n` (textContent) or
 * `data-i18n-placeholder` (placeholder) attributes. Call once after initI18n().
 */
export function applyStaticI18n(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n")!);
  });
  root.querySelectorAll<HTMLElement>("[data-i18n-title]").forEach((el) => {
    el.setAttribute("title", t(el.getAttribute("data-i18n-title")!));
  });
  root.querySelectorAll<HTMLElement>(
    "input[data-i18n-placeholder], textarea[data-i18n-placeholder]"
  ).forEach((el) => {
    el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")!));
  });
}

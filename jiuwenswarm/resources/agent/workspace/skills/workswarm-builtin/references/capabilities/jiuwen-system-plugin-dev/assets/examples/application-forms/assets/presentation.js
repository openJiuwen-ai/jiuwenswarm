(() => {
  'use strict';
  const protocol = 'workswarm.presentation';
  const $ = id => document.getElementById(id);
  const words = {
    zh: {
      newConversation: '新建会话', expandList: '展开会话列表', model: '模型', chooseModel: '选择模型', lastReply: '最后回复', expandReply: '展开最后回复', collapseReply: '收起最后回复', viewConversation: '在应用中查看会话', loadingReply: '正在加载完整回复…', replyFailed: '完整回复未能加载，请重新展开后查看。',
      bee: '蜜蜂桌宠', island: '胶囊卡片', conversation: '当前会话',
      workspace: '打开工作台', collapse: '收起卡片', send: '发送', stop: '停止当前会话', you: '你', finalReply: '回复', noReply: '本轮暂无回复', closePanel: '关闭面板', showSummary: '显示会话摘要', hideSummary: '隐藏会话摘要',
      placeholder: '回复当前会话…', form: '应用形态',
      settings: '形态设置', upload: '选择形象', resetImage: '恢复内置蜜蜂',
      imageLifetime: '自选形象仅在当前小窗中使用，支持 PNG、JPEG、WebP 和 GIF。',
      beeHelp: '单击查看会话，双击展开工作台，拖动移动',
      runningConversations: '运行中的会话', recentConversations: '最近会话', allConversations: '查看全部会话', noRecent: '暂无最近执行的会话',
      confirmation: '当前会话需要你确认。', empty: '从这里开始对话。',
      replyingTo: '回复：', chooseImage: '请选择不超过 10 MiB 的 PNG、JPEG、WebP 或 GIF 图片。',
      chooseConversation: '请选择会话', targetMissing: '原会话已不可用，请选择回复目标。',
      disconnected: '连接已断开', unknown: '结果尚未确认，请在工作台检查，勿重复发送。',
      idle: '随时可以开始', thinking: '正在思考', toolRunning: '正在调用工具', replying: '正在回复',
      previousPage: '上一页会话', nextPage: '下一页会话', sessionCount: '个会话',
      lookInput: '看你输入', lookPointer: '看向鼠标',
      beeCanvasFailed: '蜜蜂动效无法显示，请打开工作台检查图形环境。',
      transitionFailed: '窗口过渡未能完成，已恢复工作台。',
      soundFailed: '切换音效无法播放，请在应用形态设置中检查。',
      needsInput: '需要你确认', failed: '运行失败', stopped: '已停止', paused: '已暂停', done: '已完成',
    },
    en: {
      newConversation: 'New conversation', expandList: 'Expand conversations', model: 'Model', chooseModel: 'Choose a model', lastReply: 'Last reply', expandReply: 'Expand last reply', collapseReply: 'Collapse last reply', viewConversation: 'View conversation in the app', loadingReply: 'Loading the complete reply…', replyFailed: 'The complete reply could not be loaded. Collapse and expand to try again.',
      bee: 'Bee companion', island: 'Capsule card', conversation: 'CONVERSATION',
      workspace: 'Open workspace', collapse: 'Collapse card', send: 'Send', stop: 'Stop conversation', you: 'You', finalReply: 'Reply', noReply: 'No reply for this turn yet', closePanel: 'Close panel', showSummary: 'Show conversation summary', hideSummary: 'Hide conversation summary',
      placeholder: 'Reply to this conversation…', form: 'Application form',
      settings: 'Form settings', upload: 'Choose image', resetImage: 'Restore built-in bee',
      imageLifetime: 'Custom images last for this window. PNG, JPEG, WebP and GIF are supported.',
      beeHelp: 'Click for conversations, double-click for workspace, drag to move',
      runningConversations: 'Running conversations', recentConversations: 'Recent conversations', allConversations: 'View all conversations', noRecent: 'No recent conversations yet',
      confirmation: 'This conversation needs your confirmation.', empty: 'Start a conversation here.',
      replyingTo: 'Replying to: ', chooseImage: 'Choose a PNG, JPEG, WebP or GIF image up to 10 MiB.',
      chooseConversation: 'Choose a conversation', targetMissing: 'The previous conversation is unavailable. Choose a reply target.',
      disconnected: 'Disconnected', unknown: 'The result is unconfirmed. Check the workspace before sending again.',
      idle: 'Ready when you are', thinking: 'Thinking', toolRunning: 'Using tools', replying: 'Replying',
      previousPage: 'Previous sessions', nextPage: 'Next sessions', sessionCount: 'conversations',
      lookInput: 'Watching your input', lookPointer: 'Watching the pointer',
      beeCanvasFailed: 'The bee animation could not be displayed. Open the workspace to check graphics support.',
      transitionFailed: 'The window transition could not complete. The workspace has been restored.',
      soundFailed: 'The transition sound could not be played. Check Application forms settings.',
      needsInput: 'Needs your input', failed: 'Run failed', stopped: 'Stopped', paused: 'Paused', done: 'Completed',
    },
  };
  let state = null;
  let config = {};
  let mode = 'bee';
  let expanded = false;
  let deckOrder = [];
  let settingsOpen = false;
  let noticesOpen = false;
  let noticePage = 0;
  let rotation = 0;
  let sequence = 0;
  let lastInputKey = null;
  let customImage = '';
  let pendingSelection = false;
  let sending = false;
  let dragging = false;
  let clickTimer;
  let lastSize = '';
  let formAnchor = null, geometryPending = false, geometryRevision = 0, geometryFrame = 0, crossingPoint = null, screenPointer = null;
  const geometry = window.WorkSwarmFormGeometry;
  let interactive = false;
  let renderedLocale = null;
  let accordionKey = null;
  let deckPage = 0;
  let viewRevision = 0;
  let replySource = null, replyText = '', lastUserText = '', replyLoading = false, changingModel = false, stopping = false;
  const card = $('card');
  let recallMotion = null;
  let windowTransition = false;
  let transitionWorkArea = null, initialWorkArea = null;
  let openingWorkspace = false;
  const transitionOptions = { durationMs: 1120 };
  const recallEnabled = document.documentElement.hasAttribute('data-window-transition');
  let beeAnimator = null, beeUnavailable = false, beePointer = null;
  let celebrationKey = null, celebrationTimer;
  const pending = new Map();
  const localDrafts = new Map();
  const busy = item => ['thinking', 'toolRunning', 'replying'].includes(item.activity);
  const awaitingReply = item => busy(item) || ['failed', 'stopped', 'paused', 'needsInput'].includes(item.activity);
  const stoppable = item => busy(item) || item.activity === 'needsInput';
  function currentTurn(item) {
    const matchingReply = item.inputMessageId === item.replyToMessageId;
    return { ...item, replyId: matchingReply ? item.replyId : null, preview: matchingReply ? item.preview : '',
      activity: item.inputOutcome ?? (item.activity === 'done' && !matchingReply ? 'idle' : item.activity) };
  }
  const tr = key => words[state?.locale.startsWith('zh') ? 'zh' : 'en'][key];
  let soundTransition = null, playedTransition = null;
  const transitionSound = window.WorkSwarmTransitionSound.create({ onError: error => {
    console.error('Transition sound failed', error); showError(new Error(tr('soundFailed')));
  } });
  const current = () => state?.conversations.find(item => item.conversationKey === state.selectedConversationKey);

  function request(action, params = {}) {
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(tr('unknown')));
      }, 30000);
      pending.set(id, { resolve, reject, timer });
      parent.postMessage({ protocol, version: 1, type: 'request', id, action, params }, '*');
    });
  }
  function showError(error) {
    $('error').textContent = error.message || tr('unknown');
    $('error').hidden = false;
    if (mode === 'bee' && !expanded) {
      $('bee-list-error').textContent = $('error').textContent; $('bee-list-error').hidden = false;
    }
  }
  function run(action, params) { return request(action, params).catch(showError); }
  function setText() {
    const locale = state.locale.startsWith('zh') ? 'zh' : 'en';
    if (renderedLocale === locale) return;
    renderedLocale = locale;
    document.documentElement.lang = locale;
    document.title = tr('form');
    for (const element of document.querySelectorAll('[data-text]')) element.textContent = tr(element.dataset.text);
    for (const element of document.querySelectorAll('[data-title]')) {
      element.title = tr(element.dataset.title);
      element.setAttribute('aria-label', tr(element.dataset.title));
    }
    for (const element of document.querySelectorAll('[data-placeholder]')) element.placeholder = tr(element.dataset.placeholder);
    $('sessions').setAttribute('aria-label', tr('conversation'));
    $('input').setAttribute('aria-label', tr('placeholder'));
    $('badge').setAttribute('aria-label', tr('conversation'));
    $('sprite').alt = tr('bee');
    $('card').setAttribute('aria-label', tr('conversation'));
  }
  function screenArea() {
    if (windowTransition) return transitionWorkArea;
    return { x: screen.availLeft, y: screen.availTop, width: screen.availWidth, height: screen.availHeight };
  }
  function initialAnchor(area) {
    const rect = globalRect(mode === 'island' ? $('capsule') : $('bee-shell'));
    return { ...rect,
      x: mode === 'island' ? area.x + (area.width - rect.width) / 2 : area.x + area.width - rect.width - 24,
      y: mode === 'island' ? area.y + 32 : area.y + area.height - rect.height - 32 };
  }
  function windowRect() { return { x: window.screenX, y: window.screenY, width: innerWidth, height: innerHeight }; }
  function globalRect(element) {
    const rect = element.getBoundingClientRect();
    return { x: window.screenX + rect.left, y: window.screenY + rect.top, width: rect.width, height: rect.height };
  }
  function position(element, rect) {
    element.style.left = `${rect.x - window.screenX}px`; element.style.top = `${rect.y - window.screenY}px`;
    element.style.right = 'auto'; element.style.bottom = 'auto';
    element.style.width = `${rect.width}px`; element.style.height = `${rect.height}px`;
  }
  function deckHeight() {
    const deck = $('capsule-deck'), style = getComputedStyle(deck), header = deck.querySelector('.deck-tools');
    const page = $('deck-pages').children[deckPage], footer = $('deck-pagination'), rows = [...page.children];
    const open = rows.find(row => row.dataset.open === 'true');
    const contentHeight = open ? Math.max(
      Number.parseFloat(getComputedStyle(open).minHeight) - open.children[0].getBoundingClientRect().height,
      card.dataset.empty === 'true' ? card.getBoundingClientRect().height : Number.parseFloat(style.getPropertyValue('--conversation-height')),
    ) : 0;
    return Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom)
      + header.getBoundingClientRect().height + Number.parseFloat(getComputedStyle(header).marginBottom)
      + rows.reduce((sum, row) => sum + row.children[0].getBoundingClientRect().height, 0)
      + Number.parseFloat(getComputedStyle(page).rowGap) * Math.max(0, rows.length - 1)
      + contentHeight
      + (footer.hidden ? 0 : footer.getBoundingClientRect().height + Number.parseFloat(getComputedStyle(footer).marginTop));
  }
  function paintGeometry() {
    const area = screenArea(), anchor = geometry.contain(formAnchor, area);
    position(document.querySelector('.dock'), anchor);
    const shape = mode === 'island' ? $('capsule') : $('bee-shell'); shape.style.width = `${anchor.width}px`; shape.style.height = `${anchor.height}px`;
    shape.style.maxWidth = 'none';
    if (mode === 'island' && expanded) {
      const placed = geometry.panel(anchor, { width: 364, height: Math.ceil(deckHeight()) }, area, 0, true, 'below');
      position($('capsule-deck'), placed.rect); return placed.rect;
    }
    if (mode === 'bee' && expanded) {
      card.style.width = '364px'; card.style.maxHeight = 'none';
      card.style.height = card.dataset.empty === 'true' ? 'auto' : '480px';
      const placed = geometry.panel(anchor, { width: 364, height: card.getBoundingClientRect().height }, area, 20, false, 'above');
      position(card, placed.rect);
      return { x: Math.min(anchor.x, placed.rect.x), y: Math.min(anchor.y, placed.rect.y),
        width: Math.max(anchor.x + anchor.width, placed.rect.x + placed.rect.width) - Math.min(anchor.x, placed.rect.x),
        height: Math.max(anchor.y + anchor.height, placed.rect.y + placed.rect.height) - Math.min(anchor.y, placed.rect.y) };
    }
    if (mode === 'bee' && !$('notices').hidden) {
      const notices = $('notices'); notices.style.width = '342px'; notices.style.height = 'auto';
      const placed = geometry.panel(anchor, { width: 342, height: notices.getBoundingClientRect().height }, area, 17, false, 'above');
      position(notices, placed.rect);
      return { x: Math.min(anchor.x, placed.rect.x), y: Math.min(anchor.y, placed.rect.y),
        width: Math.max(anchor.x + anchor.width, placed.rect.x + placed.rect.width) - Math.min(anchor.x, placed.rect.x),
        height: Math.max(anchor.y + anchor.height, placed.rect.y + placed.rect.height) - Math.min(anchor.y, placed.rect.y) };
    }
    return anchor;
  }
  async function syncGeometry() {
    if (geometryPending || !state || !formAnchor || windowTransition) return;
    geometryPending = true; watchGeometry();
    try {
      let applied;
      do {
        applied = geometryRevision;
        let area = screenArea();
        const point = crossingPoint;
        if (point && (point.x < area.x || point.x >= area.x + area.width || point.y < area.y || point.y >= area.y + area.height)) {
          const x = point.x < area.x ? point.x - innerWidth : point.x >= area.x + area.width ? point.x : window.screenX;
          const y = point.y < area.y ? point.y - innerHeight : point.y >= area.y + area.height ? point.y : window.screenY;
          await request('move', { deltaX: Math.round(x - window.screenX), deltaY: Math.round(y - window.screenY) });
          area = screenArea();
        }
        if (crossingPoint === point) crossingPoint = null;
        const width = Math.min(960, area.width), height = Math.min(960, area.height);
        const sizeKey = `${width}:${height}`;
        if (lastSize !== sizeKey) { await request('resize', { width, height }); lastSize = sizeKey; }
        if (windowTransition) break;
        const visible = paintGeometry();
        const target = geometry.viewport(windowRect(), { width, height }, visible, screenArea());
        const deltaX = Math.round(target.x - window.screenX), deltaY = Math.round(target.y - window.screenY);
        if (deltaX || deltaY) await request('move', { deltaX, deltaY });
        if (!windowTransition) paintGeometry();
      } while (applied !== geometryRevision);
    } catch (error) { lastSize = ''; showError(error); }
    finally { geometryPending = false; if (!dragging && formAnchor) formAnchor = geometry.contain(formAnchor, screenArea()); updatePointerHitTest(); }
  }
  function resize() {
    if (!state) return;
    if (windowTransition) { if (formAnchor) paintGeometry(); return; }
    if (!formAnchor) {
      formAnchor = initialAnchor(initialWorkArea || screenArea());
      initialWorkArea = null;
    }
    geometryRevision += 1; paintGeometry(); void syncGeometry();
  }
  window.addEventListener('resize', () => { if (formAnchor) resize(); });
  function status(item) {
    if (!state.connected) return tr('disconnected');
    if (item.activity === 'idle' && item.inputMessageId && !item.replyId) return tr('noReply');
    return item.activity === 'toolRunning' && item.toolNames.length
      ? `${tr('toolRunning')} · ${item.toolNames.join(', ')}` : tr(item.activity);
  }
  function select(key) {
    if (pendingSelection || sending || changingModel || stopping) return Promise.resolve(false);
    if (key === state.selectedConversationKey) {
      return Promise.resolve(true);
    }
    const revision = viewRevision;
    pendingSelection = true;
    render();
    return request('select', { conversationKey: key }).then(() => {
      return true;
    }).catch(error => {
      if (revision === viewRevision) {
        showError(error);
        if (mode === 'bee' && !expanded) noticesOpen = true;
      }
      return false;
    }).finally(() => { pendingSelection = false; render(); });
  }
  async function revealConversation(key) {
    const revision = ++viewRevision;
    $('bee-list-error').hidden = true; $('error').hidden = true;
    const show = () => {
      expanded = true; noticesOpen = false;
      if (mode === 'island') {
        accordionKey = key;
        deckPage = Math.max(0, Math.floor(deckOrder.filter(item => item !== 'new').indexOf(key) / 3));
      }
      render();
    };
    if (key === state.selectedConversationKey) { show(); return; }
    if (await select(key) && revision === viewRevision) show();
  }
  function reconcileChildren(container, items, create, update) {
    const previous = new Map([...container.children].map(element => [element.dataset.key, element]));
    items.forEach((item, index) => {
      const element = previous.get(item.key) || create(item);
      element.dataset.key = item.key;
      previous.delete(item.key);
      update(element, item);
      const position = container.children[index];
      if (position !== element) container.insertBefore(element, position || null);
    });
    for (const element of previous.values()) element.remove();
  }
  const workspaceParams = key => ({ ...(key ? { conversationKey: key } : {}), ...(recallEnabled && !state?.reducedMotion && mode === 'bee' ? { transition: transitionOptions } : {}) });
  async function openWorkspace(key) {
    if (openingWorkspace) return;
    openingWorkspace = true;
    const params = workspaceParams(key);
    try {
      // A non-animated return closes this renderer; finish its sound before handing it back.
      if (!params.transition && transitionSound.enabled()) await transitionSound.play('workspace');
      return await run('workspace', params);
    } finally { openingWorkspace = false; }
  }
  const openConversation = key => { if (!dragging && key) void openWorkspace(key); };
  function chevron() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8'); svg.setAttribute('aria-hidden', 'true'); path.setAttribute('d', 'm6 9 6 6 6-6');
    svg.append(path); return svg;
  }
  async function loadReply(item) {
    const replyId = item && !awaitingReply(item) ? item.replyId : null;
    const source = item && (replyId || item.userMessageId) ? `${item.conversationKey}:${item.userMessageId || ''}:${replyId || ''}` : null;
    if (source === replySource) return;
    replySource = source; replyText = ''; lastUserText = ''; replyLoading = !!source;
    if (!source) return;
    async function readMessage(replyId, role) {
      if (!replyId) return '';
      const parts = []; let offset = 0;
      do {
        const result = await request('reply', { conversationKey: item.conversationKey, replyId, offset, role });
        if (replySource !== source) return '';
        parts.push(result.content);
        if (result.nextOffset !== null && (!Number.isSafeInteger(result.nextOffset) || result.nextOffset <= offset)) throw new Error(tr('replyFailed'));
        offset = result.nextOffset;
      } while (offset !== null);
      return parts.join('');
    }
    try {
      const [user, reply] = await Promise.all([readMessage(item.userMessageId, 'user'), readMessage(replyId, 'assistant')]);
      if (replySource !== source) return;
      lastUserText = user; replyText = reply;
      $('reply').scrollTop = 0;
    } catch (error) {
      if (replySource === source && !['REPLY_CHANGED', 'STALE_REQUEST'].includes(error.code)) showError(error);
    }
    finally { if (replySource === source) { replyLoading = false; render(); } }
  }
  function messageLabel(role) {
    const label = document.createElement('b'); label.className = 'message-label'; label.dataset.role = role;
    if (role === 'user') {
      const icon = chevron(); icon.querySelector('path').setAttribute('d', 'M16 8a4 4 0 1 1-8 0a4 4 0 0 1 8 0M5 21v-2a7 7 0 0 1 14 0v2'); label.append(icon);
    } else {
      const avatar = document.createElement('img'); avatar.src = 'bee/idle.png'; avatar.alt = ''; label.append(avatar);
    }
    const text = document.createElement('span'); text.textContent = tr(role === 'user' ? 'you' : 'finalReply'); label.append(text);
    return label;
  }
  function renderLastTurn(output, item, text) {
    const user = lastUserText || item?.latestUserMessage || '';
    if (output.dataset.user === user && output.dataset.reply === text && output.dataset.locale === state.locale) return;
    const top = output.scrollTop;
    output.dataset.user = user; output.dataset.reply = text; output.dataset.locale = state.locale;
    output.replaceChildren();
    if (user) {
      const message = document.createElement('section'), label = messageLabel('user'), body = document.createElement('div');
      message.className = 'latest-user'; body.textContent = user;
      message.append(label, body); output.append(message);
      output.append(messageLabel('assistant'));
    }
    const body = document.createElement('div'); body.className = 'last-reply'; body.textContent = text; output.append(body);
    output.scrollTop = top;
  }
  function renderComposerButton(id, item, blocked, input) {
    const button = $(id), active = !!item && stoppable(item);
    const label = tr(active ? 'stop' : 'send');
    button.type = active ? 'button' : 'submit'; button.title = label; button.setAttribute('aria-label', label);
    button.disabled = active ? !state.connected || pendingSelection || sending || changingModel || stopping : blocked || !input.value.trim();
    const svg = button.querySelector('svg');
    if (button.dataset.stopping !== String(active)) {
      button.dataset.stopping = String(active); svg.replaceChildren();
      const shape = document.createElementNS('http://www.w3.org/2000/svg', active ? 'rect' : 'path');
      if (active) {
        for (const [name, value] of Object.entries({ x: '6', y: '6', width: '12', height: '12', rx: '2', fill: 'currentColor', stroke: 'none' })) shape.setAttribute(name, value);
      } else shape.setAttribute('d', 'M12 19V5m-6 6 6-6 6 6');
      svg.append(shape);
    }
  }
  async function stopCurrent() {
    const item = current(); if (!item || !stoppable(item) || stopping) return;
    stopping = true; render();
    try { await request('stop', { conversationKey: item.conversationKey }); }
    catch (error) { showError(error); }
    finally { stopping = false; render(); }
  }
  function renderNotices() {
    const container = $('notices');
    const active = state.conversations.filter(item => item.sessionId !== 'new' && busy(item));
    const activeKeys = new Set(active.map(item => item.conversationKey));
    const byKey = new Map(state.conversations.map(item => [item.conversationKey, item]));
    const recent = state.conversations.filter(item => item.sessionId !== 'new' && Number.isFinite(item.lastUserMessageAt))
      .sort((left, right) => right.lastUserMessageAt - left.lastUserMessageAt).slice(0, 3);
    const entries = active.length ? deckOrder.filter(key => activeKeys.has(key)).map(key => byKey.get(key)) : recent;
    const pages = Math.max(1, Math.ceil(entries.length / 3));
    noticePage = Math.max(0, Math.min(noticePage, pages - 1));
    container.hidden = expanded || !noticesOpen || mode !== 'bee' || settingsOpen;
    $('bee-shell').hidden = mode !== 'bee';
    $('bee-list-title').textContent = active.length ? `${active.length} · ${tr('runningConversations')}` : tr('recentConversations');
    $('bee-list-empty').hidden = entries.length > 0;
    const items = entries.slice(noticePage * 3, noticePage * 3 + 3).map(item => ({ ...item, key: item.conversationKey }));
    reconcileChildren($('bee-list-rows'), items, item => {
      const notice = document.createElement('section'); notice.className = 'notice';
      const button = document.createElement('button'), title = document.createElement('span'), activity = document.createElement('span');
      button.type = 'button'; button.className = 'notice-head conversation-open';
      title.className = 'notice-message'; activity.className = 'notice-status';
      button.onclick = () => { if (!dragging) void revealConversation(item.key); };
      button.append(title, activity, chevron()); notice.append(button); return notice;
    }, (notice, item) => {
      const button = notice.children[0];
      button.children[0].textContent = item.title;
      button.children[1].textContent = status(item);
      button.title = tr('expandReply'); button.setAttribute('aria-expanded', 'false');
      button.disabled = pendingSelection || sending || changingModel || stopping;
    });
    $('bee-pagination').hidden = pages < 2;
    $('bee-page').textContent = `${noticePage + 1} / ${pages}`;
    $('bee-prev').disabled = noticePage === 0; $('bee-next').disabled = noticePage === pages - 1;
    $('bee-all').hidden = active.length > 0;
    const badge = $('badge');
    badge.hidden = !active.length; badge.children[0].textContent = String(active.length);
    badge.title = `${active.length} · ${tr('runningConversations')}`;
    $('badge').setAttribute('aria-label', $('badge').title);
    $('badge').setAttribute('aria-expanded', String(noticesOpen && !expanded));
    $('bee-new').disabled = !state.connected || pendingSelection || sending || changingModel || stopping
      || !state.conversations.some(item => item.conversationKey === 'new');
  }
  function renderDeck() {
    const deck = $('capsule-deck');
    deck.hidden = mode !== 'island' || !expanded;
    const byKey = new Map(state.conversations.map(item => [item.conversationKey, item]));
    const ordered = deckOrder.map(key => byKey.get(key));
    const real = ordered.filter(item => item.sessionId !== 'new');
    const items = real.length && accordionKey !== 'new' ? real : ordered;
    const totalPages = Math.max(1, Math.ceil(items.length / 3));
    deckPage = Math.min(deckPage, totalPages - 1);
    if (!items.some(item => item.conversationKey === accordionKey)) accordionKey = null;
    // Keep the single canonical composer alive even when its old row is removed.
    const owner = card.parentElement?.parentElement?.dataset.key;
    const wanted = mode === 'island' && expanded ? accordionKey : null;
    const ownerPage = card.closest('.deck-page')?.dataset.key;
    const wantedPage = String(Math.floor(items.findIndex(item => item.conversationKey === wanted) / 3));
    if ((owner !== wanted || ownerPage !== wantedPage) && card.parentElement !== $('forms')) $('forms').append(card);
    const pages = Array.from({ length: totalPages }, (_, index) => ({ key: String(index), items: items.slice(index * 3, index * 3 + 3) }));
    reconcileChildren($('deck-pages'), pages, () => {
      const page = document.createElement('div'); page.className = 'deck-page'; return page;
    }, (page, group) => {
      reconcileChildren(page, group.items.map(item => ({ ...item, key: item.conversationKey })), item => {
        const row = document.createElement('section'); row.className = 'session-capsule';
        const summary = document.createElement('div'); summary.className = 'session-summary';
        const title = document.createElement('button'); title.type = 'button'; title.className = 'session-open';
        const icon = document.createElement('span'), name = document.createElement('b'), activity = document.createElement('span'), arrow = document.createElement('button');
        icon.className = 'session-icon'; activity.className = 'session-status'; arrow.className = 'session-expand summary-toggle';
        arrow.type = 'button'; arrow.append(chevron()); title.append(icon, name); summary.append(title, activity, arrow);
        const body = document.createElement('div'); body.className = 'session-content'; row.append(summary, body);
        const toggle = async () => {
          if (dragging) return;
          const revision = ++viewRevision;
          if (accordionKey === item.key) { accordionKey = null; render(); return; }
          if (await select(item.key) && revision === viewRevision && expanded && row.isConnected && Number(page.dataset.key) === deckPage) {
            accordionKey = item.key; render();
          }
        };
        title.onclick = arrow.onclick = toggle;
        return row;
      }, (row, item) => {
        const open = accordionKey === item.key && state.selectedConversationKey === item.key;
        row.dataset.open = String(open);
        const summary = row.children[0], title = summary.children[0], activity = summary.children[1], arrow = summary.children[2];
        title.disabled = arrow.disabled = pendingSelection || sending || changingModel;
        arrow.setAttribute('aria-expanded', String(open)); arrow.setAttribute('aria-label', tr(open ? 'collapseReply' : 'expandReply'));
        title.title = tr(open ? 'collapseReply' : 'expandReply');
        title.children[0].textContent = busy(item) ? '≋' : item.activity === 'done' ? '✓' : '•';
        title.children[1].textContent = item.title; activity.textContent = status(item);
        row.children[1].hidden = !open;
        if (open && mode === 'island' && expanded && card.parentElement !== row.children[1]) row.children[1].append(card);
      });
      page.inert = Number(group.key) !== deckPage;
      page.hidden = page.inert;
      page.setAttribute('aria-hidden', String(page.inert));
    });
    deck.dataset.expanded = String(!!accordionKey);
    $('deck-count').textContent = `${real.length} ${tr('sessionCount')}`;
    $('deck-new').disabled = !state.connected || pendingSelection || sending || changingModel || !state.conversations.some(item => item.conversationKey === 'new');
    $('deck-position').textContent = `${deckPage + 1} / ${totalPages}`;
    $('deck-pagination').hidden = totalPages < 2;
    $('deck-prev').disabled = deckPage === 0;
    $('deck-next').disabled = deckPage === totalPages - 1;
  }
  function goPage(page) {
    viewRevision += 1;
    deckPage = Math.max(0, Math.min(page, $('deck-pages').children.length - 1));
    accordionKey = null;
    render();
  }
  function renderBee() {
    if (!state || mode !== 'bee') { beeAnimator?.update({ visible: false }); return; }
    const canvas = $('bee-canvas');
    canvas.hidden = !!customImage;
    $('sprite').hidden = !customImage;
    if (customImage) {
      if ($('sprite').getAttribute('src') !== customImage) $('sprite').src = customImage;
      beeAnimator?.update({ visible: false }); return;
    }
    if (beeUnavailable) { $('bee').hidden = true; return; }
    const item = current(), box = canvas.getBoundingClientRect();
    const taskPose = item?.activity === 'done' && celebrationKey === item.conversationKey ? 'done'
      : ({ toolRunning: 'tool', thinking: 'thinking', needsInput: 'needs-input', replying: 'flying' }[item?.activity] || 'idle');
    const focus = expanded && document.hasFocus() && document.activeElement === $('input') ? $('input') : null;
    const targetElement = focus;
    let pose = taskPose, target = null, attention = '';
    if (targetElement) {
      const rect = targetElement.getBoundingClientRect();
      target = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
      pose = target.y < box.top + box.height / 2 ? 'input-look' : target.y > box.top + box.height / 2 ? 'peek' : 'idle';
      attention = 'lookInput';
    } else if (taskPose === 'tool') {
      const hands = window.WorkSwarmBeeMesh?.rigs.tool.hands;
      if (hands) target = { x: box.left + (hands[0][0] + hands[1][0]) / 2 * box.width / 512,
        y: box.top + (hands[0][1] + hands[1][1]) / 2 * box.height / 512 };
    } else if (taskPose === 'idle' && beePointer && !dragging) {
      target = beePointer; attention = 'lookPointer';
    }
    canvas.setAttribute('aria-label', tr('bee') + (attention ? ' · ' + tr(attention) : ''));
    canvas.dataset.posture = pose;
    const source = config.assetBase ? new URL(`bee/${pose}.png`, new URL(config.assetBase, location.href)).href : '';
    try {
      if (!beeAnimator) beeAnimator = window.WorkSwarmBeeAnimation.create({ canvas, onError: key => {
        beeUnavailable = true; expanded = true; render(); showError(new Error(tr(key)));
      } });
      beeAnimator.update({ pose, source, target, thinking: !focus && item?.activity === 'thinking', visible: true, motion: config.settings?.motion !== false, reducedMotion: state.reducedMotion });
    } catch {
      beeUnavailable = true; expanded = true; render(); showError(new Error(tr('beeCanvasFailed')));
    }
  }
  function render() {
    if (!state) return;
    setText();
    const item = current();
    $('forms').dataset.mode = mode;
    $('bee').hidden = mode !== 'bee';
    $('capsule').hidden = mode !== 'island' || expanded;
    card.hidden = !expanded || (mode === 'island' && (!accordionKey || accordionKey !== state.selectedConversationKey));
    renderDeck();
    const choices = state.conversations.map(conversation => ({ ...conversation, key: conversation.conversationKey, disabled: pendingSelection || sending }));
    if (!item) choices.unshift({ key: '', title: tr('chooseConversation'), disabled: true });
    reconcileChildren($('sessions'), choices, choice => {
      const option = document.createElement('button'); option.type = 'button'; option.setAttribute('role', 'menuitemradio');
      option.onclick = () => { $('conversation-menu').open = false; void select(choice.key); };
      return option;
    }, (option, choice) => {
      if (option.textContent !== choice.title) option.textContent = choice.title;
      option.disabled = choice.disabled;
      option.setAttribute('aria-checked', String(choice.key === state.selectedConversationKey));
    });
    $('selected-title').textContent = item?.title || tr('chooseConversation');
    const active = state.conversations.filter(busy);
    const rotating = active.length ? active[rotation % active.length] : item;
    $('capsule-title').textContent = rotating?.title || tr('conversation');
    $('capsule-open').dataset.key = rotating?.conversationKey || '';
    $('capsule-open').title = tr('expandList');
    $('capsule-status').textContent = rotating ? status(rotating) : tr('idle');
    const count = state.conversations.filter(conversation => conversation.sessionId !== 'new').length;
    $('capsule-count').textContent = String(count);
    $('capsule-count').hidden = count < 2;
    $('capsule').setAttribute('aria-label', `${tr('conversation')} · ${count} · ${rotating?.title || ''} · ${rotating ? status(rotating) : tr('idle')}`);
    $('capsule-expand').setAttribute('aria-expanded', String(expanded));
    $('running').hidden = active.length < 2;
    reconcileChildren($('running'), active.map(conversation => ({ ...conversation, key: conversation.conversationKey })), conversation => {
      const button = document.createElement('button'); button.type = 'button';
      button.addEventListener('click', () => void revealConversation(conversation.key));
      return button;
    }, (button, conversation) => {
      if (button.textContent !== conversation.title) button.textContent = conversation.title;
      button.title = status(conversation);
      const pressed = String(conversation.key === state.selectedConversationKey);
      if (button.getAttribute('aria-pressed') !== pressed) button.setAttribute('aria-pressed', pressed);
    });
    if (item) {
      $('bee').dataset.busy = String(busy(item));
      $('activity').textContent = status(item);
      if (expanded) void loadReply(item);
      const text = awaitingReply(item) ? status(item) : item.replyId
        ? replyText || tr(replyLoading ? 'loadingReply' : 'noReply')
        : tr(item.userMessageId || item.latestUserMessage ? 'noReply' : 'empty');
      renderLastTurn($('reply'), item, text);
      $('target').textContent = tr('replyingTo') + item.title;
      $('confirmation').hidden = item.activity !== 'needsInput';
      if (lastInputKey !== item.conversationKey) {
        $('input').value = localDrafts.get(item.conversationKey) ?? item.draft;
        lastInputKey = item.conversationKey;
      }
    } else {
      $('bee').dataset.busy = 'false';
      $('activity').textContent = tr('targetMissing');
      void loadReply(null); renderLastTurn($('reply'), null, tr('targetMissing'));
      $('target').textContent = tr('chooseConversation');
      $('confirmation').hidden = true;
      if (lastInputKey !== null) {
        $('input').value = '';
        lastInputKey = null;
      }
    }
    const blocked = !state.connected || !item?.canSend || pendingSelection || sending || changingModel;
    $('reply').hidden = !item?.replyId && !item?.userMessageId && !item?.latestUserMessage && !replyLoading;
    card.dataset.empty = String($('reply').hidden);
    for (const id of ['model']) {
      const picker = $(id), models = state.models || [];
      reconcileChildren(picker, [{ key: '', label: tr('chooseModel') }, ...models.map(model => ({ key: model.id, label: model.label }))],
        () => document.createElement('option'), (option, model) => { option.value = model.key; option.textContent = model.label; option.disabled = !model.key; });
      picker.value = item?.modelId || ''; picker.disabled = blocked || !models.length;
    }
    renderComposerButton('send', item, blocked, $('input'));
    $('input').disabled = !state.connected || !item || pendingSelection || sending || changingModel;
    renderNotices(); resize(); renderBee();
  }
  function setMode(next) {
    viewRevision += 1;
    mode = next; formAnchor = null; card.style.width = ''; card.style.height = ''; card.style.maxHeight = ''; expanded = false; accordionKey = null; deckPage = 0; settingsOpen = false; noticesOpen = false; lastInputKey = null; render();
  }
  async function send(text, input) {
    const item = current();
    if (!item?.canSend || !text.trim() || sending || pendingSelection || changingModel) return;
    sending = true; $('error').hidden = true; render();
    try {
      await request('send', { conversationKey: item.conversationKey, text });
      if (localDrafts.get(item.conversationKey) === text) localDrafts.delete(item.conversationKey);
      if (input.value === text) input.value = '';
      for (const id of ['input']) if ($(id).value === text) $(id).value = '';
    } catch (error) { showError(error); }
    finally { sending = false; render(); }
  }
  const workspace = () => openWorkspace(current()?.conversationKey);
  for (const id of ['workspace', 'card-workspace', 'capsule-workspace', 'confirm-workspace', 'settings-workspace']) $(id).onclick = workspace;
  function closePanel() {
    viewRevision += 1; $('input').blur(); $('conversation-menu').open = false; expanded = false; noticesOpen = false; render();
  }
  $('card-close').onclick = closePanel;
  $('conversation-title').onclick = event => { if (dragging) event.preventDefault(); };
  $('collapse').onclick = () => { viewRevision += 1; $('input').blur(); $('conversation-menu').open = false; expanded = false; noticesOpen = true; if (!replyText && !replyLoading) replySource = null; render(); };
  for (const id of ['model']) $(id).onchange = async () => {
    const item = current(), modelId = $(id).value; if (!item || !modelId || changingModel) return;
    changingModel = true; render();
    try { await request('model', { conversationKey: item.conversationKey, modelId }); }
    catch (error) { showError(error); }
    finally { changingModel = false; render(); }
  };
  document.addEventListener('pointerdown', event => { if (!$('conversation-menu').contains(event.target)) $('conversation-menu').open = false; });
  $('conversation-menu').addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape'].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === 'Escape') { $('conversation-menu').open = false; $('conversation-title').focus(); return; }
    $('conversation-menu').open = true;
    const buttons = [...$('conversation-menu').querySelectorAll('.conversation-options button')].filter(button => !button.disabled && !button.hidden);
    const index = buttons.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  });
  $('capsule').onclick = () => {
    if (dragging) return;
    viewRevision += 1; expanded = true; accordionKey = null; render();
  };
  $('settings-workspace').onclick = event => { event.stopPropagation(); if (!dragging) void workspace(); };
  $('deck-new').onclick = () => void revealConversation('new');
  const closeDeck = () => {
    viewRevision += 1;
    if (!replyText && !replyLoading) replySource = null;
    expanded = false; accordionKey = null; render();
  };
  $('deck-collapse').onclick = closeDeck;
  $('deck-prev').onclick = () => goPage(deckPage - 1);
  $('deck-next').onclick = () => goPage(deckPage + 1);
  document.addEventListener('keydown', event => {
    if (mode === 'bee' && event.key === 'Escape' && !event.defaultPrevented) {
      if (expanded) $('collapse').click(); else if (noticesOpen) closePanel();
      return;
    }
    if (mode !== 'island' || event.key !== 'Escape') return;
    viewRevision += 1;
    if (accordionKey) accordionKey = null; else { closeDeck(); return; }
    render();
  });
  $('bee').onclick = event => {
    if (dragging) return;
    clearTimeout(clickTimer);
    if (event.detail > 1) return;
    const toggle = toggleBeeList;
    if (!event.detail) toggle(); else clickTimer = setTimeout(toggle, 230);
  };
  $('bee').ondblclick = () => { clearTimeout(clickTimer); if (!dragging) void workspace(); };
  function toggleBeeList() {
    if (dragging) return;
    viewRevision += 1; $('input').blur(); $('conversation-menu').open = false;
    noticesOpen = expanded || !noticesOpen; expanded = false; settingsOpen = false; noticePage = 0; render();
  }
  $('badge').onclick = toggleBeeList;
  $('bee-new').onclick = () => { if (!dragging) void revealConversation('new'); };
  $('bee-list-close').onclick = closePanel;
  $('bee-prev').onclick = () => { viewRevision += 1; noticePage -= 1; render(); };
  $('bee-next').onclick = () => { viewRevision += 1; noticePage += 1; render(); };
  $('bee-all').onclick = () => { if (!dragging) void openWorkspace(); };
  for (const id of ['send']) $(id).onclick = () => { if (current() && stoppable(current())) void stopCurrent(); };
  $('composer').onsubmit = event => { event.preventDefault(); void send($('input').value, $('input')); };
  for (const id of ['input']) {
    $(id).oninput = () => {
      const item = current(); if (!item) return;
      localDrafts.set(item.conversationKey, $(id).value);
      void run('draft', { conversationKey: item.conversationKey, text: $(id).value });
      render();
    };
    $(id).onfocus = render; $(id).onblur = render;
  }
  window.addEventListener('blur', () => { beePointer = null; renderBee(); });
  window.addEventListener('focus', renderBee);
  for (const id of ['bee', 'capsule', 'capsule-deck', 'card', 'notices']) {
    let start;
    $(id).onpointerdown = event => {
      if (event.button !== 0 || event.target.closest('#settings-workspace')) return;
      if (['capsule-deck', 'card', 'notices'].includes(id)
        && event.target.closest('button,input,select,textarea,a,[contenteditable],.reply,.composer,.model-row,.card footer')) return;
      event.stopPropagation();
      dragging = false; start = { x: event.screenX, y: event.screenY, anchor: { ...formAnchor } };
      (event.target.closest('summary') || $(id)).setPointerCapture(event.pointerId);
    };
    $(id).onpointermove = event => {
      if (!start) return;
      const deltaX = Math.round(event.screenX - start.x), deltaY = Math.round(event.screenY - start.y);
      if (!dragging && Math.abs(deltaX) + Math.abs(deltaY) < 6) return;
      dragging = true;
      formAnchor = { ...start.anchor, x: start.anchor.x + deltaX, y: start.anchor.y + deltaY };
      crossingPoint = { x: event.screenX, y: event.screenY };
      resize();
    };
    const finish = event => {
      start = null; screenPointer = { x: event.screenX, y: event.screenY };
      setTimeout(() => { dragging = false; if (!geometryPending && formAnchor) formAnchor = geometry.contain(formAnchor, screenArea()); updatePointerHitTest(); }, 0);
    };
    $(id).onpointerup = finish; $(id).onpointercancel = finish;
  }
  function watchGeometry() {
    if (geometryFrame) return;
    geometryFrame = requestAnimationFrame(() => {
      geometryFrame = 0;
      if (formAnchor && (geometryPending || dragging)) { if (!windowTransition) paintGeometry(); watchGeometry(); }
    });
  }
  function updatePointerHitTest(target) {
    if (!target && screenPointer) target = document.elementFromPoint(screenPointer.x - window.screenX, screenPointer.y - window.screenY);
    const next = dragging || !!target?.closest('button,input,select,.card,.settings,.session-capsule,.capsule,.capsule-deck,.notice,#notices');
    if (next === interactive) return;
    interactive = next; void run('hitTest', { interactive });
  }
  document.addEventListener('pointermove', event => {
    beePointer = $('bee').contains(event.target) ? { x: event.clientX, y: event.clientY } : null;
    renderBee();
    screenPointer = { x: event.screenX, y: event.screenY }; updatePointerHitTest(event.target);
  });
  document.documentElement.addEventListener('mouseleave', () => {
    beePointer = null; renderBee();
    if (!interactive || dragging) return;
    interactive = false;
    void run('hitTest', { interactive });
  });
  setInterval(() => {
    if (!state || expanded || settingsOpen || document.querySelector('.capsule:hover')) return;
    rotation += 1; render();
    $('capsule-status').classList.remove('rotating');
    requestAnimationFrame(() => $('capsule-status').classList.add('rotating'));
  }, 4000);
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.protocol !== protocol || event.data.version !== 1) return;
    if (event.data.type === 'transition') {
      soundTransition = event.data.transition;
      void Promise.resolve().then(async () => {
        const area = event.data.transition.workArea;
        if (!area || !['x', 'y', 'width', 'height'].every(key => Number.isFinite(area[key])) || area.width <= 0 || area.height <= 0) throw new Error('Invalid window work area');
        transitionWorkArea = area; windowTransition = true;
        formAnchor = event.data.transition.direction === 'compact' ? initialAnchor(area) : geometry.contain(formAnchor, area);
        paintGeometry();
        await transitionSound.prepare();
        if (!recallMotion) recallMotion = window.WorkSwarmRecallMotion.create({ root: $('forms'), bee: $('bee'), onError: () => showError(new Error(tr('transitionFailed'))) });
        return recallMotion.prepare(event.data.transition);
      }).catch(() => {
        parent.postMessage({ protocol, version: 1, type: 'transition-failed', id: event.data.transition.id }, '*');
      });
      return;
    }
    if (event.data.type === 'transition-start') {
      if (soundTransition?.id === event.data.id && playedTransition !== event.data.id) {
        playedTransition = event.data.id;
        void transitionSound.play(soundTransition.direction, soundTransition.durationMs);
      }
      recallMotion?.start(event.data.id); if (state?.reducedMotion) recallMotion?.reduce(); return;
    }
    if (event.data.type === 'transition-ended') { recallMotion?.end(event.data.id); windowTransition = false; transitionWorkArea = null; resize(); return; }
    if (event.data.type === 'result') {
      const item = pending.get(event.data.id); if (!item) return;
      pending.delete(event.data.id); clearTimeout(item.timer);
      if (event.data.ok) item.resolve(event.data.value);
      else {
        const error = new Error(event.data.error?.message || tr('unknown'));
        error.code = event.data.error?.code; item.reject(error);
      }
      return;
    }
    if (event.data.type !== 'state') return;
    const firstState = !state;
    if (!state) initialWorkArea = event.data.initialWorkArea || null;
    const previousKey = state?.selectedConversationKey;
    const previousItem = current();
    state = { ...event.data.state, conversations: event.data.state.conversations.map(currentTurn) }; config = event.data.config;
    transitionSound.configure(config.transitionSound);
    const item = current();
    if (previousKey !== state.selectedConversationKey || item?.activity !== 'done') {
      clearTimeout(celebrationTimer); celebrationKey = null;
    }
    if (previousItem && item && previousKey === state.selectedConversationKey && busy(previousItem)
      && item.activity === 'done' && previousItem.inputMessageId === item.inputMessageId) {
      clearTimeout(celebrationTimer); celebrationKey = item.conversationKey;
      celebrationTimer = setTimeout(() => { celebrationKey = null; renderBee(); }, 3000);
    }
    const createdFromNew = sending && mode === 'island' && previousKey === 'new' && state.selectedConversationKey !== 'new';
    if (createdFromNew) accordionKey = state.selectedConversationKey;
    const keys = new Set(state.conversations.map(item => item.conversationKey));
    // Capsule positions belong to this view, independent of host activity sorting.
    deckOrder = deckOrder.filter(key => keys.has(key));
    const retained = new Set(deckOrder);
    for (const item of state.conversations) if (!retained.has(item.conversationKey)) deckOrder.push(item.conversationKey);
    if (createdFromNew) deckPage = Math.max(0, Math.floor(deckOrder.filter(key => key !== 'new').indexOf(accordionKey) / 3));
    const nextMode = config.mode || 'bee';
    if (nextMode !== mode) setMode(nextMode);
    customImage = config.settings?.image || '';
    document.documentElement.dataset.appearance = config.settings?.appearance || 'dark';
    document.documentElement.toggleAttribute('data-reduced-motion', state.reducedMotion);
    const colors = { '--paper': '--color-surface-elevated', '--ink': '--color-text-primary', '--muted': '--color-text-secondary', '--line': '--color-border-default', '--wash': '--color-surface-muted', '--link': '--color-text-link' };
    for (const [name, source] of Object.entries(colors)) if (state.theme[source]) document.documentElement.style.setProperty(name, state.theme[source]);
    for (const key of localDrafts.keys()) if (!keys.has(key)) localDrafts.delete(key);
    render();
    if (firstState && (!recallEnabled || state.reducedMotion)) void transitionSound.play('compact');
    if (state.reducedMotion) recallMotion?.reduce();
  });
  parent.postMessage({ protocol, version: 1, type: 'ready', ...(recallEnabled ? { transition: transitionOptions } : {}) }, '*');
  window.addEventListener('pagehide', () => { clearTimeout(celebrationTimer); beeAnimator?.dispose(); recallMotion?.dispose(); transitionSound.dispose(); }, { once: true });
})();

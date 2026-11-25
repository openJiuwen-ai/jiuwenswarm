/* ============================================================
 * 任务关系页（动态工作流图）
 * 契约：contract-snapshot-api.md v1.3 §2.5 / §5 / §6
 *
 * 交互：拖动平移 / 滚轮连续缩放 / 放大缩小 / 显示全图 /
 *       横纵布局切换 / 搜索 / 状态筛选 / 依赖链聚焦 /
 *       节点点击开详情浮层（关闭按钮 + Esc）
 * 视图状态：selected_task_id / detail_open / search / status_filter / canvas
 * ============================================================ */
(function (global) {
  'use strict';

  var TCV = global.TCV;
  var NS_SVG = 'http://www.w3.org/2000/svg';
  var NS_XHTML = 'http://www.w3.org/1999/xhtml';
  var STATUS_ORDER = ['in_progress', 'planning', 'in_review', 'blocked', 'pending', 'completed', 'cancelled'];

  var snap = null;
  var layout = null;
  var view = { k: 1, x: 0, y: 0 };
  var mode = 'horizontal';
  var selected = null;
  var focusOn = false;
  var searchTerm = '';
  var statusFilter = [];
  var nodeEls = {};   // task_id -> {fo, el, task}
  var edgeEls = [];   // {from, to, resolved, el}
  var dragging = null;
  var suppressClick = false;
  var userAdjusted = false;
  var connLost = false;
  var poller = null;
  var els = {};

  function $(id) { return document.getElementById(id); }

  boot();

  /* ============================================================ 启动 */
  function boot() {
    ['svg', 'viewport', 'edge-layer', 'node-layer', 'canvas', 'zoom-label', 'chips',
      'search-input', 'btn-zoom-in', 'btn-zoom-out', 'btn-fit', 'btn-layout', 'btn-focus',
      'graph-title', 'graph-status-badge', 'graph-meta', 'graph-note',
      'detail', 'detail-title', 'detail-body', 'detail-close',
      'canvas-empty', 'canvas-empty-t', 'canvas-empty-d'
    ].forEach(function (id) { els[id] = $(id); });

    TCV.Status.init('statusbar');
    bindEvents();

    TCV.State.load().then(function () {
      TCV.Theme.init();
      applyViewFromState();
      syncControls();
      refreshFull(true);
    });
  }

  function applyViewFromState() {
    var st = TCV.State.data;
    mode = st.canvas && st.canvas.layout_mode === 'vertical' ? 'vertical' : 'horizontal';
    searchTerm = st.search.task_graph || '';
    els['search-input'].value = searchTerm;
    statusFilter = (st.status_filter || []).slice();
    // ▣ 契约锚点：contract §5.3.2 W4 / §9 L10 —— 首屏是否 fit 的判据在这里决定。
    // 当前 W4 = 并集：empty===true 或 canvas 恰为缺省 → fit；否则严格还原、绝不二次 fit。
    // 该判据的前提是「没有任何入口能让视角精确停在缺省值」(zoom==1 且 pan==(0,0))。
    // 将来若新增「重置视角 / 100%」这类入口，W4 会立刻失效且容易静默回归：
    // 必须同时改 W4 判据，或由后端在 GET /ui-state 提供 written_keys（契约 §9 L10）。
    if (TCV.State.hasStoredCanvas()) {
      // 有存值就严格还原（哪怕正好等于缺省值），不再二次 fit
      view = { k: TCV.clamp(TCV.num(st.canvas.zoom, 1), 0.2, 4), x: TCV.num(st.canvas.pan.x, 0), y: TCV.num(st.canvas.pan.y, 0) };
      userAdjusted = true;
    }
  }

  function syncControls() {
    els['btn-layout'].textContent = mode === 'vertical' ? '纵向' : '横向';
    els['btn-layout'].title = '当前：' + (mode === 'vertical' ? '纵向（自上而下分层）' : '横向（自左向右分层）') + '，点击切换';
    els['btn-focus'].classList.toggle('is-on', focusOn);
    applyView();
  }

  /* ============================================================ 数据 */
  function refreshFull(first) {
    TCV.getJSON(TCV.endpoint('/snapshot', { wait: 0 })).then(function (s) {
      applySnapshot(s, first);
      if (first) startPoll();
    }).catch(function (e) {
      if (first) {
        TCV.Status.set('无法连接插件服务（' + (e.status ? 'HTTP ' + e.status : '网络/CORS 错误') + '），3 秒后重试…', 'bad');
        setTimeout(function () { refreshFull(true); }, 3000);
      } else {
        connLost = true;
        renderStatusBar();
      }
    });
  }

  function startPoll() {
    if (poller) return;
    poller = TCV.createPoller({
      onSnapshot: function (s) { applySnapshot(s, false); },
      onUnchanged: function (d) { applyUnchanged(d); },
      onOffline: function () { connLost = true; renderStatusBar(); },
      onOnline: function () { connLost = false; renderStatusBar(); },
      onResume: function () { connLost = false; refreshFull(false); }
    });
    if (snap && typeof snap.revision === 'number') poller.setRevision(snap.revision);
  }

  /** 契约 v1.6：unchanged 也要刷新「数据时刻」与 stale 徽标（不重渲染图） */
  function applyUnchanged(d) {
    if (!snap || !d) return;
    if (typeof d.revision === 'number') snap.revision = d.revision;
    if (typeof d.generated_at === 'number') snap.generated_at = d.generated_at;
    if (typeof d.served_at === 'number') snap.served_at = d.served_at;
    if (typeof d.stale === 'boolean') snap.stale = d.stale;
    if (d.stale_reason !== undefined) snap.stale_reason = d.stale_reason;
    renderMeta();
    renderStatusBar();
  }

  function applySnapshot(s, first) {
    var chk = TCV.checkSchema(s);
    if (!chk.ok) {
      TCV.Status.set(chk.msg, 'bad');
      showCanvasEmpty('无法显示任务关系', chk.msg);
      return;
    }
    var prevTasksKey = snap ? (snap.tasks || []).map(function (t) { return t.task_id + ':' + t.status + ':' + t.title; }).join('|') : '';
    snap = s;
    connLost = false;

    var tasksKey = (s.tasks || []).map(function (t) { return t.task_id + ':' + t.status + ':' + t.title; }).join('|');
    var graphChanged = tasksKey !== prevTasksKey;

    // 选中项校验：任务可能消失
    var exists = (s.tasks || []).some(function (t) { return t.task_id === TCV.State.data.selected_task_id; });
    selected = exists ? TCV.State.data.selected_task_id : null;
    if (!exists && TCV.State.data.selected_task_id) TCV.State.patch({ selected_task_id: null, detail_open: false });

    renderMeta();
    renderStatusBar();
    rebuildGraph(graphChanged);
    renderDetail();

    // ▣ W4（contract §5.3.2 / §9 L10）：只有「肯定没存过」才 fit。
    // 注意 fitAll(false) —— 自动 fit **不回写** /ui-state（W3），否则 empty 立刻翻 false，
    // 下次挂载不再 fit，而面板宽度/节点数一变，旧 zoom 就不合适了。
    if (first && !TCV.State.hasStoredCanvas()) {
      var better = bestMode();
      if (better !== mode) {
        mode = better;
        rebuildGraph(true);
        syncControls();
      }
      requestAnimationFrame(function () { fitAll(false); });
    }
  }

  /* ============================================================ 渲染 */
  function renderMeta() {
    var t = snap && snap.team;
    els['graph-title'].textContent = t ? (t.display_name || t.team_name) : '任务关系';
    els['graph-title'].title = t ? (t.display_name || t.team_name) + '\n' + t.team_name : '任务关系';

    var badge = els['graph-status-badge'];
    if (t) {
      badge.hidden = false;
      badge.textContent = (t.status_label || t.status || '—') + (t.status_source === 'derived' ? '·推导' : '') +
        (t.status_source === 'history' ? '·历史' : '');
      badge.className = 'badge ' + (t.status === 'running' || t.status === 'completed' ? 'badge-ok'
        : t.status === 'standby' ? 'badge-warn' : 'badge-mute');
      badge.title = '团队状态：' + (t.status_label || t.status) + '（依据：' +
        ({ event: '事件', derived: '推导', history: '历史记录' }[t.status_source] || t.status_source) + '）';
    } else {
      badge.hidden = true;
    }

    var meta = els['graph-meta'];
    meta.innerHTML = '';
    if (!snap) { meta.textContent = '正在读取…'; return; }
    var tasks = snap.tasks || [];
    var edges = (snap.graph && snap.graph.edges) || [];
    var g = snap.graph || {};
    [
      tasks.length + ' 个任务',
      edges.length + ' 条依赖',
      g.layers ? (g.layers.length + ' 层') : '',
      g.has_cycle ? '检测到环（层级已截断）' : '',
      '更新 ' + TCV.fmtTime(snap.generated_at),
      'r' + snap.revision
    ].filter(Boolean).forEach(function (p) { meta.appendChild(TCV.h('span', { text: p })); });
    if (snap.scope && snap.scope.kind === 'global') {
      meta.appendChild(TCV.h('span', { class: 'badge badge-scope', text: '全局视图', title: '会话标识缺失，视图状态按全局作用域保存' }));
    }
  }

  function renderStatusBar() {
    if (!snap) return;
    var text = '', kind = '';
    if (!snap.team) {
      text = '尚未定位到该会话的团队（历史会话或无团队会话），收到团队事件后会自动填充';
      kind = 'info';
    } else {
      var st = TCV.snapshotStatus(snap, connLost);
      text = st.text || '';
      kind = st.kind || '';
    }
    var extra = TCV.State.saveErrorText();
    if (extra) {
      text = text ? (text + '；' + extra) : extra;
      kind = kind || 'warn';
    }
    TCV.Status.set(text, kind);
  }

  function showCanvasEmpty(title, desc) {
    els['canvas-empty'].hidden = false;
    els['canvas-empty-t'].textContent = title;
    els['canvas-empty-d'].textContent = desc || '';
  }

  function hideCanvasEmpty() { els['canvas-empty'].hidden = true; }

  function rebuildGraph(rebuildLayout) {
    var tasks = snap ? (snap.tasks || []) : [];
    var graph = (snap && snap.graph) || {};

    if (!snap || !snap.team) {
      clearGraph();
      showCanvasEmpty('历史记录', '尚未获取到该会话的团队数据（插件安装前的历史会话，或当前会话不属于任何团队）。');
      els['graph-note'].textContent = '';
      return;
    }
    if (!tasks.length) {
      clearGraph();
      showCanvasEmpty('暂无任务', '该团队当前没有任务；创建任务后这里会出现依赖关系图。');
      els['graph-note'].textContent = '';
      return;
    }
    hideCanvasEmpty();

    if (rebuildLayout || !layout) {
      layout = TCV.computeLayout(tasks, graph, mode, layoutOpts());
      buildEdges();
      buildNodes();
    } else {
      // 仅状态/文案变化：原地更新节点文案，保持视角不跳
      (snap.tasks || []).forEach(function (t) {
        var n = nodeEls[t.task_id];
        if (!n) return;
        n.task = t;
        n.el.className = 'gnode st-' + (t.status || 'unknown');
        var titleEl = n.el.firstChild;
        if (titleEl) titleEl.textContent = t.title || t.task_id;
        var mEl = n.el.children[1];
        if (mEl) {
          mEl.children[0].className = 'dot st-' + (t.status || 'unknown');
          mEl.children[1].textContent = t.status_label || t.status || '';
          mEl.children[2].textContent = t.assignee ? (t.assignee.display_name || t.assignee.member_name) : '未认领';
        }
      });
    }

    renderChips();
    applyDecorations();
    els['graph-note'].textContent = (graph.edges && graph.edges.length) ? '' : '无依赖关系';
  }

  function clearGraph() {
    nodeEls = {};
    edgeEls = [];
    els['node-layer'].textContent = '';
    els['edge-layer'].textContent = '';
    els['chips'].textContent = '';
    layout = null;
  }

  function buildNodes() {
    var layer = els['node-layer'];
    layer.textContent = '';
    nodeEls = {};
    if (!layout) return;

    (snap.tasks || []).forEach(function (t) {
      var box = layout.nodes[t.task_id];
      if (!box) return;

      var fo = document.createElementNS(NS_SVG, 'foreignObject');
      fo.setAttribute('x', box.x);
      fo.setAttribute('y', box.y);
      fo.setAttribute('width', box.w);
      fo.setAttribute('height', box.h);

      var div = document.createElementNS(NS_XHTML, 'div');
      div.setAttribute('class', 'gnode st-' + (t.status || 'unknown'));
      div.setAttribute('title', (t.title || t.task_id) + '\n' + (t.status_label || t.status || '') +
        (t.assignee ? '\n负责人：' + (t.assignee.display_name || t.assignee.member_name) : '\n未认领'));

      var titleEl = document.createElementNS(NS_XHTML, 'div');
      titleEl.setAttribute('class', 'gnode-t');
      titleEl.textContent = t.title || t.task_id;

      var meta = document.createElementNS(NS_XHTML, 'div');
      meta.setAttribute('class', 'gnode-m');
      var dot = document.createElementNS(NS_XHTML, 'i');
      dot.setAttribute('class', 'dot st-' + (t.status || 'unknown'));
      var stText = document.createElementNS(NS_XHTML, 'span');
      stText.textContent = t.status_label || t.status || '';
      var asg = document.createElementNS(NS_XHTML, 'span');
      asg.setAttribute('class', 'gnode-a');
      asg.textContent = t.assignee ? (t.assignee.display_name || t.assignee.member_name) : '未认领';
      meta.appendChild(dot);
      meta.appendChild(stText);
      meta.appendChild(asg);

      div.appendChild(titleEl);
      div.appendChild(meta);

      div.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (suppressClick) return;
        onNodeClick(t.task_id);
      });

      fo.appendChild(div);
      layer.appendChild(fo);
      nodeEls[t.task_id] = { fo: fo, el: div, task: t };
    });
  }

  function buildEdges() {
    var layer = els['edge-layer'];
    layer.textContent = '';
    edgeEls = [];
    if (!layout || !snap || !snap.graph) return;

    (snap.graph.edges || []).forEach(function (e) {
      var a = layout.nodes[e.from];
      var b = layout.nodes[e.to];
      if (!a || !b) return;
      var p = document.createElementNS(NS_SVG, 'path');
      p.setAttribute('class', 'gedge' + (e.resolved ? ' is-resolved' : ' is-unresolved'));
      p.setAttribute('d', TCV.edgePath(a, b, mode));
      p.setAttribute('marker-end', e.resolved ? 'url(#tcv-arrow)' : 'url(#tcv-arrow-bad)');
      layer.appendChild(p);
      edgeEls.push({ from: e.from, to: e.to, resolved: !!e.resolved, el: p });
    });
  }

  /* ---------------- 状态筛选 chips ---------------- */
  function renderChips() {
    var box = els['chips'];
    box.textContent = '';
    var tasks = (snap && snap.tasks) || [];
    if (!tasks.length) return;

    var counts = {};
    var labelOf = {};
    tasks.forEach(function (t) {
      counts[t.status] = (counts[t.status] || 0) + 1;
      if (!labelOf[t.status]) labelOf[t.status] = t.status_label || t.status;
    });
    var present = Object.keys(counts).sort(function (a, b) {
      var ia = STATUS_ORDER.indexOf(a), ib = STATUS_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });

    var all = TCV.h('div', {
      class: 'chip' + (statusFilter.length ? '' : ' is-on'),
      role: 'button', tabindex: '0', title: '显示全部状态'
    }, [TCV.h('span', { text: '全部' }), TCV.h('span', { class: 'n', text: String(tasks.length) })]);
    all.addEventListener('click', function () { setFilter([]); });
    box.appendChild(all);

    present.forEach(function (s) {
      var on = statusFilter.indexOf(s) >= 0;
      var chip = TCV.h('div', {
        class: 'chip' + (on ? ' is-on' : ''),
        role: 'button', tabindex: '0',
        title: '按「' + (labelOf[s] || s) + '」筛选'
      }, [
        TCV.dot(s),
        TCV.h('span', { text: labelOf[s] || s }),
        TCV.h('span', { class: 'n', text: String(counts[s]) })
      ]);
      chip.addEventListener('click', function () {
        var next = statusFilter.slice();
        var i = next.indexOf(s);
        if (i >= 0) next.splice(i, 1); else next.push(s);
        setFilter(next);
      });
      box.appendChild(chip);
    });
  }

  function setFilter(next) {
    statusFilter = next.slice(0, 7);
    TCV.State.patch({ status_filter: statusFilter });
    renderChips();
    applyDecorations();
  }

  /* ---------------- 装饰：搜索 / 筛选 / 选中 / 依赖链 ---------------- */
  function taskMatches(t, q) {
    if (!q) return true;
    var hay = [
      t.task_id, t.title, t.content, t.status_label,
      t.assignee ? (t.assignee.display_name || '') : '',
      t.assignee ? t.assignee.member_name : ''
    ].join('\n').toLowerCase();
    return hay.indexOf(q) >= 0;
  }

  function chainSet() {
    var set = {};
    if (!snap || !selected) return set;
    var byId = {};
    (snap.tasks || []).forEach(function (t) { byId[t.task_id] = t; });
    if (!byId[selected]) return set;
    set[selected] = true;
    var stack = [selected];
    while (stack.length) {
      var cur = stack.pop();
      var t = byId[cur];
      if (!t) continue;
      (t.blocked_by || []).forEach(function (u) {
        if (byId[u] && !set[u]) { set[u] = true; stack.push(u); }
      });
    }
    stack = [selected];
    while (stack.length) {
      var c = stack.pop();
      var t2 = byId[c];
      if (!t2) continue;
      (t2.blocks || []).forEach(function (d) {
        if (byId[d] && !set[d]) { set[d] = true; stack.push(d); }
      });
    }
    return set;
  }

  function applyDecorations() {
    var q = (searchTerm || '').trim().toLowerCase();
    var hasSearch = !!q;
    var filterOn = statusFilter.length > 0;
    var chain = (focusOn && selected) ? chainSet() : null;
    var matchIds = {};

    Object.keys(nodeEls).forEach(function (id) {
      var n = nodeEls[id];
      var t = n.task || {};
      var match = !hasSearch || taskMatches(t, q);
      if (hasSearch && match) matchIds[id] = true;
      var stOk = !filterOn || statusFilter.indexOf(t.status) >= 0;

      var cl = n.el.classList;
      cl.toggle('is-match', hasSearch && match);
      cl.toggle('is-faded', hasSearch && !match);
      cl.toggle('is-filtered', !stOk);
      cl.toggle('is-selected', id === selected);
      cl.toggle('is-chain', !!(chain && chain[id]));
      cl.toggle('is-chain-dim', !!(chain && !chain[id]));
    });

    edgeEls.forEach(function (e) {
      var cl = e.el.classList;
      var bothMatch = !hasSearch || (matchIds[e.from] && matchIds[e.to]);
      cl.toggle('is-dim', !bothMatch);
      var inChain = !!(chain && chain[e.from] && chain[e.to]);
      cl.toggle('is-chain', inChain);
      e.el.setAttribute('marker-end', inChain ? 'url(#tcv-arrow-chain)'
        : (e.resolved ? 'url(#tcv-arrow)' : 'url(#tcv-arrow-bad)'));
    });
  }

  /* ============================================================ 视角 */
  function applyView() {
    els['viewport'].setAttribute('transform',
      'translate(' + round(view.x) + ',' + round(view.y) + ') scale(' + round(view.k, 4) + ')');
    els['zoom-label'].textContent = Math.round(view.k * 100) + '%';
  }

  function round(n, p) {
    var f = Math.pow(10, p || 1);
    return Math.round(n * f) / f;
  }

  /** 窄面板把节点收窄，避免「显示全图」压到 60% 以下看不清字 */
  function nodeWidth() {
    var w = canvasSize().w;
    if (w < 360) return 120;
    if (w < 460) return 132;
    return 168;
  }

  function layoutOpts() { return { nodeW: nodeWidth() }; }

  function canvasSize() {
    var r = els['canvas'].getBoundingClientRect();
    return { w: Math.max(1, r.width), h: Math.max(1, r.height) };
  }

  /** 某种布局在当前画布尺寸下「显示全图」能得到多大缩放 */
  function fitScaleFor(lay) {
    if (!lay) return 0;
    var size = canvasSize();
    var pad = 32;
    return Math.min((size.w - pad) / Math.max(1, lay.width),
      (size.h - pad) / Math.max(1, lay.height));
  }

  /**
   * 首次挂载且没有存过的画布视角时，横向/纵向各试一次，选缩放更大的那种。
   * 窄面板里 4 层横向链只能压到 37%（字看不清），纵向能到 68% —— 让默认视图自己挑。
   */
  function bestMode() {
    if (!snap || !snap.tasks || !snap.tasks.length) return mode;
    var g = snap.graph || {};
    var sh = fitScaleFor(TCV.computeLayout(snap.tasks, g, 'horizontal', layoutOpts()));
    var sv = fitScaleFor(TCV.computeLayout(snap.tasks, g, 'vertical', layoutOpts()));
    if (sv > sh * 1.05) return 'vertical';
    if (sh > sv * 1.05) return 'horizontal';
    return mode;
  }

  function fitAll(save) {
    if (!layout) return;
    var size = canvasSize();
    var pad = 16;
    var k = TCV.clamp(Math.min((size.w - pad * 2) / Math.max(1, layout.width),
      (size.h - pad * 2) / Math.max(1, layout.height)), 0.2, 4);
    view.k = k;
    view.x = (size.w - layout.width * k) / 2;
    view.y = (size.h - layout.height * k) / 2;
    applyView();
    userAdjusted = false;
    // save===false 表示「自动 fit」（首屏/未存过时的兜底）：绝不落盘，见 W3。
    // 只有用户显式点「全图」才 save（那是用户选择，需要持久化）。
    if (save !== false) saveCanvas();
  }

  function zoomBy(factor, anchorX, anchorY) {
    var size = canvasSize();
    var ax = typeof anchorX === 'number' ? anchorX : size.w / 2;
    var ay = typeof anchorY === 'number' ? anchorY : size.h / 2;
    var k2 = TCV.clamp(view.k * factor, 0.2, 4);
    if (k2 === view.k) return;
    view.x = ax - (ax - view.x) * (k2 / view.k);
    view.y = ay - (ay - view.y) * (k2 / view.k);
    view.k = k2;
    applyView();
    userAdjusted = true;
    saveCanvas();
  }

  var saveCanvas = TCV.debounce(function () {
    TCV.State.patch({
      canvas: { layout_mode: mode, pan: { x: round(view.x), y: round(view.y) }, zoom: round(view.k, 4) }
    });
  }, 420);

  function setLayout(next) {
    mode = next === 'vertical' ? 'vertical' : 'horizontal';
    els['btn-layout'].textContent = mode === 'vertical' ? '纵向' : '横向';
    els['btn-layout'].title = '当前：' + (mode === 'vertical' ? '纵向（自上而下分层）' : '横向（自左向右分层）') + '，点击切换';
    if (!snap || !snap.tasks || !snap.tasks.length) { saveCanvas(); return; }
    layout = TCV.computeLayout(snap.tasks, snap.graph || {}, mode, layoutOpts());
    buildEdges();
    buildNodes();
    applyDecorations();
    fitAll(true);
    userAdjusted = false;
  }

  /* ============================================================ 详情浮层 */
  function selectedTask() {
    if (!snap || !selected) return null;
    var ts = snap.tasks || [];
    for (var i = 0; i < ts.length; i++) if (ts[i].task_id === selected) return ts[i];
    return null;
  }

  function onNodeClick(id) {
    selected = id;
    TCV.State.patch({ selected_task_id: id, detail_open: true });
    applyDecorations();
    renderDetail();
  }

  function closeDetail() {
    TCV.State.patch({ detail_open: false });
    renderDetail();
  }

  function renderDetail() {
    var box = els['detail'];
    var t = selectedTask();
    var open = !!(t && TCV.State.data.detail_open);
    if (!open) {
      box.hidden = true;
      els['detail-body'].textContent = '';
      els['detail-title'].textContent = '';
      return;
    }
    box.hidden = false;
    els['detail-title'].textContent = t.title || t.task_id;

    var body = els['detail-body'];
    body.textContent = '';

    /* 徽标 */
    var badges = TCV.h('div', { class: 'detail-badges' }, [
      TCV.statusPill(t.status, t.status_label),
      t.flags && t.flags.is_root ? TCV.h('span', { class: 'badge', text: '起点任务' }) : null,
      t.flags && t.flags.is_join ? TCV.h('span', { class: 'badge', text: '汇合点（多上游）' }) : null,
      t.flags && t.flags.is_parallel_branch ? TCV.h('span', { class: 'badge', text: '并行分支（多下游）' }) : null,
      t.flags && t.flags.is_isolated ? TCV.h('span', { class: 'badge', text: '孤立任务' }) : null,
      t.unresolved_blocked_by && t.unresolved_blocked_by.length
        ? TCV.h('span', { class: 'badge badge-hint', text: '被未解除依赖阻塞' }) : null,
      TCV.h('span', { class: 'badge', text: '第 ' + (t.depth || 0) + ' 层' })
    ]);
    body.appendChild(badges);

    /* 基本信息 */
    var info = TCV.h('div', { class: 'detail-section' }, [
      TCV.h('h4', { text: '基本信息' }),
      TCV.kv('负责人', t.assignee ? (t.assignee.display_name || t.assignee.member_name) : '未认领', { dim: !t.assignee }),
      t.assignee && !t.assignee.display_name ? TCV.kv('成员 ID', t.assignee.member_name) : null,
      TCV.kv('任务 ID', t.task_id),
      TCV.kv('最近流转', TCV.fmtTimeFull(t.updated_at), { dim: t.updated_at === null })
    ]);
    body.appendChild(info);

    /* 依赖 */
    var byId = {};
    (snap.tasks || []).forEach(function (x) { byId[x.task_id] = x; });
    var up = (t.blocked_by || []);
    var down = (t.blocks || []);
    var unresolved = t.unresolved_blocked_by || [];

    var relBox = TCV.h('div', { class: 'detail-section' }, [TCV.h('h4', { text: '依赖关系' })]);
    relBox.appendChild(TCV.h('div', { class: 'rel-list' }, [
      TCV.h('span', { class: 'kv-k', style: 'flex:0 0 auto;font-size:10.5px;color:var(--fg-mute)', text: '前置' })
    ].concat(up.length ? up.map(function (id) {
      var isUnresolved = unresolved.indexOf(id) >= 0;
      var label = byId[id] ? byId[id].title : id;
      var chip = TCV.h('span', {
        class: 'rel' + (isUnresolved ? ' is-unresolved' : ''),
        title: (isUnresolved ? '未解除：' : '') + label + '\n' + id,
        text: label
      });
      chip.addEventListener('click', function () { if (byId[id]) onNodeClick(id); });
      return chip;
    }) : [TCV.h('span', { class: 'kv-v dim', text: '无（起点任务）' })])));

    relBox.appendChild(TCV.h('div', { class: 'rel-list', style: 'margin-top:4px' }, [
      TCV.h('span', { class: 'kv-k', style: 'flex:0 0 auto;font-size:10.5px;color:var(--fg-mute)', text: '后置' })
    ].concat(down.length ? down.map(function (id) {
      var label = byId[id] ? byId[id].title : id;
      var chip = TCV.h('span', { class: 'rel', title: label + '\n' + id, text: label });
      chip.addEventListener('click', function () { if (byId[id]) onNodeClick(id); });
      return chip;
    }) : [TCV.h('span', { class: 'kv-v dim', text: '无（末端任务）' })])));
    body.appendChild(relBox);

    /* 正文全文 */
    var contentBox = TCV.h('div', { class: 'detail-section' }, [
      TCV.h('h4', { text: '任务正文' }),
      TCV.h('pre', { class: 'detail-content', text: t.content || '（该任务没有正文）' })
    ]);
    body.appendChild(contentBox);

    body.appendChild(TCV.h('div', {
      class: 'detail-note',
      text: '提示：平台任务记录不含创建时间与交接摘要，故此处不展示。按 Esc 或点右上角关闭。'
    }));
  }

  /* ============================================================ 事件 */
  function bindEvents() {
    var svg = els['svg'];

    function isOnNode(target) {
      var n = target;
      while (n && n !== svg) {
        if (n.nodeType === 1 && n.classList && n.classList.contains('gnode')) return true;
        n = n.parentNode;
      }
      return false;
    }

    /* 滚轮连续缩放（以光标为锚点） */
    els['canvas'].addEventListener('wheel', function (ev) {
      ev.preventDefault();
      var r = svg.getBoundingClientRect();
      var factor = Math.exp(-ev.deltaY * (ev.deltaMode === 1 ? 0.04 : 0.0016));
      zoomBy(factor, ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });

    /* 拖动平移（只在空白/网格区域按下时抓取，避免 pointer capture 吞掉节点 click） */
    svg.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      // 每次按下都重置「本次手势是否拖动过」；否则一次平移后 suppressClick 会残留 true，
      // 把后续所有的节点点击全部吞掉（真实鼠标测出的问题）。
      suppressClick = false;
      if (isOnNode(ev.target)) return;
      dragging = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y, moved: 0 };
      try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      els['canvas'].classList.add('is-dragging');
    });
    svg.addEventListener('pointermove', function (ev) {
      if (!dragging) return;
      var dx = ev.clientX - dragging.x;
      var dy = ev.clientY - dragging.y;
      dragging.moved = Math.max(dragging.moved, Math.abs(dx) + Math.abs(dy));
      if (dragging.moved > 3) suppressClick = true;
      view.x = dragging.vx + dx;
      view.y = dragging.vy + dy;
      applyView();
    });
    function endDrag(ev) {
      if (!dragging) return;
      var moved = dragging.moved;
      dragging = null;
      els['canvas'].classList.remove('is-dragging');
      try { svg.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      if (moved > 3) { userAdjusted = true; saveCanvas(); }
    }
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', endDrag);
    /* 点空白处：不关闭详情（避免误触），但清掉选中的高亮链 */
    svg.addEventListener('click', function () {
      if (suppressClick) return;
      /* 点击背景 = 取消依赖链聚焦（详情保留，用 Esc/关闭按钮关） */
      if (focusOn) {
        focusOn = false;
        els['btn-focus'].classList.remove('is-on');
        applyDecorations();
      }
    });

    /* 缩放按钮 */
    els['btn-zoom-in'].addEventListener('click', function () { zoomBy(1.25); });
    els['btn-zoom-out'].addEventListener('click', function () { zoomBy(1 / 1.25); });
    els['btn-fit'].addEventListener('click', function () { fitAll(true); });

    /* 布局切换 */
    els['btn-layout'].addEventListener('click', function () {
      setLayout(mode === 'horizontal' ? 'vertical' : 'horizontal');
    });

    /* 依赖链聚焦 */
    els['btn-focus'].addEventListener('click', function () {
      focusOn = !focusOn;
      els['btn-focus'].classList.toggle('is-on', focusOn);
      if (focusOn && !selected) {
        TCV.Status.set('先在图上点一个任务，再点「依赖链」聚焦它的上下游', 'info');
        setTimeout(function () { renderStatusBar(); }, 2600);
      }
      applyDecorations();
    });

    /* 搜索 */
    var onSearch = TCV.debounce(function () {
      searchTerm = els['search-input'].value || '';
      // 同 team-info：只写本页签子键
      TCV.State.patch({ search: { task_graph: searchTerm } });
      applyDecorations();
    }, 180);
    els['search-input'].addEventListener('input', onSearch);

    /* 详情关闭 */
    els['detail-close'].addEventListener('click', closeDetail);
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' || ev.key === 'Esc') {
        if (!els['detail'].hidden) {
          ev.preventDefault();
          closeDetail();
        }
      }
    });

    /* 尺寸变化：用户没手动调整过视角就重新适配 */
    var onResize = TCV.debounce(function () {
      if (!userAdjusted) fitAll(false);
    }, 150);
    global.addEventListener('resize', onResize);
  }

})(window);

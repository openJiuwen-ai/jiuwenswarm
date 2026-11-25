/* ============================================================
 * 团队信息页
 * 契约：contract-snapshot-api.md v1.1 §2 / §5 / §6
 * 只读快照 + 视图状态回填（展开项 / 分页 / 搜索 / 主题）
 * ============================================================ */
(function (global) {
  'use strict';

  var TCV = global.TCV;
  var PAGE_SIZE = 6;

  var snap = null;
  var poller = null;
  var connLost = false;
  var searchTerm = '';
  var els = {};

  function $(id) { return document.getElementById(id); }

  boot();

  /* ---------------------------------------------------------- 启动 */
  function boot() {
    ['team-name', 'team-status-badge', 'status-src-badge', 'team-meta', 'teamcard',
      'lists', 'pager', 'search-input', 'search-note', 'theme-btn'
    ].forEach(function (id) { els[id] = $(id); });

    TCV.Status.init('statusbar');
    bindStatic();

    // 1) 拉视图状态 → 2) 拉快照 → 3) 渲染 → 4) 长轮询
    TCV.State.load().then(function () {
      TCV.Theme.init();
      searchTerm = TCV.State.data.search.team_info || '';
      els['search-input'].value = searchTerm;
      refreshFull(true);
    });
  }

  function bindStatic() {
    var onSearch = TCV.debounce(function () {
      searchTerm = els['search-input'].value || '';
      // 只发本页签的子键：服务端一级深合并会保留任务图页的 task_graph，
      // 带上「挂载时读到的旧值」反而会覆盖对方刚写的新值。
      TCV.State.patch({ search: { team_info: searchTerm } });
      renderAll();
    }, 180);
    els['search-input'].addEventListener('input', onSearch);
  }

  /* ---------------------------------------------------------- 数据 */
  function refreshFull(first) {
    TCV.getJSON(TCV.endpoint('/snapshot', { wait: 0 })).then(function (s) {
      applySnapshot(s);
      if (first) startPoll();
    }).catch(function (e) {
      if (first) {
        TCV.Status.set('无法连接插件服务（' + (e.status ? 'HTTP ' + e.status : '网络/CORS 错误') + '），3 秒后重试…', 'bad');
        renderShell(null);
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
      onSnapshot: function (s) { applySnapshot(s); },
      onUnchanged: function (d) { applyUnchanged(d); },
      onOffline: function () { connLost = true; renderStatusBar(); },
      onOnline: function () { connLost = false; renderStatusBar(); },
      onResume: function () { connLost = false; refreshFull(false); }
    });
    if (snap && typeof snap.revision === 'number') poller.setRevision(snap.revision);
  }

  /**
   * 契约 v1.6：长轮询 `unchanged` 也带 stale / generated_at / served_at。
   * 数据不变也要刷新「数据时刻」与降级徽标，否则后端进 stale 期间页面毫无提示。
   */
  function applyUnchanged(d) {
    if (!snap || !d) return;
    if (typeof d.revision === 'number') snap.revision = d.revision;
    if (typeof d.generated_at === 'number') snap.generated_at = d.generated_at;
    if (typeof d.served_at === 'number') snap.served_at = d.served_at;
    if (typeof d.stale === 'boolean') snap.stale = d.stale;
    if (d.stale_reason !== undefined) snap.stale_reason = d.stale_reason;
    renderHeader();
    renderStatusBar();
  }

  function applySnapshot(s) {
    var chk = TCV.checkSchema(s);
    if (!chk.ok) { renderFatal(chk.msg); return; }
    snap = s;
    connLost = false;
    renderAll();
  }

  function renderFatal(msg) {
    els.teamcard.innerHTML = '';
    els.teamcard.appendChild(TCV.emptyBlock('无法显示团队信息', msg));
    els.lists.innerHTML = '';
    els.pager.hidden = true;
    TCV.Status.set(msg, 'bad');
  }

  /* ---------------------------------------------------------- 渲染 */
  function renderAll() {
    renderHeader();
    renderStatusBar();
    renderTeamCard();
    renderLists();
    renderPager();
  }

  function renderShell() {
    renderStatusBar();
  }

  function statusBadgeClass(status) {
    if (status === 'running' || status === 'completed') return 'badge-ok';
    if (status === 'standby') return 'badge-warn';
    if (status === 'history') return 'badge-mute';
    return 'badge-mute';
  }

  function srcText(src) {
    return { event: '事件', derived: '推导', history: '历史记录' }[src] || (src || '未知');
  }

  function findLeader() {
    if (!snap || !snap.team) return null;
    var ms = snap.members || [];
    var id = snap.team.leader_member_name;
    for (var i = 0; i < ms.length; i++) {
      if (ms[i].is_leader || ms[i].member_name === id) return ms[i];
    }
    return null;
  }

  function renderHeader() {
    var t = snap && snap.team;
    els['team-name'].textContent = t ? (t.display_name || t.team_name) : '团队信息';
    els['team-name'].title = t ? (t.display_name || t.team_name) + '\n' + t.team_name : '团队信息';

    var badge = els['team-status-badge'];
    if (t) {
      badge.hidden = false;
      badge.textContent = t.status_label || t.status || '—';
      badge.className = 'badge ' + statusBadgeClass(t.status);
      badge.title = '团队状态：' + (t.status_label || t.status) + '（依据：' + srcText(t.status_source) + '）';
    } else {
      badge.hidden = true;
    }

    var src = els['status-src-badge'];
    if (t && t.status_source === 'derived') {
      src.hidden = false;
      src.textContent = '推导';
      src.title = '平台没有 team_completed 事件，此「已完成」由任务与成员状态推导得出';
    } else if (t && t.status_source === 'history') {
      src.hidden = false;
      src.textContent = '历史记录';
      src.title = '该团队没有收到任何生命周期事件，状态按「历史记录」展示';
    } else {
      src.hidden = true;
    }

    var meta = els['team-meta'];
    meta.innerHTML = '';
    if (!snap) { meta.textContent = '正在读取…'; return; }
    var leader = findLeader();
    var parts = [];
    if (t) {
      parts.push('队长 ' + (leader ? (leader.display_name || leader.member_name) : (t.leader_member_name || '—')));
      parts.push('创建 ' + TCV.fmtTime(t.created_at));
    }
    parts.push('更新 ' + TCV.fmtTime(snap.generated_at));
    parts.push('r' + snap.revision);
    parts.forEach(function (p) { meta.appendChild(TCV.h('span', { text: p })); });
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

  function renderTeamCard() {
    var card = els.teamcard;
    card.innerHTML = '';
    var t = snap && snap.team;

    if (!t) {
      card.appendChild(TCV.h('div', { class: 'empty', style: 'margin:0' }, [
        TCV.h('div', { class: 'empty-t', text: '历史记录' }),
        TCV.h('div', {
          class: 'empty-d',
          text: '尚未获取到该会话的团队数据（插件安装前的历史会话，或当前会话不属于任何团队）。'
        })
      ]));
      return;
    }

    var p = t.progress || {};
    var head = TCV.h('div', { class: 'tc-head' }, [
      TCV.dot(t.status === 'running' || t.status === 'completed' ? 'ready'
        : t.status === 'standby' ? 'paused' : 'shut_down'),
      TCV.h('span', { class: 'pill', text: t.status_label || t.status || '—' }),
      TCV.h('span', { class: 'tc-created', text: '创建于 ' + TCV.fmtTime(t.created_at) })
    ]);
    card.appendChild(head);

    var prog = TCV.h('div', { class: 'tc-progress' }, [
      TCV.miniBar(p.percent),
      TCV.h('div', { class: 'tc-prog-text' }, [
        TCV.h('span', { text: '团队任务完成 ' + (p.completed || 0) + '/' + (p.total || 0) }),
        TCV.h('span', { text: '进度 ' + TCV.fmtPct(p.percent) }),
        p.cancelled ? TCV.h('span', { text: '已取消 ' + p.cancelled }) : null
      ])
    ]);
    card.appendChild(prog);

    var counts = TCV.h('div', { class: 'tc-counts' });
    var fields = TCV.progressFields(p).filter(function (f) {
      return f[1] > 0 || f[0] === '进行中' || f[0] === '待开始';
    });
    if (fields.length) {
      fields.forEach(function (f) {
        counts.appendChild(TCV.h('span', { text: f[0] + ' ' + f[1] }));
      });
      card.appendChild(counts);
    }

    card.appendChild(TCV.h('div', { class: 'tc-desc' }, [
      t.desc ? TCV.h('span', { text: t.desc }) : TCV.h('span', { class: 'dim', text: '暂无团队简介' })
    ]));
  }

  /** 快照内的队长名（契约字段 team.leader_member_name） */
  function leaderName() {
    return (snap && snap.team && snap.team.leader_member_name) || '';
  }

  /**
   * 是否划入「队长」分区。三级判据：
   * 1) 成员自带 is_leader / role_group==='captain'（mock 与部分宿主）；
   * 2) 成员身份 = team.leader_member_name（宿主 Ops 常只给这一个字段，实测真机全员 role=teammate）；
   * 3) 成员 display_name 与该名相同（个别宿主 leader_member_name 给的是展示名）。
   * 兜底用契约已有字段，不新增字段、不改契约。
   */
  function isCaptain(m) {
    if (!m) return false;
    if (m.is_leader || m.role_group === 'captain') return true;
    var n = leaderName();
    if (!n) return false;
    return m.member_name === n || m.display_name === n;
  }

  function roleText(m) {
    if (!m) return '—';
    var captain = isCaptain(m);
    // 契约 v1.4：优先用后端直出的 role_label；
    // 但真机上队长成员的 role_label 也是「成员」，此时代之以「队长」，避免与分区标题自相矛盾
    if (m.role_label && !(captain && m.role_label === '成员')) return m.role_label;
    if (captain) return '队长';
    if (m.role === 'teammate') return '成员';
    return m.role || '—'; // 未知 role 原样显示（前端不自行翻译枚举）
  }

  function matchMember(m, q) {
    if (!q) return true;
    var hay = [
      m.display_name, m.member_name, m.desc, m.role, m.status_label, m.execution_label,
      m.current_task ? m.current_task.title : ''
    ].join('\n').toLowerCase();
    return hay.indexOf(q) >= 0;
  }

  function renderLists() {
    var lists = els.lists;
    lists.innerHTML = '';
    if (!snap) return;

    var members = snap.members || [];
    if (!members.length) {
      var avail = snap.availability || {};
      lists.appendChild(TCV.emptyBlock('暂无成员数据',
        avail.last_success_at === null || avail.last_success_at === undefined
          ? '后端尚未成功读取到团队数据，团队事件到达后会自动重试。'
          : '该团队当前没有成员记录。'));
      els.pager.hidden = true;
      els['search-note'].textContent = '';
      return;
    }

    var q = (searchTerm || '').trim().toLowerCase();
    var captains = members.filter(isCaptain).filter(function (m) { return matchMember(m, q); });
    var others = members.filter(function (m) { return !isCaptain(m); }).filter(function (m) { return matchMember(m, q); });

    if (q) {
      els['search-note'].textContent = '匹配 ' + (captains.length + others.length) + '/' + members.length;
    } else {
      els['search-note'].textContent = '';
    }

    if (captains.length) {
      lists.appendChild(sectionHeader('队长', captains.length));
      captains.forEach(function (m) { lists.appendChild(memberRow(m)); });
    }

    if (others.length || !q) {
      var total = others.length;
      var pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      var page = Math.min(Math.max(1, TCV.State.data.member_page || 1), pages);
      if (page !== TCV.State.data.member_page) {
        TCV.State.patch({ member_page: page }, 0);
      }
      lists.appendChild(sectionHeader('成员', q ? total + ' 条匹配' : total));
      var slice = q ? others : others.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
      if (!slice.length) {
        lists.appendChild(TCV.emptyBlock('没有匹配的成员', '换个关键词试试，或清空搜索框。'));
      }
      slice.forEach(function (m) { lists.appendChild(memberRow(m)); });
    }
  }

  function sectionHeader(text, count) {
    return TCV.h('div', { class: 'section-h' }, [
      TCV.h('span', { text: text }),
      TCV.h('span', { class: 'cnt', text: String(count === undefined ? '' : count) })
    ]);
  }

  function memberRow(m) {
    var expanded = (TCV.State.data.expanded_member_ids || []).indexOf(m.member_name) >= 0;
    var row = TCV.h('div', {
      class: 'mrow' + (expanded ? ' is-open' : ''),
      role: 'button',
      tabindex: '0',
      'aria-expanded': expanded ? 'true' : 'false',
      dataset: { name: m.member_name }
    });

    var head = TCV.h('div', { class: 'mrow-head' }, [
      TCV.dot(m.status),
      TCV.h('span', { class: 'mname', text: m.display_name || m.member_name, title: (m.display_name || m.member_name) + (m.member_name ? '\n' + m.member_name : '') }),
      TCV.h('span', { class: isCaptain(m) ? 'mtag mtag-lead' : 'mtag', text: roleText(m) }),
      TCV.h('span', { class: 'mstat', text: m.status_label || m.status || '—' })
    ]);

    var sub = TCV.h('div', { class: 'mrow-sub' }, [
      TCV.h('span', { class: 'mexec', text: m.execution_label || '未上报执行状态' }),
      TCV.h('span', { class: 'sep', text: '·' }),
      TCV.h('span', { class: 'mprog', text: TCV.progressLine(m.task_progress) }),
      m.active_task_count ? TCV.h('span', { text: '· 在办 ' + m.active_task_count }) : null
    ]);

    var ct = m.current_task;
    var taskLine = TCV.h('div', { class: 'mrow-task' }, [
      TCV.h('span', { class: 'mtask-k', text: '当前任务' }),
      ct
        ? TCV.h('span', {
          class: 'mtask-v',
          title: ct.title + '\n' + (ct.status_label || ct.status || ''),
          text: ct.title + '（' + (ct.status_label || ct.status || '—') + '）'
        })
        : TCV.h('span', { class: 'mtask-v dim', text: '暂无进行中任务' })
    ]);

    var main = TCV.h('div', { class: 'mrow-main' }, [head, sub, taskLine]);
    if (expanded) main.appendChild(memberDetail(m));
    row.appendChild(main);

    row.addEventListener('click', function () { toggleMember(m.member_name); });
    row.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        toggleMember(m.member_name);
      }
    });
    return row;
  }

  function memberDetail(m) {
    var body = TCV.h('div', { class: 'mbody' });
    body.addEventListener('click', function (ev) { ev.stopPropagation(); });

    /* 1. 简介（契约 §2.4.1：desc 为空时展示三行事实，不编造） */
    var descBlk = TCV.h('div', { class: 'mbody-block' });
    descBlk.appendChild(TCV.h('div', { class: 'blk-h' }, [
      TCV.h('span', { text: '简介' }),
      TCV.h('button', {
        class: 'mcollapse', type: 'button', text: '收起',
        style: 'margin-left:auto;font-size:10px;padding:0 5px',
        onclick: function (ev) { ev.stopPropagation(); toggleMember(m.member_name); }
      })
    ]));
    if (m.desc) {
      descBlk.appendChild(TCV.h('p', { class: 'detail-text', text: m.desc }));
    } else {
      descBlk.appendChild(TCV.h('div', { class: 'blk-h' }, [
        TCV.h('span', { class: 'badge badge-hint', text: '无简介 · 展示事实', title: '成员记录里没有简介字段，按契约展示角色 / 状态 / 当前任务三项事实' })
      ]));
      descBlk.appendChild(TCV.h('ul', { class: 'fact-list' }, [
        TCV.h('li', { text: '角色：' + roleText(m) }),
        TCV.h('li', { text: '状态：' + (m.status_label || m.status || '—') }),
        TCV.h('li', { text: '当前任务：' + (m.current_task ? m.current_task.title : '暂无进行中任务') })
      ]));
    }
    body.appendChild(descBlk);

    /* 2. 执行状态 */
    var stateBlk = TCV.h('div', { class: 'mbody-block' }, [
      TCV.h('div', { class: 'blk-h' }, [TCV.h('span', { text: '执行状态' })]),
      TCV.kv('成员状态', m.status_label || m.status || '—'),
      TCV.kv('执行状态', m.execution_label || '未上报', { dim: !m.execution_label }),
      TCV.kv('当前任务', m.current_task ? m.current_task.title : '暂无进行中任务', {
        dim: !m.current_task,
        title: m.current_task ? (m.current_task.title + '（' + (m.current_task.status_label || m.current_task.status) + '）') : ''
      }),
      TCV.kv('在办任务', String(m.active_task_count || 0)),
      TCV.kv('模式', m.mode || '—'),
      m.cli_agent ? TCV.kv('外部 CLI', m.cli_agent) : null
    ]);
    body.appendChild(stateBlk);

    /* 3. 个人任务进度 */
    var tp = m.task_progress || {};
    var progBlk = TCV.h('div', { class: 'mbody-block' }, [
      TCV.h('div', { class: 'blk-h' }, [
        TCV.h('span', { text: '任务进度' }),
        TCV.h('span', { style: 'margin-left:auto', text: TCV.fmtPct(tp.percent) })
      ]),
      TCV.miniBar(tp.percent),
      TCV.h('div', { class: 'tc-counts', style: 'margin-top:4px' },
        TCV.progressFields(tp).map(function (f) {
          return TCV.h('span', { text: f[0] + ' ' + f[1] });
        }).concat([TCV.h('span', { text: '共 ' + (tp.total || 0) })])
      )
    ]);
    body.appendChild(progBlk);

    /* 4. 标识 */
    var idBlk = TCV.h('div', { class: 'mbody-block' }, [
      TCV.h('div', { class: 'blk-h' }, [TCV.h('span', { text: '标识' })]),
      TCV.kv('成员 ID', m.member_name || '—'),
      TCV.kv('角色原值', m.role || '—'),
      TCV.kv('分区', isCaptain(m) ? '队长' : '成员'),
      m.unknown_enum ? TCV.kv('枚举', '含平台未识别的值（已原样显示）', { dim: true }) : null
    ]);
    body.appendChild(idBlk);

    return body;
  }

  function toggleMember(name) {
    var list = (TCV.State.data.expanded_member_ids || []).slice();
    var i = list.indexOf(name);
    if (i >= 0) list.splice(i, 1);
    else list.push(name);
    TCV.State.patch({ expanded_member_ids: list.slice(-200) });
    renderAll();
  }

  function renderPager() {
    var pager = els.pager;
    pager.innerHTML = '';
    if (!snap || !snap.members || !snap.members.length) { pager.hidden = true; return; }
    if ((searchTerm || '').trim()) { pager.hidden = true; return; }

    var others = snap.members.filter(function (m) { return !isCaptain(m); }).length;
    var pages = Math.max(1, Math.ceil(others / PAGE_SIZE));
    if (pages <= 1) { pager.hidden = true; return; }

    var page = Math.min(Math.max(1, TCV.State.data.member_page || 1), pages);
    pager.hidden = false;

    var prev = TCV.h('button', { type: 'button', text: '‹ 上一页' });
    var next = TCV.h('button', { type: 'button', text: '下一页 ›' });
    prev.disabled = page <= 1;
    next.disabled = page >= pages;
    prev.addEventListener('click', function () { gotoPage(page - 1); });
    next.addEventListener('click', function () { gotoPage(page + 1); });

    pager.appendChild(prev);
    pager.appendChild(TCV.h('span', { text: '第 ' + page + '/' + pages + ' 页 · 共 ' + others + ' 位成员' }));
    pager.appendChild(next);
  }

  function gotoPage(p) {
    TCV.State.patch({ member_page: p });
    renderAll();
    var lists = els.lists;
    if (lists && lists.scrollIntoView) {
      try { lists.scrollIntoView({ block: 'nearest' }); } catch (e) { /* ignore */ }
    }
  }

})(window);

/* ============================================================
 * 团队协作可视化 · 共享前端核心
 * 契约：contract-snapshot-api.md v1.1（字段/端点与 v1.0 一致）
 *
 * 设计约束（硬）：
 *  - iframe 为 opaque origin：禁用 localStorage / sessionStorage / cookie
 *  - 视图状态只走服务端 /ui-state，一次挂载 = 全量拉快照 + 拉视图状态 → 回填
 *  - 团队数据只走 HTTP JSON，不写任何数据
 *  - 不依赖构建步骤，经典脚本，全局命名空间 TCV
 * ============================================================ */
(function (global) {
  'use strict';

  var TCV = (global.TCV = global.TCV || {});
  TCV.SCHEMA_MAJOR = '1';

  /* ---------------------------------------------------------- 基础工具 */
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function num(v, dflt) {
    var n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? n : (typeof dflt === 'number' ? dflt : 0);
  }

  TCV.clone = clone;
  TCV.sleep = sleep;
  TCV.clamp = clamp;
  TCV.num = num;

  /** 极简 hyperscript：TCV.h('div', {class:'x', text:'y'}, [child]) */
  TCV.h = function (tag, attrs, kids) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'text') { el.textContent = String(v); return; }
        if (k === 'class') { el.className = v; return; }
        if (k === 'html') { el.innerHTML = v; return; }
        if (k === 'dataset') {
          Object.keys(v).forEach(function (dk) { el.dataset[dk] = v[dk]; });
          return;
        }
        if (k.indexOf('on') === 0 && typeof v === 'function') {
          el.addEventListener(k.slice(2), v);
          return;
        }
        el.setAttribute(k, v === true ? '' : String(v));
      });
    }
    (kids || []).forEach(function (k) {
      if (k === null || k === undefined || k === false) return;
      el.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
    });
    return el;
  };

  function debounce(fn, wait) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      if (t) clearTimeout(t);
      t = setTimeout(function () { t = null; fn.apply(self, args); }, wait);
    };
  }
  TCV.debounce = debounce;

  /* ---------------------------------------------------------- sid / scope */
  TCV.readSid = function () {
    try {
      var v = new URLSearchParams(global.location.search).get('sid');
      return v && v.trim() ? v.trim() : '';
    } catch (e) { return ''; }
  };
  TCV.SID = TCV.readSid();
  TCV.scopeKind = TCV.SID ? 'session' : 'global';

  /** 组 URL：自动带 sid（缺省时按 global 作用域，不带参数） */
  TCV.endpoint = function (path, params) {
    var p = new URLSearchParams();
    if (TCV.SID) p.set('sid', TCV.SID);
    if (params) {
      Object.keys(params).forEach(function (k) {
        var v = params[k];
        if (v === null || v === undefined) return;
        p.set(k, String(v));
      });
    }
    var q = p.toString();
    return q ? path + '?' + q : path;
  };

  /* ---------------------------------------------------------- HTTP */
  function request(method, url, body, signal) {
    var opt = { method: method, cache: 'no-store', headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    } else {
      opt.headers['Accept'] = 'application/json';
    }
    if (signal) opt.signal = signal;
    return fetch(url, opt).then(function (res) {
      return res.text().then(function (txt) {
        var payload = null;
        if (txt) { try { payload = JSON.parse(txt); } catch (e) { payload = null; } }
        if (!res.ok) {
          var err = new Error('HTTP ' + res.status);
          err.status = res.status;
          err.payload = payload;
          throw err;
        }
        return payload;
      });
    });
  }
  TCV.getJSON = function (url, signal) { return request('GET', url, undefined, signal); };
  TCV.patchJSON = function (url, body, signal) { return request('PATCH', url, body, signal); };

  /**
   * 写视图状态：**只走 PATCH**（服务端按 sid 加锁原子合并，契约 v1.4 / §5.3.2 W1–W4）。
   *
   * 刻意**不保留**「404/405/501 → GET→merge→PUT」的 RMW 回退分支：
   * RMW 在两个页签并发时必然静默复现 lost update（正是 BUGFIXES B3 修掉的那个 bug），
   * 而且退化过程不报错、界面全绿。首版后端自 v1.9 起 PATCH 必然存在，
   * 这条回退收益为零、风险是「静默复发」——故删除，任何非 2xx 一律由 saveErrorText 显式提示。
   */
  TCV.writeViewState = function (patch) {
    var body = { schema_version: '1.1', sid: TCV.SID, state: patch };
    return TCV.patchJSON(TCV.endpoint('/ui-state'), body).then(function (res) {
      return { ok: true, state: (res && res.state) || null, res: res };
    });
  };

  /* ---------------------------------------------------------- 格式化 */
  function pad2(n) { return n < 10 ? '0' + n : String(n); }
  var WEEK = ['日', '一', '二', '三', '四', '五', '六'];

  TCV.fmtTime = function (ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms <= 0) return '—';
    var d = new Date(ms);
    return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
      pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  };
  TCV.fmtTimeFull = function (ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms <= 0) return '—';
    var d = new Date(ms);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
      pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  };
  TCV.fmtWeek = function (ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms <= 0) return '';
    return '周' + WEEK[new Date(ms).getDay()];
  };
  /** percent 为 null 时显示「—」（契约 §2.6：不得显示 0%） */
  TCV.fmtPct = function (p) {
    if (p === null || p === undefined) return '—';
    var n = Number(p);
    if (!isFinite(n)) return '—';
    return (Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(1)) + '%';
  };
  TCV.fmtAgo = function (ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms <= 0) return '—';
    var diff = Date.now() - ms;
    if (diff < 45000) return '刚刚';
    if (diff < 3600000) return Math.round(diff / 60000) + ' 分钟前';
    if (diff < 86400000) return Math.round(diff / 3600000) + ' 小时前';
    return Math.round(diff / 86400000) + ' 天前';
  };
  /** 进度块 → 一行紧凑文案（窄面板友好） */
  TCV.progressLine = function (pb) {
    if (!pb) return '暂无任务';
    var done = pb.completed || 0;
    var total = pb.total || 0;
    var s = '完成 ' + done + '/' + total;
    if (pb.cancelled) s += '（取消 ' + pb.cancelled + '）';
    return s + ' · ' + TCV.fmtPct(pb.percent);
  };
  TCV.progressFields = function (pb) {
    if (!pb) return [];
    return [
      ['进行中', pb.in_progress || 0],
      ['待验收', pb.in_review || 0],
      ['阻塞', pb.blocked || 0],
      ['待开始', pb.pending || 0],
      ['已完成', pb.completed || 0],
      ['已取消', pb.cancelled || 0]
    ];
  };

  /* ---------------------------------------------------------- 视图状态 */
  var DEFAULT_STATE = {
    active_tab: 'team-info',
    expanded_member_ids: [],
    member_page: 1,
    selected_task_id: null,
    detail_open: false,
    search: { team_info: '', task_graph: '' },
    status_filter: [],
    canvas: { layout_mode: 'horizontal', pan: { x: 0, y: 0 }, zoom: 1 },
    theme: 'system'
  };
  TCV.DEFAULT_STATE = DEFAULT_STATE;

  var ALLOWED_STATUS = ['pending', 'blocked', 'planning', 'in_progress', 'in_review', 'completed', 'cancelled'];

  /** 本地也做一次白名单/边界收敛，避免把 400 打给后端 */
  function sanitizeState(s) {
    var out = clone(DEFAULT_STATE);
    if (!s || typeof s !== 'object') return out;

    if (s.active_tab === 'team-info' || s.active_tab === 'task-graph') out.active_tab = s.active_tab;

    if (Array.isArray(s.expanded_member_ids)) {
      out.expanded_member_ids = s.expanded_member_ids
        .filter(function (x) { return typeof x === 'string' && x.length > 0 && x.length <= 200; })
        .slice(0, 200);
    }
    out.member_page = Math.max(1, Math.floor(num(s.member_page, 1)));

    out.selected_task_id = (typeof s.selected_task_id === 'string' && s.selected_task_id) ? s.selected_task_id.slice(0, 200) : null;
    out.detail_open = !!s.detail_open;

    if (s.search && typeof s.search === 'object') {
      out.search = {
        team_info: typeof s.search.team_info === 'string' ? s.search.team_info.slice(0, 120) : '',
        task_graph: typeof s.search.task_graph === 'string' ? s.search.task_graph.slice(0, 120) : ''
      };
    }
    if (Array.isArray(s.status_filter)) {
      out.status_filter = s.status_filter
        .filter(function (v, i, arr) { return ALLOWED_STATUS.indexOf(v) >= 0 && arr.indexOf(v) === i; })
        .slice(0, 7);
    }
    if (s.canvas && typeof s.canvas === 'object') {
      var c = s.canvas;
      out.canvas = {
        layout_mode: c.layout_mode === 'vertical' ? 'vertical' : 'horizontal',
        pan: { x: num(c.pan && c.pan.x, 0), y: num(c.pan && c.pan.y, 0) },
        zoom: clamp(num(c.zoom, 1), 0.2, 4)
      };
    }
    if (s.theme === 'light' || s.theme === 'dark' || s.theme === 'system') out.theme = s.theme;
    return out;
  }
  TCV.sanitizeState = sanitizeState;

  /**
   * 只对 search / canvas 这类「两页签共享、各写各的子键」做一层深合并。
   * 服务端 PATCH 本来就是一级深合并，本地也必须一致，否则本页会用挂载时读到的旧值
   * 覆盖另一页刚写的新值（lost update）。
   */
  function mergeLevels(base, patch) {
    var out = {};
    if (base && typeof base === 'object') {
      Object.keys(base).forEach(function (k) { out[k] = base[k]; });
    }
    Object.keys(patch || {}).forEach(function (k) { out[k] = patch[k]; });
    return out;
  }

  function isSubKeyed(k) { return k === 'search' || k === 'canvas'; }

  var State = {
    data: clone(DEFAULT_STATE),
    raw: null,
    updatedAt: null,
    loadError: null,
    _pending: null,
    _timer: null,
    _chain: Promise.resolve(),
    saveFails: 0,

    /** 挂载时拉一次视图状态（契约 v1.7：GET 恒返回完整 9 键；empty=true 表示从未写过） */
    load: function () {
      return TCV.getJSON(TCV.endpoint('/ui-state')).then(function (r) {
        State.raw = (r && r.state) || null;
        State.empty = !!(r && r.empty);
        State.updatedAt = (r && r.updated_at) || null;
        State.data = sanitizeState(State.raw);
        State.loadError = null;
        return State.data;
      }).catch(function (e) {
        State.raw = null;
        State.empty = true;
        State.data = clone(DEFAULT_STATE);
        State.loadError = e;
        return State.data;
      });
    },

    /**
     * 首屏是否需要 fit-all。
     * 语义：**有存值就严格照 canvas 还原，绝不二次 fit**（这点已按 U3 收紧）；
     * 只有「肯定没存过」才 fit，判据取并集：
     *   - `empty===true`（该 sid 从未写过任何键）；
     *   - `canvas` 恰好等于缺省（zoom=1、pan=(0,0)、layout=horizontal）。
     * 第二条不是「拍脑袋」：empty 是**整条 sid 级**的标记，团队信息页写过搜索/分页/主题就会把它翻成 false，
     * 而此时 canvas 根本没被写过——只认 empty 会出现「切到任务关系页永远停在 100%、节点出界」的真 bug（实测 5/8 节点出界）。
     * 同时本产品没有「重置视角」入口，「显示全图」写入的是 fit 结果（zoom/pan 均是计算值），
     * 用户交互不可能精确落回 zoom=1 且 pan=(0,0)，所以 canvas==缺省 在实践中约等于「从未写过」。
     */
    hasStoredCanvas: function () {
      if (State.empty) return false;
      var c = State.raw && State.raw.canvas;
      if (!c || typeof c !== 'object') return false;
      var z = typeof c.zoom === 'number' ? c.zoom : 1;
      var px = (c.pan && typeof c.pan.x === 'number') ? c.pan.x : 0;
      var py = (c.pan && typeof c.pan.y === 'number') ? c.pan.y : 0;
      return Math.abs(z - 1) > 0.001 || Math.abs(px) > 0.5 || Math.abs(py) > 0.5;
    },

    /** 本地立即生效 + 防抖落盘 */
    patch: function (patch, delay) {
      if (!State._pending) State._pending = {};
      Object.keys(patch).forEach(function (k) {
        if (isSubKeyed(k) && patch[k] && typeof patch[k] === 'object' && !Array.isArray(patch[k])) {
          State._pending[k] = mergeLevels(State._pending[k], patch[k]);
        } else {
          State._pending[k] = patch[k];
        }
      });
      var merged = clone(State.data);
      Object.keys(patch).forEach(function (k) {
        if (isSubKeyed(k) && patch[k] && typeof patch[k] === 'object' && !Array.isArray(patch[k])) {
          merged[k] = mergeLevels(merged[k], patch[k]);
        } else {
          merged[k] = patch[k];
        }
      });
      State.data = sanitizeState(merged);
      if (State._timer) clearTimeout(State._timer);
      State._timer = setTimeout(function () {
        State._timer = null;
        State.flush();
      }, typeof delay === 'number' ? delay : 320);
      return State.data;
    },

    /**
     * 落盘：只走 PATCH 增量（服务端按 sid 加锁原子合并，两页并发安全）；无退化分支。
     * 每次只提交「本页改过的键」，不提交整份 state。
     */
    flush: function () {
      State._chain = State._chain.then(function () {
        if (!State._pending) return null;
        var patch = State._pending;
        State._pending = null;
        return TCV.writeViewState(patch).then(function (res) {
          // 服务端回合并后的完整 state 时以它为准（两页视图收敛），但仍叠加本次在途的本地改动
          var base = (res && res.state && typeof res.state === 'object') ? res.state : null;
          var merged = base ? sanitizeState(base) : clone(State.data);
          if (State._pending) {
            Object.keys(State._pending).forEach(function (k) {
              if (isSubKeyed(k) && State._pending[k] && typeof State._pending[k] === 'object') {
                merged[k] = mergeLevels(merged[k], State._pending[k]);
              } else {
                merged[k] = State._pending[k];
              }
            });
          }
          State.data = sanitizeState(merged);
          if (base) State.raw = base;
          State.saveFails = 0;
        });
      }).catch(function (e) {
        State.saveFails += 1;
        State.lastSaveError = { status: e && e.status, payload: (e && e.payload) || null };
        // 不静默：白名单/超限/路径错误等缺陷必须留痕（已无任何退化分支）
        try {
          console.warn('[tcv] 视图状态保存失败（不影响团队数据展示）：',
            (e && e.status) || '',
            (e && e.payload && ((e.payload.code || '') + ' ' + (e.payload.message || ''))) || '');
        } catch (_) { /* ignore */ }
        return null;
      });
      return State._chain;
    },

    /** 视图状态写入失败的可读提示（任何非 2xx 都提示；不静默、无退化分支） */
    saveErrorText: function () {
      var e = State.lastSaveError;
      if (!e) return '';
      var code = (e.payload && e.payload.code) || ('HTTP ' + (e.status || '?'));
      // 注意：契约口径的超限是 400 STATE_TOO_LARGE（请求体超 MAX_BODY_BYTES 则 400 BAD_BODY），
      // 当前实现不产生 413，这里只做防御性兜底。
      if (code === 'STATE_TOO_LARGE') return '视图状态过大未保存（刷新后不回填）';
      if (code === 'STATE_KEY_NOT_ALLOWED') return '视图状态含非法键未保存（' + code + '）';
      return '视图状态未保存（' + code + '）';
    },
  };
  TCV.State = State;

  /* ---------------------------------------------------------- 主题 */
  var THEME_ORDER = ['system', 'light', 'dark'];
  var THEME_TEXT = { system: '跟随系统', light: '浅色', dark: '深色' };

  TCV.Theme = {
    current: 'system',
    apply: function (t) {
      TCV.Theme.current = (t === 'light' || t === 'dark') ? t : 'system';
      document.documentElement.setAttribute('data-theme', TCV.Theme.current);
      var btn = document.getElementById('theme-btn');
      if (btn) {
        btn.textContent = TCV.Theme.current === 'system' ? '系统' : (TCV.Theme.current === 'light' ? '浅色' : '深色');
        btn.title = '主题：' + THEME_TEXT[TCV.Theme.current] + '（点击切换 系统→浅色→深色）';
        btn.setAttribute('data-theme-state', TCV.Theme.current);
      }
    },
    cycle: function () {
      var next = THEME_ORDER[(THEME_ORDER.indexOf(TCV.Theme.current) + 1) % THEME_ORDER.length];
      TCV.Theme.apply(next);
      State.patch({ theme: next });
    },
    init: function () {
      TCV.Theme.apply(State.data.theme || 'system');
      var btn = document.getElementById('theme-btn');
      if (btn) btn.addEventListener('click', TCV.Theme.cycle);
    }
  };

  /* ---------------------------------------------------------- 状态条 */
  TCV.Status = {
    el: null,
    init: function (id) {
      TCV.Status.el = document.getElementById(id || 'statusbar');
      return TCV.Status.el;
    },
    set: function (text, kind) {
      var el = TCV.Status.el;
      if (!el) return;
      if (!text) {
        el.hidden = true;
        el.textContent = '';
        el.className = 'statusbar';
        el.removeAttribute('title');
        return;
      }
      el.hidden = false;
      el.className = 'statusbar' + (kind ? ' sb-' + kind : '');
      el.textContent = text;
      el.title = text;
    }
  };

  /**
   * 依据快照生成状态条文案（多处复用）
   * 优先级：连接中断 > stale > 作用域提示 > 数据过期 > 正常
   */
  TCV.snapshotStatus = function (s, connLost) {
    if (connLost) return { text: '连接中断，已保留上次数据，正在重试…', kind: 'bad' };
    if (!s) return { text: '尚未取到数据…', kind: '' };
    if (s.stale) {
      var reasonMap = {
        ops_error: '读取团队数据失败',
        no_session_map: '未定位到该会话的团队',
        cold_start: '冷启动'
      };
      var reason = s.stale_reason ? (reasonMap[s.stale_reason] || s.stale_reason) : '后端暂未读到最新数据';
      return { text: '数据未刷新（' + reason + '），展示 ' + TCV.fmtTime(s.generated_at) + ' 的快照', kind: 'warn' };
    }
    if (s.scope && s.scope.kind === 'global') {
      return { text: '会话标识缺失：视图状态为全局，可能无法按会话恢复', kind: 'warn' };
    }
    if (typeof s.generated_at === 'number' && Date.now() - s.generated_at > 600000) {
      return { text: '数据可能已过期（' + TCV.fmtAgo(s.generated_at) + '更新，有变化时会自动刷新）', kind: 'warn' };
    }
    return { text: '' };
  };

  /** schema 主版本校验（契约 §8：不匹配显示提示而不是崩） */
  TCV.checkSchema = function (s) {
    if (!s) return { ok: false, msg: '未收到快照数据' };
    var v = s.schema_version;
    if (!v) return { ok: false, msg: '快照缺少 schema_version 字段' };
    if (String(v).split('.')[0] !== TCV.SCHEMA_MAJOR) {
      return { ok: false, msg: '插件版本不匹配（快照 schema ' + v + '，前端要求 ' + TCV.SCHEMA_MAJOR + '.x），请重载插件' };
    }
    return { ok: true };
  };

  /* ---------------------------------------------------------- 长轮询 */
  /**
   * handlers: { onSnapshot(data), onOffline(fails), onOnline(), onPause(), onResume() }
   * 语义（契约 §6④）：unchanged → 立即再发；失败 → 3s 退避，连续 3 次报 onOffline；
   * 页面隐藏 → 停轮询（abort 在途请求），恢复可见 → 拿全量。
   */
  TCV.createPoller = function (handlers) {
    var o = handlers || {};
    var st = { stopped: false, paused: false, fails: 0, rev: null, ac: null, hidden: false };

    function abortCurrent() {
      if (st.ac) {
        try { st.ac.abort(); } catch (e) { /* ignore */ }
        st.ac = null;
      }
    }

    function qs(rev) {
      var p = new URLSearchParams();
      if (TCV.SID) p.set('sid', TCV.SID);
      if (rev !== null && rev !== undefined) p.set('since', String(rev));
      p.set('wait', '25');
      return p.toString();
    }

    function onVisibility() {
      var hidden = document.visibilityState === 'hidden';
      if (hidden === st.hidden) return;
      st.hidden = hidden;
      if (hidden) {
        st.paused = true;
        abortCurrent();
        if (o.onPause) o.onPause();
      } else {
        st.paused = false;
        st.rev = null; // 恢复可见先拿全量，避免 25s 空等
        if (o.onResume) o.onResume();
      }
    }
    document.addEventListener('visibilitychange', onVisibility);

    function loop() {
      if (st.stopped) return;
      if (st.paused) { setTimeout(loop, 250); return; }

      st.ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      TCV.getJSON('/snapshot?' + qs(st.rev), st.ac ? st.ac.signal : undefined).then(function (data) {
        st.ac = null;
        if (st.stopped) return;
        if (data && data.unchanged === true) {
          if (st.fails >= 3 && o.onOnline) o.onOnline();
          st.fails = 0;
          // 契约 v1.6：unchanged 也带 stale / served_at / generated_at，
          // 必须据此刷新降级徽标与「数据时刻」，否则后端进 stale 期间页面完全无感
          if (o.onUnchanged) o.onUnchanged(data);
          loop();
          return;
        }
        if (data && typeof data.revision === 'number') st.rev = data.revision;
        if (st.fails >= 3 && o.onOnline) o.onOnline();
        st.fails = 0;
        if (o.onSnapshot) o.onSnapshot(data);
        loop();
      }).catch(function (e) {
        st.ac = null;
        if (st.stopped || st.paused) return;
        if (e && (e.name === 'AbortError' || e.status === 0)) return;
        st.fails += 1;
        if (st.fails >= 3 && o.onOffline) o.onOffline(st.fails);
        setTimeout(loop, 3000);
      });
    }
    loop();

    return {
      setRevision: function (r) { st.rev = r; },
      getFails: function () { return st.fails; },
      stop: function () {
        st.stopped = true;
        abortCurrent();
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  };

  /* ---------------------------------------------------------- 通用小组件 */
  TCV.dot = function (status) {
    return TCV.h('i', { class: 'dot dot-' + (status || 'unknown'), 'aria-hidden': 'true' });
  };
  TCV.statusPill = function (status, label) {
    return TCV.h('span', { class: 'pill ' + 'st-' + (status || 'unknown'), text: label || status || '—' });
  };
  TCV.kv = function (k, v, opts) {
    opts = opts || {};
    var val = TCV.h('span', { class: 'kv-v' + (opts.dim ? ' dim' : ''), text: v === null || v === undefined || v === '' ? '—' : String(v) });
    if (opts.title) val.title = opts.title;
    return TCV.h('div', { class: 'kv' }, [TCV.h('span', { class: 'kv-k', text: k }), val]);
  };

  /** 迷你进度条（percent 为 null 时不画填充，仅显示「—」） */
  TCV.miniBar = function (percent) {
    var wrap = TCV.h('div', { class: 'minibar' });
    if (percent !== null && percent !== undefined && isFinite(Number(percent))) {
      var p = clamp(Number(percent), 0, 100);
      wrap.appendChild(TCV.h('i', { style: 'width:' + p.toFixed(1) + '%' }));
    } else {
      wrap.classList.add('is-na');
    }
    return wrap;
  };

  /** 空态/致命态面板 */
  TCV.emptyBlock = function (title, desc) {
    return TCV.h('div', { class: 'empty' }, [
      TCV.h('div', { class: 'empty-t', text: title }),
      TCV.h('div', { class: 'empty-d', text: desc })
    ]);
  };

})(window);

/*!
 * WorkSwarm 主题 SDK 2.0（兼容 1.x 页面写法）
 * 主题包中 html 背景与开场在 WorkSwarm 的媒体容器（sandbox="allow-scripts" 的 iframe）里运行。
 * 页面接口与 1.x 相同：WorkSwarmThemeSDK.onStart / onVisibility / ready / complete / context / reducedMotion。
 * 与宿主之间使用 V2 媒体容器协议：令牌取自 location.hash，事件为 ready、heartbeat、complete、error。
 * 宿主不知道的开场信息（研究员代号、主题、声音、上限）由插件写在页面地址的查询参数里。
 */
(function () {
  'use strict';
  var TYPE = 'webui.surface';
  var VERSION = 1;
  var hash = new URLSearchParams(location.hash.slice(1));
  var query = new URLSearchParams(location.search);
  var token = hash.get('webui-surface-token') || '';
  var role = query.get('role') === 'boot' ? 'boot' : 'background';
  var handlers = { start: [], visibility: [] };
  var context = null;
  var startedAt = 0;
  var finished = false;
  var heartbeat = null;
  var sound = null;

  function report(error) { if (window.console) console.error(error); }
  function clamp(value, lo, hi) { return Math.min(hi, Math.max(lo, value)); }
  function number(name, fallback, lo, hi) {
    var raw = query.get(name);
    var value = raw === null || raw === '' ? NaN : Number(raw);
    return isFinite(value) ? clamp(value, lo, hi) : fallback;
  }
  function hashCode(text) {
    var h = 0;
    for (var i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) >>> 0;
    return h;
  }

  function send(event, detail) {
    var message = { type: TYPE, version: VERSION, token: token, event: event };
    if (detail) message.detail = detail;
    try { window.parent.postMessage(message, '*'); } catch (error) { report(error); }
  }

  function fire(name, value) {
    handlers[name].slice().forEach(function (fn) {
      try { fn(value); } catch (error) { report(error); }
    });
  }

  function buildContext(host) {
    var operator = query.get('operator') || '研究员';
    return {
      role: role,
      operator: operator,
      operator_code: query.get('operator_code') || String(1000 + (hashCode(operator) % 9000)),
      locale: host.locale || navigator.language || 'zh-CN',
      theme: { id: query.get('theme_id') || '', name: query.get('theme_name') || '', version: query.get('theme_version') || '' },
      engine: query.get('engine') || '',
      reduced_motion: host.reducedMotion === true || query.get('reduced') === '1',
      visible: host.visible !== false,
      host_ready: true,
      started_at: startedAt
    };
  }

  // ---------------------------------------------------------------- 开场声音

  function fader(media) {
    var timer = null;
    return function fade(target, ms, done) {
      if (timer) clearInterval(timer);
      var from = media.volume;
      var steps = Math.max(1, Math.round(ms / 40));
      var i = 0;
      timer = setInterval(function () {
        i += 1;
        media.volume = clamp(from + (target - from) * (i / steps), 0, 1);
        if (i >= steps) {
          clearInterval(timer);
          timer = null;
          if (done) done();
        }
      }, 40);
    };
  }

  function createSound() {
    var kind = query.get('audio');
    var volume = number('volume', 0.6, 0, 1);
    var fadeIn = number('fade_in', 300, 0, 5000);
    var fadeOut = number('fade_out', 900, 0, 5000);
    if (!kind || volume <= 0) return null;
    if (kind === 'synth') {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      var library = window.WorkSwarmLabSynth;
      if (!Ctor || !library) return null;
      var ac;
      try { ac = new Ctor(); } catch (error) { report(error); return null; }
      if (ac.state === 'suspended' && ac.resume) ac.resume().catch(function () {});
      var master = ac.createGain();
      master.gain.value = 0;
      var limiter = ac.createDynamicsCompressor();
      limiter.threshold.value = -6;
      limiter.knee.value = 6;
      limiter.ratio.value = 8;
      limiter.attack.value = 0.003;
      limiter.release.value = 0.25;
      master.connect(limiter);
      limiter.connect(ac.destination);
      var synth = library.create(ac, master);
      var stopped = false;
      return {
        sfx: synth,
        fadeOut: fadeOut,
        start: function () {
          var t = ac.currentTime;
          synth.ambient(t);
          master.gain.setValueAtTime(0, t);
          master.gain.linearRampToValueAtTime(volume, t + fadeIn / 1000);
        },
        stop: function () {
          if (stopped) return;
          stopped = true;
          var t = ac.currentTime;
          var fade = Math.max(0.05, fadeOut / 1000);
          master.gain.cancelScheduledValues(t);
          master.gain.setValueAtTime(master.gain.value, t);
          master.gain.linearRampToValueAtTime(0, t + fade);
          setTimeout(function () { if (ac.close) ac.close().catch(function () {}); }, fade * 1000 + 150);
        }
      };
    }
    if (kind !== 'file' || !query.get('audio_src')) return null;
    var media = new Audio();
    var fade = fader(media);
    var ended = false;
    media.preload = 'auto';
    media.loop = query.get('loop') === '1';
    media.volume = 0;
    media.src = query.get('audio_src');
    return {
      sfx: null,
      fadeOut: fadeOut,
      start: function () {
        var promise = media.play();
        if (promise && promise.catch) promise.catch(function () { /* Autoplay refused: the splash plays silently. */ });
        fade(volume, fadeIn);
      },
      stop: function () {
        if (ended) return;
        ended = true;
        fade(0, fadeOut, function () { media.pause(); media.removeAttribute('src'); });
      }
    };
  }

  function beginBoot() {
    if (role !== 'boot') return;
    // The host ends a splash whose page stops reporting; keep it alive while the page runs.
    heartbeat = setInterval(function () { if (!finished) send('heartbeat'); }, 1000);
    var maximum = number('max', 0, 0, 60000);
    if (maximum > 0) setTimeout(function () { complete(); }, maximum);
    if (sound) {
      try { sound.start(); } catch (error) { report(error); }
    }
  }

  function complete() {
    if (finished) return;
    finished = true;
    if (heartbeat) clearInterval(heartbeat);
    if (!sound) { send('complete'); return; }
    // Let the music fade out before the host closes the splash.
    try { sound.stop(); } catch (error) { report(error); }
    setTimeout(function () { send('complete'); }, Math.min(sound.fadeOut, 1200));
  }

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || data.type !== TYPE || data.version !== VERSION) return;
    if (data.event === 'start') {
      var first = !context;
      if (!startedAt) startedAt = Date.now();
      context = buildContext(data.context || {});
      if (first) beginBoot();
      fire('start', context);
    } else if (data.event === 'context') {
      var host = data.context || {};
      if (context) context = buildContext(host);
      fire('visibility', host.visible !== false);
    }
  });

  if (role === 'boot') sound = createSound();

  window.WorkSwarmThemeSDK = Object.freeze({
    version: '2.0.0',
    /** 宿主开始播放后回调，参数含 operator、theme、reduced_motion、role、started_at 等；若已开始则立即回调。 */
    onStart: function (fn) {
      handlers.start.push(fn);
      if (context) {
        try { fn(context); } catch (error) { report(error); }
      }
    },
    /** 主窗口可见性变化时回调，背景动画可据此暂停。 */
    onVisibility: function (fn) { handlers.visibility.push(fn); },
    /** 页面资源就绪。SDK 在 DOMContentLoaded 时已自动发送。 */
    ready: function () { send('ready'); },
    /** 开场播放完毕；声音淡出后通知宿主结束。背景页调用无效。 */
    complete: function () { if (role === 'boot') complete(); },
    /** 页面无法显示。 */
    error: function (code, message) {
      finished = true;
      if (heartbeat) clearInterval(heartbeat);
      if (sound) { try { sound.stop(); } catch (error) { report(error); } }
      send('error', { code: String(code || 'ERROR').slice(0, 64), message: String(message || '').slice(0, 200) });
    },
    get context() { return context; },
    /** 开场声音（合成声景时含 sfx），没有声音时为 null。 */
    get sound() { return sound; },
    reducedMotion: function () {
      if (context && typeof context.reduced_motion === 'boolean') return context.reduced_motion;
      return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    },
    /** 减少动态时是否压缩时长（主题 reduced_motion 为 shorten）。 */
    shortened: function () { return query.get('reduced') === '1'; }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { send('ready'); });
  } else {
    send('ready');
  }
})();

/*!
 * WorkSwarm 主题背景 2.0：科研终端播放页。
 * 渲染器（LabTerminal 及其样式、标志、晶格与扫描图形）与工具函数原样移植自主题引擎 1.3.2 运行时；
 * 末尾的驱动部分为 V2 新写：从兼容 SDK 取得开场上下文，在本沙箱页面中播放时间线，播完通知宿主。
 * 时间线由转换步骤写入同目录的 sequence.js（window.WORKSWARM_SEQUENCE）。
 */
(function () {
  'use strict';
  var doc = document;
  var html = doc.documentElement;
  function noop() {}
  function report(error) { if (window.console) console.error(error); }
  // Assets come from the same sandboxed origin as this page, so they can load at once.
  function whenAssetsReady(fn) { fn(); }
  var SILENT = { ambient: noop, intro: noop, tick: noop, status: noop, phase: noop, meter: noop, row: noop, scan: noop, check: noop, result: noop, grant: noop };

  function clamp(value, lo, hi) { return Math.min(hi, Math.max(lo, value)); }
  function num(value, fallback) { return typeof value === 'number' && isFinite(value) ? value : fallback; }
  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function hashCode(text) {
    var h = 0;
    for (var i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) >>> 0;
    return h;
  }

  function parseColor(value) {
    if (typeof value !== 'string') return null;
    var v = value.trim();
    var m = /^#([0-9a-f]{3,8})$/i.exec(v);
    if (m) {
      var hex = m[1];
      if (hex.length === 3 || hex.length === 4) hex = hex.split('').map(function (c) { return c + c; }).join('');
      if (hex.length !== 6 && hex.length !== 8) return null;
      return {
        r: parseInt(hex.slice(0, 2), 16),
        g: parseInt(hex.slice(2, 4), 16),
        b: parseInt(hex.slice(4, 6), 16),
        a: hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1
      };
    }
    m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)$/i.exec(v);
    if (m) {
      var alpha = m[4] == null ? 1 : (m[4].slice(-1) === '%' ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
      return { r: +m[1], g: +m[2], b: +m[3], a: alpha };
    }
    return null;
  }

  function rgbList(c) { return Math.round(c.r) + ', ' + Math.round(c.g) + ', ' + Math.round(c.b); }
  function rgba(c, a) { return 'rgba(' + rgbList(c) + ', ' + a + ')'; }
  function mix(c, target, t) {
    return {
      r: c.r + (target.r - c.r) * t,
      g: c.g + (target.g - c.g) * t,
      b: c.b + (target.b - c.b) * t,
      a: 1
    };
  }
  var WHITE = { r: 255, g: 255, b: 255, a: 1 };
  var BLACK = { r: 0, g: 0, b: 0, a: 1 };
  function cssColor(c) { return 'rgb(' + rgbList(c) + ')'; }

  function el(tag, props, children) {
    var node = doc.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value == null || value === false) return;
        if (key === 'text') node.textContent = value;
        else if (key === 'className') node.className = value;
        else if (key === 'style') node.style.cssText = value;
        else node.setAttribute(key, value === true ? '' : value);
      });
    }
    (children || []).forEach(function (child) {
      if (child) node.appendChild(typeof child === 'string' ? doc.createTextNode(child) : child);
    });
    return node;
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, children) {
    var node = doc.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function attachTo(node) {
    (doc.body || html).appendChild(node);
  }

  function today() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function randomHex(len) {
    var out = '';
    var bytes = new Uint8Array(len);
    (window.crypto || window.msCrypto).getRandomValues(bytes);
    for (var i = 0; i < len; i += 1) out += (bytes[i] % 16).toString(16);
    return out.toUpperCase();
  }

  function template(text, ctx) {
    if (typeof text !== 'string') return '';
    return text.replace(/\{\{\s*([a-z_]+)\s*\}\}/g, function (all, name) {
      return Object.prototype.hasOwnProperty.call(ctx, name) ? String(ctx[name]) : all;
    });
  }

  function monogramOf(name) {
    var text = String(name || '').trim();
    if (!text) return 'R';
    if (/^[\x00-\x7f]+$/.test(text)) {
      var words = text.split(/\s+/).filter(Boolean);
      return (words.length > 1 ? words[0][0] + words[1][0] : text.slice(0, 2)).toUpperCase();
    }
    return Array.from(text)[0];
  }

  function Timers() {
    var ids = [];
    return {
      after: function (ms, fn) {
        var id = setTimeout(function () {
          try { fn(); } catch (e) { report(e); }
        }, Math.max(0, ms));
        ids.push(id);
        return id;
      },
      clear: function () { ids.forEach(clearTimeout); ids = []; }
    };
  }

  var LAB_CSS = [
    ':host{all:initial}',
    '*{box-sizing:border-box}',
    '.lt{--paper:#f2f4f3;--ink:#121614;--accent:#17b893;--accent2:#c9e84a;--muted:#7b8682;--console:#0e1413;--console-ink:#c6f1e3;--warn:#e3a13b;--danger:#e0554a;',
    '  --line:rgba(18,22,20,.12);--line-strong:rgba(18,22,20,.28);position:absolute;inset:0;overflow:hidden;background:var(--paper);color:var(--ink);',
    '  font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC","Hiragino Sans GB",system-ui,sans-serif;-webkit-font-smoothing:antialiased;user-select:none;cursor:default}',
    '.mono{font-family:"SF Mono","JetBrains Mono",Menlo,Consolas,"Liberation Mono",monospace;letter-spacing:.02em}',
    '.grid{position:absolute;inset:0;background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);background-size:28px 28px;opacity:.55;',
    '  -webkit-mask-image:radial-gradient(ellipse at 50% 45%,#000 30%,transparent 78%);mask-image:radial-gradient(ellipse at 50% 45%,#000 30%,transparent 78%)}',
    '.lattice{position:absolute;right:-6vw;bottom:-10vh;width:62vw;max-width:880px;opacity:.9;pointer-events:none}',
    '.lattice path{fill:none;stroke:var(--line-strong);stroke-width:1}',
    '.lattice .node{fill:var(--paper);stroke:var(--accent);stroke-width:1.4}',
    '.lattice .node.on{fill:var(--accent);animation:lt-pulse 2.4s ease-in-out infinite}',
    '.sweep{position:absolute;left:0;right:0;height:22vh;top:-22vh;background:linear-gradient(180deg,transparent,rgba(23,184,147,.07) 70%,rgba(23,184,147,.18));border-bottom:1px solid rgba(23,184,147,.35);animation:lt-sweep 3.6s cubic-bezier(.4,0,.2,1) .4s infinite;pointer-events:none}',
    '.marks i{position:absolute;width:18px;height:18px;border-color:var(--ink);border-style:solid;opacity:.55}',
    '.marks .tl{left:22px;top:22px;border-width:1.5px 0 0 1.5px}.marks .tr{right:22px;top:22px;border-width:1.5px 1.5px 0 0}',
    '.marks .bl{left:22px;bottom:22px;border-width:0 0 1.5px 1.5px}.marks .br{right:22px;bottom:22px;border-width:0 1.5px 1.5px 0}',
    '.hud{position:absolute;left:52px;right:52px;display:flex;justify-content:space-between;font-size:11px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}',
    '.hud.top{top:26px}.hud.bottom{bottom:26px}',
    '.hud span{display:flex;align-items:center;gap:8px}',
    '.hud .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(23,184,147,.18);animation:lt-blink 1.2s steps(2) infinite}',
    '.intro{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;transition:opacity .35s ease,transform .45s cubic-bezier(.16,1,.3,1)}',
    '.intro.out{opacity:0;transform:translateY(-14px) scale(.98)}',
    '.intro-mark{position:relative;width:132px;height:132px;display:grid;place-items:center}',
    '.intro-mark img,.intro-mark .fallback{width:84px;height:84px;animation:lt-rise .7s cubic-bezier(.16,1,.3,1) both}',
    '.intro-mark .ring{position:absolute;inset:0;animation:lt-spin 6s linear infinite}',
    '.intro-mark .ring circle{fill:none;stroke:var(--accent);stroke-width:1.2;stroke-dasharray:4 7}',
    '.intro-mark .ring .arc{stroke:var(--ink);stroke-width:2;stroke-dasharray:60 400;animation:lt-arc 1.4s cubic-bezier(.4,0,.2,1) infinite}',
    '.intro-title{font-size:30px;font-weight:600;letter-spacing:.32em;padding-left:.32em;animation:lt-fade .6s ease .25s both}',
    '.intro-sub{font-size:11px;letter-spacing:.42em;color:var(--muted);animation:lt-fade .6s ease .4s both}',
    '.intro-bar{width:280px;height:2px;background:var(--line);margin-top:10px;overflow:hidden;animation:lt-fade .4s ease .4s both}',
    '.intro-bar i{display:block;height:100%;width:100%;background:var(--ink);transform-origin:left;animation:lt-fill var(--intro-ms,1000ms) cubic-bezier(.65,0,.35,1) both}',
    '.dash{position:absolute;inset:64px 52px 64px;display:grid;grid-template-rows:auto 1fr auto;gap:18px;opacity:0;transform:translateY(18px);transition:opacity .45s ease,transform .6s cubic-bezier(.16,1,.3,1)}',
    '.dash.in{opacity:1;transform:none}',
    '.brand{display:flex;align-items:center;gap:16px;padding:0 0 16px;border-bottom:1px solid var(--line-strong);position:relative}',
    '.brand:after{content:"";position:absolute;left:0;bottom:-1px;width:120px;height:3px;background:var(--accent)}',
    '.brand-mark{width:44px;height:44px;flex:none}.brand-mark img,.brand-mark .fallback{width:100%;height:100%}',
    '.brand-text{display:flex;flex-direction:column;gap:3px;min-width:0}',
    '.brand-text b{font-size:20px;font-weight:600;letter-spacing:.18em;white-space:nowrap}',
    '.brand-text small{font-size:10.5px;letter-spacing:.32em;color:var(--muted);white-space:nowrap}',
    '.brand-side{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;gap:6px;font-size:11px;color:var(--muted);letter-spacing:.1em}',
    '.brand-tag{padding:3px 10px;border:1px solid var(--ink);color:var(--ink);font-size:11px;letter-spacing:.16em}',
    '.body{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(0,1fr);gap:18px;min-height:0}',
    '.console{background:var(--console);color:var(--console-ink);border-radius:4px;display:flex;flex-direction:column;min-height:0;overflow:hidden;box-shadow:0 18px 40px -24px rgba(0,0,0,.55)}',
    '.console-head{display:flex;align-items:center;gap:6px;padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.08);font-size:10.5px;color:rgba(198,241,227,.55);letter-spacing:.14em}',
    '.console-head i{width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,.18)}.console-head i:first-child{background:var(--accent)}',
    '.console-head span{margin-left:8px}',
    '.console-lines{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:flex-start;gap:5px;padding:12px 16px 4px;font-size:12.5px;line-height:1.55;overflow:hidden}',
    '.line{display:flex;gap:10px;align-items:baseline;white-space:nowrap;animation:lt-fade .18s ease both}',
    '.line .p{color:var(--accent);flex:none}.line .t{overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}',
    '.line .s{flex:none;font-size:11px;padding:0 6px;border:1px solid currentColor;border-radius:2px;color:var(--accent);opacity:0;transition:opacity .15s}',
    '.line .s.on{opacity:1}.line.warn .s,.line.warn .p{color:var(--warn)}.line.danger .s,.line.danger .p{color:var(--danger)}.line.muted{opacity:.6}.line.accent .t{color:#fff}',
    '.console .cursor{height:18px;margin:0 16px 12px;width:9px;background:var(--accent);animation:lt-blink 1s steps(2) infinite}',
    '.panel{border:1px solid var(--line-strong);background:rgba(255,255,255,.66);border-radius:4px;display:flex;flex-direction:column;min-height:0;position:relative;overflow:hidden}',
    '.panel-head{display:flex;justify-content:space-between;align-items:center;padding:11px 16px;border-bottom:1px solid var(--line);font-size:12px;letter-spacing:.16em}',
    '.panel-head .ph{color:var(--muted);font-size:10.5px}',
    '.panel-body{flex:1;min-height:0;padding:16px 18px;position:relative}',
    '.panel-body>.view{position:absolute;inset:16px 18px;animation:lt-fade .3s ease both}',
    '.meter{display:grid;grid-template-columns:96px 1fr 70px;gap:12px;align-items:center;margin-bottom:14px;font-size:12px}',
    '.meter .bar{height:6px;background:var(--line);overflow:hidden;border-radius:1px}',
    '.meter .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--ink),var(--accent));transform-origin:left;transform:scaleX(0);transition:transform .9s cubic-bezier(.16,1,.3,1)}',
    '.meter .v{text-align:right;color:var(--muted)}',
    '.arc-head{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);letter-spacing:.12em;margin-bottom:10px}',
    '.row{display:grid;grid-template-columns:118px 1fr 34px 64px;gap:10px;align-items:center;padding:8px 0;border-bottom:1px dashed var(--line);font-size:12.5px;opacity:0;transform:translateX(-6px);transition:opacity .2s,transform .3s}',
    '.row.on{opacity:1;transform:none}',
    '.row .c{font-size:11.5px;color:var(--muted)}.row .lv{font-size:11px;text-align:center;border:1px solid var(--line-strong);border-radius:2px}',
    '.row .st{font-size:11.5px;text-align:right;color:var(--muted)}.row .st.ok{color:var(--accent);font-weight:600}',
    '.row .fill{grid-column:1/-1;height:2px;background:var(--line);margin-top:6px;overflow:hidden}',
    '.row .fill i{display:block;height:100%;background:var(--accent);transform-origin:left;transform:scaleX(0);transition:transform .38s ease}',
    '.idv{display:grid;grid-template-columns:150px 1fr;gap:20px;align-items:start}',
    '.scan{position:relative;width:150px;height:150px}',
    '.scan svg{position:absolute;inset:0;width:100%;height:100%}',
    '.scan .r1{animation:lt-spin 7s linear infinite;transform-origin:75px 75px}.scan .r2{animation:lt-spin 4.5s linear infinite reverse;transform-origin:75px 75px}',
    '.scan circle{fill:none}',
    '.scan .mono-c{position:absolute;inset:38px;border-radius:50%;display:grid;place-items:center;background:var(--ink);color:var(--paper);font-size:26px;font-weight:600;overflow:hidden}',
    '.scan .beam{position:absolute;left:30px;right:30px;height:2px;top:30px;background:var(--accent);box-shadow:0 0 12px 2px rgba(23,184,147,.55);animation:lt-beam 1.3s ease-in-out infinite alternate}',
    '.fields{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;font-size:12.5px;align-content:start}',
    '.fields dt{color:var(--muted);font-size:11.5px;letter-spacing:.08em}.fields dd{margin:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.checks{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-top:6px}',
    '.chk{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border:1px solid var(--line);font-size:12px;border-radius:2px}',
    '.chk b{font-weight:500;font-size:11.5px;color:var(--muted)}.chk.ok{border-color:var(--accent)}.chk.ok b{color:var(--accent);font-weight:600}',
    '.result{grid-column:1/-1;margin-top:8px;padding:10px 12px;background:var(--ink);color:var(--paper);display:flex;align-items:center;gap:10px;font-size:13px;letter-spacing:.2em;opacity:0;transform:translateY(6px);transition:opacity .25s,transform .35s}',
    '.result.on{opacity:1;transform:none}.result i{width:8px;height:8px;background:var(--accent2)}',
    '.txt p{margin:0 0 8px;font-size:13px}',
    '.rail{display:flex;gap:14px}',
    '.chip{flex:1;display:flex;flex-direction:column;gap:8px;font-size:12px;color:var(--muted);letter-spacing:.1em}',
    '.chip .n{font-size:10.5px;letter-spacing:.2em}.chip .bar{height:3px;background:var(--line);overflow:hidden}',
    '.chip .bar i{display:block;height:100%;background:var(--ink);transform-origin:left;transform:scaleX(0)}',
    '.chip.active{color:var(--ink)}.chip.active .bar i{animation:lt-fill var(--d,1000ms) linear both}',
    '.chip.done{color:var(--ink)}.chip.done .bar i{transform:none;background:var(--accent)}',
    '.grant{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;background:var(--paper);opacity:0;pointer-events:none;transition:opacity .35s ease}',
    '.grant.on{opacity:1}',
    '.grant .bars{position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0 46px,var(--line) 46px 47px);opacity:.6}',
    '.grant .stamp{position:relative;font-size:13px;letter-spacing:.5em;padding:6px 8px 6px 14px;border:1.5px solid var(--accent);color:var(--accent);transform:scale(1.6) rotate(-4deg);opacity:0}',
    '.grant.on .stamp{animation:lt-stamp .45s cubic-bezier(.2,1.6,.4,1) .1s forwards}',
    '.grant .title{position:relative;font-size:46px;font-weight:700;letter-spacing:.3em;padding-left:.3em}',
    '.grant .sub{position:relative;font-size:15px;color:var(--muted);letter-spacing:.2em}',
    '.grant .rule{position:relative;width:min(560px,60vw);height:4px;background:linear-gradient(90deg,var(--accent),var(--accent2));transform-origin:left;transform:scaleX(0)}',
    '.grant.on .rule{animation:lt-fill .7s cubic-bezier(.65,0,.35,1) .2s forwards}',
    '.skip{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);z-index:5;font:inherit;font-size:12px;letter-spacing:.14em;color:var(--ink);background:rgba(255,255,255,.7);border:1px solid var(--line-strong);padding:7px 14px;border-radius:2px;cursor:pointer;opacity:0;pointer-events:none;transition:opacity .3s}',
    '.skip.on{opacity:1;pointer-events:auto}.skip:hover{border-color:var(--ink)}.skip kbd{font:inherit;font-size:10px;color:var(--muted);margin-left:8px}',
    '.lt.reduced *,.lt.reduced *:before,.lt.reduced *:after{animation-duration:1ms !important;animation-iteration-count:1 !important;transition-duration:1ms !important}',
    '.lt.reduced .sweep{display:none}',
    '@keyframes lt-fade{from{opacity:0}to{opacity:1}}',
    '@keyframes lt-rise{from{opacity:0;transform:translateY(10px) scale(.92)}to{opacity:1;transform:none}}',
    '@keyframes lt-spin{to{transform:rotate(360deg)}}',
    '@keyframes lt-arc{0%{stroke-dashoffset:0}100%{stroke-dashoffset:-460}}',
    '@keyframes lt-fill{from{transform:scaleX(0)}to{transform:scaleX(1)}}',
    '@keyframes lt-blink{50%{opacity:0}}',
    '@keyframes lt-pulse{50%{opacity:.35}}',
    '@keyframes lt-sweep{0%{transform:translateY(0)}70%,100%{transform:translateY(140vh)}}',
    '@keyframes lt-beam{from{top:34px}to{top:114px}}',
    '@keyframes lt-stamp{to{opacity:1;transform:scale(1) rotate(-4deg)}}',
    '@media (max-height:620px){.dash{inset:52px 40px 52px}.grant .title{font-size:36px}.idv{grid-template-columns:120px 1fr}.scan{width:120px;height:120px;transform:scale(.8);transform-origin:0 0}}'
  ].join('\n');

  function fallbackMark() {
    var hex = 'M50 4 L90 27 L90 73 L50 96 L10 73 L10 27 Z';
    return svg('svg', { viewBox: '0 0 100 100', class: 'fallback' }, [
      svg('path', { d: hex, fill: 'none', stroke: 'currentColor', 'stroke-width': '4' }),
      svg('circle', { cx: '50', cy: '50', r: '12', fill: 'currentColor' })
    ]);
  }

  function markNode(src) {
    if (!src) return fallbackMark();
    var img = el('img', { alt: '', draggable: 'false' });
    img.addEventListener('error', function () { img.replaceWith(fallbackMark()); });
    // 冷启动时网关可能还没监听，等资源可达再加载，免得直接退回备用标志。
    whenAssetsReady(function () { img.src = src; });
    return img;
  }

  function latticeSvg(seed) {
    var r = 26;
    var w = Math.sqrt(3) * r;
    var parts = [];
    var nodes = [];
    for (var row = 0; row < 12; row += 1) {
      for (var col = 0; col < 11; col += 1) {
        var cx = col * w + (row % 2 ? w / 2 : 0);
        var cy = row * r * 1.5;
        var d = '';
        for (var k = 0; k < 6; k += 1) {
          var ang = Math.PI / 180 * (60 * k - 30);
          d += (k ? 'L' : 'M') + (cx + r * Math.cos(ang)).toFixed(1) + ' ' + (cy + r * Math.sin(ang)).toFixed(1);
        }
        parts.push(d + 'Z');
        var h = (row * 31 + col * 17 + seed) % 23;
        if (h === 0 || h === 7) nodes.push({ x: cx + r * Math.cos(-Math.PI / 6), y: cy + r * Math.sin(-Math.PI / 6), on: h === 0 });
      }
    }
    var maskId = 'lt-m' + seed;
    var children = [
      svg('defs', {}, [
        svg('radialGradient', { id: maskId + 'g', cx: '70%', cy: '70%', r: '70%' }, [
          svg('stop', { offset: '0', 'stop-color': '#fff', 'stop-opacity': '1' }),
          svg('stop', { offset: '1', 'stop-color': '#fff', 'stop-opacity': '0' })
        ]),
        svg('mask', { id: maskId }, [svg('rect', { width: '600', height: '600', fill: 'url(#' + maskId + 'g)' })])
      ]),
      svg('g', { mask: 'url(#' + maskId + ')' }, [svg('path', { d: parts.join('') })].concat(nodes.map(function (n) {
        return svg('circle', { class: 'node' + (n.on ? ' on' : ''), cx: n.x.toFixed(1), cy: n.y.toFixed(1), r: '4' });
      })))
    ];
    return svg('svg', { class: 'lattice', viewBox: '0 0 600 600', 'aria-hidden': 'true' }, children);
  }

  function scannerSvg() {
    function ring(r, cls, dash, color, width) {
      return svg('circle', { cx: '75', cy: '75', r: String(r), class: cls || '', stroke: color, 'stroke-width': String(width), 'stroke-dasharray': dash || '' });
    }
    return svg('svg', { viewBox: '0 0 150 150', 'aria-hidden': 'true' }, [
      ring(72, '', '', 'var(--line-strong)', 1),
      svg('g', { class: 'r1' }, [ring(64, '', '2 6', 'var(--ink)', 1.5), ring(64, '', '40 362', 'var(--accent)', 3)]),
      svg('g', { class: 'r2' }, [ring(54, '', '18 10', 'var(--line-strong)', 1.2), ring(54, '', '12 330', 'var(--ink)', 3)])
    ]);
  }

  function LabTerminal(shadow, theme, sequence, ctx, reduced, sound) {
    var timers = Timers();
    var sfx = sound || SILENT;
    var palette = sequence.palette || {};
    var t = function (text) { return template(text, ctx); };
    var root = el('div', { className: 'lt' + (reduced ? ' reduced' : '') });
    var paletteVars = { paper: '--paper', ink: '--ink', accent: '--accent', accent2: '--accent2', muted: '--muted', console: '--console', console_ink: '--console-ink', warn: '--warn', danger: '--danger' };
    Object.keys(paletteVars).forEach(function (key) { if (palette[key]) root.style.setProperty(paletteVars[key], palette[key]); });

    shadow.appendChild(el('style', { text: LAB_CSS }));
    var brand = sequence.brand || {};
    var hud = sequence.hud || {};
    var phases = (sequence.phases || []).slice();
    var introMs = reduced ? 0 : 1000;
    root.style.setProperty('--intro-ms', introMs + 'ms');

    root.appendChild(el('div', { className: 'grid' }));
    root.appendChild(latticeSvg(hashCode(theme.id) % 97));
    if (!reduced) root.appendChild(el('div', { className: 'sweep' }));
    root.appendChild(el('div', { className: 'marks' }, [el('i', { className: 'tl' }), el('i', { className: 'tr' }), el('i', { className: 'bl' }), el('i', { className: 'br' })]));
    root.appendChild(el('div', { className: 'hud top mono' }, [
      el('span', {}, [el('i', { className: 'dot' }), t(hud.tl || 'TERMINAL')]),
      el('span', { text: t(hud.tr || '{{date}}  {{time}}') })
    ]));
    root.appendChild(el('div', { className: 'hud bottom mono' }, [
      el('span', { text: t(hud.bl || '') }),
      el('span', { text: t(hud.br || 'BUILD {{theme_version}}') })
    ]));

    var intro = el('div', { className: 'intro' }, [
      el('div', { className: 'intro-mark' }, [
        markNode(brand.mark),
        svg('svg', { class: 'ring', viewBox: '0 0 132 132' }, [
          svg('circle', { cx: '66', cy: '66', r: '62' }),
          svg('circle', { class: 'arc', cx: '66', cy: '66', r: '58' })
        ])
      ]),
      el('div', { className: 'intro-title', text: t(brand.title || '研究终端') }),
      brand.subtitle ? el('div', { className: 'intro-sub mono', text: t(brand.subtitle) }) : null,
      el('div', { className: 'intro-bar' }, [el('i')])
    ]);
    root.appendChild(intro);

    var lines = el('div', { className: 'console-lines mono' });
    var panelTitle = el('span', { text: '' });
    var panelPhase = el('span', { className: 'ph mono', text: '' });
    var panelBody = el('div', { className: 'panel-body' });
    var railPhases = phases.filter(function (p) { return !p.panel || p.panel.type !== 'grant'; });
    var chips = railPhases.map(function (phase, index) {
      var bar = el('i');
      var chip = el('div', { className: 'chip' }, [
        el('span', { className: 'n mono', text: pad(index + 1) + ' / ' + pad(railPhases.length) }),
        el('span', { text: t(phase.label) }),
        el('div', { className: 'bar' }, [bar])
      ]);
      chip.phase = phase;
      return chip;
    });
    var dash = el('div', { className: 'dash' }, [
      el('header', { className: 'brand' }, [
        el('div', { className: 'brand-mark' }, [markNode(brand.mark)]),
        el('div', { className: 'brand-text' }, [
          el('b', { text: t(brand.title || '研究终端') }),
          brand.subtitle ? el('small', { className: 'mono', text: t(brand.subtitle) }) : null
        ]),
        el('div', { className: 'brand-side mono' }, [
          brand.tag ? el('span', { className: 'brand-tag', text: t(brand.tag) }) : null,
          el('span', { text: 'SESSION ' + ctx.session })
        ])
      ]),
      el('main', { className: 'body' }, [
        el('section', { className: 'console' }, [
          el('div', { className: 'console-head mono' }, [el('i'), el('i'), el('i'), el('span', { text: 'TERM / TTY01 · ' + ctx.time })]),
          lines,
          el('div', { className: 'cursor' })
        ]),
        el('section', { className: 'panel' }, [
          el('div', { className: 'panel-head' }, [panelTitle, panelPhase]),
          panelBody
        ])
      ]),
      el('footer', { className: 'rail' }, chips)
    ]);
    root.appendChild(dash);

    var grant = el('div', { className: 'grant' });
    root.appendChild(grant);
    shadow.appendChild(root);
    sfx.intro();

    function addLine(entry, slot) {
      var text = t(entry.text);
      var tone = entry.tone && entry.tone !== 'normal' ? ' ' + entry.tone : '';
      var body = el('span', { className: 't', text: reduced ? text : '' });
      var status = entry.status ? el('span', { className: 's' + (reduced ? ' on' : ''), text: entry.status }) : null;
      lines.appendChild(el('div', { className: 'line' + tone }, [el('span', { className: 'p', text: '›' }), body, status]));
      while (lines.childElementCount > 1 && lines.scrollHeight > lines.clientHeight + 2) lines.firstElementChild.remove();
      if (reduced) return;
      var chars = Array.from(text);
      var step = Math.max(6, Math.min(22, (slot * 0.5) / Math.max(1, chars.length)));
      chars.forEach(function (ch, i) {
        timers.after(i * step, function () {
          body.textContent += ch;
          if (ch !== ' ') sfx.tick();
        });
      });
      if (status) timers.after(chars.length * step + 60, function () { status.classList.add('on'); sfx.status(entry.tone); });
    }

    function setPanel(phase, index) {
      var panel = phase.panel;
      panelPhase.textContent = 'PHASE ' + pad(index + 1);
      panelTitle.textContent = t((panel && panel.title) || phase.label);
      var view = el('div', { className: 'view' });
      var duration = phase.duration_ms;
      if (!panel || panel.type === 'text') {
        var box = el('div', { className: 'txt' });
        ((panel && panel.lines) || []).forEach(function (line) { box.appendChild(el('p', { text: t(line) })); });
        view.appendChild(box);
      } else if (panel.type === 'meters') {
        (panel.items || []).forEach(function (item, i) {
          var fill = el('i');
          view.appendChild(el('div', { className: 'meter' }, [
            el('span', { text: t(item.label) }),
            el('div', { className: 'bar' }, [fill]),
            el('span', { className: 'v mono', text: t(item.text || Math.round(item.value * 100) + '%') })
          ]));
          timers.after(reduced ? 0 : 120 + i * 140, function () {
            fill.style.transform = 'scaleX(' + clamp(item.value, 0, 1) + ')';
            if (!reduced) sfx.meter();
          });
        });
      } else if (panel.type === 'archive') {
        var entries = panel.entries || [];
        view.appendChild(el('div', { className: 'arc-head mono' }, [
          el('span', { text: 'INDEX · ' + entries.length + ' RECORDS' }),
          el('span', { text: 'CLEARANCE ' + (ctx.operator_code.slice(-1) % 3 + 2) })
        ]));
        var gap = (duration * 0.72) / Math.max(1, entries.length);
        entries.forEach(function (entry, i) {
          var fill = el('i');
          var st = el('span', { className: 'st mono', text: '检索中…' });
          var row = el('div', { className: 'row' }, [
            el('span', { className: 'c mono', text: t(entry.code) }),
            el('span', { text: t(entry.label) }),
            el('span', { className: 'lv mono', text: t(entry.level || '—') }),
            st,
            el('div', { className: 'fill' }, [fill])
          ]);
          view.appendChild(row);
          timers.after(reduced ? 0 : i * gap, function () {
            row.classList.add('on');
            fill.style.transform = 'scaleX(1)';
            if (!reduced) sfx.row();
            timers.after(reduced ? 0 : 380, function () { st.textContent = t(entry.state || '已解密'); st.classList.add('ok'); });
          });
        });
      } else if (panel.type === 'identity') {
        var fields = el('dl', { className: 'fields' });
        (panel.fields || []).forEach(function (field) {
          fields.appendChild(el('dt', { text: t(field.label) }));
          fields.appendChild(el('dd', { text: t(field.value) }));
        });
        var checks = el('div', { className: 'checks' });
        var result = el('div', { className: 'result' }, [el('i'), el('span', { text: t(panel.result || '身份确认完毕') })]);
        var list = panel.checks || [];
        list.forEach(function (item, i) {
          var badge = el('b', { text: '比对中' });
          var row = el('div', { className: 'chk' }, [el('span', { text: t(item.label) }), badge]);
          checks.appendChild(row);
          timers.after(reduced ? 0 : duration * (0.28 + 0.5 * (i + 1) / (list.length + 1)), function () {
            badge.textContent = t(item.result || '通过') + ' ✓';
            row.classList.add('ok');
            if (!reduced) sfx.check();
          });
        });
        view.appendChild(el('div', { className: 'idv' }, [
          el('div', { className: 'scan' }, [scannerSvg(), el('div', { className: 'mono-c', text: t(panel.monogram || monogramOf(ctx.operator)) }), reduced ? null : el('div', { className: 'beam' })]),
          fields,
          checks,
          result
        ]));
        if (!reduced) sfx.scan(duration * 0.8 / 1000);
        timers.after(reduced ? 0 : duration * 0.86, function () { result.classList.add('on'); sfx.result(); });
      }
      var old = panelBody.firstElementChild;
      if (old) old.remove();
      panelBody.appendChild(view);
    }

    function showGrant(phase) {
      var panel = phase.panel || {};
      grant.textContent = '';
      grant.appendChild(el('div', { className: 'bars' }));
      if (panel.stamp) grant.appendChild(el('div', { className: 'stamp mono', text: t(panel.stamp) }));
      grant.appendChild(el('div', { className: 'title', text: t(panel.title || '访问已授权') }));
      if (panel.subtitle) grant.appendChild(el('div', { className: 'sub', text: t(panel.subtitle) }));
      grant.appendChild(el('div', { className: 'rule' }));
      grant.classList.add('on');
    }

    var resolveDone;
    var done = new Promise(function (resolve) { resolveDone = resolve; });
    var total = introMs;
    var scale = reduced ? 0.22 : 1;

    timers.after(introMs, function () {
      intro.classList.add('out');
      dash.classList.add('in');
    });

    var chipIndex = 0;
    phases.forEach(function (phase, index) {
      var duration = Math.max(200, Math.round(phase.duration_ms * scale));
      var start = total;
      var isGrant = phase.panel && phase.panel.type === 'grant';
      var chip = isGrant ? null : chips[chipIndex++];
      timers.after(start, function () {
        chips.forEach(function (c) { if (c.classList.contains('active')) { c.classList.remove('active'); c.classList.add('done'); } });
        if (isGrant) {
          showGrant(phase);
          sfx.grant();
          return;
        }
        sfx.phase(phase.panel ? phase.panel.type : 'text');
        if (chip) {
          chip.style.setProperty('--d', duration + 'ms');
          chip.classList.add('active');
        }
        setPanel(phase, index);
        var logs = phase.log || [];
        var slot = (duration * 0.86) / Math.max(1, logs.length);
        logs.forEach(function (entry, i) { timers.after(reduced ? 0 : i * slot, function () { addLine(entry, slot); }); });
      });
      total += duration;
    });
    timers.after(total, function () {
      chips.forEach(function (c) { c.classList.remove('active'); c.classList.add('done'); });
      resolveDone();
    });

    return {
      root: root,
      done: done,
      dispose: function () { timers.clear(); }
    };
  }

  // ---------------------------------------------------------------- 开场声音

  // ---------------------------------------------------------------- V2 驱动

  function bootContext(context) {
    var operator = context.operator || '研究员';
    var d = new Date();
    var theme = context.theme || {};
    return {
      operator: operator,
      operator_code: context.operator_code || String(1000 + (hashCode(operator) % 9000)),
      date: d.getFullYear() + '.' + pad(d.getMonth() + 1) + '.' + pad(d.getDate()),
      time: pad(d.getHours()) + ':' + pad(d.getMinutes()),
      weekday: '星期' + '日一二三四五六'.charAt(d.getDay()),
      theme_name: theme.name || '',
      theme_version: theme.version || '',
      engine_version: context.engine || '',
      session: randomHex(6)
    };
  }

  var sdk = window.WorkSwarmThemeSDK;
  var sequence = window.WORKSWARM_SEQUENCE;
  var started = false;
  if (!sdk || !sequence) {
    report(new Error('科研终端播放页缺少 SDK 或时间线'));
    if (sdk) sdk.error('SEQUENCE_MISSING', 'timeline is missing');
    return;
  }
  sdk.onStart(function (context) {
    if (started) return;
    started = true;
    try {
      var host = el('div', { style: 'position:fixed;inset:0;display:block' });
      attachTo(host);
      var shadow = host.attachShadow({ mode: 'open' });
      var sound = sdk.sound;
      var renderer = LabTerminal(shadow, context.theme || { id: '' }, sequence, bootContext(context),
        sdk.shortened(), sound && sound.sfx);
      renderer.done.then(function () { sdk.complete(); });
    } catch (error) {
      report(error);
      sdk.error('RENDER_FAILED', String(error && error.message || error));
    }
  });
})();

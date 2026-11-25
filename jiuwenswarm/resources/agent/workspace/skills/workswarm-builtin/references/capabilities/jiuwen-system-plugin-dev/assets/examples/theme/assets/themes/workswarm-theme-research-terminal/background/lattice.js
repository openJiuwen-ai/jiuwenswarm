/* 科研终端 · 分子晶格动态背景。运行在主题沙箱 iframe 中，低帧率、可暂停。 */
(function () {
  'use strict';

  var canvas = document.getElementById('field');
  var ctx = canvas.getContext('2d');
  var INK = '18, 22, 20';
  var ACCENT = '23, 184, 147';
  var FPS = 30;

  var width = 0;
  var height = 0;
  var dpr = 1;
  var staticLayer = null;
  var latticeLayer = null;
  var nodes = [];
  var running = false;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var visible = true;
  var last = 0;
  var t0 = performance.now();

  function seeded(seed) {
    return function () {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
  }

  function layer(w, h) {
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w * dpr));
    c.height = Math.max(1, Math.round(h * dpr));
    var g = c.getContext('2d');
    g.scale(dpr, dpr);
    return { canvas: c, ctx: g };
  }

  function drawStatic() {
    var l = layer(width, height);
    var g = l.ctx;
    var grad = g.createLinearGradient(0, 0, width, height);
    grad.addColorStop(0, '#f4f6f5');
    grad.addColorStop(1, '#e6ebe9');
    g.fillStyle = grad;
    g.fillRect(0, 0, width, height);
    var glow = g.createRadialGradient(width * 0.28, height * 0.18, 0, width * 0.28, height * 0.18, Math.max(width, height) * 0.7);
    glow.addColorStop(0, 'rgba(255,255,255,.75)');
    glow.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = glow;
    g.fillRect(0, 0, width, height);

    g.strokeStyle = 'rgba(' + INK + ', .045)';
    g.lineWidth = 1;
    g.beginPath();
    for (var x = 0.5; x < width; x += 28) { g.moveTo(x, 0); g.lineTo(x, height); }
    for (var y = 0.5; y < height; y += 28) { g.moveTo(0, y); g.lineTo(width, y); }
    g.stroke();

    g.strokeStyle = 'rgba(' + INK + ', .35)';
    g.lineWidth = 1.2;
    var m = 18;
    var s = 14;
    [[m, m, 1, 1], [width - m, m, -1, 1], [m, height - m, 1, -1], [width - m, height - m, -1, -1]].forEach(function (c) {
      g.beginPath();
      g.moveTo(c[0], c[1] + s * c[3]);
      g.lineTo(c[0], c[1]);
      g.lineTo(c[0] + s * c[2], c[1]);
      g.stroke();
    });
    staticLayer = l.canvas;
  }

  function drawLattice() {
    var size = Math.max(width, height) * 0.85;
    var l = layer(size, size);
    var g = l.ctx;
    var r = 30;
    var w = Math.sqrt(3) * r;
    g.strokeStyle = 'rgba(' + INK + ', .11)';
    g.lineWidth = 1;
    g.beginPath();
    for (var row = 0; row * r * 1.5 < size + r; row += 1) {
      for (var col = 0; col * w < size + w; col += 1) {
        var cx = col * w + (row % 2 ? w / 2 : 0);
        var cy = row * r * 1.5;
        for (var k = 0; k < 6; k += 1) {
          var a = Math.PI / 180 * (60 * k - 30);
          var px = cx + r * Math.cos(a);
          var py = cy + r * Math.sin(a);
          if (k === 0) g.moveTo(px, py); else g.lineTo(px, py);
        }
        g.closePath();
      }
    }
    g.stroke();
    g.globalCompositeOperation = 'destination-in';
    var fade = g.createRadialGradient(size, size, 0, size, size, size);
    fade.addColorStop(0, 'rgba(0,0,0,1)');
    fade.addColorStop(0.55, 'rgba(0,0,0,.55)');
    fade.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = fade;
    g.fillRect(0, 0, size, size);
    latticeLayer = { canvas: l.canvas, size: size };
  }

  function makeNodes() {
    var rand = seeded(20261103);
    var count = Math.min(64, Math.max(18, Math.round(width * height / 36000)));
    nodes = [];
    for (var i = 0; i < count; i += 1) {
      var angle = rand() * Math.PI * 2;
      var speed = 0.08 + rand() * 0.16;
      nodes.push({
        x: rand() * width,
        y: rand() * height,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        r: 1.4 + rand() * 1.8,
        accent: rand() < 0.14,
        phase: rand() * Math.PI * 2
      });
    }
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawStatic();
    drawLattice();
    makeNodes();
    frame(performance.now(), true);
  }

  function step(dt) {
    for (var i = 0; i < nodes.length; i += 1) {
      var n = nodes[i];
      n.x += n.vx * dt;
      n.y += n.vy * dt;
      if (n.x < -40) n.x = width + 40; else if (n.x > width + 40) n.x = -40;
      if (n.y < -40) n.y = height + 40; else if (n.y > height + 40) n.y = -40;
    }
  }

  function frame(now, force) {
    var elapsed = (now - t0) / 1000;
    ctx.drawImage(staticLayer, 0, 0, width, height);

    if (latticeLayer) {
      var drift = reduced ? 0 : Math.sin(elapsed / 18) * 14;
      var s = latticeLayer.size;
      ctx.drawImage(latticeLayer.canvas, width - s + drift, height - s + drift * 0.6, s, s);
    }

    var maxD = 150;
    ctx.lineWidth = 1;
    for (var i = 0; i < nodes.length; i += 1) {
      var a = nodes[i];
      for (var j = i + 1; j < nodes.length; j += 1) {
        var b = nodes[j];
        var dx = a.x - b.x;
        var dy = a.y - b.y;
        var d2 = dx * dx + dy * dy;
        if (d2 > maxD * maxD) continue;
        var alpha = (1 - Math.sqrt(d2) / maxD) * 0.16;
        ctx.strokeStyle = 'rgba(' + ((a.accent && b.accent) ? ACCENT : INK) + ',' + alpha.toFixed(3) + ')';
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
    for (var k = 0; k < nodes.length; k += 1) {
      var n = nodes[k];
      if (n.accent) {
        var pulse = reduced ? 0.5 : (Math.sin(elapsed * 1.6 + n.phase) + 1) / 2;
        ctx.fillStyle = 'rgba(' + ACCENT + ',' + (0.08 + pulse * 0.12).toFixed(3) + ')';
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r + 5 + pulse * 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = 'rgba(' + ACCENT + ', .75)';
      } else {
        ctx.fillStyle = 'rgba(' + INK + ', .34)';
      }
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fill();
    }

    if (!reduced) {
      var cycle = 16;
      var p = (elapsed % cycle) / 5;
      if (p <= 1) {
        var y = p * (height + 160) - 80;
        var band = ctx.createLinearGradient(0, y - 80, 0, y);
        band.addColorStop(0, 'rgba(' + ACCENT + ', 0)');
        band.addColorStop(1, 'rgba(' + ACCENT + ', .07)');
        ctx.fillStyle = band;
        ctx.fillRect(0, y - 80, width, 80);
        ctx.fillStyle = 'rgba(' + ACCENT + ', .22)';
        ctx.fillRect(0, y, width, 1);
      }
    }
  }

  function loop(now) {
    if (!running) return;
    requestAnimationFrame(loop);
    var dt = now - last;
    if (dt < 1000 / FPS) return;
    last = now;
    step(Math.min(dt, 100) / 33);
    frame(now);
  }

  function start() {
    if (running || reduced || !visible) return;
    running = true;
    last = performance.now();
    requestAnimationFrame(loop);
  }

  function stop() { running = false; }

  window.addEventListener('resize', function () { resize(); });
  document.addEventListener('visibilitychange', function () {
    visible = !document.hidden;
    if (visible) start(); else stop();
  });

  if (window.WorkSwarmThemeSDK) {
    window.WorkSwarmThemeSDK.onStart(function (context) {
      reduced = !!context.reduced_motion;
      visible = context.visible !== false;
      if (reduced) { stop(); frame(performance.now(), true); } else start();
    });
    window.WorkSwarmThemeSDK.onVisibility(function (isVisible) {
      visible = isVisible;
      if (visible) start(); else stop();
    });
  }

  resize();
  start();
})();

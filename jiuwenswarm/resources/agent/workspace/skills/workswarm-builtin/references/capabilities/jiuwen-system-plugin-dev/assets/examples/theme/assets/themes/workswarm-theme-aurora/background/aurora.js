/* 流光极昼：低帧率的柔和光带动画。以 1/4 分辨率绘制再放大，开销很小。 */
(function () {
  'use strict';

  var canvas = document.getElementById('sky');
  var ctx = canvas.getContext('2d');
  var SCALE = 0.25;
  var FPS = 24;
  var RIBBONS = [
    { color: [137, 120, 230], amp: 0.12, freq: 1.3, speed: 0.045, y: 0.32, thick: 0.22, alpha: 0.32 },
    { color: [94, 196, 214], amp: 0.1, freq: 1.8, speed: -0.035, y: 0.5, thick: 0.18, alpha: 0.28 },
    { color: [236, 160, 200], amp: 0.08, freq: 2.2, speed: 0.028, y: 0.7, thick: 0.2, alpha: 0.24 }
  ];
  var running = false;
  var visible = true;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var last = 0;
  var t0 = performance.now();

  function resize() {
    canvas.width = Math.max(64, Math.round(window.innerWidth * SCALE));
    canvas.height = Math.max(40, Math.round(window.innerHeight * SCALE));
    draw(performance.now());
  }

  function draw(now) {
    var w = canvas.width;
    var h = canvas.height;
    var t = (now - t0) / 1000;
    var base = ctx.createLinearGradient(0, 0, 0, h);
    base.addColorStop(0, '#f3f2fb');
    base.addColorStop(1, '#e7ecf6');
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'multiply';
    RIBBONS.forEach(function (r, index) {
      var phase = reduced ? index : t * r.speed * Math.PI * 2;
      ctx.beginPath();
      var top = [];
      for (var x = 0; x <= w; x += 2) {
        var u = x / w;
        var y = (r.y + Math.sin(u * Math.PI * r.freq + phase) * r.amp + Math.sin(u * 7 + phase * 1.7) * 0.015) * h;
        top.push([x, y]);
      }
      var thick = r.thick * h;
      ctx.moveTo(top[0][0], top[0][1]);
      top.forEach(function (p) { ctx.lineTo(p[0], p[1]); });
      for (var i = top.length - 1; i >= 0; i -= 1) {
        var wobble = Math.sin(i / top.length * Math.PI * 2 + phase) * 0.25 + 1;
        ctx.lineTo(top[i][0], top[i][1] + thick * wobble);
      }
      ctx.closePath();
      var g = ctx.createLinearGradient(0, r.y * h - thick, 0, r.y * h + thick * 1.5);
      g.addColorStop(0, 'rgba(' + r.color.join(',') + ',0)');
      g.addColorStop(0.5, 'rgba(' + r.color.join(',') + ',' + r.alpha + ')');
      g.addColorStop(1, 'rgba(' + r.color.join(',') + ',0)');
      ctx.fillStyle = g;
      ctx.fill();
    });
    ctx.globalCompositeOperation = 'source-over';
  }

  function loop(now) {
    if (!running) return;
    requestAnimationFrame(loop);
    if (now - last < 1000 / FPS) return;
    last = now;
    draw(now);
  }

  function start() {
    if (running || reduced || !visible) return;
    running = true;
    requestAnimationFrame(loop);
  }

  function stop() { running = false; }

  window.addEventListener('resize', resize);
  document.addEventListener('visibilitychange', function () {
    visible = !document.hidden;
    if (visible) start(); else stop();
  });
  if (window.WorkSwarmThemeSDK) {
    window.WorkSwarmThemeSDK.onStart(function (context) {
      reduced = !!context.reduced_motion;
      if (reduced) { stop(); draw(performance.now()); } else start();
    });
    window.WorkSwarmThemeSDK.onVisibility(function (isVisible) {
      visible = isVisible;
      if (visible) start(); else stop();
    });
  }
  resize();
  start();
})();

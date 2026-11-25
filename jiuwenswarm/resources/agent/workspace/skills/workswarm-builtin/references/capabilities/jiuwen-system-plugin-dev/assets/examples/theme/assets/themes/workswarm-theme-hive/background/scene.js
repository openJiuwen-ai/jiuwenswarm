/* 蜂巢协同 · 动态背景：晨光下缓慢呼吸的蜡质蜂巢，小蜜蜂在上方巡游并彼此连线。30 帧上限，窗口隐藏时暂停。 */
(function () {
  'use strict';

  var H = window.Hive, V = H.v3, M = H.m4;
  var canvas = document.getElementById('scene');
  var params = new URLSearchParams(location.search);
  var FPS = 30;
  var RINGS = 24, RADIUS = 0.62, BEES = 12;
  var ctx = H.create(canvas, { dprCap: 1.25, power: 'low-power', preserve: params.has('capture') });
  if (!ctx) { document.documentElement.classList.add('no-webgl'); return; }

  var sky = new H.Sky(ctx);
  var cells = new H.Cells(ctx, 4000);
  var bees = new H.Bees(ctx, BEES);
  var dust = new H.Points(ctx, 200);
  var links = new H.Lines(ctx, 120);
  var rnd = H.rng(20261104);

  var grid = H.hexGrid(RINGS, RADIUS).map(function (c) {
    var honey = rnd() < 0.55;
    return { x: c.x, z: c.z, seed: rnd(), honey: honey, fill: honey ? 0.35 + rnd() * 0.5 : 0.03,
      worker: honey && rnd() < 0.22, base: 0.8 + rnd() * 0.25 };
  });

  var swarm = [];
  for (var i = 0; i < BEES; i++) {
    swarm.push({
      c: [(rnd() - 0.5) * 15, 1.6 + rnd() * 2.0, -4 + rnd() * 8],
      a: [3 + rnd() * 4, 0.3 + rnd() * 0.5, 1.5 + rnd() * 2.5],
      w: [0.07 + rnd() * 0.08, 0.21 + rnd() * 0.2, 0.05 + rnd() * 0.07],
      p: [rnd() * 6.28, rnd() * 6.28, rnd() * 6.28],
      scale: 0.15 + rnd() * 0.06, phase: rnd() * 6.28, blink: rnd() * 6
    });
  }
  var motes = [];
  for (i = 0; i < 140; i++) motes.push({ p: [(rnd() - 0.5) * 30, rnd() * 7, -14 + rnd() * 22], s: 0.03 + rnd() * 0.07, k: rnd() });

  var LOOK = {
    sky: { top: H.rgb('#fbf8f2'), bottom: H.rgb('#f3e8d4'), sunCol: H.rgb('#ffcf7a', 0.16), sun: [0.84, 0.95], sunSize: 0.42,
      hazeCol: H.rgb('#ece5fb'), haze: [0.1, 0.92], hazeSize: 0.5 },
    light: H.lighting({
      keyDir: [-0.55, 0.75, 0.4], keyCol: [1.0, 0.9, 0.76], fillDir: [0.7, 0.35, 0.4], fillCol: [0.22, 0.2, 0.34],
      rimDir: [0.25, 0.55, -0.8], rimCol: [0.7, 0.55, 0.32], skyAmb: [0.42, 0.4, 0.47], groundAmb: [0.4, 0.33, 0.24],
      fogCol: H.rgb('#f4ecdd'), fog: [0.085, 5.5]
    }),
    cells: { wax: H.rgb('#e9d1a0'), waxDeep: H.rgb('#c58f4c'), rim: H.rgb('#fff4dc'), honey: H.rgb('#d9850b'),
      honeyHi: H.rgb('#ffb534'), honeyGlow: 0.75 },
    bee: { emissive: [0, 0, 0], wingAlpha: 0.9 }
  };

  var start = performance.now(), last = 0, running = false, visible = true;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (params.has('t')) reduced = true;

  function frame(t) {
    ctx.resize();
    var aspect = ctx.w / ctx.h;
    var sway = Math.sin(t * 0.05) * 0.8;
    var eye = [sway, 4.3 + Math.sin(t * 0.07) * 0.15, 13], target = [sway * 0.4, 0.9, -6];
    var proj = M.perspective(38 * Math.PI / 180, aspect, 0.1, 80);
    var view = M.lookAt(eye, target, [0, 1, 0]);
    var vp = M.mul(proj, view);

    for (var i = 0; i < grid.length; i++) {
      var g = grid[i];
      var wave = Math.sin(g.x * 0.33 + t * 0.22) * Math.cos(g.z * 0.29 - t * 0.17) * 0.22;
      var glow = g.worker ? Math.pow(Math.max(0, Math.sin(t * 0.45 + g.seed * 40)), 10) : 0;
      cells.set(i, g.x, g.z, g.base + wave, g.fill, glow, 1, g.seed, 0);
    }
    cells.upload(grid.length);

    dust.reset(); links.reset();
    var pos = [];
    for (i = 0; i < swarm.length; i++) {
      var b = swarm[i];
      var p = [b.c[0] + b.a[0] * Math.sin(b.w[0] * t * 6.28 + b.p[0]),
        b.c[1] + b.a[1] * Math.sin(b.w[1] * t * 6.28 + b.p[1]),
        b.c[2] + b.a[2] * Math.sin(b.w[2] * t * 6.28 + b.p[2])];
      var vel = [b.a[0] * b.w[0] * Math.cos(b.w[0] * t * 6.28 + b.p[0]),
        b.a[1] * b.w[1] * Math.cos(b.w[1] * t * 6.28 + b.p[1]) * 0.4,
        b.a[2] * b.w[2] * Math.cos(b.w[2] * t * 6.28 + b.p[2])];
      // 朝飞行方向，略带侧倾；速度很小时转向镜头。
      var speed = V.len(vel);
      var fwd = V.norm(V.lerp(V.norm(V.sub(eye, p)), V.norm(vel), H.ease.smooth(0.05, 0.4, speed)));
      var bank = Math.max(-0.5, Math.min(0.5, -vel[0] * 0.35));
      var quat = H.quatLook(fwd, [Math.sin(bank), Math.cos(bank), 0]);
      var blink = Math.max(0, 1 - Math.abs(((t + b.blink) % 4.5) - 0.1) / 0.09);
      bees.set(i, p, b.scale, quat, b.phase, blink, 0);
      pos.push(p);
    }
    bees.upload(swarm.length);
    for (i = 0; i < pos.length; i++) {
      for (var j = i + 1; j < pos.length; j++) {
        var d = V.len(V.sub(pos[i], pos[j]));
        if (d < 6) links.push(pos[i], pos[j], (1 - d / 6) * 0.9);
      }
    }
    for (i = 0; i < motes.length; i++) {
      var m = motes[i];
      var y = (m.p[1] + t * (0.08 + m.k * 0.1)) % 7;
      dust.push([m.p[0] + Math.sin(t * 0.2 + m.k * 9) * 0.6, y, m.p[2]], m.s, H.rgb('#f5b73b'),
        0.35 * Math.sin(Math.PI * y / 7));
    }

    var o = { viewProj: vp, camPos: eye, light: LOOK.light, time: t };
    H.clear(ctx, LOOK.sky.bottom);
    sky.draw(LOOK.sky);
    cells.draw({ viewProj: vp, camPos: eye, light: LOOK.light, time: t, radius: RADIUS * 0.985, look: LOOK.cells });
    bees.draw({ viewProj: vp, camPos: eye, light: LOOK.light, time: t, shells: 3, furLen: 0.08, furDensity: 46,
      flapAmp: 0.55, flapFreq: 38, look: LOOK.bee, lowDetail: true }, 'opaque');
    links.draw(vp, [0.62, 0.5, 0.86, 0.22], false);
    bees.draw({ viewProj: vp, camPos: eye, light: LOOK.light, time: t, flapAmp: 0.55, flapFreq: 38, look: LOOK.bee }, 'wings');
    dust.draw(vp, false, 1.2);
  }

  function loop(now) {
    if (!running) return;
    requestAnimationFrame(loop);
    if (now - last < 1000 / FPS - 2) return;
    last = now;
    frame((now - start) / 1000 + 40);
  }

  function play() {
    if (reduced) { frame(params.has('t') ? Number(params.get('t')) : 52); return; }
    if (running || !visible) return;
    running = true;
    requestAnimationFrame(loop);
  }

  window.addEventListener('resize', function () { if (!running) frame(52); });
  window.HiveBackground = { frame: frame, canvas: canvas };

  if (window.WorkSwarmThemeSDK) {
    WorkSwarmThemeSDK.onStart(function (context) {
      if (context && context.reduced_motion) reduced = true;
      if (context && context.visible === false) visible = false;
      play();
    });
    WorkSwarmThemeSDK.onVisibility(function (v) {
      visible = v;
      if (!v) running = false; else play();
    });
  }
  // 单独打开页面（不在主题运行时的 iframe 里）时直接播放，便于预览。
  if (window.parent === window) play();
})();

/*
 * 蜂巢协同 · 开场（约 7 秒）
 * 0.2s 小蜜蜂拖着金色光迹飞入 → 1.1s 光迹勾出六边形网络标志 → 2.0s 蜂巢由内向外逐环生长 →
 * 2.6s 蜂群从巢格中涌出、彼此连线 → 4.3s 列成六边形阵列，WorkSwarm 字标出现 → 5.5s 镜头推入，暖光收束进入工作台。
 */
(function () {
  'use strict';

  var H = window.Hive, V = H.v3, M = H.m4, E = H.ease;
  var params = new URLSearchParams(location.search);
  var root = document.documentElement;
  var canvas = document.getElementById('scene');
  var TOTAL = 6.95;
  var CREAM = H.rgb('#f6efe2');

  var el = {
    kicker: document.getElementById('kicker'), word: document.getElementById('word'),
    greet: document.getElementById('greet'), count: document.getElementById('count')
  };

  function greeting(operator) {
    var h = new Date().getHours();
    var part = h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 14 ? '中午好' : h < 18 ? '下午好' : '晚上好';
    return '蜂群已就绪 · ' + part + '，' + (operator || '研究员');
  }

  // 拆字，供字标逐字浮现。
  el.word.innerHTML = '';
  'WorkSwarm'.split('').forEach(function (ch, i) {
    var span = document.createElement('span');
    span.textContent = ch;
    span.className = i < 4 ? 'work' : 'swarm';
    span.style.transitionDelay = (i * 0.035) + 's';
    el.word.appendChild(span);
  });

  var ctx = H.create(canvas, { post: true, dprCap: 1.5, maxPixels: 2.2e6, power: 'high-performance', preserve: params.has('capture') });
  var completed = false;
  function complete() {
    if (completed) return;
    completed = true;
    if (window.WorkSwarmThemeSDK && window.parent !== window) WorkSwarmThemeSDK.complete();
  }

  if (!ctx) {
    // 没有 WebGL2：只放文字，短暂停留后进入。
    root.classList.add('no-webgl');
    var startText = function (context) {
      el.greet.textContent = greeting(context && context.operator);
      root.classList.add('show-kicker', 'show-word', 'show-greet');
      setTimeout(complete, 2200);
    };
    if (window.WorkSwarmThemeSDK) WorkSwarmThemeSDK.onStart(startText);
    if (window.parent === window) startText({});
    return;
  }

  var sky = new H.Sky(ctx);
  var cells = new H.Cells(ctx, 400);
  var swarmBees = new H.Bees(ctx, 32);
  var heroBee = new H.Bees(ctx, 1);
  var glow = new H.Points(ctx, 4000);
  var lines = new H.Lines(ctx, 600);
  var rnd = H.rng(9527);

  // ---------------------------------------------------------------- 场景数据
  var R = 0.5, WALL_Z = -3.6;
  var wallModel = M.mul(M.translate(0, 0.15, WALL_Z), M.rotX(Math.PI / 2));
  var grid = H.hexGrid(8, R).map(function (c) {
    return { x: c.x, z: c.z, d: c.d, seed: rnd(), fill: 0.45 + rnd() * 0.45, h: 0.75 + rnd() * 0.35,
      worker: rnd() < 0.25 };
  });
  function cellStart(c) { return 2.0 + c.d * 0.16 + c.seed * 0.1; }
  function cellMouth(c, height) {
    var p = [c.x, height, c.z, 1], m = wallModel;
    return [m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12], m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
      m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14]];
  }

  var HERO_HOME = [0, 0.3, 0.4];
  var LOGO_CENTER = [0, 0.33, -0.45], LOGO_R = 1.18;
  var logo = [];
  for (var k = 0; k <= 6; k++) {
    var a = (90 + 60 * k) * Math.PI / 180;
    logo.push([LOGO_CENTER[0] + Math.cos(a) * LOGO_R, LOGO_CENTER[1] + Math.sin(a) * LOGO_R, LOGO_CENTER[2]]);
  }

  // 阵列：内环 18 位（半径 1.85），外环 24 位（半径 2.7），都在面向镜头的平面上。
  function ringSlots(radius, perEdge, z) {
    var out = [];
    for (var e = 0; e < 6; e++) {
      var a0 = (90 + 60 * e) * Math.PI / 180, a1 = (150 + 60 * e) * Math.PI / 180;
      for (var s = 0; s < perEdge; s++) {
        var t = s / perEdge;
        out.push([Math.cos(a0) * radius * (1 - t) + Math.cos(a1) * radius * t + HERO_HOME[0],
          Math.sin(a0) * radius * (1 - t) + Math.sin(a1) * radius * t + HERO_HOME[1] + 0.05, z]);
      }
    }
    return out;
  }
  var slots = ringSlots(2.08, 5, 0.05);

  var spawnCells = grid.filter(function (c) { return c.d >= 1 && c.d <= 4; });
  var swarm = [];
  for (var i = 0; i < slots.length; i++) {
    var cell = spawnCells[Math.floor(rnd() * spawnCells.length)];
    var tilt = rnd() * Math.PI;
    swarm.push({
      cell: cell, born: cellStart(cell) + 0.45 + rnd() * 0.6, pos: null, vel: [0, 0, 0],
      orbitR: 1.5 + rnd() * 1.4, orbitW: (0.6 + rnd() * 0.6) * (rnd() < 0.5 ? -1 : 1), orbitP: rnd() * 6.28,
      axisA: [Math.cos(tilt), Math.sin(tilt) * 0.35, Math.sin(tilt) * 0.5], axisB: [-Math.sin(tilt) * 0.3, 0.85, 0.35],
      slot: slots[i], slotIndex: i, phase: rnd() * 6.28, blink: rnd() * 3, wob: rnd() * 6.28
    });
  }

  var motes = [];
  for (i = 0; i < 260; i++) {
    motes.push({ p: [(rnd() - 0.5) * 14, (rnd() - 0.5) * 8, -3 + rnd() * 8], s: 0.012 + rnd() * 0.035, k: rnd(),
      lilac: rnd() < 0.3 });
  }
  var bokeh = [];
  for (i = 0; i < 14; i++) bokeh.push({ p: [(rnd() - 0.5) * 6, (rnd() - 0.5) * 3.5, 2.6 + rnd() * 1.8], s: 0.18 + rnd() * 0.3, k: rnd() });

  var trail = [];

  var LOOK = {
    sky: { top: H.rgb('#120d18'), bottom: H.rgb('#06050a'), radial: 0.85, sunCol: H.rgb('#ff9f2e', 0.10), sun: [0.5, 0.52],
      sunSize: 0.36, hazeCol: H.rgb('#1d1430'), haze: [0.14, 0.86], hazeSize: 0.45 },
    light: H.lighting({
      keyDir: [-0.5, 0.75, 0.75], keyCol: [1.0, 0.86, 0.66], fillDir: [0.8, 0.1, 0.5], fillCol: [0.30, 0.25, 0.55],
      rimDir: [0.35, 0.5, -0.8], rimCol: [1.0, 0.72, 0.36], skyAmb: [0.16, 0.13, 0.2], groundAmb: [0.1, 0.07, 0.06],
      fogCol: H.rgb('#07060b'), fog: [0.09, 8.5]
    }),
    cells: { wax: H.rgb('#2a1b0d'), waxDeep: H.rgb('#080503'), rim: H.rgb('#ffb347'), honey: H.rgb('#7a3d04'),
      honeyHi: H.rgb('#ff9a1a'), honeyGlow: 0.5 },
    hero: { emissive: [0.04, 0.025, 0.0], wingAlpha: 1.0 },
    swarm: { emissive: [0.06, 0.035, 0.0], wingAlpha: 0.95 }
  };
  var GOLD = [1.0, 0.72, 0.28], LILAC = [0.72, 0.6, 1.0];

  // ---------------------------------------------------------------- 动画
  function bezier(p0, p1, p2, p3, t) {
    var u = 1 - t;
    return [0, 1, 2].map(function (i) {
      return u * u * u * p0[i] + 3 * u * u * t * p1[i] + 3 * u * t * t * p2[i] + t * t * t * p3[i];
    });
  }
  var FLIGHT = [[4.6, 2.1, -1.8], [2.8, -0.9, 2.0], [0.9, -0.35, 1.6], HERO_HOME];

  function heroState(t) {
    var k = E.inOutCubic((t - 0.2) / 1.45);
    var p = bezier(FLIGHT[0], FLIGHT[1], FLIGHT[2], FLIGHT[3], k);
    var ahead = bezier(FLIGHT[0], FLIGHT[1], FLIGHT[2], FLIGHT[3], Math.min(1, k + 0.02));
    var hover = E.smooth(1.4, 2.0, t);
    p = V.add(p, [Math.sin(t * 2.1) * 0.035 * hover, Math.sin(t * 3.3) * 0.055 * hover, 0]);
    var fly = V.norm(V.sub(ahead, p));
    var face = V.norm([Math.sin(t * 0.8) * 0.18 + 0.12, 0.05, 1]);
    var fwd = V.norm(V.lerp(k < 0.999 ? fly : face, face, E.smooth(1.15, 1.75, t)));
    var leave = E.smooth(5.4, 6.3, t);
    if (leave > 0) {
      var lk = leave * leave;
      p = V.add(p, [2.4 * lk, 1.5 * lk, 2.6 * lk]);
      fwd = V.norm(V.lerp(fwd, [0.6, 0.45, 0.65], E.smooth(5.35, 5.6, t)));
    }
    var bank = (1 - hover) * 0.45 * Math.sin(k * Math.PI) - leave * 0.4;
    var blink = Math.max(0, 1 - Math.abs(t - 1.95) / 0.08) + Math.max(0, 1 - Math.abs(t - 4.05) / 0.08);
    return { pos: p, quat: H.quatLook(fwd, [Math.sin(bank), Math.cos(bank), 0]), blink: Math.min(1, blink) };
  }

  function camera(t) {
    var drift = [Math.sin(t * 0.5) * 0.06, Math.sin(t * 0.37) * 0.04, 0];
    var eye = V.add([0.55 - 0.3 * E.outCubic(t / 5), 0.62, 8.8 - 0.9 * E.outCubic(t / 5)], drift);
    var target = [0.03, 0.26, -0.6];
    var dive = E.inOutCubic((t - 5.45) / 1.45);
    eye = V.lerp(eye, [0.0, 0.3, -1.0], dive);
    target = V.lerp(target, [0, 0.3, -4.5], dive);
    return { eye: eye, target: target, fov: 30 - dive * 4 };
  }

  var lastT = 0;
  function frame(t) {
    ctx.resize();
    var dt = Math.min(0.05, Math.max(0.001, t - lastT));
    lastT = t;
    var cam = camera(t);
    var proj = M.perspective(cam.fov * Math.PI / 180, ctx.w / ctx.h, 0.05, 60);
    var vp = M.mul(proj, M.lookAt(cam.eye, cam.target, [0, 1, 0]));

    // 蜂巢生长。
    for (var i = 0; i < grid.length; i++) {
      var c = grid[i], s0 = cellStart(c);
      var appear = E.outBack((t - s0) / 0.55);
      var flash = Math.max(0, 1 - (t - s0) / 1.1) * (t > s0 ? 1 : 0);
      var pulse = c.worker ? Math.pow(Math.max(0, Math.sin(t * 1.6 + c.seed * 30)), 8) * 0.6 : 0;
      var core = E.smooth(5.3, 6.5, t) * Math.max(0, 1 - c.d / 3);
      cells.set(i, c.x, c.z, c.h, c.fill * E.smooth(s0, s0 + 0.8, t), flash * 0.7 + pulse + core * 3.5, appear, c.seed, 0);
    }
    cells.upload(grid.length);

    // 主角。
    var hero = heroState(t);
    heroBee.set(0, hero.pos, 0.5, hero.quat, 0.3, hero.blink, 0);
    heroBee.upload(t >= 0.2 ? 1 : 0);

    glow.reset(); lines.reset();

    // 光迹：飞行途中持续撒下金色光点，逐渐暗去。
    if (t > 0.25 && t < 1.75) {
      for (var e = 0; e < 3; e++) {
        trail.push({ p: V.add(hero.pos, [(rnd() - 0.5) * 0.08, (rnd() - 0.5) * 0.08 - 0.05, (rnd() - 0.5) * 0.08]), t: t, s: 0.03 + rnd() * 0.05 });
      }
    }
    trail = trail.filter(function (q) { return t - q.t < 1.1; });
    trail.forEach(function (q) {
      var life = 1 - (t - q.t) / 1.1;
      glow.push(V.add(q.p, [0, -(t - q.t) * 0.12, 0]), q.s * (0.5 + life), GOLD, life * life * 0.9);
    });

    // 六边形标志：光迹勾边、顶点亮起；之后保持暗金描边，列阵时再次点亮。
    var draw = E.inOutCubic((t - 1.1) / 1.0) * 6;
    var logoAlpha = t < 2.3 ? 1 : 0.35 + 0.65 * E.smooth(4.3, 4.9, t);
    logoAlpha *= 1 - E.smooth(5.6, 6.2, t);
    for (var edge = 0; edge < 6 && edge < draw; edge++) {
      var a0 = logo[edge], a1 = logo[edge + 1], part = Math.min(1, draw - edge);
      var steps = Math.ceil(46 * part);
      for (var st = 0; st <= steps; st++) {
        var p = V.lerp(a0, a1, st / 46);
        glow.push(p, 0.035, GOLD, 0.55 * logoAlpha);
      }
      lines.push(a0, V.lerp(a0, a1, part), 0.9 * logoAlpha);
      var nodePop = E.outBack((draw - edge) / 0.4);
      glow.push(a0, 0.16 * nodePop, GOLD, 0.95 * logoAlpha);
      glow.push(a0, 0.5 * nodePop, GOLD, 0.18 * logoAlpha);
    }
    if (draw > 0 && draw < 6) {
      var ei = Math.floor(draw), head = V.lerp(logo[ei], logo[ei + 1], draw - ei);
      glow.push(head, 0.22, [1, 0.9, 0.7], 1);
      glow.push(head, 0.6, GOLD, 0.25);
    }

    // 蜂群。
    var formation = E.smooth(4.25, 4.9, t);
    var visible = 0, online = 0, positions = [], owners = [];
    for (i = 0; i < swarm.length; i++) {
      var b = swarm[i];
      if (t < b.born) continue;
      online++;
      if (!b.pos) { b.pos = cellMouth(b.cell, b.cell.h * 0.8); b.vel = [0, 0, 3.2]; }
      var ang = b.orbitP + t * b.orbitW;
      var orbit = V.add(hero.pos, V.add(V.scale(b.axisA, Math.cos(ang) * b.orbitR), V.scale(b.axisB, Math.sin(ang) * b.orbitR * 0.75)));
      var wobble = V.scale([Math.sin(t * 2.3 + b.wob) * 0.05, Math.sin(t * 3.1 + b.wob) * 0.06, 0], 1 - 0.7 * formation);
      var spread = 1 + E.smooth(5.45, 6.4, t) * 1.6;
      var slot = [HERO_HOME[0] + (b.slot[0] - HERO_HOME[0]) * spread, HERO_HOME[1] + (b.slot[1] - HERO_HOME[1]) * spread, b.slot[2]];
      var target = V.add(V.lerp(orbit, slot, formation), wobble);
      var age = t - b.born;
      var kStiff = 9 + 9 * formation, damp = 5.2 + 2 * formation;
      if (age < 0.35) kStiff *= age / 0.35;
      var acc = V.sub(V.scale(V.sub(target, b.pos), kStiff), V.scale(b.vel, damp));
      b.vel = V.add(b.vel, V.scale(acc, dt));
      var sp = V.len(b.vel);
      if (sp > 7) b.vel = V.scale(b.vel, 7 / sp);
      b.pos = V.add(b.pos, V.scale(b.vel, dt));
      var faceCam = V.norm(V.sub(cam.eye, b.pos));
      var fwd = V.norm(V.lerp(V.norm(b.vel), faceCam, Math.max(formation * 0.85, 1 - E.smooth(0.3, 2.0, sp))));
      var bank = Math.max(-0.6, Math.min(0.6, -b.vel[0] * 0.12)) * (1 - formation);
      var blink = Math.max(0, 1 - Math.abs(((t + b.blink) % 3.4) - 0.1) / 0.08);
      var scale = 0.12 * E.outBack(age / 0.35);
      swarmBees.set(visible++, b.pos, scale, H.quatLook(fwd, [Math.sin(bank), Math.cos(bank), 0]), b.phase, blink, 0.6);
      positions.push(b.pos); owners.push(b);
      glow.push(V.add(b.pos, [0, -0.06, -0.05]), 0.14, GOLD, 0.22 * E.smooth(0, 0.4, age));
    }
    swarmBees.upload(visible);

    // 协作网络：每只蜜蜂连到最近的两只，列阵时内环连向主角。
    var net = E.smooth(3.0, 3.6, t) * (1 - E.smooth(5.6, 6.1, t));
    if (net > 0) {
      for (i = 0; i < positions.length; i++) {
        var best = [-1, -1], bd = [1e9, 1e9];
        for (var j = 0; j < positions.length; j++) {
          if (j === i) continue;
          var d = V.len(V.sub(positions[i], positions[j]));
          if (d < bd[0]) { bd[1] = bd[0]; best[1] = best[0]; bd[0] = d; best[0] = j; } else if (d < bd[1]) { bd[1] = d; best[1] = j; }
        }
        for (var n = 0; n < 2; n++) {
          if (best[n] < 0 || best[n] < i || bd[n] > 2.2) continue;
          var q0 = positions[i], q1 = positions[best[n]];
          var la = net * (1 - bd[n] / 2.4);
          lines.push(q0, q1, la * 0.7);
          var f = (t * 0.9 + i * 0.137 + n * 0.5) % 1;
          glow.push(V.lerp(q0, q1, f), 0.05, [1, 0.85, 0.55], la * 0.9);
        }
        if (formation > 0.3 && owners[i].slotIndex % 5 === 0) {
          lines.push(positions[i], logo[owners[i].slotIndex / 5], net * (formation - 0.3) * 0.9);
        }
      }
    }

    // 花粉与前景虚化光斑。
    for (i = 0; i < motes.length; i++) {
      var m = motes[i];
      var y = ((m.p[1] + 4 + t * (0.05 + m.k * 0.08)) % 8) - 4;
      glow.push([m.p[0] + Math.sin(t * 0.4 + m.k * 9) * 0.15, y, m.p[2]], m.s, m.lilac ? LILAC : GOLD,
        (0.25 + 0.4 * m.k) * E.smooth(0, 0.8, t));
    }
    for (i = 0; i < bokeh.length; i++) {
      var bk = bokeh[i];
      glow.push([bk.p[0] + Math.sin(t * 0.3 + bk.k * 7) * 0.2, bk.p[1] + Math.sin(t * 0.25 + bk.k * 5) * 0.12, bk.p[2]],
        bk.s, bk.k < 0.35 ? LILAC : GOLD, 0.05 * E.smooth(0.3, 1.5, t));
    }

    // 绘制。
    var dive = E.smooth(5.5, 6.9, t);
    ctx.post.begin();
    Hive.clear(ctx, [0, 0, 0]);
    sky.draw(LOOK.sky);
    var common = { viewProj: vp, camPos: cam.eye, light: LOOK.light, time: t };
    cells.draw({ viewProj: vp, model: wallModel, camPos: cam.eye, light: LOOK.light, time: t, radius: R * 0.985, look: LOOK.cells,
      spot: [0, 0.3, WALL_Z + 1, 7.5 + dive * 20], spotMin: 0.1 });
    heroBee.draw({ viewProj: vp, camPos: cam.eye, light: LOOK.light, time: t, shells: 16, furLen: 0.075, furDensity: 64,
      flapAmp: 0.5, flapFreq: 26, look: LOOK.hero }, 'opaque');
    swarmBees.draw({ viewProj: vp, camPos: cam.eye, light: LOOK.light, time: t, shells: 3, furLen: 0.09, furDensity: 40,
      flapAmp: 0.6, flapFreq: 46, look: LOOK.swarm, lowDetail: true }, 'opaque');
    lines.draw(vp, [1.0, 0.74, 0.36, 0.55], true);
    glow.draw(vp, true, 1.0);
    heroBee.draw({ viewProj: vp, camPos: cam.eye, light: LOOK.light, time: t, flapAmp: 0.5, flapFreq: 26, look: LOOK.hero, additiveWings: true }, 'wings');
    swarmBees.draw({ viewProj: vp, camPos: cam.eye, light: LOOK.light, time: t, flapAmp: 0.6, flapFreq: 46, look: LOOK.swarm, additiveWings: true }, 'wings');
    ctx.post.end({ bloom: 0.9 + dive * 2.2, threshold: 0.5 - dive * 0.3, vignette: 0.9 * (1 - dive), white: E.smooth(6.05, 6.9, t),
      whiteCol: CREAM, time: t, exposure: 1 + dive * 0.6 });

    // 文字。
    root.classList.toggle('show-kicker', t > 1.9 && t < 5.6);
    root.classList.toggle('show-word', t > 4.4 && t < 5.65);
    root.classList.toggle('show-greet', t > 4.85 && t < 5.65);
    root.classList.toggle('show-count', t > 2.5 && t < 5.6);
    el.count.textContent = 'AGENTS ONLINE  ' + String(online).padStart(2, '0') + ' / ' + swarm.length;
  }

  // ---------------------------------------------------------------- 播放
  var t0 = 0, running = false;
  function loop(now) {
    if (!running) return;
    var t = (now - t0) / 1000;
    if (t >= TOTAL) {
      frame(TOTAL);
      running = false;
      if (params.has('loop')) { setTimeout(function () { reset(); play(); }, 600); return; }
      complete();
      return;
    }
    frame(t);
    requestAnimationFrame(loop);
  }
  function reset() {
    trail = []; lastT = 0;
    swarm.forEach(function (b) { b.pos = null; b.vel = [0, 0, 0]; });
  }
  function play(context) {
    if (running) return;
    el.greet.textContent = greeting(context && context.operator);
    running = true;
    t0 = performance.now();
    requestAnimationFrame(loop);
  }

  // 预览：?t=4.8 定格在某一时刻（逐帧推进物理以得到一致的蜂群位置）。
  window.HiveBoot = {
    canvas: canvas,
    renderAt: function (time) {
      reset();
      for (var x = 0; x <= time; x += 1 / 60) frame(Math.min(x, time));
      frame(time);
    }
  };

  if (params.has('t')) {
    el.greet.textContent = greeting('研究员');
    HiveBoot.renderAt(Number(params.get('t')));
    return;
  }
  if (window.WorkSwarmThemeSDK) WorkSwarmThemeSDK.onStart(play);
  if (window.parent === window) play({});
})();

/*
 * 蜂巢协同 · 共享 WebGL2 小引擎
 * 绒毛小蜜蜂（分层壳体毛发）、蜡质蜂巢与蜂蜜、光点、连线、天空与泛光后期。
 * 经典脚本（沙箱 iframe 中不能用 ES module），挂在 window.Hive。
 */
(function () {
  'use strict';

  var Hive = window.Hive = {};

  // ---------------------------------------------------------------- 数学
  var V = Hive.v3 = {
    add: function (a, b) { return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]; },
    sub: function (a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; },
    scale: function (a, s) { return [a[0] * s, a[1] * s, a[2] * s]; },
    dot: function (a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; },
    cross: function (a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; },
    len: function (a) { return Math.hypot(a[0], a[1], a[2]); },
    norm: function (a) { var l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l]; },
    lerp: function (a, b, t) { return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
  };

  var M = Hive.m4 = {
    identity: function () { return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]); },
    mul: function (a, b) {
      var o = new Float32Array(16);
      for (var c = 0; c < 4; c++) {
        for (var r = 0; r < 4; r++) {
          o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
        }
      }
      return o;
    },
    chain: function () {
      var m = arguments[0];
      for (var i = 1; i < arguments.length; i++) m = M.mul(m, arguments[i]);
      return m;
    },
    translate: function (x, y, z) { var m = M.identity(); m[12] = x; m[13] = y; m[14] = z; return m; },
    scale: function (x, y, z) {
      var m = M.identity(); m[0] = x; m[5] = y === undefined ? x : y; m[10] = z === undefined ? x : z; return m;
    },
    rotX: function (a) { var c = Math.cos(a), s = Math.sin(a), m = M.identity(); m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m; },
    rotY: function (a) { var c = Math.cos(a), s = Math.sin(a), m = M.identity(); m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m; },
    rotZ: function (a) { var c = Math.cos(a), s = Math.sin(a), m = M.identity(); m[0] = c; m[1] = s; m[4] = -s; m[5] = c; return m; },
    perspective: function (fovy, aspect, near, far) {
      var f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far), m = new Float32Array(16);
      m[0] = f / aspect; m[5] = f; m[10] = (far + near) * nf; m[11] = -1; m[14] = 2 * far * near * nf;
      return m;
    },
    lookAt: function (eye, target, up) {
      var z = V.norm(V.sub(eye, target)), x = V.norm(V.cross(up, z)), y = V.cross(z, x);
      return new Float32Array([
        x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
        -V.dot(x, eye), -V.dot(y, eye), -V.dot(z, eye), 1
      ]);
    },
    // 法线矩阵：取左上 3x3 的逆转置（部件只含旋转与正缩放）。
    normal3: function (m) {
      var a = m[0], b = m[1], c = m[2], d = m[4], e = m[5], f = m[6], g = m[8], h = m[9], i = m[10];
      var A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
      var det = a * A + b * B + c * C || 1;
      return new Float32Array([
        A / det, B / det, C / det,
        -(b * i - c * h) / det, (a * i - c * g) / det, -(a * h - b * g) / det,
        (b * f - c * e) / det, -(a * f - c * d) / det, (a * e - b * d) / det
      ]);
    }
  };

  // 由前向/上方向得到四元数（蜜蜂本地 +z 朝前、+y 朝上）。
  Hive.quatLook = function (forward, up) {
    var z = V.norm(forward), x = V.cross(up || [0, 1, 0], z);
    if (V.len(x) < 1e-4) x = V.cross([0, 0, 1], z);
    x = V.norm(x);
    var y = V.cross(z, x);
    var m00 = x[0], m01 = y[0], m02 = z[0], m10 = x[1], m11 = y[1], m12 = z[1], m20 = x[2], m21 = y[2], m22 = z[2];
    var t = m00 + m11 + m22, s;
    if (t > 0) {
      s = 0.5 / Math.sqrt(t + 1);
      return [(m21 - m12) * s, (m02 - m20) * s, (m10 - m01) * s, 0.25 / s];
    }
    if (m00 > m11 && m00 > m22) {
      s = 2 * Math.sqrt(1 + m00 - m11 - m22);
      return [0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s];
    }
    if (m11 > m22) {
      s = 2 * Math.sqrt(1 + m11 - m00 - m22);
      return [(m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s];
    }
    s = 2 * Math.sqrt(1 + m22 - m00 - m11);
    return [(m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s];
  };

  Hive.ease = {
    clamp: function (x) { return x < 0 ? 0 : x > 1 ? 1 : x; },
    smooth: function (a, b, x) { var t = Hive.ease.clamp((x - a) / (b - a)); return t * t * (3 - 2 * t); },
    outCubic: function (t) { t = Hive.ease.clamp(t); return 1 - Math.pow(1 - t, 3); },
    inOutCubic: function (t) { t = Hive.ease.clamp(t); return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; },
    outBack: function (t) { t = Hive.ease.clamp(t); var c = 1.6; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); }
  };

  // 可复现的伪随机数。
  Hive.rng = function (seed) {
    var s = seed >>> 0 || 1;
    return function () { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return (s >>> 0) / 4294967296; };
  };

  // 尖顶六边形网格（轴坐标），返回中心与到原点的环数。
  Hive.hexGrid = function (rings, radius) {
    var out = [];
    for (var q = -rings; q <= rings; q++) {
      for (var r = Math.max(-rings, -q - rings); r <= Math.min(rings, -q + rings); r++) {
        out.push({ q: q, r: r, d: (Math.abs(q) + Math.abs(r) + Math.abs(q + r)) / 2,
          x: radius * Math.sqrt(3) * (q + r / 2), z: radius * 1.5 * r });
      }
    }
    return out;
  };

  // ---------------------------------------------------------------- 几何
  function sphereGeom(seg, rings) {
    var pos = [], nrm = [], idx = [];
    for (var j = 0; j <= rings; j++) {
      var v = j / rings, phi = v * Math.PI;
      for (var i = 0; i <= seg; i++) {
        var u = i / seg, th = u * Math.PI * 2;
        var x = Math.sin(phi) * Math.cos(th), y = Math.cos(phi), z = Math.sin(phi) * Math.sin(th);
        pos.push(x, y, z); nrm.push(x, y, z);
      }
    }
    for (j = 0; j < rings; j++) {
      for (i = 0; i < seg; i++) {
        var a = j * (seg + 1) + i, b = a + seg + 1;
        idx.push(a, b, a + 1, b, b + 1, a + 1);
      }
    }
    return { pos: pos, nrm: nrm, idx: idx };
  }

  // 半径 1、沿 +y 从 0 到 1 的圆柱侧面（触角）。
  function cylinderGeom(seg) {
    var pos = [], nrm = [], idx = [];
    for (var j = 0; j <= 1; j++) {
      for (var i = 0; i <= seg; i++) {
        var th = i / seg * Math.PI * 2, x = Math.cos(th), z = Math.sin(th);
        pos.push(x, j, z); nrm.push(x, 0, z);
      }
    }
    for (i = 0; i < seg; i++) idx.push(i, i + seg + 1, i + 1, i + seg + 1, i + seg + 2, i + 1);
    return { pos: pos, nrm: nrm, idx: idx };
  }

  // 翅膀：XY 平面上的椭圆片，根部在原点、沿 +y 伸展，长 1。
  function wingGeom(seg) {
    var pos = [0, 0.5, 0], nrm = [0, 0, 1], idx = [];
    for (var i = 0; i <= seg; i++) {
      var th = i / seg * Math.PI * 2;
      // 根部收窄，尖端圆润。
      var y = 0.5 - Math.cos(th) * 0.5, w = Math.sin(th) * 0.5 * (0.55 + 0.45 * Math.sqrt(y));
      pos.push(w, y, 0); nrm.push(0, 0, 1);
    }
    for (i = 1; i <= seg; i++) idx.push(0, i, i + 1);
    return { pos: pos, nrm: nrm, idx: idx };
  }

  // 空心蜂巢格：外半径 1、内半径 0.84、高 0..1，顶部带倒角。逐面平直法线。
  function hexTubeGeom() {
    var pos = [], nrm = [], idx = [];
    var profile = [[1, 0], [1, 0.965], [0.965, 1], [0.875, 1], [0.84, 0.965], [0.84, 0]];
    for (var k = 0; k < 6; k++) {
      var a0 = (30 + 60 * k) * Math.PI / 180, a1 = (90 + 60 * k) * Math.PI / 180, am = (60 + 60 * k) * Math.PI / 180;
      var c0 = [Math.cos(a0), Math.sin(a0)], c1 = [Math.cos(a1), Math.sin(a1)], side = [Math.cos(am), Math.sin(am)];
      for (var s = 0; s < profile.length - 1; s++) {
        var p0 = profile[s], p1 = profile[s + 1];
        var dr = p1[0] - p0[0], dy = p1[1] - p0[1], l = Math.hypot(dr, dy);
        var nr = dy / l, ny = -dr / l;
        var n = [side[0] * nr, ny, side[1] * nr];
        var base = pos.length / 3;
        pos.push(c0[0] * p0[0], p0[1], c0[1] * p0[0], c1[0] * p0[0], p0[1], c1[1] * p0[0],
          c1[0] * p1[0], p1[1], c1[1] * p1[0], c0[0] * p1[0], p1[1], c0[1] * p1[0]);
        for (var q = 0; q < 4; q++) nrm.push(n[0], n[1], n[2]);
        idx.push(base, base + 1, base + 2, base, base + 2, base + 3);
      }
    }
    return { pos: pos, nrm: nrm, idx: idx };
  }

  // 蜂蜜面：内半径的六边形片，y = 0（顶点着色器里抬到蜂蜜高度）。
  function hexDiscGeom() {
    var pos = [0, 0, 0], nrm = [0, 1, 0], idx = [];
    for (var k = 0; k <= 6; k++) {
      var a = (30 + 60 * k) * Math.PI / 180;
      pos.push(Math.cos(a) * 0.84, 0, Math.sin(a) * 0.84); nrm.push(0, 1, 0);
    }
    for (k = 1; k <= 6; k++) idx.push(0, k, k + 1);
    return { pos: pos, nrm: nrm, idx: idx };
  }

  // ---------------------------------------------------------------- GLSL
  var COMMON = [
    'uniform vec3 uCamPos;',
    'uniform vec3 uKeyDir, uKeyCol, uFillDir, uFillCol, uRimDir, uRimCol, uSkyAmb, uGroundAmb;',
    'uniform vec3 uFogCol; uniform vec2 uFog;',
    'float hash13(vec3 p) { p = fract(p * 0.1031); p += dot(p, p.zyx + 31.32); return fract((p.x + p.y) * p.z); }',
    'vec3 shade(vec3 albedo, vec3 n, vec3 v, float wrap, float specPow, float specAmt, float rimAmt) {',
    '  vec3 amb = mix(uGroundAmb, uSkyAmb, n.y * 0.5 + 0.5);',
    '  float dk = max(0.0, (dot(n, uKeyDir) + wrap) / (1.0 + wrap));',
    '  float df = max(0.0, (dot(n, uFillDir) + wrap) / (1.0 + wrap));',
    '  float sp = pow(max(dot(n, normalize(uKeyDir + v)), 0.0), specPow) * specAmt;',
    '  float fr = pow(1.0 - max(dot(n, v), 0.0), 3.0);',
    '  float rl = max(0.0, dot(n, uRimDir) * 0.5 + 0.5);',
    '  return albedo * (amb + uKeyCol * dk + uFillCol * df) + uKeyCol * sp + uRimCol * fr * rl * rimAmt;',
    '}',
    'vec3 applyFog(vec3 col, vec3 world) {',
    '  float d = length(world - uCamPos);',
    '  float f = 1.0 - exp(-max(0.0, d - uFog.y) * uFog.x);',
    '  return mix(col, uFogCol, clamp(f, 0.0, 1.0));',
    '}'
  ].join('\n');

  var BEE_VS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNrm;',
    'layout(location=2) in vec4 iPos; layout(location=3) in vec4 iQuat; layout(location=4) in vec4 iMisc;',
    'uniform mat4 uViewProj, uPart; uniform mat3 uPartN;',
    'uniform int uKind; uniform float uMirror, uShell, uFurLen, uTime, uFlapAmp, uFlapFreq;',
    'uniform vec3 uHinge;',
    'out vec3 vWorld, vNrm, vLocal, vLN; out vec2 vUV; out vec4 vMisc;',
    'vec3 qrot(vec4 q, vec3 v) { return v + 2.0 * cross(q.xyz, cross(q.xyz, v) + q.w * v); }',
    'vec3 rotZ(vec3 p, float a) { float c = cos(a), s = sin(a); return vec3(c * p.x - s * p.y, s * p.x + c * p.y, p.z); }',
    'void main() {',
    '  vec3 p = (uPart * vec4(aPos, 1.0)).xyz; vec3 n = normalize(uPartN * aNrm);',
    '  if (uKind == 4) {',
    '    float ang = uFlapAmp * sin(uTime * uFlapFreq + iMisc.x);',
    '    p = rotZ(p - uHinge, -ang) + uHinge; n = rotZ(n, -ang);',
    '  }',
    '  if (uKind == 0) p += n * uShell * uFurLen;',
    '  if (uKind == 1 || uKind == 2) { float c = uPart[3].y; p.y = c + (p.y - c) * (1.0 - 0.92 * iMisc.y); }',
    '  p.x *= uMirror; n.x *= uMirror;',
    '  vLocal = p; vLN = n; vUV = aPos.xy; vMisc = iMisc;',
    '  vec3 w = qrot(iQuat, p * iPos.w) + iPos.xyz;',
    '  vWorld = w; vNrm = qrot(iQuat, n);',
    '  gl_Position = uViewProj * vec4(w, 1.0);',
    '}'
  ].join('\n');

  var BEE_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    COMMON,
    'in vec3 vWorld, vNrm, vLocal, vLN; in vec2 vUV; in vec4 vMisc;',
    'uniform int uKind; uniform float uShell, uTime, uFurDensity, uBaseAO, uWingAlpha, uGlow;',
    'uniform vec3 uBodyScale, uFur, uFurDark, uEmissive, uWingCol, uWingCol2;',
    'out vec4 outColor;',
    'float band(float z, float a, float b) { return smoothstep(a - 0.025, a + 0.025, z) * (1.0 - smoothstep(b - 0.025, b + 0.025, z)); }',
    'vec3 furColor(vec3 lp) {',
    '  float s = band(lp.z, -0.40, -0.10) + band(lp.z, -0.80, -0.58) + smoothstep(-0.90, -0.94, lp.z);',
    '  vec3 c = mix(uFur, uFurDark, clamp(s, 0.0, 1.0));',
    '  if (lp.z > 0.3) {',
    '    vec2 xy = lp.xy;',
    '    float blush = smoothstep(0.19, 0.0, length(vec2(abs(xy.x) - 0.60, xy.y + 0.27)));',
    '    c = mix(c, vec3(1.0, 0.52, 0.46), blush * 0.8);',
    '    vec2 m = xy - vec2(0.0, -0.11);',
    '    float smile = smoothstep(0.024, 0.012, abs(length(m) - 0.12)) * step(m.y, -0.055) * step(abs(m.x), 0.085);',
    '    float mouth = step(length(m), 0.12) * step(m.y, -0.085) * step(abs(m.x), 0.05) * 0.0;',
    '    c = mix(c, vec3(0.22, 0.07, 0.03), max(smile, mouth));',
    '    vec2 b = vec2(abs(xy.x) - 0.37, xy.y - 0.36);',
    '    float brow = smoothstep(0.022, 0.010, abs(length(b) - 0.09)) * step(0.045, b.y) * step(abs(b.x), 0.05);',
    '    c = mix(c, vec3(0.18, 0.08, 0.03), brow);',
    '  }',
    '  return c;',
    '}',
    'void main() {',
    '  vec3 n = normalize(vNrm); if (!gl_FrontFacing && uKind == 4) n = -n;',
    '  vec3 v = normalize(uCamPos - vWorld);',
    '  vec3 col; float alpha = 1.0;',
    '  if (uKind == 0) {',
    '    vec3 lp = normalize(vLocal / uBodyScale);',
    '    vec3 c = furColor(lp);',
    '    vec2 e = vec2(abs(lp.x) - 0.36, lp.y - 0.05);',
    '    float ao = mix(uBaseAO, 0.97, smoothstep(0.40, 0.29, length(e)) * step(0.5, lp.z));',
    '    if (uShell > 0.0) {',
    '      if (lp.z > 0.5 && length(e) < 0.288) discard;',
    '      vec3 q = lp * uFurDensity;',
    '      float h = hash13(floor(q));',
    '      float rr = length(fract(q) - 0.5);',
    '      float face = smoothstep(0.55, 0.95, lp.z) * 0.45;',
    '      if (h < uShell * (0.9 + face) || rr > 0.66 * (1.0 - uShell * 0.75)) discard;',
    '      ao = mix(0.62, 1.1, uShell);',
    '    }',
    '    col = shade(c * ao, n, v, 0.6, 14.0, 0.05, 1.0);',
    '    col += uFur * uRimCol * pow(1.0 - max(dot(n, v), 0.0), 2.0) * 0.35 * uShell;',
    '  } else if (uKind == 1) {',
    '    col = shade(vec3(0.97, 0.96, 0.95), n, v, 0.35, 70.0, 0.55, 0.25);',
    '  } else if (uKind == 2) {',
    '    col = shade(vec3(0.025, 0.02, 0.035), n, v, 0.2, 90.0, 0.9, 0.3);',
    '    vec3 ln = normalize(vLN);',
    '    float h1 = smoothstep(0.935, 0.955, dot(ln, normalize(vec3(-0.40, 0.48, 0.78))));',
    '    float h2 = smoothstep(0.985, 0.992, dot(ln, normalize(vec3(0.30, -0.34, 0.89)))) * 0.75;',
    '    col = mix(col, vec3(1.0), max(h1, h2));',
    '  } else if (uKind == 3) {',
    '    col = shade(vec3(0.035, 0.03, 0.03), n, v, 0.3, 40.0, 0.4, 0.7);',
    '  } else {',
    '    vec2 uv = vUV;',
    '    float r = length(vec2(uv.x * 2.1, (uv.y - 0.5) * 2.0));',
    '    float edge = smoothstep(0.78, 1.0, r);',
    '    float a = atan(uv.x, uv.y + 0.12);',
    '    float vein = smoothstep(0.06, 0.0, abs(fract(a * 2.4 + 0.5) - 0.5)) * smoothstep(0.1, 0.3, uv.y);',
    '    vein = max(vein, smoothstep(0.018, 0.0, abs(uv.y - 0.62 - uv.x * 0.3)) * 0.8);',
    '    float fr = 1.0 - abs(dot(n, v));',
    '    vec3 c = mix(uWingCol, uWingCol2, 0.5 + 0.5 * sin(fr * 5.0 + uv.y * 4.0 + uv.x * 3.0));',
    '    float sp = step(0.988, hash13(vec3(floor(uv * 34.0), 1.0))) * (0.6 + 0.4 * sin(uTime * 5.0 + hash13(vec3(floor(uv * 34.0), 7.0)) * 40.0));',
    '    col = c * (uSkyAmb + uKeyCol * 0.8) + vec3(sp) * 0.9 + uRimCol * fr * 0.3;',
    '    alpha = clamp(0.24 + 0.3 * fr + 0.55 * edge + 0.32 * vein + sp * 0.6, 0.0, 0.94) * uWingAlpha;',
    '  }',
    '  col += uEmissive * vMisc.z;',
    '  col = applyFog(col, vWorld);',
    '  outColor = vec4(col, alpha);',
    '}'
  ].join('\n');

  var CELL_VS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNrm;',
    'layout(location=2) in vec4 iA; layout(location=3) in vec4 iB;',
    'uniform mat4 uViewProj, uModel; uniform float uRadius; uniform int uPass;',
    'out vec3 vWorld, vNrm, vLocal, vLN; out vec4 vA, vB;',
    'void main() {',
    '  float a = clamp(iB.y, 0.0, 1.0);',
    '  float s = smoothstep(0.0, 0.35, a);',
    '  float h = iA.z * a;',
    '  vec3 lp = aPos; if (uPass == 1) lp.y = iA.w;',
    '  vec3 p = vec3(lp.x * uRadius * s + iA.x, lp.y * h + iB.w, lp.z * uRadius * s + iA.y);',
    '  vec4 w = uModel * vec4(p, 1.0);',
    '  vWorld = w.xyz; vNrm = normalize(mat3(uModel) * normalize(vec3(aNrm.x, aNrm.y * uRadius / max(h, 0.05), aNrm.z)));',
    '  vLN = aNrm; vLocal = lp; vA = iA; vB = iB;',
    '  gl_Position = uViewProj * w;',
    '}'
  ].join('\n');

  var CELL_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    COMMON,
    'in vec3 vWorld, vNrm, vLocal, vLN; in vec4 vA, vB;',
    'uniform int uPass; uniform float uTime, uHoneyGlow, uSpotMin; uniform vec4 uSpot;',
    'uniform vec3 uWax, uWaxDeep, uRimHi, uHoney, uHoneyHi;',
    'out vec4 outColor;',
    'void main() {',
    '  vec3 n = normalize(vNrm); vec3 v = normalize(uCamPos - vWorld);',
    '  float glow = vB.x; float fill = vA.w; vec3 col;',
    '  if (uPass == 0) {',
    '    float inner = step(dot(vLN.xz, vLocal.xz), 0.0) * step(abs(vLN.y), 0.5);',
    '    float top = smoothstep(0.4, 0.9, vLN.y);',
    '    float y = vLocal.y;',
    '    vec3 c = mix(uWax, uWaxDeep, inner * pow(clamp(1.0 - y, 0.0, 1.0), 1.3) * 0.9);',
    '    col = shade(c, n, v, 0.55, 26.0, 0.06 + 0.25 * top, 0.55);',
    '    float bounce = inner * smoothstep(0.42, 0.0, y - fill) * step(fill, y + 0.02) * (0.25 + glow);',
    '    col += uHoney * bounce * uHoneyGlow * 0.55;',
    '    col += uRimHi * top * (0.10 + 0.25 * glow * uHoneyGlow);',
    '    col += uWax * uKeyCol * max(0.0, dot(-n, uKeyDir)) * 0.28;',
    '  } else {',
    '    vec2 q = abs(vLocal.xz);',
    '    float hx = max(q.x * 0.8660 + q.y * 0.5, q.y) / 0.84;',
    '    float men = smoothstep(0.62, 1.0, hx);',
    '    float caus = 0.5 + 0.5 * sin(vLocal.x * 9.0 + uTime * 0.7 + vB.z * 6.0) * sin(vLocal.z * 8.0 - uTime * 0.5);',
    '    if (fill < 0.06) {',
    '      col = shade(mix(uWaxDeep, uWax, 0.3) * (0.92 - 0.22 * men), n, v, 0.5, 20.0, 0.04, 0.2);',
    '    } else {',
    '      col = shade(uHoney * (1.0 - 0.45 * men), n, v, 0.2, 110.0, 1.0, 0.5);',
    '      col += uHoneyHi * (0.18 + 0.12 * caus + glow) * uHoneyGlow * (1.0 - 0.6 * men);',
    '    }',
    '  }',
    '  vec3 sd = vWorld - uSpot.xyz;',
    '  col *= mix(uSpotMin, 1.0, exp(-dot(sd, sd) / max(uSpot.w, 1e-3)));',
    '  col = applyFog(col, vWorld);',
    '  outColor = vec4(col, 1.0);',
    '}'
  ].join('\n');

  var POINT_VS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'layout(location=0) in vec4 aPos; layout(location=1) in vec4 aCol;',
    'uniform mat4 uViewProj; uniform float uPx;',
    'out vec4 vCol;',
    'void main() {',
    '  vec4 c = uViewProj * vec4(aPos.xyz, 1.0);',
    '  gl_Position = c; gl_PointSize = clamp(aPos.w * uPx / max(c.w, 0.05), 1.0, 256.0); vCol = aCol;',
    '}'
  ].join('\n');

  var POINT_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'in vec4 vCol; out vec4 outColor;',
    'void main() {',
    '  vec2 d = gl_PointCoord * 2.0 - 1.0; float r = dot(d, d); if (r > 1.0) discard;',
    '  float a = exp(-r * 3.2) * (1.0 - r);',
    '  outColor = vec4(vCol.rgb, vCol.a * a);',
    '}'
  ].join('\n');

  var LINE_VS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'layout(location=0) in vec4 aPos;',
    'uniform mat4 uViewProj; out float vA;',
    'void main() { gl_Position = uViewProj * vec4(aPos.xyz, 1.0); vA = aPos.w; }'
  ].join('\n');

  var LINE_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'in float vA; uniform vec4 uColor; out vec4 outColor;',
    'void main() { outColor = vec4(uColor.rgb, uColor.a * vA); }'
  ].join('\n');

  var FULL_VS = [
    '#version 300 es',
    'out vec2 vUV;',
    'void main() { vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); vUV = p; gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }'
  ].join('\n');

  var SKY_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'in vec2 vUV; out vec4 outColor;',
    'uniform vec3 uTop, uBottom, uSunCol, uHazeCol; uniform vec2 uSun, uHaze, uAspect; uniform float uSunSize, uHazeSize, uRadial;',
    'void main() {',
    '  vec2 uv = vUV;',
    '  vec3 c = mix(uBottom, uTop, smoothstep(0.0, 1.0, uv.y));',
    '  vec2 d = (uv - vec2(0.5, 0.55)) * uAspect;',
    '  c = mix(c, uBottom, uRadial * smoothstep(0.0, 1.0, length(d)));',
    '  c += uSunCol * exp(-dot((uv - uSun) * uAspect, (uv - uSun) * uAspect) / (uSunSize * uSunSize));',
    '  c = mix(c, uHazeCol, 0.6 * exp(-dot((uv - uHaze) * uAspect, (uv - uHaze) * uAspect) / (uHazeSize * uHazeSize)));',
    '  outColor = vec4(c, 1.0);',
    '}'
  ].join('\n');

  var BRIGHT_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'in vec2 vUV; uniform sampler2D uTex; uniform float uThreshold; out vec4 outColor;',
    'void main() {',
    '  vec3 c = texture(uTex, vUV).rgb; float l = dot(c, vec3(0.2126, 0.7152, 0.0722));',
    '  float k = smoothstep(uThreshold, uThreshold + 0.25, l);',
    '  outColor = vec4(c * k, 1.0);',
    '}'
  ].join('\n');

  var BLUR_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'in vec2 vUV; uniform sampler2D uTex; uniform vec2 uDir; out vec4 outColor;',
    'void main() {',
    '  vec3 c = texture(uTex, vUV).rgb * 0.2270;',
    '  c += (texture(uTex, vUV + uDir * 1.3846).rgb + texture(uTex, vUV - uDir * 1.3846).rgb) * 0.3162;',
    '  c += (texture(uTex, vUV + uDir * 3.2308).rgb + texture(uTex, vUV - uDir * 3.2308).rgb) * 0.0703;',
    '  outColor = vec4(c, 1.0);',
    '}'
  ].join('\n');

  var COMPOSE_FS = [
    '#version 300 es',
    'precision highp float; precision highp int;',
    'in vec2 vUV; out vec4 outColor;',
    'uniform sampler2D uScene, uBloomA, uBloomB; uniform float uBloom, uVignette, uWhite, uTime, uExposure; uniform vec3 uWhiteCol;',
    'void main() {',
    '  vec3 c = texture(uScene, vUV).rgb * uExposure;',
    '  c += (texture(uBloomA, vUV).rgb * 0.7 + texture(uBloomB, vUV).rgb * 1.1) * uBloom;',
    '  vec2 d = vUV - 0.5; c *= 1.0 - uVignette * dot(d, d) * 1.6;',
    '  c = c / (1.0 + max(c - 1.0, 0.0) * 0.6);',
    '  c += (fract(sin(dot(vUV * 1000.0 + uTime, vec2(12.9898, 78.233))) * 43758.5453) - 0.5) * 0.012;',
    '  c = mix(c, uWhiteCol, uWhite);',
    '  outColor = vec4(c, 1.0);',
    '}'
  ].join('\n');

  // ---------------------------------------------------------------- GL 工具
  function compile(gl, type, src) {
    var sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh) + '\n' + src.split('\n').slice(0, 3).join('\n'));
    return sh;
  }

  function Program(gl, vs, fs) {
    var p = gl.createProgram();
    gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
    gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    var uniforms = {};
    var count = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < count; i++) {
      var info = gl.getActiveUniform(p, i);
      uniforms[info.name.replace(/\[0\]$/, '')] = { loc: gl.getUniformLocation(p, info.name), type: info.type };
    }
    this.gl = gl; this.p = p; this.uniforms = uniforms;
  }
  Program.prototype.use = function (values) {
    var gl = this.gl;
    gl.useProgram(this.p);
    if (values) this.set(values);
    return this;
  };
  Program.prototype.set = function (values) {
    var gl = this.gl;
    for (var name in values) {
      var u = this.uniforms[name];
      if (!u) continue;
      var v = values[name];
      switch (u.type) {
        case gl.FLOAT: gl.uniform1f(u.loc, v); break;
        case gl.FLOAT_VEC2: gl.uniform2fv(u.loc, v); break;
        case gl.FLOAT_VEC3: gl.uniform3fv(u.loc, v); break;
        case gl.FLOAT_VEC4: gl.uniform4fv(u.loc, v); break;
        case gl.FLOAT_MAT3: gl.uniformMatrix3fv(u.loc, false, v); break;
        case gl.FLOAT_MAT4: gl.uniformMatrix4fv(u.loc, false, v); break;
        default: gl.uniform1i(u.loc, v);
      }
    }
    return this;
  };

  // 网格 + 实例缓冲（实例属性从 location 2 起，每个 vec4 一个槽位）。
  function makeVAO(gl, geom, instanceBuffer, instanceVec4s) {
    var vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    var pb = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, pb);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(geom.pos), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    var nb = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, nb);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(geom.nrm), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
    if (instanceBuffer) {
      gl.bindBuffer(gl.ARRAY_BUFFER, instanceBuffer);
      for (var i = 0; i < instanceVec4s; i++) {
        gl.enableVertexAttribArray(2 + i);
        gl.vertexAttribPointer(2 + i, 4, gl.FLOAT, false, instanceVec4s * 16, i * 16);
        gl.vertexAttribDivisor(2 + i, 1);
      }
    }
    var ib = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(geom.idx), gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    return { vao: vao, count: geom.idx.length };
  }

  // ---------------------------------------------------------------- 上下文
  Hive.create = function (canvas, options) {
    options = options || {};
    var gl = canvas.getContext('webgl2', {
      antialias: !options.post, alpha: false, depth: true, premultipliedAlpha: false,
      preserveDrawingBuffer: !!options.preserve, powerPreference: options.power || 'default'
    });
    if (!gl) return null;
    var ctx = { gl: gl, canvas: canvas, w: 1, h: 1, dpr: 1, options: options };
    ctx.resize = function () {
      var dpr = Math.min(window.devicePixelRatio || 1, options.dprCap || 1.5) * (options.scale || 1);
      // 大窗口按像素总数封顶，避免 4K 屏上后期与多重采样过重。
      var area = canvas.clientWidth * canvas.clientHeight * dpr * dpr, cap = options.maxPixels || 2.4e6;
      if (area > cap) dpr *= Math.sqrt(cap / area);
      var w = Math.max(2, Math.round(canvas.clientWidth * dpr)), h = Math.max(2, Math.round(canvas.clientHeight * dpr));
      if (w === ctx.w && h === ctx.h) return false;
      canvas.width = w; canvas.height = h; ctx.w = w; ctx.h = h; ctx.dpr = dpr;
      if (ctx.post) ctx.post.resize(w, h);
      return true;
    };
    ctx.fullVAO = gl.createVertexArray();
    if (options.post) ctx.post = new Post(ctx);
    ctx.resize();
    return ctx;
  };

  // 灯光/雾效统一参数（各程序共用 COMMON）。
  Hive.lighting = function (o) {
    function nz(v) { return V.norm(v); }
    return {
      uKeyDir: nz(o.keyDir), uKeyCol: o.keyCol, uFillDir: nz(o.fillDir), uFillCol: o.fillCol,
      uRimDir: nz(o.rimDir), uRimCol: o.rimCol, uSkyAmb: o.skyAmb, uGroundAmb: o.groundAmb,
      uFogCol: o.fogCol, uFog: o.fog
    };
  };

  Hive.rgb = function (hex, k) {
    var n = parseInt(hex.replace('#', ''), 16), s = k === undefined ? 1 : k;
    return [((n >> 16) & 255) / 255 * s, ((n >> 8) & 255) / 255 * s, (n & 255) / 255 * s];
  };

  // ---------------------------------------------------------------- 天空
  Hive.Sky = function (ctx) {
    this.ctx = ctx;
    this.prog = new Program(ctx.gl, FULL_VS, SKY_FS);
  };
  Hive.Sky.prototype.draw = function (o) {
    var gl = this.ctx.gl, a = this.ctx.w / this.ctx.h;
    gl.disable(gl.DEPTH_TEST); gl.depthMask(false); gl.disable(gl.BLEND);
    this.prog.use({
      uTop: o.top, uBottom: o.bottom, uSunCol: o.sunCol || [0, 0, 0], uHazeCol: o.hazeCol || o.top,
      uSun: o.sun || [0.8, 0.9], uHaze: o.haze || [0.2, 0.9], uAspect: [a, 1], uSunSize: o.sunSize || 0.3,
      uHazeSize: o.hazeSize || 0.4, uRadial: o.radial || 0
    });
    gl.bindVertexArray(this.ctx.fullVAO);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.depthMask(true);
  };

  // ---------------------------------------------------------------- 蜜蜂
  var BEE_FLOATS = 12;

  function partMatrices() {
    var T = M.translate, S = M.scale;
    function stalk(base, tip, r) {
      var d = V.sub(tip, base), l = V.len(d), dir = V.norm(d);
      var ang = Math.acos(Math.max(-1, Math.min(1, dir[1])));
      var axis = V.norm(V.cross([0, 1, 0], dir));
      // 绕任意轴旋转（Rodrigues）。
      var c = Math.cos(ang), s = Math.sin(ang), t = 1 - c, x = axis[0], y = axis[1], z = axis[2];
      var R = new Float32Array([t * x * x + c, t * x * y + s * z, t * x * z - s * y, 0,
        t * x * y - s * z, t * y * y + c, t * y * z + s * x, 0,
        t * x * z + s * y, t * y * z - s * x, t * z * z + c, 0, 0, 0, 0, 1]);
      return M.chain(T(base[0], base[1], base[2]), R, S(r, l, r));
    }
    var antennaBase = [0.2, 0.84, 0.3], antennaTip = [0.31, 1.4, 0.36];
    var foreHinge = [0.26, 0.6, -0.42], hindHinge = [0.3, 0.44, -0.58];
    return {
      body: S(1.04, 0.96, 1.0),
      eye: M.chain(T(0.36, 0.05, 0.80), S(0.275, 0.30, 0.19)),
      pupil: M.chain(T(0.335, 0.025, 0.925), S(0.185, 0.205, 0.105)),
      stalk: stalk(antennaBase, antennaTip, 0.03),
      tip: M.chain(T(antennaTip[0], antennaTip[1], antennaTip[2]), S(0.105)),
      arm: M.chain(T(0.45, -0.78, 0.5), M.rotZ(0.4), S(0.22, 0.15, 0.24)),
      foreWing: M.chain(T(foreHinge[0], foreHinge[1], foreHinge[2]), M.rotY(-0.55), M.rotX(-0.62), M.rotZ(-0.42), S(0.62, 1.06, 1)),
      hindWing: M.chain(T(hindHinge[0], hindHinge[1], hindHinge[2]), M.rotY(-0.7), M.rotX(-0.95), M.rotZ(-0.95), S(0.46, 0.8, 1)),
      foreHinge: foreHinge, hindHinge: hindHinge
    };
  }

  Hive.Bees = function (ctx, max) {
    var gl = ctx.gl;
    this.ctx = ctx; this.max = max; this.count = 0;
    this.data = new Float32Array(max * BEE_FLOATS);
    this.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, this.data.byteLength, gl.DYNAMIC_DRAW);
    this.prog = new Program(gl, BEE_VS, BEE_FS);
    this.sphere = makeVAO(gl, sphereGeom(40, 28), this.buffer, 3);
    this.lowSphere = makeVAO(gl, sphereGeom(16, 12), this.buffer, 3);
    this.cyl = makeVAO(gl, cylinderGeom(10), this.buffer, 3);
    this.wing = makeVAO(gl, wingGeom(40), this.buffer, 3);
    this.parts = partMatrices();
  };
  // 写入第 i 只蜜蜂：位置、缩放、四元数、翅膀相位、眨眼(0-1)、自发光。
  Hive.Bees.prototype.set = function (i, pos, scale, quat, phase, blink, glow) {
    var d = this.data, o = i * BEE_FLOATS;
    d[o] = pos[0]; d[o + 1] = pos[1]; d[o + 2] = pos[2]; d[o + 3] = scale;
    d[o + 4] = quat[0]; d[o + 5] = quat[1]; d[o + 6] = quat[2]; d[o + 7] = quat[3];
    d[o + 8] = phase || 0; d[o + 9] = blink || 0; d[o + 10] = glow || 0; d[o + 11] = 0;
  };
  Hive.Bees.prototype.upload = function (count) {
    var gl = this.ctx.gl;
    this.count = count;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.data, 0, count * BEE_FLOATS);
  };
  Hive.Bees.prototype.drawPart = function (mesh, kind, matrix, mirror, extra) {
    var gl = this.ctx.gl, p = this.prog;
    var u = { uKind: kind, uPart: matrix, uPartN: M.normal3(matrix), uMirror: mirror };
    if (extra) for (var k in extra) u[k] = extra[k];
    p.set(u);
    gl.bindVertexArray(mesh.vao);
    gl.drawElementsInstanced(gl.TRIANGLES, mesh.count, gl.UNSIGNED_SHORT, 0, this.count);
  };
  // o: { viewProj, camPos, light, time, shells, furLen, furDensity, look: {...} }
  Hive.Bees.prototype.draw = function (o, pass) {
    if (!this.count) return;
    var gl = this.ctx.gl, P = this.parts, look = o.look || {};
    var common = {
      uViewProj: o.viewProj, uCamPos: o.camPos, uTime: o.time, uShell: 0, uFurLen: o.furLen || 0.07,
      uFurDensity: o.furDensity || 70, uBaseAO: o.shells ? 0.62 : 1.0, uBodyScale: [1.04, 0.96, 1.0],
      uFur: look.fur || Hive.rgb('#ffb21f'), uFurDark: look.furDark || Hive.rgb('#1c130c'),
      uEmissive: look.emissive || [0, 0, 0], uWingCol: look.wing || Hive.rgb('#cdbcff'),
      uWingCol2: look.wing2 || Hive.rgb('#a9e4ff'), uWingAlpha: look.wingAlpha || 1,
      uFlapAmp: o.flapAmp || 0.5, uFlapFreq: o.flapFreq || 30, uHinge: [0, 0, 0]
    };
    for (var k in o.light) common[k] = o.light[k];
    var sphere = o.lowDetail ? this.lowSphere : this.sphere;
    this.prog.use(common);
    if (pass !== 'wings') {
      gl.enable(gl.DEPTH_TEST); gl.depthMask(true); gl.disable(gl.BLEND); gl.disable(gl.CULL_FACE);
      this.drawPart(sphere, 0, P.body, 1);
      var shells = o.shells || 0;
      for (var s = 1; s <= shells; s++) this.drawPart(sphere, 0, P.body, 1, { uShell: s / shells });
      this.prog.set({ uShell: 0 });
      for (var m = -1; m <= 1; m += 2) {
        this.drawPart(sphere, 1, P.eye, m);
        this.drawPart(sphere, 2, P.pupil, m);
        this.drawPart(this.cyl, 3, P.stalk, m);
        this.drawPart(this.lowSphere, 3, P.tip, m);
        this.drawPart(this.lowSphere, 3, P.arm, m);
      }
    }
    if (pass !== 'opaque') {
      gl.enable(gl.DEPTH_TEST); gl.depthMask(false); gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, o.additiveWings ? gl.ONE : gl.ONE_MINUS_SRC_ALPHA);
      for (m = -1; m <= 1; m += 2) {
        this.drawPart(this.wing, 4, P.hindWing, m, { uHinge: P.hindHinge });
        this.drawPart(this.wing, 4, P.foreWing, m, { uHinge: P.foreHinge });
      }
      gl.depthMask(true); gl.disable(gl.BLEND);
    }
  };

  // ---------------------------------------------------------------- 蜂巢
  var CELL_FLOATS = 8;
  Hive.Cells = function (ctx, max) {
    var gl = ctx.gl;
    this.ctx = ctx; this.max = max; this.count = 0;
    this.data = new Float32Array(max * CELL_FLOATS);
    this.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, this.data.byteLength, gl.DYNAMIC_DRAW);
    this.prog = new Program(gl, CELL_VS, CELL_FS);
    this.tube = makeVAO(gl, hexTubeGeom(), this.buffer, 2);
    this.disc = makeVAO(gl, hexDiscGeom(), this.buffer, 2);
  };
  // 第 i 格：中心 (x,z)、高度、蜂蜜高度(0-1)、发光、出现进度(0-1)、随机种子、整体抬升。
  Hive.Cells.prototype.set = function (i, x, z, height, fill, glow, appear, seed, lift) {
    var d = this.data, o = i * CELL_FLOATS;
    d[o] = x; d[o + 1] = z; d[o + 2] = height; d[o + 3] = fill;
    d[o + 4] = glow; d[o + 5] = appear; d[o + 6] = seed || 0; d[o + 7] = lift || 0;
  };
  Hive.Cells.prototype.upload = function (count) {
    var gl = this.ctx.gl;
    this.count = count;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.data, 0, count * CELL_FLOATS);
  };
  // o: { viewProj, model, camPos, light, time, radius, look: {wax, waxDeep, rim, honey, honeyHi, honeyGlow} }
  Hive.Cells.prototype.draw = function (o) {
    if (!this.count) return;
    var gl = this.ctx.gl, look = o.look;
    var u = {
      uViewProj: o.viewProj, uModel: o.model || M.identity(), uCamPos: o.camPos, uTime: o.time, uRadius: o.radius,
      uWax: look.wax, uWaxDeep: look.waxDeep, uRimHi: look.rim, uHoney: look.honey, uHoneyHi: look.honeyHi,
      uHoneyGlow: look.honeyGlow, uPass: 0, uSpot: o.spot || [0, 0, 0, 1e6], uSpotMin: o.spotMin === undefined ? 1 : o.spotMin
    };
    for (var k in o.light) u[k] = o.light[k];
    gl.enable(gl.DEPTH_TEST); gl.depthMask(true); gl.disable(gl.BLEND);
    gl.enable(gl.CULL_FACE); gl.cullFace(gl.BACK);
    this.prog.use(u);
    gl.bindVertexArray(this.tube.vao);
    gl.disable(gl.CULL_FACE);
    gl.drawElementsInstanced(gl.TRIANGLES, this.tube.count, gl.UNSIGNED_SHORT, 0, this.count);
    this.prog.set({ uPass: 1 });
    gl.bindVertexArray(this.disc.vao);
    gl.drawElementsInstanced(gl.TRIANGLES, this.disc.count, gl.UNSIGNED_SHORT, 0, this.count);
  };

  // ---------------------------------------------------------------- 光点
  Hive.Points = function (ctx, max) {
    var gl = ctx.gl;
    this.ctx = ctx; this.max = max; this.count = 0;
    this.data = new Float32Array(max * 8);
    this.prog = new Program(gl, POINT_VS, POINT_FS);
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    this.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, this.data.byteLength, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 32, 0);
    gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 32, 16);
    gl.bindVertexArray(null);
  };
  Hive.Points.prototype.reset = function () { this.count = 0; };
  // 世界坐标、世界尺寸、颜色 rgba。
  Hive.Points.prototype.push = function (p, size, c, a) {
    if (this.count >= this.max) return;
    var d = this.data, o = this.count++ * 8;
    d[o] = p[0]; d[o + 1] = p[1]; d[o + 2] = p[2]; d[o + 3] = size;
    d[o + 4] = c[0]; d[o + 5] = c[1]; d[o + 6] = c[2]; d[o + 7] = a;
  };
  Hive.Points.prototype.draw = function (viewProj, additive, fovScale) {
    if (!this.count) return;
    var gl = this.ctx.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.data, 0, this.count * 8);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(false); gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, additive ? gl.ONE : gl.ONE_MINUS_SRC_ALPHA);
    this.prog.use({ uViewProj: viewProj, uPx: this.ctx.h * (fovScale || 1) });
    gl.bindVertexArray(this.vao);
    gl.drawArrays(gl.POINTS, 0, this.count);
    gl.depthMask(true); gl.disable(gl.BLEND);
  };

  // ---------------------------------------------------------------- 连线
  Hive.Lines = function (ctx, max) {
    var gl = ctx.gl;
    this.ctx = ctx; this.max = max; this.count = 0;
    this.data = new Float32Array(max * 8);
    this.prog = new Program(gl, LINE_VS, LINE_FS);
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    this.buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, this.data.byteLength, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 16, 0);
    gl.bindVertexArray(null);
  };
  Hive.Lines.prototype.reset = function () { this.count = 0; };
  Hive.Lines.prototype.push = function (a, b, alphaA, alphaB) {
    if (this.count >= this.max) return;
    var d = this.data, o = this.count++ * 8;
    d[o] = a[0]; d[o + 1] = a[1]; d[o + 2] = a[2]; d[o + 3] = alphaA;
    d[o + 4] = b[0]; d[o + 5] = b[1]; d[o + 6] = b[2]; d[o + 7] = alphaB === undefined ? alphaA : alphaB;
  };
  Hive.Lines.prototype.draw = function (viewProj, color, additive) {
    if (!this.count) return;
    var gl = this.ctx.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.data, 0, this.count * 8);
    gl.enable(gl.DEPTH_TEST); gl.depthMask(false); gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, additive ? gl.ONE : gl.ONE_MINUS_SRC_ALPHA);
    this.prog.use({ uViewProj: viewProj, uColor: color });
    gl.bindVertexArray(this.vao);
    gl.drawArrays(gl.LINES, 0, this.count * 2);
    gl.depthMask(true); gl.disable(gl.BLEND);
  };

  // ---------------------------------------------------------------- 后期（多重采样 + 泛光）
  function Post(ctx) {
    var gl = ctx.gl;
    this.ctx = ctx;
    this.samples = Math.min(4, gl.getParameter(gl.MAX_SAMPLES) || 0);
    this.bright = new Program(gl, FULL_VS, BRIGHT_FS);
    this.blur = new Program(gl, FULL_VS, BLUR_FS);
    this.compose = new Program(gl, FULL_VS, COMPOSE_FS);
    this.targets = null;
  }
  function target(gl, w, h) {
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    var fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return { tex: tex, fb: fb, w: w, h: h };
  }
  Post.prototype.resize = function (w, h) {
    var gl = this.ctx.gl;
    var t = this.targets = {};
    t.scene = target(gl, w, h);
    t.msFb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, t.msFb);
    var color = gl.createRenderbuffer();
    gl.bindRenderbuffer(gl.RENDERBUFFER, color);
    if (this.samples) gl.renderbufferStorageMultisample(gl.RENDERBUFFER, this.samples, gl.RGBA8, w, h);
    else gl.renderbufferStorage(gl.RENDERBUFFER, gl.RGBA8, w, h);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, color);
    var depth = gl.createRenderbuffer();
    gl.bindRenderbuffer(gl.RENDERBUFFER, depth);
    if (this.samples) gl.renderbufferStorageMultisample(gl.RENDERBUFFER, this.samples, gl.DEPTH_COMPONENT24, w, h);
    else gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, w, h);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, depth);
    var hw = Math.max(1, w >> 1), hh = Math.max(1, h >> 1), qw = Math.max(1, w >> 2), qh = Math.max(1, h >> 2);
    t.halfA = target(gl, hw, hh); t.halfB = target(gl, hw, hh);
    t.quarterA = target(gl, qw, qh); t.quarterB = target(gl, qw, qh);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  };
  Post.prototype.begin = function () {
    var gl = this.ctx.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.targets.msFb);
    gl.viewport(0, 0, this.ctx.w, this.ctx.h);
  };
  Post.prototype.pass = function (prog, src, dst, uniforms) {
    var gl = this.ctx.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, dst ? dst.fb : null);
    gl.viewport(0, 0, dst ? dst.w : this.ctx.w, dst ? dst.h : this.ctx.h);
    prog.use(uniforms);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, src.tex);
    gl.bindVertexArray(this.ctx.fullVAO);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  };
  // o: { bloom, threshold, vignette, white, whiteCol, time, exposure }
  Post.prototype.end = function (o) {
    var gl = this.ctx.gl, t = this.targets;
    gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND);
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, t.msFb);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, t.scene.fb);
    gl.blitFramebuffer(0, 0, this.ctx.w, this.ctx.h, 0, 0, this.ctx.w, this.ctx.h, gl.COLOR_BUFFER_BIT, gl.NEAREST);
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, null);
    this.pass(this.bright, t.scene, t.halfA, { uTex: 0, uThreshold: o.threshold === undefined ? 0.55 : o.threshold });
    this.pass(this.blur, t.halfA, t.halfB, { uTex: 0, uDir: [1 / t.halfA.w, 0] });
    this.pass(this.blur, t.halfB, t.halfA, { uTex: 0, uDir: [0, 1 / t.halfA.h] });
    this.pass(this.blur, t.halfA, t.quarterA, { uTex: 0, uDir: [1 / t.halfA.w, 0] });
    this.pass(this.blur, t.quarterA, t.quarterB, { uTex: 0, uDir: [2 / t.quarterA.w, 0] });
    this.pass(this.blur, t.quarterB, t.quarterA, { uTex: 0, uDir: [0, 2 / t.quarterA.h] });
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.ctx.w, this.ctx.h);
    this.compose.use({
      uScene: 0, uBloomA: 1, uBloomB: 2, uBloom: o.bloom === undefined ? 1 : o.bloom, uVignette: o.vignette || 0,
      uWhite: o.white || 0, uWhiteCol: o.whiteCol || [1, 1, 1], uTime: o.time || 0, uExposure: o.exposure || 1
    });
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t.scene.tex);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, t.halfA.tex);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, t.quarterA.tex);
    gl.bindVertexArray(this.ctx.fullVAO);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.activeTexture(gl.TEXTURE0);
  };

  // 清屏辅助。
  Hive.clear = function (ctx, color) {
    var gl = ctx.gl;
    gl.viewport(0, 0, ctx.w, ctx.h);
    gl.clearColor(color[0], color[1], color[2], 1);
    gl.depthMask(true);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  };
})();

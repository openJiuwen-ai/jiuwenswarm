// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
(() => {
  'use strict';
  function create({ canvas, onError }) {
    const mesh = window.WorkSwarmBeeMesh;
    const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: true });
    if (!gl) throw new Error('beeCanvasFailed');
    const vertex = 'attribute vec2 a_position;attribute vec2 a_uv;uniform float u_angle;uniform vec2 u_shift;varying vec2 v_uv;void main(){vec2 p=a_position-vec2(256.0,270.0);float c=cos(u_angle),s=sin(u_angle);p=vec2(c*p.x-s*p.y,s*p.x+c*p.y)+vec2(256.0,270.0)+u_shift;gl_Position=vec4(p.x/256.0-1.0,1.0-p.y/256.0,0.0,1.0);v_uv=a_uv;}';
    const fragment = 'precision mediump float;varying vec2 v_uv;uniform sampler2D u_image;void main(){gl_FragColor=texture2D(u_image,v_uv);}';
    function shader(kind, source) {
      const result = gl.createShader(kind); gl.shaderSource(result, source); gl.compileShader(result);
      if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) throw new Error('beeCanvasFailed');
      return result;
    }
    const program = gl.createProgram(), vs = shader(gl.VERTEX_SHADER, vertex), fs = shader(gl.FRAGMENT_SHADER, fragment);
    gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('beeCanvasFailed');
    gl.deleteShader(vs); gl.deleteShader(fs); gl.useProgram(program);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA); gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    const positions = new Float32Array(mesh.original.length);
    const positionBuffer = gl.createBuffer(), uvBuffer = gl.createBuffer(), indexBuffer = gl.createBuffer();
    const positionLocation = gl.getAttribLocation(program, 'a_position'), uvLocation = gl.getAttribLocation(program, 'a_uv');
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer); gl.bufferData(gl.ARRAY_BUFFER, positions.byteLength, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(positionLocation); gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer); gl.bufferData(gl.ARRAY_BUFFER, mesh.uv, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(uvLocation); gl.vertexAttribPointer(uvLocation, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW);
    const angle = gl.getUniformLocation(program, 'u_angle'), shift = gl.getUniformLocation(program, 'u_shift');
    const texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, texture);
    for (const wrap of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) gl.texParameteri(gl.TEXTURE_2D, wrap, gl.CLAMP_TO_EDGE);
    for (const filter of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER]) gl.texParameteri(gl.TEXTURE_2D, filter, gl.LINEAR);
    let snapshot = null, pose = '', source = '', rig = null, revision = 0, loaded = false, raf = null, last = null, phase = 0, disposed = false;
    const gaze = { x: 0, y: 0, near: 0 };
    function fail() { loaded = false; if (raf !== null) cancelAnimationFrame(raf); raf = null; onError('beeCanvasFailed'); }
    function draw(dt) {
      if (!loaded || !snapshot?.visible || disposed) return;
      const rect = canvas.getBoundingClientRect(), scale = rect.width / 512;
      const face = mesh.rigs[pose].eyes[0] || [230, 250];
      const target = snapshot.target;
      const dx = target ? target.x - rect.left - face[0] * scale : 0;
      const dy = target ? target.y - rect.top - face[1] * scale : 0;
      const distance = Math.hypot(dx, dy, rect.width * .5), response = -Math.expm1(-dt / .11);
      const moving = snapshot.motion && !snapshot.reducedMotion;
      gaze.x += ((target ? dx / distance : snapshot.thinking ? Math.sin(phase * .9) * .45 : 0) - gaze.x) * response;
      gaze.y += ((target ? dy / distance : snapshot.thinking ? Math.sin(phase * .7) * .25 : 0) - gaze.y) * response;
      gaze.near += ((target ? Math.max(0, 1 - Math.hypot(dx, dy) / (rect.width * .7)) : 0) - gaze.near) * response;
      if (moving) phase += dt;
      const gx = moving ? gaze.x * 3.2 : 0, gy = moving ? gaze.y * 2.7 : 0;
      const wing = moving ? Math.sin(phase * (pose === 'flying' ? 28 : 20)) * .25 : 0;
      const antenna = moving ? Math.sin(phase * (snapshot.thinking ? 1.6 : 2.8)) * 1.35 + gaze.x : 0;
      const hand = moving ? Math.sin(phase * (pose === 'tool' ? 16 : 3)) * (pose === 'tool' ? 2.3 : .6) : 0;
      const f = rig.fields;
      for (let i = 0; i < mesh.count; i++) {
        positions[i * 2] = mesh.original[i * 2] + f.gaze[i] * gx + f.wing[i] * wing + f.antenna[i] * antenna;
        positions[i * 2 + 1] = mesh.original[i * 2 + 1] + f.gaze[i] * gy + (f.hand0[i] - f.hand1[i]) * hand;
      }
      gl.viewport(0, 0, canvas.width, canvas.height); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer); gl.bufferSubData(gl.ARRAY_BUFFER, 0, positions);
      gl.uniform1f(angle, ((snapshot.thinking ? -5 : 0) + (moving ? gaze.x * 1.6 : 0)) * Math.PI / 180);
      gl.uniform2f(shift, moving ? gaze.x * gaze.near * 2 : 0, moving ? Math.sin(phase * 2.2) * (pose === 'flying' ? 2.6 : 1) : 0);
      gl.drawElements(gl.TRIANGLES, mesh.indices.length, gl.UNSIGNED_SHORT, 0);
    }
    function tick(timestamp) {
      raf = null;
      if (!snapshot?.visible || document.hidden || disposed || !loaded) { last = null; return; }
      const dt = last === null ? 1 / 60 : (timestamp - last) / 1000; last = timestamp;
      draw(dt);
      if (snapshot.motion && !snapshot.reducedMotion) raf = requestAnimationFrame(tick);
    }
    function start() { if (raf === null && loaded && snapshot?.visible && !document.hidden && !disposed) raf = requestAnimationFrame(tick); }
    function update(next) {
      if (!next.visible) {
        snapshot = { ...snapshot, ...next };
        if (raf !== null) cancelAnimationFrame(raf);
        raf = null; last = null; return;
      }
      snapshot = next;
      if (next.pose !== pose || next.source !== source) {
        pose = next.pose; source = next.source; rig = mesh.build(pose); loaded = false;
        const version = ++revision, image = new Image(); image.crossOrigin = 'anonymous';
        image.onload = () => {
          if (disposed || version !== revision) return;
          try { gl.bindTexture(gl.TEXTURE_2D, texture); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image); loaded = true; start(); }
          catch { fail(); }
        };
        image.onerror = () => { if (!disposed && version === revision) fail(); }; image.src = source;
      }
      if (!next.visible || !next.motion || next.reducedMotion) { if (raf !== null) cancelAnimationFrame(raf); raf = null; last = null; }
      start();
    }
    const resize = new ResizeObserver(() => {
      canvas.width = Math.round(canvas.clientWidth * devicePixelRatio); canvas.height = Math.round(canvas.clientHeight * devicePixelRatio); start();
    });
    resize.observe(canvas);
    const visibility = () => { last = null; start(); };
    document.addEventListener('visibilitychange', visibility);
    const lost = event => { event.preventDefault(); fail(); };
    canvas.addEventListener('webglcontextlost', lost);
    return { update, dispose() {
      disposed = true; revision += 1; resize.disconnect(); if (raf !== null) cancelAnimationFrame(raf);
      document.removeEventListener('visibilitychange', visibility); canvas.removeEventListener('webglcontextlost', lost);
      for (const buffer of [positionBuffer, uvBuffer, indexBuffer]) gl.deleteBuffer(buffer);
      gl.deleteTexture(texture); gl.deleteProgram(program);
    } };
  }
  window.WorkSwarmBeeAnimation = Object.freeze({ create });
})();

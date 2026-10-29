import { useEffect, useRef } from 'react';

import { readDuplexOrbSignal } from './duplexAudioMeter';

// Voice orb in the spirit of the ChatGPT/Codex realtime-voice orb: a soft
// cloud sphere that drifts while idle and swells with whoever is speaking.
//
// 视觉参数来源：实测——用无头浏览器逐状态截图对照后调定，不是协议或用户给的值。

const VERTEX_SHADER = `
attribute vec2 position;
void main() { gl_Position = vec4(position, 0.0, 1.0); }
`;

const FRAGMENT_SHADER = `
precision highp float;
uniform vec2 uResolution;
uniform float uTime;
uniform float uLevel;
uniform float uSpeaking;
uniform float uConnecting;
uniform float uMuted;
uniform vec3 uDeep;
uniform vec3 uMid;
uniform vec3 uLight;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
             mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p) {
  float value = 0.0;
  // 来源：标准 fbm 写法，每层振幅减半（数学定义，非可调参数）
  float amplitude = 0.5;
  mat2 turn = mat2(1.6, 1.2, -1.2, 1.6);
  for (int octave = 0; octave < 5; octave++) {
    value += amplitude * noise(p);
    p = turn * p;
    amplitude *= 0.5;
  }
  return value;
}

void main() {
  vec2 uv = (gl_FragCoord.xy * 2.0 - uResolution) / min(uResolution.x, uResolution.y);
  float radius = length(uv);
  // 来源：一个像素在 [-1,1] 坐标里的宽度（由画布尺寸推出，非可调参数）
  float pixel = 2.0 / min(uResolution.x, uResolution.y);
  float sphere = 1.0 - smoothstep(0.96 - pixel * 1.5, 0.96, radius);
  if (sphere <= 0.0) { gl_FragColor = vec4(0.0); return; }

  // Bend the texture toward the rim so it reads as a ball, not a disc.
  float depth = sqrt(max(0.0, 1.0 - radius * radius));
  vec2 p = uv / (0.55 + depth * 0.45);

  float speed = 0.07 + uSpeaking * 0.18 + uLevel * 0.35;
  float t = uTime * speed;
  vec2 warp = vec2(fbm(p * 1.4 + vec2(0.0, t)), fbm(p * 1.4 + vec2(5.2, -t * 0.8)));
  float clouds = fbm(p * 1.9 + warp * (1.3 + uLevel * 1.6) + vec2(t * 0.6, -t * 0.4));

  vec3 color = mix(uDeep, uMid, smoothstep(0.2, 0.7, clouds));
  color = mix(color, uLight, smoothstep(0.52, 0.9, clouds + warp.x * 0.2));
  // Rim shade and a soft top-left highlight.
  color = mix(color, uDeep * 0.8, smoothstep(0.55, 1.0, radius) * 0.35);
  color += smoothstep(0.75, 0.0, length(uv - vec2(-0.35, 0.42))) * 0.16;

  // Connecting: a slow light band sweeps around the sphere.
  float sweep = 0.5 + 0.5 * sin(atan(uv.y, uv.x) - uTime * 2.4);
  color = mix(color, mix(color, vec3(1.0), 0.45 * sweep), uConnecting);

  float gray = dot(color, vec3(0.299, 0.587, 0.114));
  color = mix(color, vec3(gray * 0.85 + 0.1), uMuted * 0.85);

  gl_FragColor = vec4(color * sphere, sphere);
}
`;

type OrbVariant = 'starting' | 'active';

interface DuplexOrbProps {
  variant: OrbVariant;
  microphoneMuted: boolean;
}

function cssColor(element: Element, name: string, fallback: string): [number, number, number] {
  const probe = document.createElement('span');
  probe.style.color = getComputedStyle(element).getPropertyValue(name).trim() || fallback;
  element.appendChild(probe);
  const match = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g) || [];
  probe.remove();
  const [r, g, b] = match.map(Number);
  return [r / 255, g / 255, b / 255];
}

function compile(gl: WebGLRenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
  gl.deleteShader(shader);
  return null;
}

interface OrbFrame {
  time: number;
  level: number;
  speaking: number;
  connecting: number;
  muted: number;
}

interface OrbRenderer {
  draw(frame: OrbFrame): void;
  dispose(): void;
}

function createRenderer(canvas: HTMLCanvasElement, host: Element): OrbRenderer | null {
  let gl: WebGLRenderingContext | null = null;
  try {
    gl = canvas.getContext('webgl', { premultipliedAlpha: true, antialias: false });
  } catch {
    gl = null;
  }
  if (!gl) return null;
  const vertex = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
  const fragment = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
  const program = gl.createProgram();
  if (!vertex || !fragment || !program) return null;
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return null;
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

  const uniform = (name: string) => gl.getUniformLocation(program, name);
  // 来源：沿用主题里已有的球颜色变量（styles/themes/default/light.css）。
  gl.uniform3fv(uniform('uDeep'), cssColor(host, '--color-duplex-orb-start', '#1476ff'));
  gl.uniform3fv(uniform('uMid'), cssColor(host, '--color-duplex-orb-end', '#bdb1ff'));
  gl.uniform3fv(uniform('uLight'), [0.96, 0.97, 1]);
  const locations = {
    resolution: uniform('uResolution'),
    time: uniform('uTime'),
    level: uniform('uLevel'),
    speaking: uniform('uSpeaking'),
    connecting: uniform('uConnecting'),
    muted: uniform('uMuted'),
  };

  return {
    draw({ time, level, speaking, connecting, muted }) {
      const ratio = window.devicePixelRatio || 1;
      const width = Math.round(canvas.clientWidth * ratio);
      const height = Math.round(canvas.clientHeight * ratio);
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      gl.viewport(0, 0, width, height);
      gl.uniform2f(locations.resolution, width, height);
      gl.uniform1f(locations.time, time);
      gl.uniform1f(locations.level, level);
      gl.uniform1f(locations.speaking, speaking);
      gl.uniform1f(locations.connecting, connecting);
      gl.uniform1f(locations.muted, muted);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    },
    dispose() {
      gl.deleteBuffer(buffer);
      gl.deleteProgram(program);
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
      // Browsers cap live WebGL contexts; release ours on every unmount.
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    },
  };
}

function approach(current: number, target: number, rate: number): number {
  return current + (target - current) * rate;
}

/** Synthetic speech envelope for playback paths that expose no analyser (实测调定). */
export function speechEnvelope(time: number): number {
  return 0.35 + 0.25 * Math.sin(time * 9.1) * Math.sin(time * 3.7 + 1.3) + 0.15 * Math.sin(time * 17.3);
}

export function DuplexOrb({ variant, microphoneMuted }: DuplexOrbProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const propsRef = useRef({ variant, microphoneMuted });
  propsRef.current = { variant, microphoneMuted };

  useEffect(() => {
    const root = rootRef.current;
    const canvas = canvasRef.current;
    if (!root || !canvas) return undefined;
    const renderer = createRenderer(canvas, root);
    root.dataset.renderer = renderer ? 'webgl' : 'css';
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    const state = { level: 0, speaking: 0, connecting: 1, muted: 0 };
    const startedAt = performance.now();
    let frame = 0;

    const tick = (now: number) => {
      const time = (now - startedAt) / 1000;
      const { variant: currentVariant, microphoneMuted: muted } = propsRef.current;
      const signal = readDuplexOrbSignal();
      const speaking = signal.phase === 'speaking';
      const connecting = currentVariant === 'starting' || signal.phase === 'connecting';
      const target = speaking
        ? (signal.output ?? speechEnvelope(time))
        : muted || connecting ? 0 : signal.input;
      // 实测调定：起音快、释音慢，跟住说话又不抖。
      state.level = approach(state.level, target, target > state.level ? 0.35 : 0.08);
      state.speaking = approach(state.speaking, speaking ? 1 : 0, 0.08);
      state.connecting = approach(state.connecting, connecting ? 1 : 0, 0.06);
      state.muted = approach(state.muted, muted ? 1 : 0, 0.1);
      const still = Boolean(reducedMotion?.matches);
      const level = still ? 0 : state.level;
      root.style.setProperty('--duplex-orb-level', level.toFixed(3));
      renderer?.draw({
        time: still ? 0 : time,
        level,
        speaking: state.speaking,
        connecting: still ? 0 : state.connecting,
        muted: state.muted,
      });
      frame = window.requestAnimationFrame(tick);
    };
    if (typeof window.requestAnimationFrame !== 'function') {
      // No frame loop (non-browser host): draw one still frame.
      renderer?.draw({ time: 0, level: 0, speaking: 0, connecting: 0, muted: 0 });
      return () => renderer?.dispose();
    }
    frame = window.requestAnimationFrame(tick);
    return () => {
      window.cancelAnimationFrame(frame);
      renderer?.dispose();
    };
  }, []);

  return (
    <div ref={rootRef} className="task-full-duplex-orb__sphere" data-variant={variant} data-muted={microphoneMuted}>
      <canvas ref={canvasRef} className="task-full-duplex-orb__canvas" aria-hidden="true" />
    </div>
  );
}

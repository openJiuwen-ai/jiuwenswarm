import * as ort from 'onnxruntime-web/wasm';
import modelUrl from '../../../node_modules/@ricky0123/vad-web/dist/silero_vad_v5.onnx?url';
import wasmUrl from '../../../node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.wasm?url';
import wasmModuleUrl from '../../../node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.mjs?url';
import { SpeechGate } from './speechGate';

const scope = self as unknown as {
  onmessage: ((event: MessageEvent) => void) | null;
  postMessage: (message: unknown) => void;
};
let session: ort.InferenceSession;
let state: ort.Tensor;
let sampleRate: ort.Tensor;
let pending = new Float32Array(0);
let gate = new SpeechGate();

async function initialize(): Promise<void> {
  // This worker owns inference; do not create another worker or require cross-origin isolation.
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = false;
  ort.env.wasm.wasmPaths = { wasm: wasmUrl, mjs: wasmModuleUrl };
  session = await ort.InferenceSession.create(modelUrl, { executionProviders: ['wasm'] });
  state = new ort.Tensor('float32', new Float32Array(256), [2, 1, 128]);
  sampleRate = new ort.Tensor('int64', BigInt64Array.from([16000n]), [1]);
  scope.postMessage({ type: 'ready' });
}

async function processAudio(pcm: Int16Array, capturedAt: number, generation: number, reset: boolean): Promise<void> {
  const startedAt = performance.now();
  if (reset) {
    state.dispose();
    state = new ort.Tensor('float32', new Float32Array(256), [2, 1, 128]);
    pending = new Float32Array(0);
    gate = new SpeechGate();
  }
  const samples = new Float32Array(pending.length + pcm.length);
  samples.set(pending);
  for (let i = 0; i < pcm.length; i++) samples[pending.length + i] = pcm[i] / 32768;
  let offset = 0;
  const detections = [];
  while (offset + 512 <= samples.length) {
    const frame = samples.slice(offset, offset + 512);
    const input = new ort.Tensor('float32', frame, [1, 512]);
    let output: ort.InferenceSession.OnnxValueMapType;
    try {
      output = await session.run({ input, state, sr: sampleRate });
    } finally {
      input.dispose();
    }
    state.dispose();
    state = output.stateN;
    const probability = Number(output.output.data[0]);
    output.output.dispose();
    let energy = 0;
    for (const sample of frame) energy += sample * sample;
    detections.push(gate.process(probability, Math.sqrt(energy / frame.length) * 32768));
    offset += 512;
  }
  pending = samples.slice(offset);
  scope.postMessage({
    type: 'detections',
    detections,
    capturedAt,
    generation,
    inferenceMs: performance.now() - startedAt,
  });
}

scope.onmessage = ({ data }) => {
  const operation =
    data.type === 'init'
      ? initialize()
      : processAudio(new Int16Array(data.pcm), data.capturedAt, data.generation, data.reset);
  void operation.catch((error: unknown) => {
    scope.postMessage({ type: 'error', message: error instanceof Error ? error.message : String(error) });
  });
};

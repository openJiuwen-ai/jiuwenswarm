// Live voice signal for the full-duplex orb.
//
// Voice sessions attach AnalyserNodes to their microphone source and playback
// output; the orb polls them once per animation frame. Nothing here goes
// through React state, so a per-frame level never re-renders the chat panel.

export type DuplexVoicePhase = 'connecting' | 'listening' | 'speaking' | 'closed';
type MeterKind = 'input' | 'output';

// 来源：沿用已有配置，与任务语音输入波形同一套音量算法（features/taskAsr/useTaskAsr.ts:165,173）。
const ANALYSER_FFT_SIZE = 256; // 沿用 useTaskAsr.ts: analyser.fftSize = 256
const RMS_GAIN = 4; // 沿用 useTaskAsr.ts: Math.min(1, rms * 4)

const meters: Record<MeterKind, AnalyserNode | null> = { input: null, output: null };
const samples = new Uint8Array(ANALYSER_FFT_SIZE);
let phase: DuplexVoicePhase = 'closed';

export function setDuplexVoicePhase(next: DuplexVoicePhase): void {
  phase = next;
}

/** Taps `node` with an analyser; returns a detach function. */
export function attachDuplexAudioMeter(
  kind: MeterKind,
  context: BaseAudioContext,
  node: AudioNode,
): () => void {
  // Metering is optional (as in useTaskAsr.ts): voice keeps working without it.
  let analyser: AnalyserNode;
  try {
    analyser = context.createAnalyser();
    analyser.fftSize = ANALYSER_FFT_SIZE;
    node.connect(analyser);
  } catch {
    return () => undefined;
  }
  meters[kind] = analyser;
  return () => {
    if (meters[kind] === analyser) meters[kind] = null;
    try {
      node.disconnect(analyser);
    } catch {
      // The context may already be closed.
    }
  };
}

/** 0..1 level of unsigned 8-bit time-domain samples (128 = silence). */
export function levelFromByteSamples(values: ArrayLike<number>): number {
  if (!values.length) return 0;
  let energy = 0;
  for (let index = 0; index < values.length; index += 1) {
    const sample = (values[index] - 128) / 128;
    energy += sample * sample;
  }
  return Math.min(1, Math.sqrt(energy / values.length) * RMS_GAIN);
}

function readLevel(analyser: AnalyserNode | null): number | null {
  if (!analyser) return null;
  analyser.getByteTimeDomainData(samples);
  return levelFromByteSamples(samples);
}

export interface DuplexOrbSignal {
  phase: DuplexVoicePhase;
  input: number;
  /** null when the active playback path has no analyser (HTML audio). */
  output: number | null;
}

export function readDuplexOrbSignal(): DuplexOrbSignal {
  return { phase, input: readLevel(meters.input) ?? 0, output: readLevel(meters.output) };
}

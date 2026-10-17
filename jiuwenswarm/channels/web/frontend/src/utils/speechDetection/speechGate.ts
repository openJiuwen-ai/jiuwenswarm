export interface SpeechDetection {
  state: 'idle' | 'candidate' | 'started' | 'active' | 'ended';
  probability: number;
  level: number;
  noiseFloor: number;
  speechMs: number;
  silenceMs: number;
}

/** Silero v5 consumes 512 samples at 16 kHz (32 ms per inference). */
export class SpeechGate {
  private evidence: boolean[] = [];
  private active = false;
  private silenceMs = 0;
  private speechMs = 0;
  private noiseFloor = 120;

  process(probability: number, level: number): SpeechDetection {
    const speech = Number.isFinite(probability) && probability >= 0.8 && level >= 80;
    // Background estimation is diagnostic only: loud noise must not become speech,
    // and a previously loud environment must not prevent a quiet user from speaking.
    if (probability < 0.35) this.noiseFloor += (level - this.noiseFloor) * 0.05;
    let state: SpeechDetection['state'];
    if (this.active) {
      this.silenceMs = probability >= 0.45 && level >= 80 ? 0 : this.silenceMs + 32;
      this.speechMs += 32;
      if (this.silenceMs >= 640) {
        this.active = false;
        this.evidence = [];
        this.speechMs = 0;
        state = 'ended';
      } else {
        state = 'active';
      }
    } else {
      this.evidence.push(speech);
      if (this.evidence.length > 10) this.evidence.shift();
      const confirmedFrames = this.evidence.filter(Boolean).length;
      this.silenceMs = speech ? 0 : this.silenceMs + 32;
      this.speechMs = confirmedFrames * 32;
      if (speech && confirmedFrames >= 8) {
        this.active = true;
        state = 'started';
      } else {
        state = confirmedFrames ? 'candidate' : 'idle';
      }
    }
    return {
      state,
      probability,
      level,
      noiseFloor: this.noiseFloor,
      speechMs: this.speechMs,
      silenceMs: this.silenceMs,
    };
  }
}

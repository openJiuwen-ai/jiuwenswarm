import { resamplePcm16 } from '../../../../../../extensions/video_duplex/frontend/VideoLivePanel/joyaiVoice';

export interface PcmCapture {
  start(): Promise<void>;
  stop(): void;
}

/** Capture only: speech detection and utterance boundaries belong to Gateway. */
export class BrowserPcmCapture implements PcmCapture {
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private capture: AudioWorkletNode | null = null;
  private silent: GainNode | null = null;
  private stopped = false;

  constructor(
    private readonly stream: MediaStream,
    private readonly onPcm: (pcm: Int16Array) => void,
    private readonly onError: () => void,
  ) {}

  async start(): Promise<void> {
    if (this.stopped) return;
    try {
      const context = new AudioContext({ sampleRate: 16_000 });
      this.context = context;
      await context.audioWorklet.addModule(
        new URL('../../../../../../extensions/video_duplex/frontend/VideoLivePanel/duplex-capture.js', import.meta.url),
      );
      if (this.stopped) return;
      this.source = context.createMediaStreamSource(this.stream);
      this.capture = new AudioWorkletNode(context, 'jiuwen-duplex-capture');
      this.capture.port.onmessage = ({ data }) => {
        if (!this.stopped) this.onPcm(resamplePcm16(new Int16Array(data), context.sampleRate, 16_000));
      };
      this.capture.onprocessorerror = () => {
        if (!this.stopped) this.onError();
      };
      this.silent = context.createGain();
      this.silent.gain.value = 0;
      this.source.connect(this.capture);
      this.capture.connect(this.silent).connect(context.destination);
      await context.resume();
    } catch (error) {
      this.stop();
      throw error;
    }
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    if (this.capture) {
      this.capture.port.onmessage = null;
      this.capture.onprocessorerror = null;
      this.capture.port.close();
      this.capture.disconnect();
    }
    this.source?.disconnect();
    this.silent?.disconnect();
    void this.context?.close().catch(() => {});
  }
}

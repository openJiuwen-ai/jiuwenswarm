class DuplexCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Int16Array(Math.round(sampleRate / 10));
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (let index = 0; index < channel.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, channel[index]));
      this.chunk[this.offset++] = sample < 0 ? sample * 32768 : sample * 32767;
      if (this.offset === this.chunk.length) {
        const ready = this.chunk;
        this.chunk = new Int16Array(Math.round(sampleRate / 10));
        this.offset = 0;
        this.port.postMessage(ready.buffer, [ready.buffer]);
      }
    }
    return true;
  }
}

registerProcessor('jiuwen-duplex-capture', DuplexCaptureProcessor);

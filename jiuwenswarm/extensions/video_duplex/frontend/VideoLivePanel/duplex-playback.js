class DuplexPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.playedSamples = 0;
    this.underrunSamples = 0;
    this.drain = null;
    this.terminalFadeSamples = 0;
    this.responseId = null;
    this.started = false;
    this.rebuffering = false;
    this.initialBufferSamples = Math.round(sampleRate * 0.4);
    this.bufferWaitSamples = this.initialBufferSamples;
    this.fadeSamples = Math.max(1, Math.round(sampleRate * 0.005));
    this.fadeInSamples = 0;
    this.port.onmessage = ({ data }) => this.handleMessage(data || {});
  }

  handleMessage(data) {
    if (data.type === 'audio' && data.pcm) {
      const wasEmpty = this.queue.length === 0;
      if (!this.started && !this.responseId) this.responseId = data.responseId || null;
      if (!this.started && Number.isFinite(data.initialBufferMs)) {
        this.initialBufferSamples = Math.max(0, Math.round(sampleRate * data.initialBufferMs / 1000));
      }
      this.queue.push(new Int16Array(data.pcm));
      if (!this.started && wasEmpty && !this.rebuffering) {
        this.bufferWaitSamples = this.initialBufferSamples;
      }
      return;
    }
    if (data.type === 'drain') {
      this.drain = { responseId: data.responseId || this.responseId };
      this.terminalFadeSamples = Math.min(this.fadeSamples, this.bufferedSamples());
      if (!this.started && this.bufferedSamples() > 0) {
        this.bufferWaitSamples = 0;
        this.startPlayback();
      }
      this.notifyIfDrained();
      return;
    }
    if (data.type === 'clear') {
      this.port.postMessage({
        type: 'cleared',
        responseId: this.responseId,
        playedMs: Math.round(this.playedSamples * 1000 / sampleRate),
        cancelResponse: data.cancelResponse !== false,
      });
      this.reset();
    }
  }

  bufferedSamples() {
    return this.queue.reduce((total, chunk, index) => (
      total + chunk.length - (index === 0 ? this.offset : 0)
    ), 0);
  }

  startPlayback() {
    if (this.started) return;
    this.started = true;
    this.fadeInSamples = this.fadeSamples;
  }

  notifyIfDrained() {
    if (!this.drain || this.queue.length > 0) return;
    this.port.postMessage({
      type: 'drained',
      responseId: this.drain.responseId,
      playedMs: Math.round(this.playedSamples * 1000 / sampleRate),
      underrunMs: Math.round(this.underrunSamples * 1000 / sampleRate),
    });
    this.reset();
  }

  reset() {
    this.queue = [];
    this.offset = 0;
    this.playedSamples = 0;
    this.underrunSamples = 0;
    this.drain = null;
    this.terminalFadeSamples = 0;
    this.responseId = null;
    this.started = false;
    this.rebuffering = false;
    this.bufferWaitSamples = this.initialBufferSamples;
    this.fadeInSamples = 0;
  }

  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    output.fill(0);
    if (!this.started) {
      if (this.rebuffering && !this.drain) this.underrunSamples += output.length;
      if (this.queue.length > 0 && this.bufferWaitSamples > 0) {
        this.bufferWaitSamples = Math.max(0, this.bufferWaitSamples - output.length);
        return true;
      }
      if (this.queue.length > 0) {
        this.startPlayback();
        this.rebuffering = false;
      }
    }
    if (!this.started) {
      this.notifyIfDrained();
      return true;
    }

    let target = 0;
    while (target < output.length && this.queue.length > 0) {
      const chunk = this.queue[0];
      const count = Math.min(output.length - target, chunk.length - this.offset);
      const remainingBeforeChunk = this.bufferedSamples();
      for (let index = 0; index < count; index += 1) {
        let sample = chunk[this.offset + index] / 32768;
        if (this.fadeInSamples > 0) {
          sample *= (this.fadeSamples - this.fadeInSamples) / this.fadeSamples;
          this.fadeInSamples -= 1;
        }
        const remainingSamples = remainingBeforeChunk - index;
        if (this.drain && this.terminalFadeSamples > 0
          && remainingSamples <= this.terminalFadeSamples) {
          sample *= this.terminalFadeSamples === 1
            ? 0
            : (remainingSamples - 1) / (this.terminalFadeSamples - 1);
        }
        output[target + index] = sample;
      }
      target += count;
      this.offset += count;
      this.playedSamples += count;
      if (this.offset >= chunk.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }

    if (target < output.length && !this.drain) {
      const fadeCount = Math.min(target, this.fadeSamples);
      for (let index = 0; index < fadeCount; index += 1) {
        output[target - fadeCount + index] *= (fadeCount - index - 1) / fadeCount;
      }
      this.underrunSamples += output.length - target;
      this.started = false;
      this.rebuffering = true;
      this.bufferWaitSamples = this.initialBufferSamples;
      this.fadeInSamples = 0;
    }
    this.notifyIfDrained();
    return true;
  }
}

registerProcessor('jiuwen-duplex-playback', DuplexPlaybackProcessor);

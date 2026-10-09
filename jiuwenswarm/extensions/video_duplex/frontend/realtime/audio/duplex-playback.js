class DuplexPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.playedByResponse = {};
    this.activeResponses = new Set();
    this.enqueuedSamples = 0;
    this.drains = [];
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
      const chunk = new Int16Array(data.pcm);
      chunk.responseId = data.responseId || this.responseId;
      this.activeResponses.add(chunk.responseId);
      this.queue.push(chunk);
      this.enqueuedSamples += chunk.length;
      if (this.bufferedSamples() > sampleRate * 60) {
        this.port.postMessage({ type: 'overflow' });
        this.reset();
      }
      if (!this.started && wasEmpty && !this.rebuffering) {
        this.bufferWaitSamples = this.initialBufferSamples;
      }
      return;
    }
    if (data.type === 'drain') {
      this.drain = { responseId: data.responseId || this.responseId, generation: data.generation, end: this.enqueuedSamples };
      this.drains.push(this.drain);
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
        clearId: data.clearId,
        played: Object.fromEntries(Object.entries(this.playedByResponse).map(([id, count]) => [id, Math.round(count * 1000 / sampleRate)])),
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
    while (this.drains.length && this.drains[0].end <= this.playedSamples) {
      const drain = this.drains.shift();
      this.port.postMessage({
        type: 'drained', responseId: drain.responseId, generation: drain.generation,
        playedMs: Math.round((this.playedByResponse[drain.responseId] || 0) * 1000 / sampleRate),
        underrunMs: Math.round(this.underrunSamples * 1000 / sampleRate),
      });
      delete this.playedByResponse[drain.responseId];
      this.activeResponses.delete(drain.responseId);
    }
    // A completed response must not keep the next streaming response in drain mode.
    this.drain = this.drains.length ? this.drains[this.drains.length - 1] : null;
    if (!this.drain) this.terminalFadeSamples = 0;
    // Empty audio is only an underrun while a response still lacks its end marker.
    if (!this.queue.length && !this.drains.length && !this.activeResponses.size) this.reset();
  }

  reset() {
    this.queue = [];
    this.playedByResponse = {};
    this.activeResponses.clear();
    this.enqueuedSamples = 0;
    this.drains = [];
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
      for (let index = 0; index < count; index += 1) {
        let sample = chunk[this.offset + index] / 32768;
        if (this.fadeInSamples > 0) {
          sample *= (this.fadeSamples - this.fadeInSamples) / this.fadeSamples;
          this.fadeInSamples -= 1;
        }
        const remainingSamples = this.drain ? this.drain.end - this.playedSamples - index : 0;
        if (this.drain && chunk.responseId === this.drain.responseId && this.terminalFadeSamples > 0
          && remainingSamples > 0
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
      if (chunk.responseId) this.playedByResponse[chunk.responseId] = (this.playedByResponse[chunk.responseId] || 0) + count;
      if (this.offset >= chunk.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }

    this.notifyIfDrained();
    if (target < output.length && !this.drain && this.activeResponses.size) {
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
    return true;
  }
}

registerProcessor('jiuwen-duplex-playback', DuplexPlaybackProcessor);

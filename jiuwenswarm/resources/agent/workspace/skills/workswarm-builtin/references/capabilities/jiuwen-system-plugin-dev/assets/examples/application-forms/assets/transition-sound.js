// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
(() => {
  'use strict';

  function create({ onError = error => console.error('Transition sound failed', error) } = {}) {
    let config = null, key = '', context = null, loading = null, generation = 0, disposed = false;
    const voices = new Set();

    function stop() {
      for (const voice of [...voices]) {
        voice.source.onended = null;
        voice.source.stop(); voice.source.disconnect(); voice.gain.disconnect();
        voices.delete(voice); voice.resolve(false);
      }
    }

    function configure(value) {
      if (value !== undefined && (value === null || typeof value.enabled !== 'boolean'
        || typeof value.volume !== 'number' || !Number.isFinite(value.volume) || value.volume < 0 || value.volume > 1
        || typeof value.compact !== 'string' || typeof value.workspace !== 'string')) {
        throw new Error('Invalid transition sound configuration');
      }
      const next = value === undefined ? '' : JSON.stringify(value);
      if (next === key) return;
      stop(); generation += 1; loading = null; key = next; config = value || null;
    }

    async function prepare() {
      if (disposed || !config?.enabled || config.volume === 0) return null;
      if (!loading) {
        const current = generation;
        try {
          context ||= new AudioContext();
          loading = Promise.all(['compact', 'workspace'].map(async direction => {
            const response = await fetch(config[direction]);
            if (!response.ok) throw new Error(`Sound asset returned ${response.status}`);
            return context.decodeAudioData(await response.arrayBuffer());
          })).then(([compact, workspace]) => ({ compact, workspace })).catch(error => {
            if (current === generation && !disposed) { loading = null; onError(error); }
            return null;
          });
        } catch (error) { onError(error); return null; }
      }
      return loading;
    }

    async function play(direction, durationMs = 0) {
      if (!['compact', 'workspace'].includes(direction)) throw new Error('Invalid sound direction');
      const started = performance.now(), current = generation;
      const buffers = await prepare();
      if (!buffers || disposed || current !== generation) return false;
      try {
        await context.resume();
        if (disposed || current !== generation) return false;
        stop();
        const source = context.createBufferSource(), gain = context.createGain();
        source.buffer = buffers[direction]; gain.gain.value = config.volume;
        source.connect(gain); gain.connect(context.destination);
        // End the sample at the animation endpoint using the actual decoded duration.
        const delay = Math.max(0, durationMs / 1000 - source.buffer.duration - (performance.now() - started) / 1000);
        return await new Promise(resolve => {
          const voice = { source, gain, resolve }; voices.add(voice);
          source.onended = () => {
            voices.delete(voice); source.disconnect(); gain.disconnect(); resolve(true);
          };
          try { source.start(context.currentTime + delay); }
          catch (error) { voices.delete(voice); source.disconnect(); gain.disconnect(); onError(error); resolve(false); }
        });
      } catch (error) { onError(error); return false; }
    }

    return { configure, prepare, play, stop, enabled() { return !disposed && config?.enabled === true && config.volume > 0; }, dispose() {
      disposed = true; generation += 1; stop(); loading = null;
      if (context) void context.close().catch(onError);
    } };
  }

  window.WorkSwarmTransitionSound = { create };
})();

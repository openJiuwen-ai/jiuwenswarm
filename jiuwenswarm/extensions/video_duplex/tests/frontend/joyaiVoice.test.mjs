import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canPlayJoyAIResponse,
  JoyAITtsInterruptionState,
  JoyAIVoiceSession,
} from '../../../../channels/web/frontend/node_modules/.cache/joyai-voice/joyaiVoice.js';
import { SpeechGate } from '../../../../channels/web/frontend/node_modules/.cache/joyai-voice/speechGate.js';

class FakeAudio {
  static instances = [];

  constructor(src) {
    this.src = src;
    this.onended = null;
    this.onerror = null;
    this.paused = false;
    FakeAudio.instances.push(this);
  }

  play() {
    return Promise.resolve();
  }

  pause() {
    this.paused = true;
  }

  removeAttribute() {}

  load() {}
}

globalThis.Audio = FakeAudio;

class FakeAudioBufferSource {
  constructor() {
    this.buffer = null;
    this.onended = null;
    this.startedAt = null;
    this.stopped = false;
  }

  connect() {}
  disconnect() {}

  start(at) {
    this.startedAt = at;
  }

  stop() {
    this.stopped = true;
  }

  finish() {
    this.onended?.();
  }
}

class FakeAudioContext {
  static instances = [];

  constructor() {
    this.currentTime = 1;
    this.destination = {};
    this.sources = [];
    FakeAudioContext.instances.push(this);
  }

  resume() {
    return Promise.resolve();
  }

  close() {
    return Promise.resolve();
  }

  createBuffer(_channels, length, sampleRate) {
    const samples = new Float32Array(length);
    return {
      duration: length / sampleRate,
      getChannelData() {
        return samples;
      },
    };
  }

  createBufferSource() {
    const source = new FakeAudioBufferSource();
    this.sources.push(source);
    return source;
  }
}

globalThis.AudioContext = FakeAudioContext;

function pcmBase64(sampleCount) {
  const bytes = Buffer.alloc(sampleCount * 2);
  for (let index = 0; index < sampleCount; index += 1) {
    bytes.writeInt16LE(index % 1024, index * 2);
  }
  return bytes.toString('base64');
}

function createSession(states) {
  return new JoyAIVoiceSession({
    onSpeechStart() {},
    onTurnAudio() {},
    onState(state) {
      states.push(state);
    },
    onError(message) {
      throw new Error(message);
    },
  });
}

test('response audio is gated by user speech and request generation', () => {
  assert.equal(canPlayJoyAIResponse(4, 4, false), true);
  assert.equal(canPlayJoyAIResponse(4, 4, true), false);
  assert.equal(canPlayJoyAIResponse(3, 4, false), false);
});

test('JoyAI ignores loud noise but captures quiet confirmed speech with its onset', () => {
  let starts = 0;
  const turns = [];
  const session = new JoyAIVoiceSession({
    onSpeechStart() { starts += 1; },
    onTurnAudio(audio) { turns.push(audio); },
    onState() {},
    onError(message) { throw new Error(message); },
  });
  const gate = new SpeechGate();
  const feed = (probability, level, frames) => {
    for (let i = 0; i < frames; i++) {
      session.processAudio(new Int16Array(512).fill(level));
      session.handleSpeechDetection(gate.process(probability, level));
    }
  };
  feed(0.1, 14000, 40);
  assert.equal(starts, 0);
  assert.equal(turns.length, 0);
  feed(0.95, 120, 8);
  assert.equal(starts, 1);
  feed(0.95, 120, 10);
  feed(0.1, 0, 20);
  assert.equal(turns.length, 1);
  const wav = Buffer.from(turns[0].split(',')[1], 'base64');
  let speechSamples = 0;
  for (let i = 44; i < wav.length; i += 2) if (wav.readInt16LE(i) === 120) speechSamples++;
  assert.equal(speechSamples, 18 * 512, 'VAD confirmation must preserve the first spoken samples');
  session.stop();
  feed(0.95, 120, 12);
  assert.equal(starts, 1, 'late worker results must not restart a stopped session');
});

test('interrupted TTS resumes only when ASR rejects the same speech turn', () => {
  const interruption = new JoyAITtsInterruptionState();
  interruption.capture('需要恢复的回答', 4);

  assert.equal(interruption.takeAfterRejectedTurn(3, ''), '');
  assert.equal(interruption.takeAfterRejectedTurn(4, ''), '需要恢复的回答');
  assert.equal(interruption.takeAfterRejectedTurn(4, ''), '');
});

test('a meaningful instruction permanently discards interrupted TTS', () => {
  const interruption = new JoyAITtsInterruptionState();
  interruption.capture('旧回答', 7);

  assert.equal(interruption.takeAfterRejectedTurn(7, '请搜索香港天气'), '');
  assert.equal(interruption.takeAfterRejectedTurn(7, ''), '');
});

test('a newer speech turn cannot be cleared by an older ASR result', () => {
  const interruption = new JoyAITtsInterruptionState();
  interruption.capture('较新的回答', 9);

  interruption.discard(8);
  assert.equal(interruption.takeAfterRejectedTurn(9, '   '), '较新的回答');
});

test('speak resolves only after playback ends', async () => {
  const states = [];
  const session = createSession(states);
  let resolved = false;
  const speaking = session.speak('data:audio/wav;base64,AA==').then(() => {
    resolved = true;
  });

  await Promise.resolve();
  assert.equal(resolved, false);
  assert.deepEqual(states, ['speaking']);

  FakeAudio.instances.at(-1).onended();
  await speaking;
  assert.equal(resolved, true);
  assert.deepEqual(states, ['speaking', 'listening']);
});

test('interruptPlayback resolves the active playback', async () => {
  const states = [];
  const session = createSession(states);
  const speaking = session.speak('data:audio/wav;base64,AA==');
  await Promise.resolve();

  const audio = FakeAudio.instances.at(-1);
  session.interruptPlayback();
  await speaking;

  assert.equal(audio.paused, true);
  assert.deepEqual(states, ['speaking', 'listening']);
});

test('PCM stream starts after its short startup buffer and resolves after playback', async () => {
  const states = [];
  const session = createSession(states);
  let resolved = false;
  const playback = session.beginPcmStream('stream-1').then(() => {
    resolved = true;
  });

  session.appendPcm16Chunk('stream-1', pcmBase64(6_000), 24_000);
  const context = FakeAudioContext.instances.at(-1);
  assert.equal(context.sources.length, 1);
  assert.equal(context.sources[0].startedAt !== null, true);
  assert.deepEqual(states, ['speaking']);

  session.finishPcmStream('stream-1');
  await Promise.resolve();
  assert.equal(resolved, false);
  context.sources[0].finish();
  await playback;

  assert.equal(resolved, true);
  assert.deepEqual(states, ['speaking', 'listening']);
});

test('interruptPlayback stops and resolves an active PCM stream', async () => {
  const states = [];
  const session = createSession(states);
  const playback = session.beginPcmStream('stream-2');
  session.appendPcm16Chunk('stream-2', pcmBase64(6_000), 24_000);
  const source = FakeAudioContext.instances.at(-1).sources[0];

  session.interruptPlayback();
  await playback;

  assert.equal(source.stopped, true);
  assert.deepEqual(states, ['speaking', 'listening']);
});

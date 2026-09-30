import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { OpenAIProvider } from '../../../../channels/web/frontend/node_modules/.cache/realtime-providers/openai.mjs';
import { QwenProvider } from '../../../../channels/web/frontend/node_modules/.cache/realtime-providers/qwen.mjs';
import { RealtimeDuplexSession } from '../../../../channels/web/frontend/node_modules/.cache/realtime-providers/session.mjs';

function session(provider = new OpenAIProvider()) {
  const sent = [], calls = [], posted = [], errors = [];
  globalThis.WebSocket = { OPEN: 1 };
  globalThis.window = { setTimeout: () => 1, clearTimeout() {} };
  const rt = new RealtimeDuplexSession({ url: 'wss://example', adapter: provider }, {
    getVideoFrame: () => null, onAssistantText() {}, onUserText() {}, onState() {},
    onError: e => errors.push(e), onFunctionCall: e => calls.push(e),
  });
  rt.socket = { readyState: 1, close() {}, send: s => sent.push(JSON.parse(s)) };
  rt.playbackNode = { port: { postMessage: m => posted.push(m) } };
  rt.sessionReady = true;
  return { rt, sent, calls, posted, errors };
}

test('provider session schemas and rates remain distinct', () => {
  const qwen = new QwenProvider(), openai = new OpenAIProvider();
  assert.equal(qwen.inputRate, 16000);
  assert.equal(openai.inputRate, 24000);
  const q = qwen.configure({}), o = openai.configure({ replyLanguage: 'en' });
  assert.equal(q.session.audio.input.format.type, 'pcm');
  assert.deepEqual(o.session.audio.input.format, { type: 'audio/pcm', rate: 24000 });
  assert.equal(o.session.type, 'realtime');
  assert.deepEqual(o.session.output_modalities, ['audio']);
  assert.match(o.session.instructions, /Speak to the user in English/);
  assert.equal(o.session.audio.input.turn_detection.create_response, true);
  assert.equal(openai.localVad, false);
});

test('OpenAI image sampling distinguishes no new sample from removed source', () => {
  const p = new OpenAIProvider();
  assert.equal(p.audio('audio', 'aW1n').events.length, 2);
  assert.equal(p.audio('audio', null).events.length, 1);
  const removed = p.audio('audio', '').events;
  assert.equal(removed.length, 2);
  assert.equal(removed[1].type, 'conversation.item.delete');
  assert.equal(p.audio('audio', '').events.length, 1);
});

test('OpenAI truncation maps played samples to each response content part', () => {
  const p = new OpenAIProvider();
  const audio = btoa('\0'.repeat(48000));
  for (const item of ['a', 'b']) p.normalize({
    type: 'response.output_audio.delta', response_id: 'r', item_id: item, content_index: 0, delta: audio,
  });
  assert.deepEqual(p.truncate('r', 1200), [{
    type: 'conversation.item.truncate', item_id: 'b', content_index: 0, audio_end_ms: 200,
  }]);
  assert.deepEqual(p.truncate('r', 0), []);
});

test('argument completion and response.done fallback execute a function only once', () => {
  const {rt, calls} = session();
  const call = {call_id: 'c', name: 'jiuwen_delegate', arguments: '{"task":"write a report"}'};
  rt.handleEvent({type: 'response.function_call_arguments.done', ...call});
  rt.handleEvent({type: 'response.done', response: {id:'r', output: [{type:'function_call', ...call}]}});
  assert.equal(calls.length, 1);
  rt.handleEvent({type: 'response.function_call_arguments.done', ...call, call_id:'broken', arguments:'{'});
  assert.equal(calls.length, 1);
});

test('server VAD clears playback without redundant cancel and queues task notices', () => {
  const {rt, sent, posted} = session();
  rt.handleEvent({type: 'response.created', response: {id:'old'}});
  rt.handleEvent({type: 'input_audio_buffer.speech_started', item_id:'user'});
  assert.equal(sent.filter(e=>e.type==='response.cancel').length, 0);
  assert.equal(posted[0].type, 'clear');
  assert.equal(typeof posted[0].clearId, 'number');
  rt.enqueueOperationResult('call', {state:'accepted'});
  assert.equal(sent.length, 0);
  rt.stop();
});

test('model generation completion does not erase playback needed for interruption', async () => {
  const {rt, posted} = session();
  rt.handleEvent({type:'response.created', response:{id:'r'}});
  rt.handleEvent({type:'response.output_audio.delta', response_id:'r', item_id:'i', content_index:0, delta:btoa('aa')});
  await rt.playbackOperation;
  rt.handleEvent({type:'response.done', response:{id:'r',status:'completed'}});
  await rt.playbackOperation;
  rt.handleEvent({type:'input_audio_buffer.speech_started', item_id:'user'});
  assert.equal(rt.responseActive, false);
  assert.equal(posted.at(-1).type, 'clear');
  assert.equal(rt.pendingClears.size, 1);
  rt.stop();
});

test('a minimal injected provider uses the same media and scheduling runtime', async () => {
  const fake = {
    inputRate: 16000, outputRate: 24000, localVad:false, readyOnOpen:true, truncatePlayback:false,
    configure:()=>({}), normalize:e=>[e], audio:audio=>({events:[{sendAudio:audio}],diagnostics:[]}),
    text:text=>({text}), toolOutput:(callId,output)=>({callId,output}), response:()=>({answer:true}),
    cancel:()=>[], finish:()=>[], truncate:()=>[], played() {}, reset() {}, diagnostic:n=>n, snapshot:()=>({}),
  };
  const {rt,sent} = session(fake);
  rt.sendAudio(new Int16Array([10,20]), false);
  await rt.sendTextTurn('hello');
  assert.deepEqual(sent, [{sendAudio:btoa('\x0a\0\x14\0')},{text:'hello'},{answer:true}]);
});

test('shared worklet reports response-specific playback and echoes clear identity', () => {
  const posted = [];
  let Processor;
  runInNewContext(readFileSync(new URL('../../frontend/realtime/audio/duplex-playback.js', import.meta.url),'utf8'), {
    AudioWorkletProcessor: class { constructor() { this.port={postMessage:m=>posted.push(m)}; } },
    sampleRate:24000, registerProcessor:(_name,cls)=>{Processor=cls;},
  });
  const worklet = new Processor();
  worklet.handleMessage({type:'audio',pcm:new Int16Array(2400).buffer,responseId:'a',initialBufferMs:0});
  worklet.handleMessage({type:'audio',pcm:new Int16Array(2400).buffer,responseId:'b',initialBufferMs:0});
  worklet.process([], [[new Float32Array(3000)]]);
  worklet.handleMessage({type:'clear',clearId:7});
  assert.equal(posted.at(-1).clearId,7);
  assert.equal(posted.at(-1).played.a,100);
  assert.equal(posted.at(-1).played.b,25);
});


for (const firstSamples of [1200, 1280]) {
  for (const ending of ['clear', 'drain']) {
    test(`consecutive responses retain playback across underrun: ${firstSamples} samples then ${ending}`, () => {
      const posted = [];
      let Processor;
      runInNewContext(readFileSync(new URL('../../frontend/realtime/audio/duplex-playback.js', import.meta.url), 'utf8'), {
        AudioWorkletProcessor: class { constructor() { this.port = { postMessage: message => posted.push(message) }; } },
        sampleRate: 24000, registerProcessor: (_name, cls) => { Processor = cls; },
      });
      const worklet = new Processor();
      const provider = new OpenAIProvider();
      const audio = (responseId, samples) => worklet.handleMessage({
        type: 'audio', responseId, pcm: new Int16Array(samples).fill(1000).buffer, initialBufferMs: 0,
      });
      const process = () => worklet.process([], [[new Float32Array(128)]]);
      const playQueued = () => {
        for (let block = 0; worklet.queue.length && block < 100; block += 1) process();
        assert.equal(worklet.queue.length, 0);
      };
      audio('A', 128);
      worklet.handleMessage({ type: 'drain', responseId: 'A', generation: 3 });
      audio('B', firstSamples);
      playQueued();
      // B has no end marker yet. Silence is a network gap, not playback completion.
      for (let block = 0; block < 5; block += 1) process();
      assert.equal(worklet.playedByResponse.B, firstSamples);
      assert.equal(worklet.drain, null);
      assert.equal(worklet.rebuffering, true);
      audio('B', 2400 - firstSamples);
      playQueued();
      provider.normalize({
        type: 'response.output_audio.delta', response_id: 'B', item_id: 'item-B',
        content_index: 0, delta: Buffer.alloc(4800).toString('base64'),
      });
      worklet.handleMessage({ type: ending, responseId: 'B', generation: 3, clearId: 9 });
      const event = posted.at(-1);
      const playedMs = ending === 'clear' ? event.played.B : event.playedMs;
      assert.equal(playedMs, 100);
      assert.equal(posted.filter(message => message.type === 'drained' && message.responseId === 'A').length, 1);
      if (ending === 'clear') {
        assert.equal(event.clearId, 9);
        assert.equal(provider.truncate('B', playedMs)[0].audio_end_ms, 100);
      } else {
        assert.equal(event.type, 'drained');
        assert.equal(event.responseId, 'B');
        assert.equal(event.generation, 3);
      }
      assert.equal(Object.keys(worklet.playedByResponse).length, 0);
      assert.equal(worklet.drain, null);
    });
  }
}

test('late first audio after interruption is truncated without being heard', () => {
  const {rt, sent} = session();
  rt.provider.truncate('late', 0);
  const event = {type:'response.output_audio.delta', response_id:'late', item_id:'item-late', content_index:0, delta:btoa('\0'.repeat(480))};
  rt.handleEvent(event);
  assert.deepEqual(sent.filter(e => e.type === 'conversation.item.truncate'), [{type:'conversation.item.truncate',item_id:'item-late',content_index:0,audio_end_ms:0}]);
  rt.handleEvent(event);
  assert.equal(sent.filter(e => e.type === 'conversation.item.truncate').length, 1);
});

for (const [overlap, finalSeen] of [[false, true], [true, true], [true, false]]) {
  test(`per-response playback counts survive interruption (overlap=${overlap}, finalSeen=${finalSeen})`, async () => {
    const { rt, sent } = session();
    const playback = [];
    rt.callbacks.onPlayback = update => playback.push(update);
    const posted = [];
    const previousContext = globalThis.AudioContext;
    const previousNode = globalThis.AudioWorkletNode;
    const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    class Node {
      constructor() { this.port = { postMessage: message => posted.push(message) }; }
      connect() { return this; }
      disconnect() {}
    }
    globalThis.AudioContext = class {
      constructor({ sampleRate }) { this.sampleRate = sampleRate; this.audioWorklet = { addModule: async () => {} }; }
      createMediaStreamSource() { return new Node(); }
      createGain() { const node = new Node(); node.gain = { value: 0 }; return node; }
      async resume() {}
      async close() {}
    };
    globalThis.AudioWorkletNode = Node;
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
      mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) },
    } });
    Object.assign(globalThis.window, { setInterval: () => 1, clearInterval() {} });
    rt.openSocket = async () => {};
    try {
      await rt.start();
      const deliver = data => rt.playbackNode.port.onmessage({ data });
      const workletEvents = [];
      let Processor;
      runInNewContext(readFileSync(new URL('../../frontend/realtime/audio/duplex-playback.js', import.meta.url), 'utf8'), {
        AudioWorkletProcessor: class { constructor() { this.port = { postMessage: message => workletEvents.push(message) }; } },
        sampleRate: 24000, registerProcessor: (_name, cls) => { Processor = cls; },
      });
      const worklet = new Processor();
      rt.handleEvent({ type: 'response.created', response: { id: 'A' } });
      rt.handleEvent({
        type: 'response.output_audio.delta', response_id: 'A', item_id: 'item-A',
        content_index: 0, delta: Buffer.alloc(4800).toString('base64'),
      });
      rt.handleEvent({ type: 'response.done', response: { id: 'A', status: 'completed' } });
      await rt.playbackOperation;
      if (overlap) {
        rt.handleEvent({ type: 'response.created', response: { id: 'B' } });
        rt.handleEvent({
          type: 'response.output_audio.delta', response_id: 'B', item_id: 'item-B',
          content_index: 0, delta: Buffer.alloc(2400).toString('base64'),
        });
        await rt.playbackOperation;
      }
      for (const message of posted.splice(0)) worklet.handleMessage({ ...message, initialBufferMs: 0 });
      for (let block = 0; block < 30; block += 1) worklet.process([], [[new Float32Array(128)]]);
      const drained = workletEvents.shift();
      assert.equal(drained.type, 'drained');
      assert.equal(drained.playedMs, 100);
      // Model socket speech arrives before the already-posted worklet completion.
      rt.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'user' });
      const clear = posted.find(message => message.type === 'clear');
      assert.ok(clear);
      rt.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'user' });
      rt.handleEvent({ type: 'response.created', response: { id: 'C' } });
      rt.assistantPlaying = true;
      rt.playbackResponses.add('C');
      rt.queuedDrainResponseId = 'C';
      deliver({ ...drained, generation: drained.generation - 1, playedMs: 999 });
      deliver({ ...drained, responseId: 'unrelated', playedMs: 999 });
      if (finalSeen) deliver(drained);
      assert.equal(rt.responseId, 'C');
      assert.equal(rt.assistantPlaying, true);
      assert.equal(rt.queuedDrainResponseId, 'C');
      assert.equal(rt.playbackResponses.has('C'), true);
      worklet.handleMessage(clear);
      const cleared = workletEvents.at(-1);
      assert.equal(Object.keys(cleared.played).length, overlap ? 1 : 0);
      if (overlap) {
        assert.equal(cleared.responseId, 'A');
        assert.equal(cleared.playedMs, 150);
        assert.equal(cleared.played.B, 50);
        assert.equal(cleared.played.A, undefined);
      }
      deliver(cleared);
      assert.equal(playback.find(update => update.responseId === 'A').playedMs, finalSeen ? 100 : 0);
      assert.equal(sent.find(message => message.type === 'conversation.item.truncate' && message.item_id === 'item-A').audio_end_ms, finalSeen ? 100 : 0);
      if (overlap) {
        assert.equal(playback.find(update => update.responseId === 'B').playedMs, 50);
        assert.equal(sent.find(message => message.type === 'conversation.item.truncate' && message.item_id === 'item-B').audio_end_ms, 50);
      }
      assert.equal(rt.pendingClears.size, 0);
    } finally {
      rt.stop();
      globalThis.AudioContext = previousContext;
      globalThis.AudioWorkletNode = previousNode;
      if (previousNavigator) Object.defineProperty(globalThis, 'navigator', previousNavigator);
      else delete globalThis.navigator;
    }
  });
}

test('cancelled or failed response does not replay tool output as a new call', () => {
  for (const status of ['cancelled','failed','incomplete']) {
    const provider = new OpenAIProvider();
    const events = provider.normalize({type:'response.done',response:{id:'r',status,output:[{type:'function_call',call_id:'c',name:'jiuwen_delegate',arguments:'{"task":"A"}'}]}});
    assert.equal(events.filter(e => e.type === 'tool_call').length,0);
  }
});

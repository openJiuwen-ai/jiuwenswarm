import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

import { RealtimeDuplexSession } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/realtimeDuplex.mjs';
import { SpeechGate } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/speechGate.mjs';

test('tool output diagnostics distinguish sending from provider acknowledgement without result text', () => {
  const { session, diagnostics, sent } = createSession();
  session.enqueueOperationResult('call-trace', { state: 'accepted', result: 'private result body' });
  const transmitted = diagnostics.find(event => event.event === 'qwen_tool_output_sent');
  assert.equal(transmitted.call_id, 'call-trace');
  assert.equal(sent[0].item.call_id, 'call-trace');
  assert.equal(diagnostics.some(event => event.event === 'qwen_tool_output_acknowledged'), false);
  session.handleEvent({ type: 'conversation.item.created', item: {
    type: 'function_call_output', call_id: 'call-trace', id: 'item-trace',
  } });
  const acknowledged = diagnostics.find(event => event.event === 'qwen_tool_output_acknowledged');
  assert.equal(acknowledged.call_id, 'call-trace');
  assert.equal(acknowledged.item_id, 'item-trace');
  assert.equal(JSON.stringify(diagnostics).includes('private result body'), false);
});

test('accepted delegation returns its ID before completion and never sends two outputs for one call', () => {
  const {session,sent}=createSession();
  session.enqueueOperationResult('create-a',{state:'accepted',job_id:'a'});
  assert.equal(JSON.parse(sent[0].item.output).job_id,'a');
  assert.equal(typeof sent[1].response.instructions, 'string', 'operation receipt carries response-level guidance');
  session.handleEvent({type:'response.created',response:{id:'ack'}});
  session.handleEvent({type:'response.done',response:{id:'ack'}});
  session.enqueueToolResult({jobId:'a',callId:'create-a',question:'杭州',brief:realtimeBrief('完成')});
  assert.equal(sent.filter(e=>e.item?.type==='function_call_output').length,1);
  assert.equal(sent.filter(e=>e.type==='response.create').length,2);
  assert.equal(sent.at(-1).response, undefined, 'completion notice uses its own existing presentation path');
  assert.match(sent.at(-2).item.content[0].text,/杭州/);
});

test('a tool receipt delayed past a new spoken turn does not use its expired call ID', () => {
  const { session, sent, diagnostics } = createSession();
  session.handleEvent({ type: 'response.function_call_arguments.done', name: 'jiuwen_delegate',
    call_id: 'call-old', arguments: JSON.stringify({ task: 'First task' }) });
  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'new-input' });
  session.enqueueOperationResult('call-old', { state: 'accepted', job_id: 'first-job' });
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'new-input' });
  session.handleEvent({ type: 'conversation.item.input_audio_transcription.completed',
    item_id: 'new-input', transcript: 'Second task' });
  session.handleEvent({ type: 'response.created', response: { id: 'second-answer' } });
  session.handleEvent({ type: 'response.done', response: { id: 'second-answer' } });

  assert.deepEqual(sent.map((event) => event.type), ['conversation.item.create', 'response.create']);
  assert.equal(sent[0].item.type, 'message');
  assert.match(sent[0].item.content[0].text, /first-job/);
  assert.equal(sent.some((event) => event.item?.type === 'function_call_output'), false);
  assert.equal(diagnostics.some((event) => event.event === 'qwen_late_tool_output_rerouted'), true);
});

test('provider speech and its pending automatic answer exclude notification responses', () => {
  const {session,sent}=createSession();
  session.handleEvent({type:'input_audio_buffer.speech_started'});
  session.enqueueOperationResult('op',{state:'accepted'});
  session.handleEvent({type:'input_audio_buffer.speech_stopped'});
  session.dispatchQueuedToolResult();
  assert.equal(sent.length,0);
  session.handleEvent({type:'response.created',response:{id:'voice'}});
  session.handleEvent({type:'response.done',response:{id:'voice'}});
  assert.equal(sent.filter(e=>e.type==='response.create').length,1);
  session.handleEvent({type:'response.done',response:{id:'voice'}});
  assert.equal(session.responseActive,true, 'late done cannot release a reserved response');
});

test('cancel request does not free a response slot before cancellation acknowledgement', () => {
  const {session,sent}=createSession();
  session.handleEvent({type:'response.created',response:{id:'old'}});
  session.interruptQwenResponse('turn',300,1000,80);
  session.enqueueOperationResult('op',{state:'accepted'});
  assert.equal(sent.filter(e=>e.type==='response.create').length,0);
  session.handleEvent({type:'response.done',response:{id:'old',status:'cancelled'}});
  assert.equal(sent.filter(e=>e.type==='response.create').length,1);
});

test('active-response conflict retries only response creation after done, without replaying receipts', () => {
  const {session,sent}=createSession();
  session.enqueueOperationResult('op',{state:'accepted'});
  session.handleEvent({type:'error',error:{message:'Conversation already has an active response'}});
  assert.equal(sent.length,2);
  session.handleEvent({type:'response.done',response:{id:'server-active'}});
  assert.deepEqual(sent.map(e=>e.type),['conversation.item.create','response.create','response.create']);
  assert.deepEqual(sent[2], sent[1], 'retry preserves the operation response instructions');
});

test('three failures stop new call IDs and new speech opens a fresh operation budget', () => {
  const {session,functionCalls,sent}=createSession();
  const call=(id,version)=>({type:'response.function_call_arguments.done',name:'jiuwen_task_reorder',call_id:id,arguments:JSON.stringify({job_id:'a',action:'next',queue_version:version})});
  for(let i=0;i<3;i++){
    session.handleEvent(call('c'+i,i));
    session.enqueueOperationResult('c'+i,{state:'rejected',error:'Queue changed'});
  }
  session.handleEvent(call('c3',3));
  assert.equal(functionCalls.length,3);
  assert.equal(JSON.parse(sent.at(-1).item.output).retryable,false);
  session.handleEvent({type:'conversation.item.input_audio_transcription.completed',transcript:'重新查询后调整'});
  session.handleEvent(call('fresh',4));
  assert.equal(functionCalls.length,4);
});

test('identical rejected arguments are not retried with another call ID', () => {
  const {session,functionCalls}=createSession();
  const args={type:'response.function_call_arguments.done',name:'jiuwen_task_reorder',arguments:'{"job_id":"a","action":"wrong","queue_version":1}'};
  session.handleEvent({...args,call_id:'one'});
  session.enqueueOperationResult('one',{state:'rejected',error:'Invalid queue action'});
  session.handleEvent({...args,call_id:'two'});
  assert.equal(functionCalls.length,1);
});

test('failed control budget does not block queries or another task answer', () => {
  const {session,functionCalls}=createSession();
  const call=(id,name,args)=>session.handleEvent({type:'response.function_call_arguments.done',name,call_id:id,arguments:JSON.stringify(args)});
  for(let i=0;i<3;i++) {
    call(`failed-${i}`,'jiuwen_task_modify',{job_id:'a',revision:i,instruction:'update'});
    session.enqueueOperationResult(`failed-${i}`,{state:'rejected'});
  }
  call('query','jiuwen_task_query',{status:'unfinished'});
  call('answer-b','jiuwen_task_answer',{job_id:'b',interaction_id:'budget-b',answers:['3000']});
  assert.equal(functionCalls.length,5);
});

test('late and partial tool calls from an interrupted response cannot start work or report parser errors', () => {
  const errors = [];
  const { session, functionCalls } = createSession(null, { onError: e => errors.push(e) });
  session.handleEvent({ type: 'response.created', response: { id: 'interrupted' } });
  session.interruptQwenResponse('new-input', 300, 1000, 80);
  session.handleEvent({ type: 'response.function_call_arguments.done', response_id: 'interrupted',
    name: 'jiuwen_delegate', call_id: 'stale', arguments: '{"task":"outdated request"}' });
  assert.equal(functionCalls.length, 0);
  session.handleEvent({ type: 'response.function_call_arguments.done', response_id: 'interrupted',
    name: 'jiuwen_delegate', call_id: 'partial', arguments: '{"task":"unfinished' });
  assert.deepEqual(errors, []);
});

test('pending information questions are announced once without completing their task', () => {
  const { session, sent, dispatchedToolResults } = createSession();
  const question = { id: 'interaction-a', request_id: 'budget', state: 'pending', questions: [{ question: '预算是多少？' }] };
  session.enqueueQuestion('trip', question);
  session.enqueueQuestion('trip', question);
  assert.equal(sent.filter(event => event.type === 'response.create').length, 1);
  assert.match(sent[0].item.content[0].text, /预算是多少/);
  assert.match(sent[0].item.content[0].text, /call jiuwen_queue_manage/);
  assert.doesNotMatch(sent[0].item.content[0].text, /call jiuwen_task_answer/);
  assert.deepEqual(dispatchedToolResults, []);
  assert.equal(session.acceptedToolResultIds.has('trip'), false);
  session.handleEvent({ type: 'response.created', response: { id: 'question-notice' } });
  session.handleEvent({ type: 'response.done', response: { id: 'question-notice' } });
  session.enqueueQuestion('trip', { ...question, id: 'interaction-b' });
  assert.equal(sent.filter(event => event.type === 'response.create').length, 2);
  assert.deepEqual(dispatchedToolResults, []);
});

test('operation receipts route unfinished management through the exposed queue tool', () => {
  const { session, sent } = createSession();
  session.enqueueOperationResult('query', { operation: 'query', applied: false, jobs: [] });
  const instructions = sent.find(event => event.type === 'response.create').response.instructions;
  assert.match(instructions, /call jiuwen_queue_manage/);
  assert.doesNotMatch(instructions, /call jiuwen_task_(?:answer|reorder|modify|query|cancel)/);
  assert.match(instructions, /Do not call low-level task tools or jiuwen_queue_manage again for the same request/);
  assert.match(instructions, /a failed write means the task was unsuccessful and its final state is unconfirmed/);
  assert.doesNotMatch(instructions, /read-only when applied=false/);
});

test('pending approval cards ask the user to click instead of answering by voice', () => {
  // Voice must never approve cards, so it must not invite a spoken "允许" either.
  const { session, sent } = createSession();
  session.enqueueQuestion('cua', { id: 'card-a', request_id: 'core-approval', state: 'pending',
    source: 'confirm_interrupt', questions: [{ header: '操作确认: cua_agent', question: '打开计算器' }] });
  const text = sent.find(e => e.type === 'conversation.item.create').item.content[0].text;
  assert.match(text, /操作确认: cua_agent/);
  assert.match(text, /click/);
  assert.match(text, /Do not call jiuwen_task_answer/);
  assert.doesNotMatch(text, /then call jiuwen_task_answer/);
});

test('an open approval card stays in the session instructions until it closes', () => {
  // A one-off notice is forgotten by the next turn, so a spoken "允许" was
  // answered as consent (#8941). Every reply must see the open card.
  const { session, sent } = createSession();
  const card = { id: 'card-a', request_id: 'core-approval', state: 'pending',
    source: 'confirm_interrupt', questions: [{ header: '操作确认: cua_agent', question: '打开计算器' }] };
  session.enqueueQuestion('cua', card);
  const updates = () => sent.filter(e => e.type === 'session.update');
  assert.equal(updates().length, 1);
  assert.match(updates()[0].session.instructions, /\[Pending approval cards\].*操作确认: cua_agent/);
  assert.match(updates()[0].session.instructions, /Never say a pending card was allowed/);

  session.enqueueQuestion('cua', card); // A repeated progress event must not resend.
  assert.equal(updates().length, 1);

  session.enqueueQuestion('cua', { ...card, state: 'accepted' });
  assert.equal(updates().length, 2);
  assert.doesNotMatch(updates()[1].session.instructions, /Pending approval cards/);
});

const openCard = (session) => session.enqueueQuestion('cua', { id: 'card-a', request_id: 'core-approval', state: 'pending',
  source: 'confirm_interrupt', questions: [{ header: '操作确认: cua_agent', question: '打开计算器' }] });

test('every reply to speech while a card is open reads the card status last', () => {
  // The model hears audio, not the transcript ("允许" was transcribed as "人是"), and it
  // answered a spoken "允许" with "你已经在屏幕上点击了允许" despite the instructions (#8941).
  const { session, sent } = createSession();
  openCard(session);
  session.handleEvent({ type: 'response.done', response: { id: 'notice', status: 'completed' } });
  sent.length = 0;

  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'u1' });
  session.handleEvent({ type: 'response.created', response: { id: 'auto' } });
  assert.deepEqual(sent.map(e => e.type), ['response.cancel'], 'the automatic reply is cancelled');

  session.handleEvent({ type: 'response.done', response: { id: 'auto', status: 'cancelled' } });
  assert.deepEqual(sent.slice(1).map(e => e.type), ['conversation.item.create', 'response.create']);
  const status = sent[1].item.content[0].text;
  assert.match(status, /操作确认: cua_agent/);
  assert.match(status, /has NOT clicked/);
  assert.match(status, /Never say the card was allowed/);

  session.handleEvent({ type: 'response.created', response: { id: 'replacement' } });
  assert.equal(sent.filter(e => e.type === 'response.cancel').length, 1, 'the re-requested reply plays');
});

test('without an open card, and for the assistant notices, replies are left alone', () => {
  const { session, sent } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'u1' });
  session.handleEvent({ type: 'response.created', response: { id: 'r1' } });
  session.handleEvent({ type: 'response.done', response: { id: 'r1', status: 'completed' } });
  openCard(session); // its notice reply is not a user turn
  session.handleEvent({ type: 'response.created', response: { id: 'notice' } });
  assert.equal(sent.filter(e => e.type === 'response.cancel').length, 0);
});

test('a card that opens before the reply to just-finished speech redirects that reply', () => {
  // Consent spoken just before the card appeared was answered "已收到您的确认…继续执行"
  // just after it (#5538 robustness run): the reply had been formed without the card.
  const { session, sent } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'u1' });
  openCard(session);
  sent.length = 0;
  session.handleEvent({ type: 'response.created', response: { id: 'auto' } });
  assert.deepEqual(sent.map(e => e.type), ['response.cancel']);
  session.handleEvent({ type: 'response.done', response: { id: 'auto', status: 'cancelled' } });
  assert.match(sent.find(e => e.type === 'conversation.item.create').item.content[0].text, /has NOT clicked/);
});

test('a card that opens while the reply to speech is playing cancels and redirects it', () => {
  const { session, sent } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'u1' });
  session.handleEvent({ type: 'response.created', response: { id: 'auto' } });
  sent.length = 0;
  openCard(session);
  assert.ok(sent.some(e => e.type === 'response.cancel'), 'the playing reply is cancelled');
  session.handleEvent({ type: 'response.done', response: { id: 'auto', status: 'cancelled' } });
  assert.match(sent.find(e => e.type === 'conversation.item.create').item.content[0].text, /has NOT clicked/);
});

test('a card that opens while an assistant notice plays leaves the notice alone', () => {
  const { session, sent } = createSession();
  session.enqueueQuestion('ask', { id: 'q-a', request_id: 'ask-1', state: 'pending',
    source: 'ask_user_interrupt', questions: [{ question: '预算多少？' }] });
  session.handleEvent({ type: 'response.created', response: { id: 'notice' } });
  sent.length = 0;
  openCard(session);
  assert.equal(sent.filter(e => e.type === 'response.cancel').length, 0);
});

test('information questions do not add approval instructions', () => {
  const { session, sent } = createSession();
  session.enqueueQuestion('ask', { id: 'q-a', request_id: 'ask-1', state: 'pending',
    source: 'ask_user_interrupt', questions: [{ question: '预算多少？' }] });
  assert.equal(sent.filter(e => e.type === 'session.update').length, 0);
});

test('delegation retains valid scheduling arguments and dispatches only once', () => {
  const errors = [];
  const { session, functionCalls } = createSession(null, { onError: (message) => errors.push(message) });
  const args = { task: '查询杭州天气', title: '查询杭州天气与出行建议', independent: true, depends_on: [], resources: [] };
  const event = { type: 'response.function_call_arguments.done', name: 'jiuwen_delegate', call_id: 'parallel-weather', arguments: JSON.stringify(args) };
  session.handleEvent(event);
  session.handleEvent(event);
  assert.equal(functionCalls.length, 1);
  assert.deepEqual(JSON.parse(functionCalls[0].arguments), args);
  assert.deepEqual(errors, []);
});

test('invalid scheduling and ambiguous task fields never dispatch work', () => {
  for (const args of [
    { task: 'weather', independent: 'true' },
    { task: 'weather', resources: 'file' },
    { task: 'weather', depends_on: [''] },
    { task: 'weather', resources: Array(33).fill('x') },
    { task: 'weather', query: 'other' },
    { task: 'weather', unexpected: true },
  ]) {
    const errors = [];
    const { session, functionCalls } = createSession(null, { onError: (message) => errors.push(message) });
    session.handleEvent({ type: 'response.function_call_arguments.done', name: 'jiuwen_delegate', call_id: 'invalid', arguments: JSON.stringify(args) });
    assert.equal(functionCalls.length, 0);
    assert.equal(errors.length, 1);
  }
});

test('confirmed cancellation sends one terminal notice and rejects a late result', () => {
  const { session, sent } = createSession();
  session.cancelToolTask('stopped-job', 'stopped-call');
  session.cancelToolTask('stopped-job', 'stopped-call');
  assert.equal(sent.length, 2);
  assert.match(sent[0].item.content[0].text, /Task stop confirmed/);
  assert.equal(sent.filter(event => event.type === 'response.create').length, 1);
  assert.equal(session.enqueueToolResult({ jobId: 'stopped-job', callId: 'stopped-call', question: '已取消任务', brief: { summary: '迟到的成功结果' } }), false);
});

test('confirmed speech interrupts each response once, including responses that start mid-utterance', () => {
  const { session, sent } = createSession();
  const gate = new SpeechGate();
  const feed = (probability, count, level = 1000) => {
    for (let i = 0; i < count; i++) session.handleSpeechDetection(gate.process(probability, level));
  };
  session.handleEvent({ type: 'response.created', response: { id: 'first' } });
  feed(0.1, 40, 15000);
  feed(0.99, 3);
  feed(0.1, 12);
  assert.equal(sent.length, 0);
  feed(0.95, 8);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 1);
  session.handleEvent({ type: 'response.created', response: { id: 'second' } });
  assert.equal(session.userActivityActive, true);
  feed(0.95, 25);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 2);
  feed(0.1, 40);
  session.handleEvent({ type: 'response.created', response: { id: 'third' } });
  feed(0.95, 8);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 3);
});

test('audio arriving during ongoing speech is blocked, but speech hangover permits the next answer', async () => {
  const { session, posted } = createSession();
  const gate = new SpeechGate();
  for (let i = 0; i < 8; i++) session.handleSpeechDetection(gate.process(0.95, 1000));
  session.handleEvent({ type: 'response.audio.delta', response_id: 'late-audio', delta: btoa('aa') });
  await session.playbackOperation;
  assert.ok(posted.every((message) => message.type !== 'audio'));
  for (let i = 0; i < 10; i++) session.handleSpeechDetection(gate.process(0.1, 20));
  assert.equal(session.userActivityActive, true); // the VAD's 640ms hangover has not expired
  session.handleEvent({ type: 'response.created', response: { id: 'after-speech' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'after-speech', delta: btoa('aa') });
  session.handleEvent({ type: 'response.done', response: { id: 'after-speech', status: 'completed' } });
  await session.playbackOperation;
  assert.equal(posted.filter((message) => message.type === 'audio').length, 1);
});

test('a stale speaking flag after a worker stall does not cancel a new answer', () => {
  const { session, sent } = createSession();
  session.userActivityActive = true;
  session.activeUserTurnId = 'old-voice';
  session.lastSpeechDetectionAt = performance.now() - 600;
  session.handleEvent({ type: 'response.created', response: { id: 'fresh-answer' } });
  assert.deepEqual(sent, []);
});

test('cancelled response discards unclassified text and late audio', async () => {
  const { session, posted, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'interrupted' } });
  session.interruptQwenResponse('voice-test', 256, 1000, 80);
  session.handleEvent({ type: 'response.audio.delta', response_id: 'interrupted', delta: btoa('aa') });
  session.handleEvent({ type: 'response.text.done', response_id: 'interrupted', text: '已经返回的文字' });
  await session.playbackOperation;
  assert.ok(posted.every((message) => message.type !== 'audio'));
  assert.deepEqual(assistantTexts, []);
});

function realtimeBrief(summary, resultKind = 'generic') {
  return {
    status: 'completed',
    result_kind: resultKind,
    summary,
    displayed_in_ui: true,
    response_mode: 'brief',
    source: 'core_agent',
  };
}

test('speech before and after delegation is discarded; acceptance is spoken exactly once', async () => {
  const { session, assistantTexts, posted, sent, functionCalls } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'delegate' } });
  session.handleEvent({ type: 'response.audio_transcript.delta', response_id: 'delegate', delta: 'I will check your files.' });
  session.handleEvent({ type: 'response.audio_transcript.done', response_id: 'delegate', transcript: 'I will check your files.' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'delegate', delta: btoa('aa') });
  session.handleEvent({ type: 'response.audio.done', response_id: 'delegate' });
  await session.playbackOperation;
  assert.deepEqual(assistantTexts, []);
  assert.deepEqual(posted, []);
  const call = { type: 'response.function_call_arguments.done', response_id: 'delegate', name: 'jiuwen_delegate', call_id: 'files', arguments: '{"task":"List desktop files"}' };
  session.handleEvent(call);
  session.handleEvent(call);
  assert.equal(functionCalls.length, 1);
  session.handleEvent({ type: 'response.text.delta', response_id: 'delegate', delta: 'Already working on it.' });
  session.enqueueOperationResult('files', { state: 'accepted', job_id: 'files-job' });
  assert.deepEqual(sent, []);
  session.handleEvent({ type: 'response.done', response: { id: 'delegate', status: 'completed' } });
  assert.deepEqual(assistantTexts, []);
  assert.equal(sent.filter(event => event.type === 'response.create').length, 1);
  session.handleEvent({ type: 'response.created', response: { id: 'accepted' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'accepted', delta: 'Your desktop file request was accepted.' });
  session.handleEvent({ type: 'response.output_audio_transcript.done', response_id: 'accepted', transcript: 'Your desktop file request was accepted.' });
  session.handleEvent({ type: 'response.output_audio.delta', response_id: 'accepted', delta: btoa('bb') });
  session.handleEvent({ type: 'response.done', response: { id: 'accepted', status: 'completed' } });
  session.handleEvent({ type: 'response.done', response: { id: 'accepted', status: 'completed' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'delegate', delta: btoa('cc') });
  await session.playbackOperation;
  assert.deepEqual(assistantTexts.map(item => item.text), ['Your desktop file request was accepted.']);
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
});

test('a spoken action promise and its later tool receipt both retain audio', async () => {
  const { session, assistantTexts, posted, sent, functionCalls } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'wechat-input' });
  session.handleEvent({ type: 'conversation.item.input_audio_transcription.completed',
    item_id: 'wechat-input', transcript: '你打开微信。' });
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'wechat-input' });
  session.handleEvent({ type: 'response.created', response: { id: 'wechat-response' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'wechat-response',
    item: { id: 'promise', type: 'message' } });
  session.handleEvent({ type: 'response.text.done', response_id: 'wechat-response',
    item_id: 'promise', text: '这就帮你把微信打开。' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'wechat-response',
    item_id: 'promise', delta: btoa('aa') });
  await session.playbackOperation;
  assert.equal(assistantTexts.at(-1).text, '这就帮你把微信打开。');
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);

  session.handleEvent({ type: 'response.function_call_arguments.done', response_id: 'wechat-response',
    name: 'jiuwen_delegate', call_id: 'wechat-call', arguments: '{"task":"打开微信"}' });
  session.handleEvent({ type: 'response.done', response: { id: 'wechat-response', status: 'completed' } });
  assert.equal(functionCalls.length, 1);
  assert.deepEqual(assistantTexts.filter(item => item.final).map(item => item.text), ['这就帮你把微信打开。']);
  session.enqueueOperationResult('wechat-call', { state: 'accepted', job_id: 'wechat-job' });
  assert.equal(sent.filter(event => event.type === 'response.create').length, 1);
  session.handleEvent({ type: 'response.created', response: { id: 'wechat-receipt' } });
  session.handleEvent({ type: 'response.text.done', response_id: 'wechat-receipt', text: '微信任务已排队。' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'wechat-receipt', delta: btoa('bb') });
  session.handleEvent({ type: 'response.done', response: { id: 'wechat-receipt', status: 'completed' } });
  await session.playbackOperation;
  assert.deepEqual(assistantTexts.filter(item => item.final).map(item => item.text), ['这就帮你把微信打开。', '微信任务已排队。']);
  assert.equal(posted.filter(item => item.type === 'audio').length, 2);
});

test('an action turn without a tool streams its message text and audio', async () => {
  const { session, assistantTexts, posted } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'action-input' });
  session.handleEvent({ type: 'conversation.item.input_audio_transcription.completed',
    item_id: 'action-input', transcript: '帮我查询任务状态' });
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'action-input' });
  session.handleEvent({ type: 'response.created', response: { id: 'no-tool' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'no-tool',
    item: { id: 'answer', type: 'message' } });
  session.handleEvent({ type: 'response.text.done', response_id: 'no-tool',
    item_id: 'answer', text: '目前没有待执行任务。' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'no-tool',
    item_id: 'answer', delta: btoa('aa') });
  await session.playbackOperation;
  assert.equal(assistantTexts.at(-1).text, '目前没有待执行任务。');
  assert.equal(assistantTexts.at(-1).final, false);
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
  session.handleEvent({ type: 'response.done', response: { id: 'no-tool', status: 'completed' } });
  await session.playbackOperation;
  assert.deepEqual(assistantTexts.filter(item => item.final).map(item => item.text), ['目前没有待执行任务。']);
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
});

test('direct answers release text and audio once, only after response.done', async () => {
  const { session, assistantTexts, posted } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'direct' } });
  session.handleEvent({ type: 'response.text.done', response_id: 'direct', text: 'Hello.' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'direct', delta: btoa('aa') });
  session.handleEvent({ type: 'response.audio.done', response_id: 'direct' });
  await session.playbackOperation;
  assert.deepEqual(assistantTexts, []);
  assert.deepEqual(posted, []);
  session.handleEvent({ type: 'response.done', response: { id: 'direct', status: 'completed' } });
  await session.playbackOperation;
  assert.equal(assistantTexts.length, 1);
  assert.equal(assistantTexts[0].text, 'Hello.');
  assert.deepEqual(posted.map(item => item.type), ['audio', 'drain']);
});

test('a tool listed only in response.done still suppresses the buffered answer', async () => {
  const { session, assistantTexts, posted } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'late-tool' } });
  session.handleEvent({ type: 'response.text.done', response_id: 'late-tool', text: 'I will do it.' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'late-tool', delta: btoa('aa') });
  session.handleEvent({ type: 'response.done', response: { id: 'late-tool', status: 'completed', output: [{ type: 'function_call', name: 'jiuwen_delegate' }] } });
  await session.playbackOperation;
  assert.deepEqual(assistantTexts, []);
  assert.ok(posted.every(item => item.type !== 'audio'));
});

test('message item text and audio stream before response.done and finalize only once', async () => {
  const { session, assistantTexts, posted } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'stream' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'stream', item: { id: 'message', type: 'message' } });
  session.handleEvent({ type: 'response.audio_transcript.delta', response_id: 'stream', delta: 'Hello.' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'stream', item_id: 'message', delta: btoa('aa') });
  await session.playbackOperation;
  assert.deepEqual(posted.map(item => item.type), ['audio']);
  assert.equal(assistantTexts[0].text, 'Hello.');
  assert.equal(assistantTexts[0].final, false);
  session.handleEvent({ type: 'response.audio.done', response_id: 'stream', item_id: 'message' });
  await session.playbackOperation;
  assert.deepEqual(posted.map(item => item.type), ['audio', 'drain']);
  session.handleEvent({ type: 'response.done', response: { id: 'stream', status: 'completed' } });
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 1, 'completion does not replay streamed audio');
  assert.equal(assistantTexts.at(-1).text, 'Hello.');
  assert.equal(assistantTexts.filter(update => update.final).length, 1);
});

for (const streamType of ['text', 'audio_transcript', 'output_audio_transcript']) {
  test(`${streamType} message deltas update one text stream without repeating done content`, () => {
    const { session, assistantTexts } = createSession();
    session.handleEvent({ type: 'response.created', response: { id: 'text-stream' } });
    session.handleEvent({ type: 'response.output_item.added', response_id: 'text-stream', item: { type: 'message', id: 'message' } });
    const delta = text => session.handleEvent({ type: `response.${streamType}.delta`, response_id: 'text-stream', item_id: 'message', delta: text });
    delta('Hello');
    delta(' world');
    assert.deepEqual(assistantTexts.map(update => update.text), ['Hello', 'Hello world']);
    assert.ok(assistantTexts.every(update => !update.final));
    session.handleEvent({ type: `response.${streamType}.done`, response_id: 'text-stream', item_id: 'message', text: 'Hello world', transcript: 'Hello world' });
    assert.equal(assistantTexts.length, 2);
    session.handleEvent({ type: 'response.done', response: { id: 'text-stream', status: 'completed' } });
    session.handleEvent({ type: 'response.done', response: { id: 'text-stream', status: 'completed' } });
    assert.deepEqual(assistantTexts.map(update => [update.text, update.final]), [
      ['Hello', false], ['Hello world', false], ['Hello world', true],
    ]);
  });
}

test('text received before its item type is released once when that message item arrives', () => {
  const { session, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'delayed-text' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'delayed-text', item_id: 'message', delta: 'Hello' });
  assert.equal(assistantTexts.length, 0);
  session.handleEvent({ type: 'response.output_item.added', response_id: 'delayed-text', item: { type: 'message', id: 'other' } });
  assert.equal(assistantTexts.length, 0);
  const added = { type: 'response.output_item.added', response_id: 'delayed-text', item: { type: 'message', id: 'message' } };
  session.handleEvent(added);
  session.handleEvent(added);
  assert.deepEqual(assistantTexts.map(update => [update.text, update.final]), [['Hello', false]]);
});

for (const ending of ['interrupt', 'failed']) {
  test(`streamed text is finalized on ${ending} and late deltas cannot reopen it`, () => {
    const { session, assistantTexts } = createSession();
    session.handleEvent({ type: 'response.created', response: { id: 'partial-text' } });
    const added = { type: 'response.output_item.added', response_id: 'partial-text', item: { type: 'message' } };
    const delta = { type: 'response.text.delta', response_id: 'partial-text', delta: 'Partial answer' };
    session.handleEvent(added);
    session.handleEvent(delta);
    if (ending === 'interrupt') session.interruptQwenResponse('new-turn', 200, 100, 80);
    else session.handleEvent({ type: 'response.done', response: { id: 'partial-text', status: 'failed' } });
    session.handleEvent(added);
    session.handleEvent(delta);
    assert.deepEqual(assistantTexts.map(update => [update.text, update.final]), [['Partial answer', false], ['Partial answer', true]]);
  });
}

test('stopping media finalizes the visible text stream and clears output item state', () => {
  const { session, assistantTexts } = createSession();
  session.socket.close = () => {};
  session.handleEvent({ type: 'response.created', response: { id: 'stop-text' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'stop-text', item: { type: 'message' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'stop-text', delta: 'Partial answer' });
  session.stop();
  assert.deepEqual(assistantTexts.map(update => [update.text, update.final]), [['Partial answer', false], ['Partial answer', true]]);
  assert.equal(session.messageOutputItems.size, 0);
});

test('a message following a tool item retains audio without executing incomplete arguments', async () => {
  const { session, assistantTexts, posted, functionCalls } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'tool' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'tool', delta: btoa('aa') });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'tool', item: { type: 'function_call', call_id: 'call' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'tool', item: { type: 'message' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'tool', delta: btoa('bb') });
  session.handleEvent({ type: 'response.text.done', response_id: 'tool', text: 'Unverified answer' });
  session.handleEvent({ type: 'response.done', response: { id: 'tool', status: 'completed' } });
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
  assert.equal(assistantTexts.at(-1).text, 'Unverified answer');
  assert.equal(assistantTexts.at(-1).final, true);
  assert.deepEqual(functionCalls, [], 'an item announcement must not execute incomplete arguments');
});

test('audio waits for matching item type and a delayed message item releases it only once', async () => {
  const { session, posted } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'delayed' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'delayed', item_id: 'a', delta: btoa('aa') });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'delayed', item_id: 'b', delta: btoa('bb') });
  await session.playbackOperation;
  assert.equal(posted.length, 0);
  const added = { type: 'response.output_item.added', response_id: 'delayed', item: { id: 'a', type: 'message' } };
  session.handleEvent(added);
  session.handleEvent(added);
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
  session.handleEvent({ ...added, item: { id: 'b', type: 'message' } });
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 2);
});

test('late tool item preserves the remaining audio and text of the same message', async () => {
  const { session, posted, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'mixed' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'mixed', item: { type: 'message' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'mixed', delta: '这就帮你查查' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'mixed', delta: btoa('aa') });
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
  session.handleEvent({ type: 'response.output_item.added', response_id: 'mixed', item: { type: 'function_call' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'mixed', delta: '今天香港的天气情况。' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'mixed', delta: btoa('bb') });
  session.handleEvent({ type: 'response.done', response: { id: 'mixed', status: 'completed' } });
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 2);
  assert.deepEqual(assistantTexts.filter(item => item.final).map(item => item.text), ['这就帮你查查今天香港的天气情况。']);
  assert.equal(session.messageOutputItems.size, 0);
});

test('user interruption clears streaming audio and late item events cannot reopen playback', async () => {
  const { session, posted } = createSession();
  const added = { type: 'response.output_item.added', response_id: 'interrupted-stream', item: { type: 'message' } };
  session.handleEvent({ type: 'response.created', response: { id: 'interrupted-stream' } });
  session.handleEvent(added);
  session.handleEvent({ type: 'response.audio.delta', response_id: 'interrupted-stream', delta: btoa('aa') });
  await session.playbackOperation;
  session.interruptQwenResponse('new-turn', 200, 100, 80);
  assert.equal(session.messageOutputItems.size, 0);
  assert.ok(posted.some(item => item.type === 'clear'));
  session.handleEvent(added);
  session.handleEvent({ type: 'response.audio.delta', response_id: 'interrupted-stream', delta: btoa('bb') });
  await session.playbackOperation;
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
});

function createSession(videoFrame = null, callbackOverrides = {}) {
  const states = [];
  const posted = [];
  const sent = [];
  const dispatchedToolResults = [];
  const assistantTexts = [];
  const userTexts = [];
  const diagnostics = [];
  const functionCalls = [];
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => videoFrame,
      onAssistantText: (text, final, toolJobId, responseId) =>
        assistantTexts.push({ text, final, toolJobId, responseId }),
      onUserText: (text, final) => userTexts.push({ text, final }),
      onState: (state) => states.push(state),
      onError: () => undefined,
      onToolResultDispatched: (jobId) => dispatchedToolResults.push(jobId),
      onFunctionCall: (call) => functionCalls.push(call),
      onDiagnostic: (event) => diagnostics.push(event),
      ...callbackOverrides,
    },
  );
  session.playbackNode = { port: { postMessage: (message) => posted.push(message) } };
  session.socket = { readyState: 1, send: (message) => sent.push(JSON.parse(message)) };
  session.sessionReady = true;
  globalThis.WebSocket = { OPEN: 1 };
  return {
    session,
    states,
    posted,
    sent,
    dispatchedToolResults,
    assistantTexts,
    userTexts,
    diagnostics,
    functionCalls,
  };
}

test('playback waits for the target 400ms startup buffer and drains short tails', () => {
  const workletSource = readFileSync(
    new URL('../../frontend/VideoLivePanel/duplex-playback.js', import.meta.url),
    'utf8',
  );
  const workletEvents = [];
  let PlaybackProcessor;
  class FakeAudioWorkletProcessor {
    constructor() {
      this.port = { onmessage: null, postMessage: (message) => workletEvents.push(message) };
    }
  }
  runInNewContext(workletSource, {
    AudioWorkletProcessor: FakeAudioWorkletProcessor,
    Int16Array,
    Math,
    sampleRate: 1_000,
    registerProcessor: (_name, processor) => {
      PlaybackProcessor = processor;
    },
  });
  const processor = new PlaybackProcessor();
  const send = (data) => processor.port.onmessage({ data });
  const render = () => {
    const output = new Float32Array(2);
    processor.process([], [[output]]);
    return Array.from(output).map((sample) => Math.round(sample * 32768));
  };

  send({ type: 'audio', pcm: new Int16Array(400).fill(1000).buffer, responseId: 'response-1' });
  assert.deepEqual(render(), [0, 0]);
  for (let index = 0; index < 199; index += 1) render();
  assert.notDeepEqual(render(), [0, 0]);

  send({ type: 'drain', responseId: 'response-1' });
  for (let index = 0; index < 200; index += 1) render();
  assert.equal(workletEvents.at(-1).type, 'drained');
  assert.equal(workletEvents.at(-1).responseId, 'response-1');
});

test('native transcription events drive user text callbacks', () => {
  const { session, userTexts, diagnostics } = createSession();

  session.handleEvent({
    type: 'conversation.item.input_audio_transcription.delta',
    text: '香',
    stash: '港',
  });
  session.handleEvent({
    type: 'conversation.item.input_audio_transcription.completed',
    transcript: '香港天气',
  });

  assert.deepEqual(userTexts, [
    { text: '香港', final: false },
    { text: '香港天气', final: true },
  ]);
  assert.equal(diagnostics.at(-1).event, 'qwen_native_asr_completed');
});

test('Qwen error diagnostics preserve the original websocket event', () => {
  const errors = [];
  const { session, diagnostics } = createSession(null, {
    onError: (message) => errors.push(message),
  });
  const rawEvent =
    '{"type":"error","error":{"code":"context_length_exceeded","type":"invalid_request_error","message":"input is too long"}}';

  session.handleEvent(JSON.parse(rawEvent), rawEvent);

  assert.equal(errors.at(-1), 'input is too long');
  assert.deepEqual(
    {
      event: diagnostics.at(-1).event,
      raw_event: diagnostics.at(-1).raw_event,
      code: diagnostics.at(-1).code,
      error_type: diagnostics.at(-1).error_type,
      message: diagnostics.at(-1).message,
    },
    {
      event: 'qwen_realtime_error',
      raw_event: rawEvent,
      code: 'context_length_exceeded',
      error_type: 'invalid_request_error',
      message: 'input is too long',
    },
  );
});

test('the first image is deferred until a previous audio append exists', () => {
  const { session, sent } = createSession('dGVzdC1qcGVn');

  session.sendAudio(new Int16Array([1, -1]), true);
  session.sendAudio(new Int16Array([2, -2]), true);

  assert.deepEqual(
    sent.map((event) => event.type),
    ['input_audio_buffer.append', 'input_audio_buffer.append', 'input_image_buffer.append'],
  );
  assert.equal(sent[2].image, 'dGVzdC1qcGVn');
});

test('Qwen drops deferred video at speech end and waits for new speech before resuming video', () => {
  const { session, sent } = createSession('dGVzdC1qcGVn');
  session.sendAudio(new Int16Array([1]), true);
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped' });
  session.sendAudio(new Int16Array([2]), true);
  session.sendAudio(new Int16Array([3]), true);
  assert.deepEqual(sent.map((event) => event.type), Array(3).fill('input_audio_buffer.append'));

  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'new-speech' });
  session.sendAudio(new Int16Array([4]), true);
  session.sendAudio(new Int16Array([5]), true);
  assert.deepEqual(sent.slice(3).map((event) => event.type), [
    'input_audio_buffer.append', 'input_audio_buffer.append', 'input_image_buffer.append',
  ]);
});

test('rejected Qwen image frame stays diagnostic and does not report voice failure', () => {
  const errors = [];
  const { session, sent, diagnostics } = createSession('dGVzdC1qcGVn', {
    onError: (message) => errors.push(message),
  });
  session.sendAudio(new Int16Array([1]), true);
  session.sendAudio(new Int16Array([2]), true);
  session.handleEvent({ type: 'response.created', response: { id: 'answered' } });
  session.handleEvent({ type: 'response.done', response: { id: 'answered', status: 'completed' } });
  session.handleEvent({
    type: 'error', error: { type: 'invalid_request_error', message: 'Error append image before append audio.' },
  });
  assert.deepEqual(errors, []);
  assert.equal(diagnostics.at(-1).event, 'qwen_realtime_error');
  assert.equal(diagnostics.at(-1).message, 'Error append image before append audio.');

  session.sendAudio(new Int16Array([3]), true);
  assert.equal(sent.filter((event) => event.type === 'input_image_buffer.append').length, 1);
  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'later-speech' });
  session.sendAudio(new Int16Array([4]), true);
  session.sendAudio(new Int16Array([5]), true);
  assert.equal(sent.filter((event) => event.type === 'input_image_buffer.append').length, 2);
  session.handleEvent({ type: 'error', error: { message: 'A different provider failure' } });
  assert.deepEqual(errors, ['A different provider failure']);
});

test('user speech cancels an active response and discards unclassified text', () => {
  const { session, posted, sent, states, assistantTexts, diagnostics } = createSession();
  session.responseId = 'qwen-speaking';
  session.responseActive = true;
  session.assistantPlaying = true;
  session.handleEvent({ type: 'response.text.done', response_id: 'qwen-speaking', text: '这是一段被用户打断的回答' });

  assert.equal(session.interruptQwenResponse('voice-1', 240, 900, 350), true);
  assert.equal(session.interruptQwenResponse('voice-1', 260, 900, 350), false);

  assert.deepEqual(sent, [{ type: 'response.cancel' }]);
  assert.deepEqual(posted, [{ type: 'clear', cancelResponse: false }]);
  assert.deepEqual(assistantTexts, []);
  assert.equal(session.heldOutput.size, 0);
  assert.equal(states.at(-1), 'listening');
  assert.equal(diagnostics.at(-1).event, 'qwen_response_interrupted_by_user');
});

test('continuous speech cancels a reserved response only after response.created', () => {
  const errors = [];
  const { session, sent, diagnostics } = createSession(undefined, { onError: (message) => errors.push(message) });
  session.enqueueOperationResult('alarm', { state: 'accepted', job_id: 'alarm-job' });
  assert.equal(session.responseId, null);
  for (let i = 0; i < 5; i++) session.interruptQwenResponse('next-speech', 256 + i * 32, 900, 80);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 0);
  assert.equal(diagnostics.filter((event) => event.event === 'qwen_response_interrupted_by_user').length, 1);
  session.handleEvent({ type: 'response.created', response: { id: 'receipt-response' } });
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 1);
  session.handleEvent({ type: 'response.done', response: { id: 'receipt-response', status: 'cancelled' } });
  session.handleEvent({ type: 'response.created', response: { id: 'next-answer' } });
  session.handleEvent({ type: 'error', error: { message: 'Response request cancelled by new input' } });
  assert.equal(session.responseActive, true, 'an old cancellation must not discard the new answer');
  assert.deepEqual(errors, []);
});

test('server cancellation discards held assistant text', () => {
  const { session, assistantTexts } = createSession();
  session.responseId = 'cancelled-answer';
  session.responseActive = true;
  session.handleEvent({ type: 'response.text.done', response_id: 'cancelled-answer', text: '已经生成的部分回答' });

  session.handleEvent({
    type: 'response.cancelled',
    response_id: 'cancelled-answer',
  });

  assert.deepEqual(assistantTexts, []);
  assert.equal(session.heldOutput.size, 0);
});

test('text input uses a native conversation item and response request', async () => {
  const { session, sent } = createSession();

  assert.equal(await session.sendTextTurn('查询香港天气'), true);
  assert.deepEqual(sent, [
    {
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: '查询香港天气' }],
      },
    },
    { type: 'response.create' },
  ]);
});

test('busy text turns keep their order ahead of task notifications without competing responses', async () => {
  const { session, sent } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'speaking' } });
  session.enqueueToolResult({ jobId: 'a', question: 'report', brief: realtimeBrief('Done') });
  await session.sendTextTurn('First instruction');
  await session.sendTextTurn('Second instruction');
  assert.deepEqual(sent, []);
  session.handleEvent({ type: 'response.done', response: { id: 'speaking' } });
  assert.equal(sent[0].item.content[0].text, 'First instruction');
  assert.equal(sent.length, 2);
  session.handleEvent({ type: 'response.created', response: { id: 'first' } });
  session.handleEvent({ type: 'response.done', response: { id: 'first' } });
  assert.equal(sent[2].item.content[0].text, 'Second instruction');
  assert.equal(sent.length, 4);
  session.handleEvent({ type: 'response.created', response: { id: 'second' } });
  session.handleEvent({ type: 'response.done', response: { id: 'second' } });
  assert.match(sent[4].item.content[0].text, /report/);
  assert.equal(sent.filter(event => event.type === 'response.create').length, 3);
});

test('text waits through provider speech and its automatic response', async () => {
  const { session, sent } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_started' });
  await session.sendTextTurn('A typed instruction');
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped' });
  assert.deepEqual(sent, []);
  session.handleEvent({ type: 'response.created', response: { id: 'voice' } });
  assert.deepEqual(sent, []);
  session.handleEvent({ type: 'response.done', response: { id: 'voice' } });
  assert.equal(sent[0].item.content[0].text, 'A typed instruction');
  assert.equal(sent.length, 2);
});

test('stopping the connection discards queued text before a new media session', async () => {
  const { session, sent } = createSession();
  session.socket.close = () => {};
  session.handleEvent({ type: 'response.created', response: { id: 'old' } });
  await session.sendTextTurn('Old queued instruction');
  session.stop();
  sent.length = 0;
  session.socket = { readyState: 1, send: message => sent.push(JSON.parse(message)) };
  session.sessionReady = true;
  await session.sendTextTurn('New instruction');
  assert.equal(sent[0].item.content[0].text, 'New instruction');
  assert.equal(sent.length, 2);
});

test('function calls are emitted once with parsed arguments', () => {
  const { session, functionCalls, diagnostics } = createSession();
  const event = {
    type: 'response.function_call_arguments.done',
    name: 'jiuwen_delegate',
    call_id: 'call-file',
    arguments: '{"task":"打开桌面的复习提纲并转换为 PDF"}',
  };

  session.handleEvent(event);
  session.handleEvent(event);

  assert.deepEqual(functionCalls, [
    {
      name: 'jiuwen_delegate',
      callId: 'call-file',
      arguments: '{"task":"打开桌面的复习提纲并转换为 PDF"}',
      task: '打开桌面的复习提纲并转换为 PDF',
    },
  ]);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_call_received');
});

test('a separate tool response preserves the earlier message and its delayed audio', async () => {
  const { session, assistantTexts, posted, functionCalls } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'weather-input' });
  session.handleEvent({ type: 'input_audio_buffer.speech_stopped', item_id: 'weather-input' });
  session.handleEvent({ type: 'response.created', response: { id: 'weather-ack' } });
  session.handleEvent({ type: 'response.output_item.added', response_id: 'weather-ack',
    item: { type: 'message', id: 'weather-message' } });
  session.handleEvent({ type: 'response.audio_transcript.delta', response_id: 'weather-ack',
    item_id: 'weather-message', delta: '这就帮你查查今天香港的天气情况。' });
  session.handleEvent({ type: 'response.created', response: { id: 'weather-tool' } });
  session.handleEvent({ type: 'response.function_call_arguments.done', response_id: 'weather-tool',
    name: 'jiuwen_delegate', call_id: 'weather-call', arguments: '{"task":"查询香港天气"}' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'weather-ack',
    item_id: 'weather-message', delta: btoa('aa') });
  session.handleEvent({ type: 'response.done', response: { id: 'weather-ack', status: 'completed' } });
  session.handleEvent({ type: 'response.done', response: { id: 'weather-tool', status: 'completed' } });
  await session.playbackOperation;
  assert.equal(functionCalls.length, 1);
  assert.equal(posted.filter(item => item.type === 'audio').length, 1);
  assert.deepEqual(assistantTexts.filter(item => item.final).map(item => item.text), ['这就帮你查查今天香港的天气情况。']);
});

test('a new response cannot release an earlier unclassified acknowledgement', () => {
  const { session, assistantTexts } = createSession();
  session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'input-task' });
  session.handleEvent({ type: 'response.created', response: { id: 'ack' } });
  session.handleEvent({ type: 'response.audio_transcript.delta', response_id: 'ack', delta: '我来处理。' });
  session.handleEvent({ type: 'response.created', response: { id: 'delegate' } });
  session.handleEvent({ type: 'response.function_call_arguments.done', response_id: 'delegate', name: 'jiuwen_delegate', call_id: 'split-call', arguments: '{"task":"List files"}' });
  session.handleEvent({ type: 'response.done', response: { id: 'ack', status: 'completed' } });
  session.handleEvent({ type: 'response.done', response: { id: 'delegate', status: 'completed' } });
  assert.deepEqual(assistantTexts, []);
});

test('Qwen tool receipt remains visible and response.done finalizes it', () => {
  const { session, assistantTexts } = createSession();
  session.enqueueToolResult({
    jobId: 'code-job',
    question: '请生成代码',
    callId: 'code-call',
    brief: realtimeBrief('代码已显示在界面中。', 'code'),
  });
  session.handleEvent({ type: 'response.created', response: { id: 'receipt' } });
  session.handleEvent({
    type: 'response.audio_transcript.delta',
    response_id: 'receipt',
    delta: '代码已显示在界面中。',
  });
  session.handleEvent({ type: 'response.done', response: { id: 'receipt' } });
  assert.deepEqual(assistantTexts.at(-1), {
    text: '代码已显示在界面中。',
    final: true,
    toolJobId: 'code-job',
    responseId: 'receipt',
  });
});

test('Qwen provider error discards the unclassified partial answer', () => {
  const { session, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'partial' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'partial', delta: '已经生成的部分' });
  session.handleEvent({ type: 'error', error: { code: 'COMMON_ERROR', message: 'model repeat output happened' } });
  assert.deepEqual(assistantTexts, []);
  assert.equal(session.heldOutput.size, 0);
});

test('delegate calls accept query aliases and object arguments from Qwen', () => {
  const { session, functionCalls } = createSession();

  session.handleEvent({
    type: 'response.function_call_arguments.done',
    name: 'jiuwen_delegate',
    call_id: 'call-query-alias',
    arguments: { query: '查询香港今天的天气' },
  });

  assert.equal(functionCalls.length, 1);
  assert.equal(functionCalls[0].task, '查询香港今天的天气');
  assert.equal(functionCalls[0].arguments, '{"query":"查询香港今天的天气"}');
});

test('tool results wait for active generation but dispatch during queued audio playback', () => {
  const { session, sent, dispatchedToolResults, assistantTexts } = createSession();
  session.responseId = 'acknowledgement';
  session.responseActive = true;
  session.assistantPlaying = true;
  assert.equal(
    session.enqueueToolResult({
      jobId: 'search-weather',
      question: '香港今天的天气',
      brief: realtimeBrief('香港今天有雨，出门记得带伞。', 'research'),
      result: '```python\nprint("这段完整代码绝不能发送给Qwen")\n```',
      callId: 'call-weather',
    }),
    true,
  );

  assert.equal(session.pendingToolResults.length, 1);
  assert.deepEqual(sent, []);

  session.handleEvent({ type: 'response.done', response_id: 'acknowledgement' });

  assert.equal(session.pendingToolResults.length, 0);
  assert.equal(session.assistantPlaying, true);
  assert.deepEqual(dispatchedToolResults, ['search-weather']);
  assert.deepEqual(
    sent.map((event) => event.type),
    ['conversation.item.create', 'conversation.item.create', 'response.create'],
  );
  assert.equal(sent[0].item.type, 'function_call_output');
  assert.match(sent[0].item.output, /香港今天有雨/);
  assert.doesNotMatch(sent[0].item.output, /完整代码绝不能发送给Qwen/);
  assert.match(sent[1].item.content[0].text, /authoritative full answer is already visible/i);
  assert.match(sent[1].item.content[0].text, /A completed invocation may report pending work, partial success or a blocked action/);
  assert.doesNotMatch(sent[1].item.content[0].text, /This completes the earlier task/);

  session.handleEvent({ type: 'response.created', response: { id: 'answer-weather' } });
  session.handleEvent({
    type: 'response.text.delta',
    response_id: 'answer-weather',
    delta: '香港今天有雨',
  });
  assert.deepEqual(assistantTexts, []);
  assert.equal(session.assistantPlaying, true);
  session.handleEvent({
    type: 'response.text.done',
    response_id: 'answer-weather',
    text: '香港今天有雨，出门请带伞。',
  });
  assert.deepEqual(assistantTexts, []);
  session.handleEvent({ type: 'response.done', response: { id: 'answer-weather' } });
  assert.equal(assistantTexts.at(-1).toolJobId, 'search-weather');
  assert.equal(assistantTexts.at(-1).text, '香港今天有雨，出门请带伞。');
});

test('an earlier task still gets a spoken follow-up after a newer question', () => {
  const { session, sent, dispatchedToolResults, diagnostics } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'new-question' } });

  assert.equal(
    session.enqueueToolResult({
      jobId: 'old-file-task',
      turnId: 'turn-old',
      question: '打开旧文件',
      brief: realtimeBrief('旧文件任务已完成。', 'file'),
      callId: 'call-old-file',
    }),
    true,
  );

  assert.deepEqual(sent, []);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_result_waiting');
  session.handleEvent({ type: 'response.done', response_id: 'new-question' });
  assert.deepEqual(dispatchedToolResults, ['old-file-task']);
  assert.deepEqual(
    sent.map((event) => event.type),
    ['conversation.item.create', 'conversation.item.create', 'response.create'],
  );
  assert.equal(sent[0].item.type, 'function_call_output');
  const output = JSON.parse(sent[0].item.output);
  assert.equal(output.summary, '旧文件任务已完成。');
  assert.deepEqual(output.task_context, {
    job_id: 'old-file-task',
    turn_id: 'turn-old',
    original_question: '打开旧文件',
  });
  assert.match(sent[1].item.content[0].text, /even if the user has asked other questions/);
  assert.doesNotMatch(sent[0].item.output, /Do not answer it again/);
  assert.equal(session.responseActive, true);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_result_returned');
});

test('a completed earlier task avoids a function output after a newer spoken input', () => {
  const { session, sent } = createSession();
  session.handleEvent({ type: 'response.function_call_arguments.done', name: 'jiuwen_delegate',
    call_id: 'call-earlier', arguments: JSON.stringify({ task: 'Read a file' }) });
  session.handleEvent({ type: 'conversation.item.input_audio_transcription.completed',
    item_id: 'new-input', transcript: 'Check the weather' });
  session.handleEvent({ type: 'response.created', response: { id: 'new-answer' } });
  session.enqueueToolResult({ jobId: 'earlier-job', question: 'Read a file',
    brief: realtimeBrief('The file was read.', 'file'), callId: 'call-earlier' });
  session.handleEvent({ type: 'response.done', response: { id: 'new-answer' } });

  assert.deepEqual(sent.map((event) => event.type), ['conversation.item.create', 'response.create']);
  assert.equal(sent[0].item.type, 'message');
  assert.match(sent[0].item.content[0].text, /earlier-job/);
});

test('each task waits for user speech to finish and is sent exactly once in completion order', () => {
  const { session, sent, diagnostics } = createSession();
  session.userActivityActive = true;
  const first = {
    jobId: 'first-async-task',
    turnId: 'old-turn',
    question: '打开复习提纲',
    brief: realtimeBrief('复习提纲已打开。'),
    callId: 'call-first-async',
  };
  const second = {
    jobId: 'second-async-task',
    turnId: 'another-turn',
    question: '解释差分约束',
    brief: realtimeBrief('差分约束的解题思路已整理。'),
    callId: 'call-second-async',
  };
  assert.equal(session.enqueueToolResult(first), true);
  assert.equal(session.enqueueToolResult(second), true);
  assert.equal(session.enqueueToolResult(first), false);
  assert.deepEqual(sent, []);
  assert.equal(diagnostics.at(-1).event, 'search_result_queued');
  session.dispatchQueuedToolResult();
  session.dispatchQueuedToolResult();
  assert.equal(diagnostics.filter((event) => event.event === 'qwen_tool_result_waiting').length, 1);
  session.userActivityActive = false;
  session.dispatchQueuedToolResult();
  assert.equal(sent[0].item.call_id, 'call-first-async');
  session.handleEvent({ type: 'response.created', response: { id: 'first-receipt' } });
  session.handleEvent({ type: 'response.done', response_id: 'first-receipt' });
  assert.equal(sent[3].item.call_id, 'call-second-async');
  assert.equal(JSON.parse(sent[3].item.output).task_context.original_question, '解释差分约束');
  assert.equal(session.enqueueToolResult(first), false);
});

test('task deduplication lasts for the entire session rather than just the last 32 results', () => {
  const { session } = createSession();
  session.responseActive = true;
  const task = (index) => ({
    jobId: `job-${index}`,
    question: `任务${index}`,
    brief: realtimeBrief('完成'),
    callId: `call-${index}`,
  });
  for (let index = 0; index < 40; index++) assert.equal(session.enqueueToolResult(task(index)), true);
  assert.equal(session.enqueueToolResult(task(0)), false);
  assert.equal(session.pendingToolResults.length, 40);
});

test('a disconnected socket does not consume a queued tool result', () => {
  const { session, sent, diagnostics } = createSession();
  session.socket.readyState = 3;
  session.enqueueToolResult({
    jobId: 'waiting-for-connection',
    question: '查看文档',
    brief: realtimeBrief('已查看'),
    callId: 'call-wait',
  });
  assert.equal(session.pendingToolResults.length, 1);
  assert.equal(diagnostics.at(-1).reason, 'connection_not_ready');
  assert.deepEqual(sent, []);
  session.socket.readyState = 1;
  session.dispatchQueuedToolResult();
  assert.equal(session.pendingToolResults.length, 0);
  assert.equal(sent.at(-1).type, 'response.create');
});

test('tool responses retain their own job id after another result is queued', () => {
  const { session, assistantTexts } = createSession();

  session.enqueueToolResult({
    jobId: 'job-first',
    question: '第一个问题',
    brief: realtimeBrief('第一个结果已完成。'),
    callId: 'call-first',
  });
  session.enqueueToolResult({
    jobId: 'job-second',
    question: '第二个问题',
    brief: realtimeBrief('第二个结果已完成。'),
    callId: 'call-second',
  });
  session.handleEvent({ type: 'response.created', response: { id: 'response-first' } });
  session.handleEvent({
    type: 'response.text.delta',
    response_id: 'response-first',
    delta: '第一个回答',
  });

  assert.deepEqual(assistantTexts, []);

  session.handleEvent({ type: 'response.done', response_id: 'response-first' });
  assert.equal(assistantTexts.at(-1).toolJobId, 'job-first');
  session.handleEvent({ type: 'response.created', response: { id: 'response-second' } });
  session.handleEvent({
    type: 'response.text.delta',
    response_id: 'response-second',
    delta: '第二个回答',
  });

  session.handleEvent({ type: 'response.done', response_id: 'response-second' });
  assert.equal(assistantTexts.at(-1).toolJobId, 'job-second');
});

test('session update includes Gateway-provided tools', async () => {
  const tools = [{ type: 'function', function: { name: 'jiuwen_delegate' } }];
  let socket;
  class StartupSocket {
    static OPEN = 1;
    constructor() {
      socket = this;
      this.readyState = StartupSocket.OPEN;
      this.sent = [];
    }
    close() {}
    send(message) {
      this.sent.push(JSON.parse(message));
    }
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = StartupSocket;
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime', tools },
    {
      getVideoFrame: () => null,
      onAssistantText: () => undefined,
      onUserText: () => undefined,
      onState: () => undefined,
      onError: () => undefined,
    },
  );

  const opening = session.openSocket();
  socket.onopen();
  await opening;

  assert.deepEqual(socket.sent[0].session.tools, tools);

  assert.match(socket.sent[0].session.instructions, /call the required tool before giving a spoken acknowledgement/);
  assert.match(socket.sent[0].session.instructions, /acknowledgement describes work in progress only/);
  assert.match(
    socket.sent[0].session.instructions,
    /Until the function result for that specific request arrives, do not provide facts/,
  );
  assert.match(socket.sent[0].session.instructions, /file access, document processing/);
});

test('decoded response chunks stay ordered before the playback drain', async () => {
  const { session, posted } = createSession();
  const decoded = [];
  session.decodeOutputAudio = async (_event, encoded) => {
    if (encoded === 'first') await new Promise((resolve) => setTimeout(resolve, 5));
    decoded.push(encoded);
    return new Int16Array(encoded === 'first' ? [1] : [2]);
  };

  session.handleEvent({ type: 'response.created', response: { id: 'response-ordered' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'response-ordered', delta: 'first' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'response-ordered', delta: 'second' });
  session.handleEvent({ type: 'response.done', response_id: 'response-ordered' });
  await new Promise((resolve) => setTimeout(resolve, 15));

  assert.deepEqual(decoded, ['first', 'second']);
  assert.deepEqual(
    posted.map((message) => message.type),
    ['audio', 'audio', 'drain'],
  );
});

test('session.closed before session.created rejects startup with the backend reason', async () => {
  const diagnostics = [];
  let socket;
  class StartupSocket {
    static OPEN = 1;
    constructor() {
      socket = this;
      this.readyState = StartupSocket.OPEN;
    }
    close() {}
    send() {}
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = StartupSocket;
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => null,
      onAssistantText: () => undefined,
      onUserText: () => undefined,
      onState: () => undefined,
      onError: () => undefined,
      onDiagnostic: (event) => diagnostics.push(event),
    },
  );

  const opening = session.openSocket();
  socket.onmessage({ data: JSON.stringify({ type: 'session.closed', reason: 'backend_error' }) });

  await assert.rejects(opening, /Realtime 会话初始化失败：backend_error/);
  assert.equal(diagnostics.at(-1).event, 'realtime_websocket_error');
});

test('upstream startup error is not overwritten by the relay normal close', async () => {
  let socket;
  const errors = [];
  class StartupSocket {
    static OPEN = 1;
    constructor() { socket = this; this.readyState = 1; }
    close() { this.readyState = 3; }
    send() {}
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = StartupSocket;
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => null,
      onAssistantText: () => undefined,
      onUserText: () => undefined,
      onState: () => undefined,
      onError: (message) => errors.push(message),
    },
  );
  const opening = session.openSocket();
  socket.onopen();
  await opening;
  socket.onmessage({ data: JSON.stringify({
    type: 'error', error: { code: 'qwen_gateway_upstream_error', message: 'HTTP 403: Workspace endpoint access denied.' },
  }) });
  socket.onclose({ code: 1000, reason: '' });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /403/);
  assert.doesNotMatch(errors[0], /主动关闭/);
});

test('remote disconnect releases media resources and pending receipts without losing received text', async () => {
  let socket;
  class ClosingSocket {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      socket = this;
    }
    close() {
      this.readyState = 3;
    }
    send() {}
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = ClosingSocket;
  const texts = [];
  const states = [];
  const released = [];
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => null,
      onAssistantText: (text) => texts.push(text),
      onUserText() {},
      onError() {},
      onState: (state) => states.push(state),
    },
  );
  const opening = session.openSocket();
  socket.onopen();
  await opening;
  session.sessionReady = true;
  session.handleEvent({ type: 'response.created', response: { id: 'received-answer' } });
  session.handleEvent({ type: 'response.text.done', response_id: 'received-answer', text: '已经收到的回答' });
  session.handleEvent({ type: 'response.done', response: { id: 'received-answer', status: 'completed' } });
  session.microphone = { getTracks: () => [{ stop: () => released.push('microphone') }] };
  session.captureContext = { close: () => released.push('capture') };
  session.playbackContext = { close: () => released.push('playback') };
  session.pendingToolResults = [{ jobId: 'old-job' }];
  session.sendTimer = setInterval(() => {}, 60_000);
  socket.readyState = 3;
  socket.onclose({ code: 1006, reason: 'network interrupted' });
  assert.deepEqual(released, ['microphone', 'capture', 'playback']);
  assert.deepEqual(session.pendingToolResults, []);
  assert.equal(session.sendTimer, null);
  assert.equal(session.socket, null);
  assert.equal(texts.at(-1), '已经收到的回答');
  assert.equal(states.at(-1), 'closed');
});


test('task operation receipts wait for the active response and retain accepted semantics', () => {
  const { session, sent } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'speaking' } });
  session.enqueueOperationResult('modify-call', { task_id: 'task-a', state: 'pending' });
  assert.equal(sent.length, 0);
  session.handleEvent({ type: 'response.done', response_id: 'speaking' });
  assert.deepEqual(sent.map(e => e.type), ['conversation.item.create', 'response.create']);
  assert.equal(sent[0].item.call_id, 'modify-call');
  assert.equal(JSON.parse(sent[0].item.output).state, 'pending');
  session.enqueueOperationResult('modify-call', { state: 'pending' });
  assert.equal(sent.length, 2);
  assert.equal(sent.filter(e => e.item?.type === 'message').length, 0, 'no completion notice is fabricated');
});


test('late tool call retains its input item instead of a newer transcript', () => {
  const {session,functionCalls}=createSession();
  session.handleEvent({type:'input_audio_buffer.speech_started',item_id:'input-a'});
  session.handleEvent({type:'conversation.item.input_audio_transcription.completed',item_id:'input-a',transcript:'生成沈阳行程'});
  session.handleEvent({type:'response.created',response:{id:'response-a'}});
  session.handleEvent({type:'input_audio_buffer.speech_started',item_id:'input-b'});
  session.handleEvent({type:'conversation.item.input_audio_transcription.completed',item_id:'input-b',transcript:'生成杭州行程'});
  session.handleEvent({type:'response.function_call_arguments.done',response_id:'response-a',name:'jiuwen_delegate',call_id:'call-a',arguments:JSON.stringify({task:'生成长春行程'})});
  assert.equal(functionCalls[0].originalInstruction,'生成沈阳行程');
  assert.equal(functionCalls[0].inputId,'input-a');
});


test('successor completion is a notification without an invented function call', () => {
  const {session,sent}=createSession();
  assert.equal(session.enqueueToolResult({jobId:'child',question:'杭州两日修订',brief:realtimeBrief('两日文件已生成')}),true);
  assert.equal(sent.filter(e=>e.item?.type==='function_call_output').length,0);
  assert.equal(sent.filter(e=>e.type==='response.create').length,1);
  assert.match(sent[0].item.content[0].text,/杭州两日修订/);
});


const contractCases = JSON.parse(readFileSync(new URL('../task_tool_contract.json', import.meta.url), 'utf8'));
for (const sample of contractCases) {
  test(`shared tool transport contract: ${sample.id}`, () => {
    const { session, functionCalls } = createSession();
    session.handleEvent({ type: 'response.function_call_arguments.done', name: sample.name,
      call_id: sample.id, arguments: JSON.stringify(sample.arguments) });
    assert.equal(functionCalls.length, sample.client_accepts ? 1 : 0);
    if (sample.client_accepts) assert.deepEqual(JSON.parse(functionCalls[0].arguments), sample.arguments);
    // The browser transports control arguments; the server owns their validation.
  });
}


async function createConnectedSocketSession() {
  let socket;
  class FakeSocket {
    static OPEN = 1;
    readyState = 1;
    constructor() { socket = this; }
    send() {}
    close() { this.readyState = 3; }
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = FakeSocket;
  const errors = [];
  const session = new RealtimeDuplexSession({ url: 'ws://example.test/realtime' }, {
    getVideoFrame: () => null, onAssistantText() {}, onUserText() {}, onState() {},
    onError: (message) => errors.push(message),
  });
  const opening = session.openSocket();
  socket.onopen();
  await opening;
  session.sessionReady = true;
  return { session, socket, errors };
}

for (const code of [1000, 1006, 1011]) {
  test(`remote close ${code} always displays a reason`, async () => {
    const { socket, errors } = await createConnectedSocketSession();
    socket.onclose({ code, reason: 'quota exhausted' });
    assert.match(errors.at(-1), /quota exhausted/);
  });
}

test('quota error survives gateway closure and a subsequent generic socket close', async () => {
  const { socket, errors } = await createConnectedSocketSession();
  socket.onmessage({ data: JSON.stringify({ type: 'error', error: { message: 'quota exhausted' } }) });
  socket.onmessage({ data: JSON.stringify({ type: 'error', error: { code: 'qwen_gateway_upstream_closed', message: 'closed' } }) });
  socket.onclose({ code: 1000, reason: '' });
  assert.equal(errors.at(-1), 'quota exhausted');
});

test('session.closed after startup displays the provider reason and releases the session', async () => {
  const { session, socket, errors } = await createConnectedSocketSession();
  socket.onmessage({ data: JSON.stringify({ type: 'session.closed', reason: 'quota exhausted' }) });
  assert.match(errors.at(-1), /quota exhausted/);
  assert.equal(session.socket, null);
});

test('failed response and socket error after startup are visible', async () => {
  const { session, socket, errors } = await createConnectedSocketSession();
  socket.onerror();
  assert.equal(errors.length, 1);
  socket.onmessage({ data: JSON.stringify({ type: 'response.done', response: {
    status: 'failed', status_details: { error: { message: 'insufficient_quota' } },
  } }) });
  socket.onclose({ code: 1000, reason: '' });
  assert.equal(errors.at(-1), 'insufficient_quota');
  assert.equal(session.socket, null);
});

test('remote close without a reason has a fallback, while manual stop stays silent', async () => {
  const remote = await createConnectedSocketSession();
  remote.socket.onclose({ code: 1000, reason: '' });
  assert.match(remote.errors.at(-1), /1000/);
  assert.ok(remote.errors.at(-1).length > 20);
  const local = await createConnectedSocketSession();
  local.session.stop();
  local.socket.onclose({ code: 1000, reason: '' });
  assert.deepEqual(local.errors, []);
});

function contextSocket() {
  let socket;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    sent = [];
    constructor() { socket = this; }
    send(message) { this.sent.push(JSON.parse(message)); }
    close() {}
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = Socket;
  const texts = [], calls = [], states = [];
  const session = new RealtimeDuplexSession({
    url: 'ws://example.test/realtime',
    restoreInstructions: 'Do not acknowledge this background\n' + JSON.stringify({ session_id: 'conversation-a', memory: [{ role: 'user', text: 'My name is Alice' }] }),
  }, {
    getVideoFrame: () => null, onAssistantText: text => texts.push(text),
    onUserText() {}, onError() {}, onState: state => states.push(state),
    onFunctionCall: call => calls.push(call),
  });
  const opening = session.openSocket();
  socket.onopen();
  return { session, socket, opening, texts, calls, states };
}

test('context restore initializes silently and waits for session.updated', async () => {
  const { session, socket, opening, texts, calls } = contextSocket();
  let ready = false;
  opening.then(() => { ready = true; });
  const event = value => socket.onmessage({ data: JSON.stringify(value) });
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].type, 'session.update');
  assert.match(socket.sent[0].session.instructions, /My name is Alice/);
  assert.match(socket.sent[0].session.instructions, /Do not acknowledge this background/);
  event({ type: 'session.created' });
  event({ type: 'response.created', response: { id: 'unsolicited' } });
  event({ type: 'response.text.done', response_id: 'unsolicited', text: 'I remember Alice' });
  event({ type: 'response.function_call_arguments.done', name: 'jiuwen_delegate', call_id: 'historical', arguments: '{"task":"Repeat old task"}' });
  event({ type: 'response.done', response: { id: 'unsolicited', status: 'completed' } });
  await Promise.resolve();
  assert.equal(ready, false);
  assert.equal(session.sessionReady, false);
  assert.deepEqual(texts, []);
  assert.deepEqual(calls, []);
  event({ type: 'session.updated' });
  await opening;
  assert.equal(session.sessionReady, true);
  assert.deepEqual(socket.sent.map(item => item.type), ['session.update']);
  event({ type: 'response.created', response: { id: 'new-answer' } });
  event({ type: 'response.text.done', response_id: 'new-answer', text: 'Hello Alice' });
  event({ type: 'response.done', response: { id: 'new-answer', status: 'completed' } });
  assert.deepEqual(texts, ['Hello Alice']);
});

test('cancelling context initialization rejects the old startup and ignores late acknowledgement', async () => {
  const { session, socket, opening, states } = contextSocket();
  const rejected = assert.rejects(opening, /cancelled/);
  session.stop();
  await rejected;
  socket.onmessage({ data: JSON.stringify({ type: 'session.updated' }) });
  assert.equal(session.sessionReady, false);
  assert.equal(states.at(-1), 'closed');
});


test('completed cancellation receipt permits Core follow-up for the user requested effects', () => {
  const { session, sent, functionCalls } = createSession();
  session.enqueueOperationResult('stop-created', {
    task_id: 'created', task_status: 'completed', state: 'completed', stopped: false,
  });
  const continuation = sent.find(event => event.type === 'response.create').response.instructions;
  assert.match(continuation, /call jiuwen_queue_manage/);
  assert.doesNotMatch(continuation, /call jiuwen_task_modify/);
  assert.match(continuation, /Further external actions may also be delegated to Core/);
  assert.match(continuation, /never repeat the same ineffective cancellation/);
  session.handleEvent({ type: 'response.created', response: { id: 'continue-effects' } });
  session.handleEvent({ type: 'response.function_call_arguments.done', name: 'jiuwen_queue_manage',
    call_id: 'remove-effect', response_id: 'continue-effects',
    arguments: JSON.stringify({ instruction: 'Remove the previously created result for job created and verify' }),
  });
  assert.equal(functionCalls.length, 1);
  assert.equal(functionCalls[0].name, 'jiuwen_queue_manage');
});


test('converted completed cancellation is announced as a pending Core followup', () => {
  const { session, sent } = createSession();
  session.enqueueOperationResult('cancel', {
    state: 'followup', converted_to: 'modify', task_status: 'completed', stopped: false, successor_id: 'child',
  });
  assert.equal(sent.filter(event => event.item?.type === 'function_call_output').length, 1);
  const continuation = sent.find(event => event.type === 'response.create').response.instructions;
  assert.match(continuation, /backend already submitted the original user action to Core/);
  assert.match(continuation, /do not issue another cancellation/);
  assert.match(continuation, /followup means a linked revision/);
});

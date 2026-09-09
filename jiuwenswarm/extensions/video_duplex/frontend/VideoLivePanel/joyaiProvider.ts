import { webClient, webRequest } from '../../../../channels/web/frontend/src/services/webClient';
import {
  canPlayJoyAIResponse,
  JoyAITtsInterruptionState,
  JoyAIVoiceSession,
} from './joyaiVoice';
import {
  assistantSpeechText,
} from './searchPresentation';
import {
  AgentAction,
  JoyAIFrameResult,
  SearchJobPayload,
  SearchJobState,
  TtsStreamPayload,
} from './types';
import {
  JoyAIFrameClock,
  JoyAIPromptLifecycle,
} from './joyaiPromptLifecycle';
import {
  buildJoyAIToolContextBatch,
  JoyAIToolContextEntry,
  rememberJoyAIToolContext,
  removeSentJoyAIToolContext,
} from './joyaiToolContext';

const FRAME_POLL_INTERVAL_MS = 1_000;
const RATE_LIMIT_BASE_COOLDOWN_MS = 60_000;
const RATE_LIMIT_MAX_COOLDOWN_MS = 5 * 60_000;
const FRAME_POLL_CLIENT_BUILD = 'joyai-tool-context-v11';

function isJoyAIRateLimit(error: unknown): boolean {
  const candidate = error as { code?: string; message?: string } | null;
  const message = candidate?.message || '';
  return candidate?.code === 'JOYAI_RATE_LIMIT'
    || /(?:\b429\b|RATE_LIMIT|tokens limit for minute)/i.test(message);
}

export interface JoyAIProviderCallbacks {
  getLatestFrameDataUrl: () => string;
  getFrameCount: () => number;
  getSearchSessionId: () => string;
  hasPendingTranscriptions: () => boolean;
  beginTranscription: () => void;
  finishTranscription: () => void;
  appendChat: (role: 'user' | 'assistant' | 'tool', text: string) => void;
  commitAssistantAnswer: (text: string, toolJobId?: string) => void;
  rememberSearchJob: (job: AgentAction['search_job']) => void;
  updateSearchJob: (job: SearchJobState) => void;
  setAwaitingVoiceTranscript: (awaiting: boolean) => void;
  setError: (message: string) => void;
  setToolStatus: (status: string) => void;
  setRecording: (recording: boolean) => void;
  setStatus: (status: string) => void;
  setStarting: (starting: boolean) => void;
  report: (event: string, details?: Record<string, unknown>) => void;
}

export class JoyAIProvider {
  private callbacks: JoyAIProviderCallbacks;
  private voice: JoyAIVoiceSession | null = null;
  private sessionId = '';
  private framePollTimer: number | null = null;
  private requestQueue: Promise<void> = Promise.resolve();
  private queuedRequestCount = 0;
  private promptLifecycle = new JoyAIPromptLifecycle<JoyAIFrameResult>();
  private frameClock = new JoyAIFrameClock(FRAME_POLL_INTERVAL_MS);
  private handledTurns = new Set<string>();
  private ttsGeneration = 0;
  private activeTtsText = '';
  private ttsInterruption = new JoyAITtsInterruptionState();
  private userSpeechActive = false;
  private userSpeechEpoch = 0;
  private ttsQueue: Promise<void> = Promise.resolve();
  private activeTtsStream = '';
  private searchDeliveryQueue: Promise<void> = Promise.resolve();
  private rateLimitStrikes = 0;
  private framePollingPausedUntil = 0;
  private pendingToolContext: JoyAIToolContextEntry[] = [];

  constructor(callbacks: JoyAIProviderCallbacks) {
    this.callbacks = callbacks;
  }

  updateCallbacks(callbacks: JoyAIProviderCallbacks): void {
    this.callbacks = callbacks;
  }

  get active(): boolean {
    return Boolean(this.sessionId);
  }

  async start(): Promise<void> {
    if (this.active) return;
    const sessionId = crypto.randomUUID();
    this.sessionId = sessionId;
    this.userSpeechActive = false;
    this.userSpeechEpoch += 1;
    this.promptLifecycle.reset();
    this.frameClock.reset();
    this.rateLimitStrikes = 0;
    this.framePollingPausedUntil = 0;
    this.pendingToolContext = [];
    this.startFramePolling();

    const voice = new JoyAIVoiceSession({
      onSpeechStart: () => {
        this.userSpeechActive = true;
        this.userSpeechEpoch += 1;
        this.ttsInterruption.capture(this.activeTtsText, this.userSpeechEpoch);
        this.interruptTts();
        this.callbacks.report('joyai_barge_in_started', {
          speech_epoch: this.userSpeechEpoch,
          tts_generation: this.ttsGeneration,
        });
      },
      onTurnAudio: (audioDataUrl, turnId) => {
        void this.handleTurnAudio(audioDataUrl, turnId, sessionId);
      },
      onState: (state) => {
        if (state === 'closed') {
          this.callbacks.setRecording(false);
          this.callbacks.setStatus('');
          return;
        }
        this.callbacks.setRecording(true);
        this.callbacks.setStatus(state === 'connecting'
          ? '正在申请麦克风权限…'
          : state === 'speaking'
            ? '模型正在回答…'
            : '');
        if (state === 'listening') this.callbacks.setStarting(false);
      },
      onError: this.callbacks.setError,
    });
    this.voice = voice;
    this.callbacks.setStatus('正在申请麦克风权限…');
    await voice.start();
  }

  stop(): void {
    this.ttsGeneration += 1;
    this.activeTtsText = '';
    this.ttsInterruption.discard();
    this.userSpeechActive = false;
    this.userSpeechEpoch += 1;
    const activeTtsStream = this.activeTtsStream;
    this.activeTtsStream = '';
    if (activeTtsStream) {
      void webRequest('tts.stream.cancel', { stream_id: activeTtsStream }, { timeoutMs: 5_000 })
        .catch(() => undefined);
    }
    this.voice?.stop();
    this.voice = null;
    this.ttsQueue = Promise.resolve();
    this.searchDeliveryQueue = Promise.resolve();
    this.sessionId = '';
    this.requestQueue = Promise.resolve();
    this.queuedRequestCount = 0;
    this.promptLifecycle.reset();
    this.frameClock.reset();
    this.rateLimitStrikes = 0;
    this.framePollingPausedUntil = 0;
    this.pendingToolContext = [];
    this.handledTurns.clear();
    if (this.framePollTimer !== null) {
      window.clearInterval(this.framePollTimer);
      this.framePollTimer = null;
    }
  }

  async submitUserInstruction(text: string): Promise<JoyAIFrameResult | null> {
    if (!this.active) return null;
    const replacingPendingPrompt = this.promptLifecycle.hasPending;
    const pendingResult = this.promptLifecycle.enqueue(text, text);
    this.callbacks.report('joyai_prompt_queued_for_next_frame', {
      replacing_pending_prompt: replacingPendingPrompt,
      instruction_chars: text.length,
    });
    return pendingResult;
  }

  handleCompletedSearch(payload: SearchJobPayload, existing?: SearchJobState): boolean {
    const jobId = payload.job_id?.trim() || '';
    if (!this.active || !jobId) return false;
    const result = payload.result?.trim() || '';
    const question = payload.question?.trim() || existing?.question || '';
    if (!result || !question) return false;

    const sessionId = this.sessionId;
    this.callbacks.updateSearchJob({
      id: jobId,
      searchSessionId: payload.search_session_id || existing?.searchSessionId || '',
      question,
      query: payload.query?.trim() || existing?.query || '',
      status: 'queued',
    });
    this.callbacks.setToolStatus('搜索完成，等待输出结果…');
    this.callbacks.report('search_result_waiting_for_output_slot', {
      job_id: jobId,
      message: 'Core Agent final answer queued for display',
    });

    const deliver = async () => {
      if (!await this.waitForAnswerSlot(sessionId)) return;
      this.callbacks.setToolStatus('正在整理搜索结果…');
      const responseGeneration = this.ttsGeneration;
      try {
        const finalAnswer = result.trim();
        if (!finalAnswer) {
          throw new Error('Core Agent 未返回有效最终答案');
        }
        this.pendingToolContext = rememberJoyAIToolContext(this.pendingToolContext, {
          jobId,
          question,
          query: payload.query?.trim() || existing?.query || '',
          result: finalAnswer,
          completedAt: new Date().toISOString(),
        });
        this.callbacks.report('joyai_tool_context_buffered', {
          job_id: jobId,
          context_job_count: this.pendingToolContext.length,
        });
        this.callbacks.report('search_result_dispatched', {
          job_id: jobId,
          attempt: 1,
          decision: 'core_agent',
          response_chars: finalAnswer.length,
          message: 'Core Agent chat.final content sent directly to the output queue',
        });
        this.commitAndSpeak(finalAnswer, jobId, responseGeneration);
        this.callbacks.setToolStatus('');
        this.callbacks.report('search_result_answered', {
          job_id: jobId,
          realtime_answer: finalAnswer,
          message: 'Core Agent final answer displayed directly without JoyAI summarization',
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : '请重试';
        this.callbacks.updateSearchJob({
          id: jobId,
          searchSessionId: payload.search_session_id || existing?.searchSessionId || '',
          question,
          query: payload.query?.trim() || existing?.query || '',
          status: 'failed',
        });
        this.callbacks.setToolStatus(`搜索已完成，但结果处理失败：${message}`);
        this.callbacks.report('search_result_response_empty', {
          job_id: jobId,
          message,
        });
      }
      await this.ttsQueue;
    };
    const queued = this.searchDeliveryQueue.then(deliver, deliver);
    this.searchDeliveryQueue = queued.then(() => undefined, () => undefined);
    return true;
  }

  private async handleTurnAudio(audioDataUrl: string, turnId: string, sessionId: string): Promise<void> {
    if (this.handledTurns.has(turnId)) return;
    this.handledTurns.add(turnId);
    while (this.handledTurns.size > 8) {
      const oldest = this.handledTurns.values().next().value;
      if (!oldest) break;
      this.handledTurns.delete(oldest);
    }
    const speechEpochAtTurn = this.userSpeechEpoch;
    let speechReleased = false;
    this.callbacks.beginTranscription();
    try {
      const asr = await webRequest<{ transcript?: string }>('video.transcribe', {
        audio_data_url: audioDataUrl,
      }, { timeoutMs: 45_000 });
      if (!this.active || this.sessionId !== sessionId) return;
      const transcript = asr.transcript?.trim();
      if (!transcript) {
        if (speechEpochAtTurn === this.userSpeechEpoch) {
          this.userSpeechActive = false;
          speechReleased = true;
          const interruptedText = this.ttsInterruption.takeAfterRejectedTurn(
            speechEpochAtTurn,
            transcript,
          );
          this.callbacks.report('joyai_barge_in_rejected', {
            speech_epoch: speechEpochAtTurn,
            tts_generation: this.ttsGeneration,
            resumed_text_chars: interruptedText.length,
          });
          if (interruptedText) {
            this.callbacks.report('joyai_tts_resumed_after_rejected_barge_in', {
              speech_epoch: speechEpochAtTurn,
              tts_generation: this.ttsGeneration,
              text_chars: interruptedText.length,
            });
            this.speakText(interruptedText, this.ttsGeneration);
          }
        }
        return;
      }
      this.callbacks.appendChat('user', transcript);
      this.callbacks.setAwaitingVoiceTranscript(false);
      if (speechEpochAtTurn === this.userSpeechEpoch) {
        this.ttsInterruption.discard(speechEpochAtTurn);
        this.interruptTts();
        this.userSpeechActive = false;
        speechReleased = true;
        this.callbacks.report('joyai_barge_in_released_for_instruction', {
          speech_epoch: speechEpochAtTurn,
          tts_generation: this.ttsGeneration,
          transcript_chars: transcript.length,
        });
      }
      await this.submitUserInstruction(transcript);
    } catch (error) {
      if (this.active && this.sessionId === sessionId) {
        this.callbacks.setError(error instanceof Error ? error.message : 'JoyAI 语音处理失败');
      }
    } finally {
      if (!speechReleased && speechEpochAtTurn === this.userSpeechEpoch) {
        this.ttsInterruption.discard(speechEpochAtTurn);
        this.interruptTts();
        this.userSpeechActive = false;
        this.callbacks.report('joyai_barge_in_released_without_instruction', {
          speech_epoch: speechEpochAtTurn,
          tts_generation: this.ttsGeneration,
        });
      }
      this.callbacks.finishTranscription();
    }
  }

  private startFramePolling(): void {
    if (this.framePollTimer !== null) return;
    this.callbacks.report('joyai_frame_polling_started', {
      client_build: FRAME_POLL_CLIENT_BUILD,
      frame_count: this.callbacks.getFrameCount(),
    });
    const sendLatestFrame = () => {
      if (!this.active) return;
      if (Date.now() < this.framePollingPausedUntil) return;
      // The official client skips a scheduled frame while inference is busy.
      if (this.queuedRequestCount > 0) return;
      const frameDataUrl = this.callbacks.getLatestFrameDataUrl();
      if (!frameDataUrl) return;
      const pendingPrompt = this.promptLifecycle.claim();
      const request = this.requestFrame(
        pendingPrompt?.instruction || '',
        pendingPrompt?.question || '',
        frameDataUrl,
      );
      void request.then((result) => {
        pendingPrompt?.complete(result);
        if (pendingPrompt) {
          this.callbacks.report('joyai_prompt_consumed_by_frame', {
            response_received: Boolean(result),
          });
        }
      }).catch((error) => {
        if (isJoyAIRateLimit(error) && pendingPrompt && !this.promptLifecycle.hasPending) {
          const retry = this.promptLifecycle.enqueue(
            pendingPrompt.instruction,
            pendingPrompt.question,
          );
          void retry.then(pendingPrompt.complete, pendingPrompt.fail);
        } else {
          pendingPrompt?.fail(error);
        }
        this.callbacks.setError(error instanceof Error ? error.message : 'JoyAI 画面请求失败');
      });
    };
    sendLatestFrame();
    this.framePollTimer = window.setInterval(sendLatestFrame, FRAME_POLL_INTERVAL_MS);
  }

  private async requestFrame(
    instruction: string,
    originalQuestion = '',
    frameDataUrl = this.callbacks.getLatestFrameDataUrl(),
  ): Promise<JoyAIFrameResult | null> {
    if (!this.active) return null;
    const activeSessionId = this.sessionId;
    const sessionId = activeSessionId;
    if (!sessionId || !frameDataUrl) return null;

    const ttsGenerationAtRequest = this.ttsGeneration;
    this.queuedRequestCount += 1;
    const execute = async (): Promise<JoyAIFrameResult | null> => {
      if (!this.active || this.sessionId !== activeSessionId) {
        return null;
      }
      const requestKind = originalQuestion ? 'user' : 'frame';
      const prompt = instruction.trim();
      const toolContext = requestKind === 'user'
        ? buildJoyAIToolContextBatch(this.pendingToolContext)
        : { text: '', jobIds: [] };
      const frameTimeRange = this.frameClock.nextRange();
      let result: JoyAIFrameResult;
      try {
        result = await webRequest<JoyAIFrameResult>('video.joyai.frame', {
          frame_data_url: frameDataUrl,
          instruction: prompt.slice(0, 2_000),
          question: originalQuestion.slice(0, 500),
          request_kind: requestKind,
          joyai_session_id: sessionId,
          search_session_id: this.callbacks.getSearchSessionId(),
          frame_time_range: frameTimeRange,
          ...(toolContext.text ? { tool_context: toolContext.text } : {}),
        }, { timeoutMs: 60_000 });
        if (toolContext.jobIds.length > 0) {
          this.pendingToolContext = removeSentJoyAIToolContext(
            this.pendingToolContext,
            toolContext.jobIds,
          );
          this.callbacks.report('joyai_tool_context_attached', {
            job_id: toolContext.jobIds.join(','),
            context_job_count: toolContext.jobIds.length,
            context_chars: toolContext.text.length,
          });
        }
      } catch (error) {
        if (isJoyAIRateLimit(error)) {
          this.rateLimitStrikes += 1;
          const cooldownMs = Math.min(
            RATE_LIMIT_BASE_COOLDOWN_MS * 2 ** (this.rateLimitStrikes - 1),
            RATE_LIMIT_MAX_COOLDOWN_MS,
          );
          this.framePollingPausedUntil = Date.now() + cooldownMs;
          this.callbacks.report('joyai_rate_limited', {
            cooldown_ms: cooldownMs,
            rate_limit_strikes: this.rateLimitStrikes,
            request_kind: requestKind,
          });
          this.callbacks.setStatus(`JoyAI 额度受限，${Math.ceil(cooldownMs / 1_000)} 秒后自动恢复`);
        }
        throw error;
      }
      this.rateLimitStrikes = 0;
      this.framePollingPausedUntil = 0;
      if (!this.active || this.sessionId !== activeSessionId) {
        return null;
      }
      const response = result.response?.trim() || '';
      if (response) this.commitAndSpeak(response, undefined, ttsGenerationAtRequest);
      this.callbacks.rememberSearchJob(result.search_job);
      return result;
    };
    const queuedRequest = this.requestQueue.then(execute, execute);
    this.requestQueue = queuedRequest.then(() => undefined, () => undefined);
    try {
      return await queuedRequest;
    } finally {
      this.queuedRequestCount = Math.max(0, this.queuedRequestCount - 1);
    }
  }

  private commitAndSpeak(text: string, toolJobId?: string, generation = this.ttsGeneration): void {
    this.callbacks.commitAssistantAnswer(text, toolJobId);
    if (!canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) {
      this.callbacks.report('joyai_tts_suppressed_for_barge_in', {
        user_speech_active: this.userSpeechActive,
        response_generation: generation,
        current_generation: this.ttsGeneration,
        text_chars: text.trim().length,
      });
      return;
    }
    this.speakText(text, generation);
  }

  private interruptTts(): void {
    this.ttsGeneration += 1;
    const activeTtsStream = this.activeTtsStream;
    this.activeTtsStream = '';
    this.activeTtsText = '';
    this.voice?.interruptPlayback();
    this.ttsQueue = Promise.resolve();
    if (activeTtsStream) {
      void webRequest('tts.stream.cancel', { stream_id: activeTtsStream }, { timeoutMs: 5_000 })
        .catch(() => undefined);
    }
  }

  private speakText(text: string, generation: number): void {
    const voice = this.voice;
    const spokenText = assistantSpeechText(text, 500);
    if (!this.active || !voice || !spokenText
      || !canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) return;

    const play = async () => {
      if (!canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) return;
      const streamId = typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `tts-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let receivedStreamAudio = false;
      let streamStarted = false;
      const playback = voice.beginPcmStream(streamId);
      this.activeTtsStream = streamId;
      this.activeTtsText = spokenText;
      const unsubscribeChunk = webClient.on<TtsStreamPayload>('video.tts.chunk', ({ payload }) => {
        if (payload.stream_id !== streamId
          || !canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) return;
        const audioBase64 = payload.audio_base64 || '';
        if (!audioBase64) return;
        receivedStreamAudio = true;
        try {
          voice.appendPcm16Chunk(streamId, audioBase64, payload.sample_rate || 24_000);
        } catch (error) {
          voice.failPcmStream(
            streamId,
            error instanceof Error ? error.message : 'JoyAI 音频分块解析失败',
          );
        }
      });
      const unsubscribeDone = webClient.on<TtsStreamPayload>('video.tts.done', ({ payload }) => {
        if (payload.stream_id === streamId) voice.finishPcmStream(streamId);
      });
      const unsubscribeCancelled = webClient.on<TtsStreamPayload>(
        'video.tts.cancelled',
        ({ payload }) => {
          if (payload.stream_id === streamId) voice.interruptPlayback();
        },
      );
      const unsubscribeError = webClient.on<TtsStreamPayload>('video.tts.error', ({ payload }) => {
        if (payload.stream_id === streamId) {
          voice.failPcmStream(streamId, payload.error || 'JoyAI 语音流失败');
        }
      });
      try {
        this.callbacks.report('realtime_tts_synthesis_started', {
          text_chars: spokenText.length,
          stream_id: streamId,
        });
        await webRequest(
          'tts.stream.start',
          { text: spokenText, stream_id: streamId },
          { timeoutMs: 10_000 },
        );
        streamStarted = true;
        await playback;
        if (!this.active
          || !canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) return;
        this.callbacks.report('realtime_tts_playback_completed', {
          text_chars: spokenText.length,
          stream_id: streamId,
          streamed: true,
        });
      } catch (caughtError) {
        let ttsError: unknown = caughtError;
        if (!this.active
          || !canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) return;
        voice.interruptPlayback();
        if (!streamStarted && !receivedStreamAudio) {
          try {
            const result = await webRequest<{ audio_base64?: string; audio_mime?: string }>(
              'tts.synthesize',
              { text: spokenText },
              { timeoutMs: 60_000 },
            );
            if (!this.active || this.voice !== voice || !result.audio_base64
              || !canPlayJoyAIResponse(generation, this.ttsGeneration, this.userSpeechActive)) return;
            await voice.speak(`data:${result.audio_mime || 'audio/wav'};base64,${result.audio_base64}`);
            this.callbacks.report('realtime_tts_playback_completed', {
              text_chars: spokenText.length,
              stream_id: streamId,
              streamed: false,
            });
            return;
          } catch (fallbackError) {
            ttsError = fallbackError;
          }
        }
        const message = ttsError instanceof Error ? ttsError.message : 'JoyAI 语音合成失败';
        this.callbacks.report('realtime_tts_failed', {
          text_chars: spokenText.length,
          stream_id: streamId,
          message,
        });
        this.callbacks.setError(message);
      } finally {
        unsubscribeChunk();
        unsubscribeDone();
        unsubscribeCancelled();
        unsubscribeError();
        if (this.activeTtsStream === streamId) {
          this.activeTtsStream = '';
          this.activeTtsText = '';
        }
      }
    };
    const queued = this.ttsQueue.then(play, play);
    this.ttsQueue = queued.then(() => undefined, () => undefined);
  }

  private async waitForAnswerSlot(sessionId: string): Promise<boolean> {
    while (this.active && this.sessionId === sessionId) {
      const requestBarrier = this.requestQueue;
      await requestBarrier;
      const ttsBarrier = this.ttsQueue;
      await ttsBarrier;
      if (!this.callbacks.hasPendingTranscriptions()
        && !this.userSpeechActive
        && this.queuedRequestCount === 0
        && requestBarrier === this.requestQueue
        && ttsBarrier === this.ttsQueue) return true;
      await new Promise((resolve) => window.setTimeout(resolve, 50));
    }
    return false;
  }
}

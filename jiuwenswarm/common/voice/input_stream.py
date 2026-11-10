"""Connection-owned microphone streams, server VAD, and ordered utterance ASR."""

from __future__ import annotations

import asyncio
import base64
import logging
from uuid import uuid4

from .input_audio import UtteranceSegmenter, pcm_wav

MAX_CHUNK_BYTES = 6_400  # Up to 200 ms of signed little-endian PCM16.
MAX_PENDING_UTTERANCES = 7  # Includes queued audio, running ASR, and unpublished results.
ASR_CONCURRENCY = 3
ASR_TIMEOUT_SECONDS = 60
BATCH_SIZE = 4
BATCH_WAIT_SECONDS = 1.5
logger = logging.getLogger(__name__)


async def transcribe_utterance(pcm: bytes) -> str:
    from agent_gateway.channel_manager.web.task_asr import (
        TaskAsrError, read_env, transcribe_task_audio,
    )
    from jiuwenswarm.extensions.video_duplex.backend.video_voice import is_ignorable_asr_filler

    # The extracted Gateway returns empty for unconfigured one-shot ASR. A
    # continuous stream must report that configuration failure, as before.
    if not str(read_env("ASR_API_BASE", "") or "").strip() or not str(read_env("ASR_MODEL_NAME", "") or "").strip():
        raise TaskAsrError("Speech transcription is not configured", "ASR_NOT_CONFIGURED")
    # Voice input uses Settings > Speech transcription.
    try:
        transcript = await transcribe_task_audio({
            "audio_base64": base64.b64encode(pcm_wav(pcm)).decode("ascii"),
            "mime_type": "audio/wav",
        })
    except TaskAsrError as exc:
        if exc.code == "ASR_EMPTY_TRANSCRIPT":
            return ""
        raise
    return "" if is_ignorable_asr_filler(transcript) else transcript.strip()


class VoiceInputStream:
    def __init__(self, channel, ws, session_id, startup_id):
        self.channel, self.ws = channel, ws
        self.session_id, self.startup_id = session_id, startup_id
        self.id = uuid4().hex
        self.closed = False
        self.listening = True
        self.audio_lock = asyncio.Lock()
        self.pending_audio_bytes = 0
        self.sequence = 0
        self.utterance = 0
        self.segmenter = None
        self.queue = asyncio.Queue(maxsize=MAX_PENDING_UTTERANCES)
        self.workers = []
        self.publisher = None
        self.results: dict[int, str | None] = {}
        self.results_ready = asyncio.Event()
        self.next_utterance = 1
        self.initializing = None

    async def start(self):
        if self.initializing is None:
            self.initializing = asyncio.create_task(self.initialize())
        await asyncio.shield(self.initializing)

    async def initialize(self):
        self.segmenter = await asyncio.to_thread(UtteranceSegmenter)
        if self.closed:
            raise ValueError("Voice microphone stream has closed")
        self.workers = [asyncio.create_task(self.transcribe()) for _ in range(ASR_CONCURRENCY)]
        self.publisher = asyncio.create_task(self.publish_results())

    def envelope(self):
        return {"session_id": self.session_id, "stream_id": self.id}

    async def emit(self, event, **payload):
        if not self.closed:
            await self.channel.send_event(self.ws, event, {**self.envelope(), **payload})

    async def append(self, params):
        sequence, encoded = params.get("sequence"), params.get("audio_base64")
        if type(sequence) is not int or sequence != self.sequence:
            raise ValueError("Out-of-order voice microphone audio")
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 8_536:
            raise ValueError("Invalid voice audio chunk size")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError("Invalid voice PCM encoding") from None
        if not 0 < len(pcm) <= MAX_CHUNK_BYTES or len(pcm) % 2:
            raise ValueError("Expected mono PCM16 audio at 16000 Hz")
        if self.closed or not self.listening or self.segmenter is None:
            raise ValueError("Voice microphone stream is not ready")
        if self.pending_audio_bytes + len(pcm) > 64_000:
            raise ValueError("Voice microphone backlog is full")
        # Reserve sequence/buffer space before yielding; concurrent RPCs may
        # arrive while the preceding frame is being inferred off-thread.
        self.sequence += 1
        self.pending_audio_bytes += len(pcm)
        try:
            async with self.audio_lock:
                if self.closed:
                    raise ValueError("Voice microphone stream has closed")
                utterances = await asyncio.to_thread(self.segmenter.feed, pcm)
                if self.closed:
                    raise ValueError("Voice microphone stream has closed")
                self.enqueue(utterances)
        finally:
            self.pending_audio_bytes -= len(pcm)

    def enqueue(self, utterances):
        for pcm in utterances:
            # Include running ASR, out-of-order results, and the publishing batch.
            if self.utterance - self.next_utterance + 1 >= MAX_PENDING_UTTERANCES:
                raise ValueError("Voice transcription backlog is full")
            self.utterance += 1
            self.queue.put_nowait((self.utterance, pcm))

    async def listening_control(self, listening):
        if self.closed or self.pending_audio_bytes or self.audio_lock.locked():
            raise ValueError("Voice microphone stream is not ready")
        async with self.audio_lock:
            self.listening = False
            pcm = await asyncio.to_thread(self.segmenter.flush)
            if self.closed:
                raise ValueError("Voice microphone stream has closed")
            if pcm:
                self.enqueue([pcm])
            self.listening = listening

    async def transcribe(self):
        while not self.closed:
            utterance_id, pcm = await self.queue.get()
            try:
                await self.emit("voice.input.transcribing", utterance_id=utterance_id)
                async with asyncio.timeout(ASR_TIMEOUT_SECONDS):
                    text = await transcribe_utterance(pcm)
            except asyncio.CancelledError:
                self.queue.task_done()
                raise
            except Exception:
                text = None  # A terminal failure advances ordering just like an empty result.
            if self.closed:
                self.queue.task_done()
                continue
            self.results[utterance_id] = text
            self.results_ready.set()
            # The publisher owns task_done from here, including buffered results.

    async def publish_results(self):
        deadline = None
        loop = asyncio.get_running_loop()
        while not self.closed:
            self.results_ready.clear()
            batch = []
            for utterance_id in range(self.next_utterance, self.next_utterance + BATCH_SIZE):
                if utterance_id not in self.results:
                    break
                batch.append((utterance_id, self.results[utterance_id]))
            if batch:
                if deadline is None:
                    deadline = loop.time() + BATCH_WAIT_SECONDS
                if len(batch) == BATCH_SIZE or loop.time() >= deadline:
                    try:
                        await self.deliver_results(batch)
                    except Exception:
                        logger.exception("Voice transcript batch delivery failed")
                        await self.emit("voice.input.error", utterance_id=batch[-1][0], code="transcriptionFailed")
                    finally:
                        for utterance_id, _ in batch:
                            self.results.pop(utterance_id)
                            self.queue.task_done()
                        self.next_utterance = batch[-1][0] + 1
                    deadline = None
                    continue
            try:
                # Keep the first deadline: later arrivals must not postpone a batch.
                async with asyncio.timeout(None if deadline is None else max(0, deadline - loop.time())):
                    await self.results_ready.wait()
            except TimeoutError:
                pass

    async def deliver_results(self, batch):
        group = []
        for utterance_id, text in batch:
            if text is None:
                # Emit failures in order, flushing earlier text before their IDs.
                await self.deliver_transcript(group)
                group = []
                await self.emit("voice.input.error", utterance_id=utterance_id, code="transcriptionFailed")
            else:
                group.append((utterance_id, text))
        await self.deliver_transcript(group)

    async def deliver_transcript(self, group):
        if not group or self.closed:
            return
        utterance_id = group[-1][0]
        text = " ".join(text for _, text in group if text)
        await self.emit("voice.input.transcript", utterance_id=utterance_id, transcript=text)

    def revoke(self):
        if self.closed:
            return
        self.closed = True
        for task in (*self.workers, self.publisher, self.initializing):
            if task is not None:
                task.cancel()

    async def close(self):
        self.revoke()
        tasks = [task for task in (*self.workers, self.publisher, self.initializing) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        for _ in self.results:
            self.queue.task_done()
        self.results.clear()

async def start_voice_input(channel, ws, req_id, params, session_id, user_id=None):
    from agent_gateway.common.protocol_ids import is_valid_session_id

    stream = None
    try:
        params = params if isinstance(params, dict) else {}
        target = str(params.get("session_id") or session_id or "")
        startup_id = params.get("startup_id")
        if not is_valid_session_id(target) or target == "new":
            raise ValueError("Voice input requires an existing chat")
        if not isinstance(startup_id, str) or not 0 < len(startup_id) <= 128:
            raise ValueError("Voice input startup identity is required")
        previous = getattr(ws, "_voice_input_stream", None)
        if previous is not None and not previous.closed:
            if (previous.session_id, previous.startup_id) != (target, startup_id):
                raise ValueError("Another voice stream is active on this connection")
            stream = previous
        else:
            stream = VoiceInputStream(channel, ws, target, startup_id)
            setattr(ws, "_voice_input_stream", stream)
        await stream.start()
        if stream.closed:
            raise ValueError("Voice input startup was cancelled")
        await channel.send_response(ws, req_id, ok=True, payload={"ready": True, "stream_id": stream.id})
    except asyncio.CancelledError:
        if stream is not None:
            await stream.close()
        await channel.send_response(ws, req_id, ok=False, error="Voice input startup cancelled", code="CANCELLED")
    except Exception as exc:
        if stream is not None:
            await stream.close()
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="VOICE_INPUT_FAILED")


async def control_voice_input(channel, ws, req_id, params, session_id, user_id=None, *, operation):
    stream = None
    try:
        params = params if isinstance(params, dict) else {}
        candidate = getattr(ws, "_voice_input_stream", None)
        if candidate is None and operation == "stop":
            await channel.send_response(ws, req_id, ok=True, payload={"accepted": True})
            return
        target = str(params.get("session_id") or session_id or "")
        if candidate is None or candidate.session_id != target or params.get("startup_id") != candidate.startup_id:
            raise ValueError("Unknown or stale voice stream")
        if operation != "stop" and (candidate.closed or params.get("stream_id") != candidate.id):
            raise ValueError("Unknown or stale voice stream")
        stream = candidate
        if operation == "append":
            await stream.append(params)
        elif operation == "stop":
            await stream.close()
        else:
            await stream.listening_control(operation == "resume")
        await channel.send_response(ws, req_id, ok=True, payload={"accepted": True})
    except Exception as exc:
        if stream is not None:
            await stream.close()
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="VOICE_INPUT_FAILED")


async def close_voice_input(ws, _sessions=None):
    stream = getattr(ws, "_voice_input_stream", None)
    if stream is not None:
        await stream.close()

"""Server-side Silero segmentation of a continuous 16 kHz mono PCM stream."""

from __future__ import annotations

from collections import deque
from functools import lru_cache
from pathlib import Path
import io
import wave

import numpy as np

from jiuwenswarm.common.meeting_speech_detection import speech_start_settings

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512
MAX_UTTERANCE_FRAMES = 937  # Less than 30 seconds, including pre-roll.


@lru_cache(maxsize=1)
def _model():
    # Load only when a voice stream opens its microphone. No runtime download.
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    path = Path(__file__).parents[2] / "resources/voice/silero_vad_v5.onnx"
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


class SileroProbability:
    """Share immutable model weights; recurrent state belongs to each stream."""

    def __init__(self):
        self.model = _model()
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, frame: bytes) -> float:
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768
        probability, self.state = self.model.run(None, {
            "input": samples.reshape(1, FRAME_SAMPLES),
            "state": self.state,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
        })
        return float(probability[0][0])


class UtteranceSegmenter:
    """Confirm speech with configurable confidence/frames; finish after 640 ms silence.

    Keep one second of pre-roll to preserve onset while confirming speech.
    Inference and this mutable state run serially off the Gateway event loop.
    """

    def __init__(self, probability=None):
        self.probability = probability if probability is not None else SileroProbability()
        settings = speech_start_settings()
        self.start_threshold = settings["startThreshold"]
        self.start_frames = settings["startFrames"]
        self.reset()

    def reset(self):
        self.pending = b""
        self.pre_roll = deque(maxlen=31)
        self.evidence = deque(maxlen=10)
        self.frames = []
        self.silence = 0
        self.probability.reset()

    def finish(self) -> bytes | None:
        pcm = b"".join(self.frames) if self.frames else None
        self.frames = []
        self.pre_roll.clear()
        self.evidence.clear()
        self.silence = 0
        return pcm

    def flush(self) -> bytes | None:
        pcm = self.finish()
        if pcm:
            pcm += self.pending
        self.reset()
        return pcm

    def feed(self, pcm: bytes) -> list[bytes]:
        data = self.pending + pcm
        utterances = []
        size = FRAME_SAMPLES * 2
        offset = 0
        while offset + size <= len(data):
            frame = data[offset:offset + size]
            offset += size
            probability = self.probability(frame)
            level = float(np.sqrt(np.mean(np.frombuffer(frame, dtype="<i2").astype(np.float32) ** 2)))
            if self.frames:
                self.frames.append(frame)
                self.silence = 0 if probability >= 0.45 and level >= 80 else self.silence + 1
                if self.silence >= 20 or len(self.frames) >= MAX_UTTERANCE_FRAMES:
                    utterances.append(self.finish())
            else:
                self.pre_roll.append(frame)
                speech = probability >= self.start_threshold and level >= 80
                self.evidence.append(speech)
                if speech and sum(self.evidence) >= self.start_frames:
                    self.frames = list(self.pre_roll)
                    self.pre_roll.clear()
                    self.silence = 0
        self.pending = data[offset:]
        return utterances


def pcm_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return buffer.getvalue()

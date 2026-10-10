import { useCallback, useEffect, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';

const MAX_RECORDING_DURATION_MS = 60_000;
const MIME_TYPE_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];

type TaskAsrOptions = {
  onTranscript: (text: string) => void;
  onError: (message: string) => void;
};

type TaskAsrResponse = {
  text?: string;
};

function preferredMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') {
    return undefined;
  }
  return MIME_TYPE_CANDIDATES.find((candidate) => MediaRecorder.isTypeSupported(candidate));
}

function blobBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('Failed to read the recording'));
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : '';
      const separator = result.indexOf(',');
      if (separator < 0) {
        reject(new Error('Failed to encode the recording'));
        return;
      }
      resolve(result.slice(separator + 1));
    };
    reader.readAsDataURL(blob);
  });
}

export function useTaskAsr({ onTranscript, onError }: TaskAsrOptions) {
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const durationTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const discardRecordingRef = useRef(false);
  const onTranscriptRef = useRef(onTranscript);
  const onErrorRef = useRef(onError);

  onTranscriptRef.current = onTranscript;
  onErrorRef.current = onError;

  const isSupported =
    typeof navigator !== 'undefined' &&
    Boolean(navigator.mediaDevices?.getUserMedia) &&
    typeof MediaRecorder !== 'undefined';

  const clearDurationTimer = useCallback(() => {
    if (durationTimerRef.current !== null) {
      window.clearTimeout(durationTimerRef.current);
      durationTimerRef.current = null;
    }
  }, []);

  const releaseMicrophone = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  const transcribe = useCallback(async (blob: Blob) => {
    if (!blob.size || !mountedRef.current) return;
    setIsTranscribing(true);
    try {
      const response = await webRequest<TaskAsrResponse>(
        'task.asr.transcribe',
        {
          audio_base64: await blobBase64(blob),
          mime_type: blob.type || 'audio/webm',
        },
        { timeoutMs: 130_000 },
      );
      const text = response.text?.trim();
      if (!text) throw new Error('ASR did not return any text');
      if (mountedRef.current) onTranscriptRef.current(text);
    } catch (error) {
      if (mountedRef.current) {
        onErrorRef.current(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (mountedRef.current) setIsTranscribing(false);
    }
  }, []);

  const stopRecording = useCallback(() => {
    clearDurationTimer();
    const recorder = recorderRef.current;
    if (recorder?.state === 'recording') {
      recorder.stop();
    } else {
      releaseMicrophone();
    }
    if (mountedRef.current) setIsRecording(false);
  }, [clearDurationTimer, releaseMicrophone]);

  const startRecording = useCallback(async () => {
    if (!isSupported || isRecording || isTranscribing) return;
    discardRecordingRef.current = false;
    chunksRef.current = [];
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (!mountedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;
      const mimeType = preferredMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      recorderRef.current = recorder;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onerror = () => {
        discardRecordingRef.current = true;
        clearDurationTimer();
        releaseMicrophone();
        if (mountedRef.current) {
          setIsRecording(false);
          onErrorRef.current('Microphone recording failed');
        }
      };
      recorder.onstop = () => {
        clearDurationTimer();
        releaseMicrophone();
        recorderRef.current = null;
        const chunks = chunksRef.current;
        chunksRef.current = [];
        if (discardRecordingRef.current || chunks.length === 0) return;
        void transcribe(new Blob(chunks, { type: recorder.mimeType || mimeType || 'audio/webm' }));
      };
      recorder.start(250);
      setIsRecording(true);
      durationTimerRef.current = window.setTimeout(stopRecording, MAX_RECORDING_DURATION_MS);
    } catch (error) {
      releaseMicrophone();
      if (mountedRef.current) {
        setIsRecording(false);
        onErrorRef.current(error instanceof Error ? error.message : String(error));
      }
    }
  }, [clearDurationTimer, isRecording, isSupported, isTranscribing, releaseMicrophone, stopRecording, transcribe]);

  const toggleRecording = useCallback(() => {
    if (isRecording) {
      stopRecording();
      return;
    }
    void startRecording();
  }, [isRecording, startRecording, stopRecording]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      discardRecordingRef.current = true;
      clearDurationTimer();
      const recorder = recorderRef.current;
      if (recorder?.state === 'recording') recorder.stop();
      releaseMicrophone();
    };
  }, [clearDurationTimer, releaseMicrophone]);

  return {
    isRecording,
    isTranscribing,
    isSupported,
    toggleRecording,
  };
}

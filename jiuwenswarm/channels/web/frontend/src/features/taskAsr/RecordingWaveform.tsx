import { useRef } from 'react';
import { useTranslation } from 'react-i18next';
import './RecordingWaveform.css';

export function RecordingWaveform({ level }: { level: number }) {
  const { t } = useTranslation();
  const levels = useRef<number[]>(Array(20).fill(0));
  levels.current = [...levels.current.slice(1), level];
  return (
    <div
      className="task-asr-waveform"
      role="status"
      aria-label={t('chat.recordingInProgress')}
      data-testid="chat-panel-recording-waveform"
    >
      {levels.current.map((value, index) => (
        <span key={index} aria-hidden="true" style={{ height: `${3 + value * 19}px` }} />
      ))}
    </div>
  );
}

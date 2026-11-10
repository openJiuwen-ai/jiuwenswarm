import { Pause, Play } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

/**
 * Fixed bar count for the default 280px audio card.
 * Content width is 214px after the 1px card border, 16px preview padding and
 * 16px waveform padding. Twenty 3px bars leave (214 - 60) / 19 ≈ 8px between
 * them when the row uses space-between. A wider card only widens those gaps;
 * peaks stay at this count and are not rebucketed.
 */
const WAVEFORM_BAR_COUNT = 20;

function emptyPeaks(): number[] {
  return Array(WAVEFORM_BAR_COUNT).fill(0);
}

/** One peak per bar, then drop the decoded samples. No denser peak cache. */
function sampleWaveformPeaks(audio: AudioBuffer, barCount: number): number[] {
  const peaks = Array<number>(barCount).fill(0);
  const length = audio.length;
  if (length === 0 || barCount <= 0) return peaks;
  const channels = Array.from({ length: audio.numberOfChannels }, (_, index) => audio.getChannelData(index));
  const samplesPerBar = length / barCount;
  for (let bar = 0; bar < barCount; bar += 1) {
    const start = Math.floor(bar * samplesPerBar);
    const end = Math.max(start + 1, Math.min(length, Math.floor((bar + 1) * samplesPerBar)));
    let peak = 0;
    for (let channel = 0; channel < channels.length; channel += 1) {
      const data = channels[channel];
      for (let index = start; index < end; index += 1) {
        const value = Math.abs(data[index]);
        if (value > peak) peak = value;
      }
    }
    peaks[bar] = peak;
  }
  const max = peaks.reduce((highest, value) => (value > highest ? value : highest), 0);
  if (max <= 0) return peaks;
  return peaks.map((value) => value / max);
}

async function decodeWaveformPeaks(
  src: string,
  barCount: number,
  signal: AbortSignal,
): Promise<{ peaks: number[]; duration: number }> {
  const response = await fetch(src, { signal });
  if (!response.ok) throw new Error(String(response.status));
  const bytes = await response.arrayBuffer();
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  const audio = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(bytes);
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  return { peaks: sampleWaveformPeaks(audio, barCount), duration: audio.duration };
}

function formatAudioClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '00:00';
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

type DesignerAudioPreviewProps = {
  src: string;
};

/**
 * Canvas audio node preview: sampled waveform bars + custom play/pause controls.
 * Keeps a hidden `<audio>` so exclusive playback arbitration still sees it.
 * The preview fills the node card, so only the play button is `nodrag`;
 * marking the whole surface `nodrag` stops React Flow from dragging the node.
 * Peaks are sampled once per `src` at `WAVEFORM_BAR_COUNT` and are not rebuilt on resize.
 */
export function DesignerAudioPreview({ src }: DesignerAudioPreviewProps) {
  const { t } = useTranslation();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [peaks, setPeaks] = useState<number[]>(emptyPeaks);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return undefined;

    const syncDuration = () => {
      const next = audio.duration;
      // Headerless MP3s (no Xing/Info/VBRI or ID3 TLEN) report Infinity until the
      // browser can estimate a length. Keep a decoded duration instead of showing 00:00.
      if (Number.isFinite(next) && next > 0) setDuration(next);
    };
    const onTimeUpdate = () => setCurrentTime(audio.currentTime || 0);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onEnded = () => {
      setPlaying(false);
      setCurrentTime(0);
    };

    setPlaying(!audio.paused);
    setCurrentTime(audio.currentTime || 0);
    syncDuration();

    audio.addEventListener('loadedmetadata', syncDuration);
    audio.addEventListener('durationchange', syncDuration);
    audio.addEventListener('timeupdate', onTimeUpdate);
    audio.addEventListener('play', onPlay);
    audio.addEventListener('pause', onPause);
    audio.addEventListener('ended', onEnded);
    return () => {
      audio.removeEventListener('loadedmetadata', syncDuration);
      audio.removeEventListener('durationchange', syncDuration);
      audio.removeEventListener('timeupdate', onTimeUpdate);
      audio.removeEventListener('play', onPlay);
      audio.removeEventListener('pause', onPause);
      audio.removeEventListener('ended', onEnded);
    };
  }, [src]);

  useEffect(() => {
    setPlaying(false);
    setCurrentTime(0);
    setDuration(0);
  }, [src]);

  useEffect(() => {
    const controller = new AbortController();
    setPeaks(emptyPeaks());
    void decodeWaveformPeaks(src, WAVEFORM_BAR_COUNT, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setPeaks(next.peaks);
        if (Number.isFinite(next.duration) && next.duration > 0) {
          setDuration((current) => (current > 0 ? current : next.duration));
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setPeaks(emptyPeaks());
      });
    return () => controller.abort();
  }, [src]);

  const togglePlayback = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      void audio.play().catch(() => {
        setPlaying(false);
      });
      return;
    }
    audio.pause();
  }, []);

  return (
    <div
      className="designer-node__audio-preview"
      data-testid="designer-node-audio-preview"
    >
      <audio
        ref={audioRef}
        className="designer-node__audio-element"
        src={src}
        preload="metadata"
        data-testid="designer-node-uploaded-audio"
      />
      <div className="designer-node__audio-waveform" aria-hidden data-testid="designer-node-audio-waveform">
        {peaks.map((height, index) => (
          <span
            key={index}
            className="designer-node__audio-waveform-bar"
            style={{ height: `${Math.round(height * 100)}%` }}
          />
        ))}
      </div>
      <div className="designer-node__audio-controls">
        <span className="designer-node__audio-time" data-testid="designer-node-audio-time">
          {`${formatAudioClock(currentTime)}/${formatAudioClock(duration)}`}
        </span>
        <button
          type="button"
          className="designer-node__audio-play nodrag nopan"
          aria-label={playing ? t('designer.nodeActions.pauseAudio') : t('designer.nodeActions.playAudio')}
          aria-pressed={playing}
          data-testid="designer-node-audio-play"
          onClick={togglePlayback}
        >
          {playing ? (
            <Pause size={18} strokeWidth={2} aria-hidden />
          ) : (
            <Play size={18} strokeWidth={2} aria-hidden />
          )}
        </button>
      </div>
    </div>
  );
}

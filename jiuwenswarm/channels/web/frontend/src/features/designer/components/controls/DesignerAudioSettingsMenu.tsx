import { ChevronUp } from 'lucide-react';
import {
  useEffect,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { useTranslation } from 'react-i18next';
import {
  audioDurationLabelForm,
  audioDurationParts,
  effectiveAudioDurationSec,
  parseAudioDurationFields,
  withAudioDuration,
} from '../../designerMediaSettings';
import { useDesignerGraphEditingLocked } from '../../useDesignerGraphEditingLocked';
import { useDesignerStore } from '../../designerStore';

type DesignerAudioSettingsMenuProps = {
  nodeId: string;
};

/**
 * Music-node length control: a summary button and a popover with minute and second fields.
 * Empty fields mean the film's own duration. A saved total above zero is shown as minutes and seconds.
 */
export function DesignerAudioSettingsMenu({ nodeId }: DesignerAudioSettingsMenuProps) {
  const { t } = useTranslation();
  const durationSec = useDesignerStore((state) => {
    const config = state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config;
    return effectiveAudioDurationSec(config?.duration_sec, config?.film_duration_sec);
  });
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  const editingLocked = useDesignerGraphEditingLocked();
  const [open, setOpen] = useState(false);
  const [minutesDraft, setMinutesDraft] = useState('');
  const [secondsDraft, setSecondsDraft] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);
  const minutesRef = useRef<HTMLInputElement>(null);
  const secondsRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setOpen(false);
  }, [nodeId]);

  useEffect(() => {
    const active = document.activeElement;
    if (active === minutesRef.current || active === secondsRef.current) return;
    if (durationSec === null) {
      setMinutesDraft('');
      setSecondsDraft('');
      return;
    }
    const parts = audioDurationParts(durationSec);
    setMinutesDraft(String(parts.minutes));
    setSecondsDraft(String(parts.seconds));
  }, [durationSec]);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const summary = (() => {
    if (durationSec === null) return t('designer.mediaSettings.audioDurationDefault');
    const parts = audioDurationParts(durationSec);
    const form = audioDurationLabelForm(durationSec);
    if (form === 'minutes') return t('designer.mediaSettings.audioDurationMinutes', { count: parts.minutes });
    if (form === 'seconds') return t('designer.mediaSettings.audioDurationSeconds', { count: parts.seconds });
    return t('designer.mediaSettings.audioDurationBoth', {
      minutes: parts.minutes,
      seconds: parts.seconds,
    });
  })();

  const revertDrafts = () => {
    if (durationSec === null) {
      setMinutesDraft('');
      setSecondsDraft('');
      return;
    }
    const parts = audioDurationParts(durationSec);
    setMinutesDraft(String(parts.minutes));
    setSecondsDraft(String(parts.seconds));
  };

  const commitDuration = () => {
    if (editingLocked) return;
    if (!minutesDraft.trim() && !secondsDraft.trim()) {
      setMinutesDraft('');
      setSecondsDraft('');
      if (durationSec !== null) updateNodeConfig(nodeId, (config) => withAudioDuration(config, null));
      return;
    }
    const next = parseAudioDurationFields(minutesDraft, secondsDraft);
    if (next === null) {
      revertDrafts();
      return;
    }
    const parts = audioDurationParts(next);
    setMinutesDraft(String(parts.minutes));
    setSecondsDraft(String(parts.seconds));
    if (next === durationSec) return;
    updateNodeConfig(nodeId, (config) => withAudioDuration(config, next));
  };

  const onFieldBlur = (event: ReactFocusEvent<HTMLInputElement>) => {
    const next = event.relatedTarget;
    if (next === minutesRef.current || next === secondsRef.current) return;
    commitDuration();
  };

  const onFieldKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    commitDuration();
    event.currentTarget.blur();
  };

  return (
    <div
      className={`designer-media-settings${editingLocked ? ' designer-edit-control' : ''}`}
      ref={rootRef}
      data-media-type="audio"
    >
      <button
        type="button"
        className={`designer-media-settings__trigger${open ? ' is-open' : ''}`}
        data-testid="designer-media-settings-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={t('designer.mediaSettings.audioTitle')}
        disabled={editingLocked}
        onClick={() => {
          if (editingLocked) return;
          setOpen((prev) => !prev);
        }}
      >
        <span className="designer-media-settings__summary">{summary}</span>
        <ChevronUp size={14} aria-hidden />
      </button>
      {open ? (
        <div
          className="designer-media-settings__popover"
          role="dialog"
          aria-label={t('designer.mediaSettings.audioTitle')}
          data-testid="designer-media-settings"
        >
          <div className="designer-media-settings__field" data-testid="designer-media-settings-audio-duration">
            <span className="designer-media-settings__label">{t('designer.toolbar.audioDuration')}</span>
            <div className="designer-media-settings__audio-duration">
              <label className="designer-media-settings__duration-value">
                <input
                  ref={minutesRef}
                  type="text"
                  inputMode="numeric"
                  className="designer-media-settings__duration-input nodrag nopan nowheel"
                  value={minutesDraft}
                  disabled={editingLocked}
                  data-testid="designer-media-settings-audio-minutes"
                  aria-label={t('designer.mediaSettings.minuteUnit')}
                  onChange={(event) => setMinutesDraft(event.target.value)}
                  onBlur={onFieldBlur}
                  onKeyDown={onFieldKeyDown}
                />
                <span className="designer-media-settings__duration-unit">
                  {t('designer.mediaSettings.minuteUnit')}
                </span>
              </label>
              <label className="designer-media-settings__duration-value">
                <input
                  ref={secondsRef}
                  type="text"
                  inputMode="numeric"
                  className="designer-media-settings__duration-input nodrag nopan nowheel"
                  value={secondsDraft}
                  disabled={editingLocked}
                  data-testid="designer-media-settings-audio-seconds"
                  aria-label={t('designer.mediaSettings.secondUnit')}
                  onChange={(event) => setSecondsDraft(event.target.value)}
                  onBlur={onFieldBlur}
                  onKeyDown={onFieldKeyDown}
                />
                <span className="designer-media-settings__duration-unit">
                  {t('designer.mediaSettings.secondUnit')}
                </span>
              </label>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

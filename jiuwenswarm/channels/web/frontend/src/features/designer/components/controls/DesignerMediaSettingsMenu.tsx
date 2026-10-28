import { ChevronDown } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { designerGraphClient } from '../../designerGraphClient';
import { DESIGNER_ASPECT_RATIO_ICONS } from '../../designerIcons';
import {
  effectiveImageSettings,
  effectiveVideoDuration,
  effectiveVideoSettings,
  generatesTimedVideo,
  generationModelLabel,
} from '../../designerMediaSettings';
import { useDesignerGraphEditingLocked } from '../../useDesignerGraphEditingLocked';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import type { DesignerMediaCapabilities, DesignerMediaSettingsPatch } from '../../executionGraphTypes';

type DesignerMediaSettingsMenuProps = {
  nodeId: string;
  nodeType: 'image' | 'video';
};

type ChoiceGroupProps = {
  label: string;
  options: string[];
  value: string;
  disabled: boolean;
  testId: string;
  onSelect: (value: string) => void;
  /** Show rectangular glyph before each option (aspect-ratio choices only). */
  showAspectIcons?: boolean;
};

function ChoiceGroup({ label, options, value, disabled, testId, onSelect, showAspectIcons }: ChoiceGroupProps) {
  return (
    <div className="designer-media-settings__field" role="radiogroup" aria-label={label} data-testid={testId}>
      <span className="designer-media-settings__label">{label}</span>
      <div className="designer-media-settings__choices">
        {options.map((option) => {
          const AspectIcon = showAspectIcons ? DESIGNER_ASPECT_RATIO_ICONS[option] : undefined;
          return (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={option === value}
              className={`designer-media-settings__choice${option === value ? ' is-selected' : ''}`}
              disabled={disabled}
              onClick={() => {
                if (option !== value) onSelect(option);
              }}
            >
              {AspectIcon ? (
                <AspectIcon className="designer-media-settings__choice-icon" aria-hidden />
              ) : null}
              {option}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Composer-row controls: a summary button that opens the canvas-wide ratio / resolution
 * (plus this video node's own length), and the read-only model name.
 */
export function DesignerMediaSettingsMenu({ nodeId, nodeType }: DesignerMediaSettingsMenuProps) {
  const { t } = useTranslation();
  const graphId = useDesignerStore((state) => state.domainGraph?.graph_id ?? '');
  const metadata = useDesignerStore((state) => state.domainGraph?.metadata);
  const durationSec = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config?.duration_sec,
  );
  const timed = useDesignerStore((state) => nodeType === 'video' && generatesTimedVideo(state.domainGraph, nodeId));
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  // A finished storyboard run can change clip lengths, so refetch with the run.
  const runVersion = useDesignerRunStore((state) => `${state.run?.run_id ?? ''}:${state.run?.updated_at ?? ''}`);
  const patchMediaSettings = useDesignerStore((state) => state.patchMediaSettings);
  const editingLocked = useDesignerGraphEditingLocked();
  const [caps, setCaps] = useState<DesignerMediaCapabilities | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!graphId) return undefined;
    let cancelled = false;
    setError('');
    designerGraphClient
      .mediaCapabilities(graphId)
      .then((result) => {
        if (!cancelled) setCaps(result);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(
            t('designer.mediaSettings.loadError', {
              message: reason instanceof Error ? reason.message : String(reason),
            }),
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [graphId, runVersion, t]);

  useEffect(() => {
    setOpen(false);
  }, [nodeId]);

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

  const apply = (patch: DesignerMediaSettingsPatch) => {
    setSaving(true);
    setError('');
    patchMediaSettings(patch)
      .catch((reason: unknown) => {
        setError(
          t('designer.mediaSettings.saveError', {
            message: reason instanceof Error ? reason.message : String(reason),
          }),
        );
      })
      .finally(() => setSaving(false));
  };

  const isVideo = nodeType === 'video';
  const video = caps ? effectiveVideoSettings(metadata, caps) : null;
  const image = caps ? effectiveImageSettings(metadata, caps) : null;
  const seconds = caps && timed ? effectiveVideoDuration(nodeId, durationSec, caps) : null;
  const current = isVideo ? video : image;
  const summary = current
    ? [
        current.aspect_ratio,
        current.resolution,
        seconds !== null ? t('designer.mediaSettings.durationValue', { seconds }) : '',
      ]
        .filter(Boolean)
        .join(' · ')
    : error || t('designer.mediaSettings.loading');
  const modelLabel = caps ? generationModelLabel(isVideo ? caps.video : caps.image) : '';

  return (
    <>
      <div
        className={`designer-media-settings${editingLocked ? ' designer-edit-control' : ''}`}
        ref={rootRef}
        data-media-type={nodeType}
      >
        <button
          type="button"
          className={`designer-media-settings__trigger${open ? ' is-open' : ''}`}
          data-testid="designer-media-settings-trigger"
          aria-haspopup="dialog"
          aria-expanded={open}
          title={isVideo ? t('designer.mediaSettings.videoTitle') : t('designer.mediaSettings.imageTitle')}
          disabled={!caps || editingLocked}
          onClick={() => {
            if (editingLocked) return;
            setOpen((prev) => !prev);
          }}
        >
          <span className="designer-media-settings__summary">{summary}</span>
          <ChevronDown size={14} aria-hidden />
        </button>
        {open && caps && video && image ? (
          <div
            className="designer-media-settings__popover"
            role="dialog"
            aria-label={isVideo ? t('designer.mediaSettings.videoTitle') : t('designer.mediaSettings.imageTitle')}
            data-testid="designer-media-settings"
          >
            <ChoiceGroup
              label={t('designer.toolbar.aspectRatio')}
              options={isVideo ? caps.video.ratios : caps.image.ratios}
              value={isVideo ? video.aspect_ratio : image.aspect_ratio}
              disabled={saving || editingLocked}
              testId={isVideo ? 'designer-media-settings-video-ratio' : 'designer-media-settings-image-ratio'}
              showAspectIcons
              onSelect={(aspect_ratio) =>
                apply(isVideo ? { video_settings: { aspect_ratio } } : { image_settings: { aspect_ratio } })
              }
            />
            <ChoiceGroup
              label={t('designer.toolbar.resolution')}
              options={isVideo ? caps.video.resolutions : caps.image.resolutions}
              value={isVideo ? video.resolution : image.resolution}
              disabled={saving || editingLocked}
              testId={isVideo ? 'designer-media-settings-video-resolution' : 'designer-media-settings-image-resolution'}
              onSelect={(resolution) =>
                apply(isVideo ? { video_settings: { resolution } } : { image_settings: { resolution } })
              }
            />
            {seconds !== null ? (
              <label className="designer-media-settings__field" data-testid="designer-media-settings-video-duration">
                <span className="designer-media-settings__label">
                  {t('designer.toolbar.duration')}
                  <span className="designer-media-settings__value">
                    {t('designer.mediaSettings.durationValue', { seconds })}
                  </span>
                </span>
                <input
                  type="range"
                  className="designer-media-settings__slider"
                  min={caps.video.min_sec}
                  max={caps.video.max_sec}
                  step={1}
                  value={seconds}
                  disabled={editingLocked}
                  onChange={(event) => {
                    if (editingLocked) return;
                    const next = Number(event.target.value);
                    updateNodeConfig(nodeId, (config) => ({ ...config, duration_sec: next }));
                  }}
                />
              </label>
            ) : null}
            {error ? (
              <p className="designer-media-settings__error" data-testid="designer-media-settings-error">
                {error}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
      {modelLabel ? (
        <span className="designer-media-settings__model" data-testid="designer-media-settings-model" title={modelLabel}>
          {modelLabel}
        </span>
      ) : null}
    </>
  );
}

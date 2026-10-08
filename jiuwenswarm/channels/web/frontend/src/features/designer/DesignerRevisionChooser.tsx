import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { classifyDesignerOutput, type DesignerMaterial, type DesignerPendingRevision } from './designerMaterials';

type DesignerRevisionChooserProps = {
  revision: DesignerPendingRevision;
  busy?: boolean;
  onChoose: (choice: 'original' | 'new') => void;
  onClose: () => void;
};

function TextPreview({ url }: { url: string }) {
  const [text, setText] = useState('');
  useEffect(() => {
    let cancelled = false;
    void fetch(url)
      .then((response) => (response.ok ? response.text() : ''))
      .then((value) => {
        if (!cancelled) setText(value);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [url]);
  return <pre className="designer-revision-chooser__text">{text}</pre>;
}

function MaterialPreview({ material }: { material: DesignerMaterial }) {
  const kind = classifyDesignerOutput(material);
  if (kind === 'image' && material.previewUrl) {
    return <img src={material.previewUrl} alt={material.label} />;
  }
  if (kind === 'video' && material.previewUrl) {
    return <video src={material.previewUrl} controls playsInline />;
  }
  if (kind === 'audio' && material.previewUrl) {
    return <audio src={material.previewUrl} controls />;
  }
  if ((kind === 'text' || material.kind === 'table') && material.textUrl) {
    return <TextPreview url={material.textUrl} />;
  }
  return <p>{material.label}</p>;
}

function RevisionColumn({
  title,
  materials,
}: {
  title: string;
  materials: DesignerMaterial[];
}) {
  return (
    <section className="designer-revision-chooser__column">
      <h3>{title}</h3>
      <div className="designer-revision-chooser__stack">
        {materials.map((item) => (
          <figure key={item.id} className="designer-revision-chooser__item">
            <figcaption>{item.label}</figcaption>
            <MaterialPreview material={item} />
          </figure>
        ))}
      </div>
    </section>
  );
}

export function DesignerRevisionChooser({
  revision,
  busy = false,
  onChoose,
  onClose,
}: DesignerRevisionChooserProps) {
  const { t } = useTranslation();
  return (
    <div
      className="designer-revision-chooser"
      role="dialog"
      aria-modal="true"
      data-testid="designer-revision-chooser"
    >
      <button
        type="button"
        className="designer-revision-chooser__backdrop"
        aria-label={t('designer.materials.closeViewer')}
        onClick={onClose}
      />
      <div className="designer-revision-chooser__panel">
        <header className="designer-revision-chooser__header">
          <div>
            <h2>{t('designer.revision.title')}</h2>
            <p>{t('designer.revision.hint', { label: revision.label })}</p>
          </div>
          <button type="button" className="btn" onClick={onClose}>
            {t('designer.materials.closeViewer')}
          </button>
        </header>
        <div className="designer-revision-chooser__compare">
          <RevisionColumn title={t('designer.revision.original')} materials={revision.original} />
          <RevisionColumn title={t('designer.revision.incoming')} materials={revision.incoming} />
        </div>
        <footer className="designer-revision-chooser__actions">
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => onChoose('original')}
            data-testid="designer-revision-keep-original"
          >
            {t('designer.revision.keepOriginal')}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={() => onChoose('new')}
            data-testid="designer-revision-keep-incoming"
          >
            {t('designer.revision.keepIncoming')}
          </button>
        </footer>
      </div>
    </div>
  );
}

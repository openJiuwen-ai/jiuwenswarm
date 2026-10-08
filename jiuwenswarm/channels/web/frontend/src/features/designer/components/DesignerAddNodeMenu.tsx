import { Headphones, Image as ImageIcon, Video, Workflow } from 'lucide-react';
import { useRef, type ChangeEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { DESIGNER_ADD_TEMPLATES, type DesignerAddTemplate } from '../designerCanvasNodes';

function TypeIcon({ type }: { type: string }) {
  if (type === 'video') return <Video size={14} aria-hidden />;
  if (type === 'audio') return <Headphones size={14} aria-hidden />;
  return <ImageIcon size={14} aria-hidden />;
}

type DesignerAddNodeMenuProps = {
  title?: string;
  testIdPrefix: string;
  onPick: (template: DesignerAddTemplate) => void;
  /** Chosen ComfyUI workflow JSON files. */
  onImportComfyui: (files: File[]) => void;
};

export function DesignerAddNodeMenu({ title, testIdPrefix, onPick, onImportComfyui }: DesignerAddNodeMenuProps) {
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = '';
    if (files.length) onImportComfyui(files);
  };

  return (
    <>
      {title ? <p className="designer-canvas-dock__panel-title">{title}</p> : null}
      <div className="designer-canvas-dock__choices">
        {DESIGNER_ADD_TEMPLATES.map((item) => (
          <button
            key={item.id}
            type="button"
            className="designer-canvas-dock__choice"
            data-testid={`${testIdPrefix}-${item.id}`}
            onClick={() => onPick(item)}
          >
            <TypeIcon type={item.type} />
            {t(`designer.dock.node.${item.id}`)}
          </button>
        ))}
        <button
          type="button"
          className="designer-canvas-dock__choice"
          title={t('designer.dock.comfyuiHint')}
          data-testid={`${testIdPrefix}-comfyui`}
          onClick={() => fileInputRef.current?.click()}
        >
          <Workflow size={14} aria-hidden />
          {t('designer.dock.node.comfyui')}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          multiple
          hidden
          data-testid={`${testIdPrefix}-comfyui-input`}
          onChange={onFileChange}
        />
      </div>
    </>
  );
}

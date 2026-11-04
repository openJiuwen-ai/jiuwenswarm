import { useTranslation } from 'react-i18next';
import type { DesignerAddTemplate } from '../designerCanvasNodes';
import { DesignerAddNodeTypeIcon } from '../designerIcons';

type DesignerAddNodeMenuProps = {
  title?: string;
  testIdPrefix: string;
  templates: DesignerAddTemplate[];
  onPick: (template: DesignerAddTemplate) => void;
};

export function DesignerAddNodeMenu({ title, testIdPrefix, templates, onPick }: DesignerAddNodeMenuProps) {
  const { t } = useTranslation();

  return (
    <>
      {title ? (
        <p className="designer-node__port-menu-title" data-testid={`${testIdPrefix}-menu-title`}>
          {title}
        </p>
      ) : null}
      {templates.map((item) => (
        <button
          key={item.id}
          type="button"
          className="chat-mode-select__option"
          role="menuitem"
          data-testid={`${testIdPrefix}-${item.id}`}
          onClick={() => onPick(item)}
        >
          <span className="chat-mode-select__option-main">
            <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden>
              <DesignerAddNodeTypeIcon type={item.type} />
            </span>
            <span className="chat-mode-select__label">{t(`designer.dock.node.${item.id}`)}</span>
          </span>
        </button>
      ))}
    </>
  );
}

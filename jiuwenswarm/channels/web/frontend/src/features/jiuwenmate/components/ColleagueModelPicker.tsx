import { Check, ChevronDown, Plus } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '../../../components/ui/DropdownMenu/DropdownMenu';
import { ModelProviderIcon } from '../../../components/ModelProviderIcon';
import { findModel, type JiuwenMateModelCatalog } from '../jiuwenmateApi';
import '../../../components/ChatPanel/ChatPanel.css';

interface ColleagueModelPickerProps {
  catalog: JiuwenMateModelCatalog;
  modelId: string;
  pending?: boolean;
  saving?: boolean;
  onChange?: (modelId: string) => void;
  onAddModel?: () => void;
}

export function ColleagueModelPicker({
  catalog, modelId, pending = false, saving = false, onChange, onAddModel,
}: ColleagueModelPickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const disabled = !onChange || saving;
  const selected = findModel(catalog, modelId);
  const label = selected?.label || t(catalog.models.length ? 'jiuwenmate.chat.modelUnavailable' : 'settingsPanel.models.empty');
  const hasPendingModel = pending && selected !== null && modelId.trim().length > 0;
  return (
    <DropdownMenu open={open && !disabled} onOpenChange={setOpen}>
      <div className={`chat-mode-select${open && !disabled ? ' chat-mode-select--open' : ''}`}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="chat-mode-select__trigger jiuwenmate-composer__model"
            aria-label={t('jiuwenmate.create.sections.model')}
            disabled={disabled}
            data-testid="jiuwenmate-composer-model"
          >
            <span className="chat-mode-select__value">
              {selected ? (
                <span className="chat-mode-select__icon" aria-hidden="true">
                  <ModelProviderIcon model={{
                    model_name: selected.id, model_provider: selected.provider, vendor_key: selected.vendorKey,
                  }} />
                </span>
              ) : null}
              <span className="chat-mode-select__label" data-testid="jiuwenmate-model-label">{label}</span>
              {hasPendingModel ? (
                <small data-testid="jiuwenmate-composer-model-pending">{t('jiuwenmate.chat.modelPending')}</small>
              ) : null}
            </span>
            {!disabled ? <ChevronDown className="chat-mode-select__chevron" aria-hidden /> : null}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          side="top"
          align="end"
          className="chat-mode-select__menu model-select__menu jiuwenmate-composer__model-menu"
          data-testid="jiuwenmate-composer-model-menu"
        >
          {catalog.models.length === 0 ? (
            <div className="model-select__section-header" role="status" data-testid="jiuwenmate-composer-model-empty">
              {t('settingsPanel.models.empty')}
            </div>
          ) : null}
          {catalog.models.map((model) => {
            const active = model.id === modelId && !hasPendingModel;
            return (
              <DropdownMenuItem
                key={model.id}
                className={`chat-mode-select__option${active ? ' chat-mode-select__option--active' : ''}`}
                aria-current={active ? 'true' : undefined}
                onSelect={() => {
                  if (model.id !== modelId || hasPendingModel) onChange?.(model.id);
                }}
                data-testid="jiuwenmate-composer-model-option"
                data-variant={model.id}
              >
                <span className="chat-mode-select__option-main">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <ModelProviderIcon model={{
                      model_name: model.id, model_provider: model.provider, vendor_key: model.vendorKey,
                    }} />
                  </span>
                  <span className="chat-mode-select__label" title={model.provider || undefined}>{model.label}</span>
                </span>
                {active ? <Check className="chat-mode-select__check" aria-hidden /> : null}
              </DropdownMenuItem>
            );
          })}
          {onAddModel ? (
            <DropdownMenuItem className="model-select__add-btn" onSelect={onAddModel} data-testid="jiuwenmate-composer-model-add">
              <Plus size={14} aria-hidden />
              {t('chat.modelSelector.addModel')}
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </div>
    </DropdownMenu>
  );
}

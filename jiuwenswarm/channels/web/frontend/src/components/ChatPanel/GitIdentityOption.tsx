// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import { Loader2 } from 'lucide-react';
import { Switch } from '../Switch';

interface GitIdentityOptionProps {
  visible: boolean;
  checked: boolean;
  pending: boolean;
  error: boolean;
  label: string;
  hint: string;
  savingLabel: string;
  errorLabel: string;
  onChange: (enabled: boolean) => void;
}

export function GitIdentityOption(props: GitIdentityOptionProps) {
  if (!props.visible) return null;
  return (
    <div className="chat-mode-select__option" data-testid="chat-panel-input-attach-menu-git-identity" title={props.hint}>
      <span className="chat-mode-select__option-main">
        <span className="chat-mode-select__label" data-testid="chat-panel-git-identity-label">{props.label}</span>
        {props.pending && <Loader2 className="w-4 h-4 animate-spin" aria-label={props.savingLabel} />}
        {props.error && (
          <span role="alert" className="text-xs text-warn" data-testid="chat-panel-git-identity-error">{props.errorLabel}</span>
        )}
      </span>
      <Switch checked={props.checked} disabled={props.pending} title={props.label} onChange={props.onChange} />
    </div>
  );
}

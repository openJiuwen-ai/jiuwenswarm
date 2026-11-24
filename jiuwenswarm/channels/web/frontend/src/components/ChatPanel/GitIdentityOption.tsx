// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import { Switch } from '../Switch';

interface GitIdentityOptionProps {
  visible: boolean;
  checked: boolean;
  label: string;
  hint: string;
  onChange: (enabled: boolean) => void;
}

/** Session switch; the value travels with the next team chat.send. */
export function GitIdentityOption(props: GitIdentityOptionProps) {
  if (!props.visible) return null;
  return (
    <div className="chat-mode-select__option" data-testid="chat-panel-input-attach-menu-git-identity" title={props.hint}>
      <span className="chat-mode-select__option-main">
        <span className="chat-mode-select__label" data-testid="chat-panel-git-identity-label">{props.label}</span>
      </span>
      <Switch checked={props.checked} title={props.label} onChange={props.onChange} />
    </div>
  );
}

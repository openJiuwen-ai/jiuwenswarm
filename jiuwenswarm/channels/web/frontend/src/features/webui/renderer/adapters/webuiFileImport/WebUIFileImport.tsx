/**
 * Schema component: FileImport (Gateway S_10 section 6).
 * A click opens the system file picker; the host-injected import service uploads the files and the
 * node then hands the ticket to the plugin with ui.change. Progress and errors are host UI, not props.
 */

import { useRef, useState } from 'react';
import { Button } from 'antd';
import { useTranslation } from 'react-i18next';
import type { WebUIAdapterProps } from '../../types.ts';
import { WebUIUploadError, describePickedFiles } from '../../../transfers/uploads.ts';
import { useWebUIUploadService } from './uploadContext.ts';
import './FileImport.css';

type Phase = 'idle' | 'uploading' | 'done' | 'error';

const ERROR_KEYS: Readonly<Record<string, string>> = {
  UPLOAD_TOO_LARGE: 'webuiFileImport.tooLarge',
  LOCAL_ONLY: 'webuiFileImport.localOnly',
  TARGET_INACTIVE: 'webuiFileImport.inactive',
};

export function WebUIFileImport({ node, context, emit, appearance }: WebUIAdapterProps) {
  const { t } = useTranslation();
  const p = node.props as { label?: string; accept?: string[]; multiple?: boolean; directory?: boolean; disabled?: boolean };
  const service = useWebUIUploadService();
  const input = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [percent, setPercent] = useState(0);
  const [errorKey, setErrorKey] = useState('webuiFileImport.failed');
  const disabled = p.disabled === true || !context.connected || !service || phase === 'uploading';
  const directoryProps = p.directory ? { webkitdirectory: '', directory: '' } : {};

  const onPicked = async (list: FileList | null) => {
    const files = list ? describePickedFiles(Array.from(list)) : [];
    if (input.current) input.current.value = '';
    if (!files.length || !service) return;
    setPhase('uploading');
    setPercent(0);
    try {
      const uploadId = await service.upload(context.instanceKey, node.key, files, ({ sentBytes, totalBytes }) => {
        setPercent(totalBytes > 0 ? Math.floor((sentBytes * 100) / totalBytes) : 100);
      });
      const result = await emit('ui.change', { upload_id: uploadId });
      if (result.status !== 'sent' || result.receipt?.accepted === false) throw new WebUIUploadError('UPLOAD_FAILED');
      setPhase('done');
    } catch (error) {
      setErrorKey(ERROR_KEYS[error instanceof WebUIUploadError ? error.code : ''] ?? 'webuiFileImport.failed');
      setPhase('error');
    }
  };

  return (
    <span className={`webui-file-import ${appearance?.className ?? ''}`.trim()} style={appearance?.style}
      data-testid="webui-file-import" data-variant={phase}>
      <Button disabled={disabled} loading={phase === 'uploading'} onClick={() => input.current?.click()}
        data-testid="webui-file-import-btn">
        {p.label}
      </Button>
      <input ref={input} type="file" hidden tabIndex={-1} aria-hidden="true" data-testid="webui-file-import-input"
        accept={p.accept?.join(',')} multiple={p.multiple === true || p.directory === true} {...directoryProps}
        onChange={(event) => { void onPicked(event.currentTarget.files); }} />
      {phase === 'uploading' ? (
        <span className="webui-file-import__status" role="status" data-testid="webui-file-import-status">
          {t('webuiFileImport.uploading', { percent })}
        </span>
      ) : phase === 'error' ? (
        <span className="webui-file-import__status webui-file-import__status--error" role="alert"
          data-testid="webui-file-import-status">
          {t(errorKey)}
        </span>
      ) : null}
    </span>
  );
}

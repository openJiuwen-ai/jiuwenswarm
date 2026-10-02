/**
 * The foot of the documents table: adopt a document by its link.
 *
 * Sharing is how documents normally arrive -- discovery adopts everything the
 * connection can edit, on panel load, on Refresh and on a timer -- but a platform
 * does not always list what it was given (a Feishu file outside every managed wiki
 * space, a document the app itself created). This field is the way in for those:
 * the link is verified against every connection that can parse it, a document that
 * passes is adopted -- polled from now on and written to the config -- and one that
 * cannot be is told why. Adoption is not authority: the watch that the adoption
 * policy issued, or did not, is part of the answer shown here, so the person who
 * pasted the link sees the effect at the moment it happens.
 *
 * Its own component so the interaction can be driven in a test: type, press the
 * button, read the result.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

export interface AddResult {
  result: 'ok' | 'exists' | 'comment_only' | 'not_shared' | 'unknown' | 'invalid' | 'no_connection';
  detail?: string;
  title?: string;
  connection_id?: string;
  /** The identity the verdict is about -- the one the document must be shared with. */
  agent_address?: string;
  /** On ``ok``: the watch level the adoption policy issued, or ``off``. */
  watch?: string;
}

export function AdoptByLink({ adopt, copyAddress, copied }: {
  /** Verify and adopt one link; resolves to the gateway's verdict. */
  adopt: (url: string) => Promise<AddResult>;
  /**
   * Copies an agent address, offered when the verdict is "not shared". Given the
   * address the verdict names: with several connections the probe ran through the
   * one that can parse the link, and a "not shared" is about that identity, not
   * the one selected elsewhere in the panel.
   */
  copyAddress: (address?: string) => void;
  copied: boolean;
}) {
  const { t } = useTranslation();
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AddResult | null>(null);

  const submit = async () => {
    const trimmed = url.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setResult(null);
    try {
      const out = await adopt(trimmed);
      setResult(out);
      if (out?.result === 'ok') setUrl('');
    } catch (e) {
      setResult({ result: 'unknown', detail: String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <input
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              setResult(null);
            }}
            onKeyDown={(e) => e.key === 'Enter' && void submit()}
            placeholder={t('docs.addPlaceholder')}
            disabled={busy}
            data-testid="docs-adopt-input"
            className="w-full rounded-lg border border-border bg-transparent px-3.5 py-2 font-mono text-xs placeholder:font-sans placeholder:text-text-muted focus:border-accent focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !url.trim()}
          data-testid="docs-adopt-button"
          className="flex-none rounded-lg border border-accent bg-accent px-3.5 py-2 text-xs font-medium text-accent-foreground disabled:opacity-50"
        >
          {busy ? t('docs.refreshing') : t('docs.addAdopt')}
        </button>
      </div>
      {result && (
        <div
          className={`mt-2.5 rounded-r-lg border-l-[3px] px-4 py-3 text-sm ${
            result.result === 'ok'
              ? 'border-ok bg-ok-subtle'
              : result.result === 'comment_only'
                ? 'border-warn bg-warn-subtle'
                : result.result === 'exists'
                  ? 'border-border-strong bg-bg-muted'
                  : 'border-danger bg-danger-subtle'
          }`}
          data-testid="docs-add-result"
          data-result={result.result}
          data-watch={result.result === 'ok' ? result.watch || 'off' : undefined}
        >
          <b className="mb-0.5 block text-[13px]">{t(`docs.add.${result.result}.title`)}</b>
          <span className="text-xs text-text-muted">
            {result.result === 'ok'
              // An adoption is also an answer about authority: the policy is off by
              // default, and when it is on the person is told here.
              ? t(result.watch && result.watch !== 'off' ? 'docs.add.ok.detailOn' : 'docs.add.ok.detailOff')
              : t(`docs.add.${result.result}.detail`)}
          </span>
          {result.result !== 'ok' && (
            <div className="mt-2 flex gap-3">
              {result.result === 'not_shared' && (
                <button
                  type="button"
                  onClick={() => copyAddress(result.agent_address)}
                  className="text-xs text-accent hover:underline"
                  data-testid="docs-adopt-copy-address"
                  data-address={result.agent_address}
                >
                  {copied ? t('docs.copied') : t('docs.copyAddress')}
                </button>
              )}
              <button
                type="button"
                onClick={() => void submit()}
                className="rounded-md border border-border-strong bg-card px-3 py-1 text-xs"
                data-testid="docs-adopt-recheck"
              >
                {t('docs.recheck')}
              </button>
            </div>
          )}
        </div>
      )}
    </>
  );
}

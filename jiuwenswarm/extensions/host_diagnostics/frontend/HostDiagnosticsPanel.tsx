/**
 * Presentation-only panel for the Host Diagnostics plugin.
 *
 * Kept deliberately free of RPC concerns so the data fetching logic in
 * `index.tsx` stays easy to follow, and so this file has no import from the
 * core frontend beyond React itself.
 */
import { useMemo } from 'react';
import type { ChangeEvent } from 'react';

export interface DiagnosticsSection {
  key: string;
  payload: Record<string, unknown> | null;
}

export interface HostDiagnosticsPanelProps {
  sections: DiagnosticsSection[];
  loading: boolean;
  error: string | null;
  includeEnv: boolean;
  onToggleEnv: (next: boolean) => void;
  onRefresh: () => void;
}

const SECTION_TITLES: Record<string, string> = {
  summary: 'Summary',
  plugins: 'Installed extensions',
  paths: 'Paths',
};

/** Render an arbitrary JSON payload as a stable, sorted key/value list. */
function JsonBlock({ value }: { value: unknown }) {
  const text = useMemo(() => {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }, [value]);

  return (
    <pre className="host-diagnostics-json" data-testid="host-diagnostics-json">
      {text}
    </pre>
  );
}

export function HostDiagnosticsPanel({
  sections,
  loading,
  error,
  includeEnv,
  onToggleEnv,
  onRefresh,
}: HostDiagnosticsPanelProps) {
  return (
    <div className="host-diagnostics" data-testid="host-diagnostics">
      <header className="host-diagnostics-header">
        <h2 data-testid="host-diagnostics-title">Host Diagnostics</h2>
        <div className="host-diagnostics-actions">
          <label className="host-diagnostics-toggle">
            <input
              type="checkbox"
              checked={includeEnv}
              onChange={(event: ChangeEvent<HTMLInputElement>) =>
                onToggleEnv(event.target.checked)
              }
              data-testid="host-diagnostics-include-env"
            />
            <span>Include environment</span>
          </label>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            data-testid="host-diagnostics-refresh"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </header>

      {error ? (
        <p className="host-diagnostics-error" data-testid="host-diagnostics-error">
          {error}
        </p>
      ) : null}

      {sections.map((section) => (
        <section key={section.key} className="host-diagnostics-section">
          <h3 data-testid={`host-diagnostics-section-${section.key}`}>
            {SECTION_TITLES[section.key] ?? section.key}
          </h3>
          {section.payload === null ? (
            <p className="host-diagnostics-empty">No data</p>
          ) : (
            <JsonBlock value={section.payload} />
          )}
        </section>
      ))}
    </div>
  );
}

export default HostDiagnosticsPanel;

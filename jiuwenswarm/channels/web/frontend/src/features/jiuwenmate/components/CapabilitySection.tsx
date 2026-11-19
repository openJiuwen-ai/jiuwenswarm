import { ChevronUp, Layers2, Plus, Puzzle, X } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import skillIcon from '../../../assets/jiuwenmate/icons/skill.svg';
import { ActionMenu } from './ActionMenu';

export type CapabilityKind = 'skills' | 'connectors' | 'plugins' | 'knowledge';

interface CollapsibleSectionProps {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  testId?: string;
}

export function CollapsibleSection({ title, action, children, testId }: CollapsibleSectionProps) {
  const [expanded, setExpanded] = useState(true);
  return (
    <section className="jiuwenmate-collapsible" data-expanded={expanded} data-testid={testId}>
      <header>
        <button type="button" aria-expanded={expanded} onClick={() => setExpanded((current) => !current)}>
          <ChevronUp aria-hidden />
          <span>{title}</span>
        </button>
        {action}
      </header>
      {expanded ? <div className="jiuwenmate-collapsible__body">{children}</div> : null}
    </section>
  );
}

function CapabilityIcon({ kind }: { kind: CapabilityKind }) {
  if (kind === 'skills') return <img src={skillIcon} alt="" aria-hidden />;
  if (kind === 'knowledge') return <Layers2 aria-hidden />;
  return <Puzzle aria-hidden />;
}

interface CapabilitySectionProps {
  kind: CapabilityKind;
  title: string;
  addLabel: string;
  removeLabel: string;
  emptyLabel: string;
  emptyMenuLabel?: string;
  items: readonly string[];
  catalog: readonly string[];
  onChange: (items: string[]) => void;
}

export function CapabilitySection({
  kind,
  title,
  addLabel,
  removeLabel,
  emptyLabel,
  emptyMenuLabel,
  items,
  catalog,
  onChange,
}: CapabilitySectionProps) {
  const options = catalog.filter((item) => !items.includes(item));
  return (
    <CollapsibleSection
      title={title}
      testId="jiuwenmate-capability-section"
      action={
        <ActionMenu
          label={addLabel}
          className="jiuwenmate-collapsible__add"
          trigger={
            <>
              <Plus aria-hidden />
              <span>{addLabel}</span>
            </>
          }
          emptyLabel={options.length === 0 ? emptyMenuLabel ?? emptyLabel : undefined}
          items={options.map((option) => ({ key: option, label: option, onSelect: () => onChange([...items, option]) }))}
        />
      }
    >
      {items.length === 0 ? (
        <p className="jiuwenmate-collapsible__empty">{emptyLabel}</p>
      ) : (
        <div className="jiuwenmate-capability-grid">
          {items.map((item) => (
            <span key={item} className="jiuwenmate-capability-chip" data-kind={kind}>
              <CapabilityIcon kind={kind} />
              <span>{item}</span>
              <button type="button" aria-label={`${removeLabel} ${item}`} onClick={() => onChange(items.filter((value) => value !== item))}>
                <X aria-hidden />
              </button>
            </span>
          ))}
        </div>
      )}
    </CollapsibleSection>
  );
}

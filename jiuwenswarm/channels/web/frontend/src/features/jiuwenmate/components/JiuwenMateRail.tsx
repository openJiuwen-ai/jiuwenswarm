import { Box, House, Layers2, LayoutGrid, MessageCirclePlus, Puzzle, Settings2, Zap, type LucideIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import logo from '../../../assets/jiuwenmate/icons/logo.svg';
import { JIUWENMATE_PUBLIC_PAGES } from '../publicNav';
import type { JiuwenMateExistingPage } from '../types';
import { JiuwenMateModeSwitch } from './JiuwenMateModeSwitch';

export type JiuwenMateRailView = 'messages' | 'office';

interface JiuwenMateRailProps {
  activeView: JiuwenMateRailView;
  activePublicLabel: string | null;
  onViewChange: (view: JiuwenMateRailView) => void;
  onOpenExistingPage: (nav: JiuwenMateExistingPage, labelKey: string) => void;
  onExit: () => void;
}

const localItems = [
  { value: 'office' as const, labelKey: 'jiuwenmate.nav.office', icon: House },
  { value: 'messages' as const, labelKey: 'jiuwenmate.nav.messages', icon: MessageCirclePlus },
];

const existingIcons: Record<string, LucideIcon> = {
  'jiuwenmate.nav.plaza': LayoutGrid,
  'jiuwenmate.nav.assets': Box,
  'jiuwenmate.nav.skills': Zap,
  'jiuwenmate.nav.extensions': Puzzle,
  'jiuwenmate.nav.knowledge': Layers2,
  'jiuwenmate.nav.settings': Settings2,
};

export function JiuwenMateRail({
  activeView,
  activePublicLabel,
  onViewChange,
  onOpenExistingPage,
  onExit,
}: JiuwenMateRailProps) {
  const { t } = useTranslation();
  return (
    <aside className="jiuwenmate-rail" data-testid="jiuwenmate-rail">
      <div className="jiuwenmate-rail__logo" aria-label="JiuwenMate">
        <img src={logo} alt="" width={28} height={28} />
      </div>
      <nav aria-label="JiuwenMate" data-testid="jiuwenmate-rail-nav">
        {localItems.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.value}
              type="button"
              aria-pressed={activePublicLabel == null && activeView === item.value}
              onClick={() => onViewChange(item.value)}
              data-testid="jiuwenmate-rail-local-item"
              data-variant={item.value}
            >
              <Icon aria-hidden />
              <span>{t(item.labelKey)}</span>
            </button>
          );
        })}
        {JIUWENMATE_PUBLIC_PAGES.map((item) => {
          const Icon = existingIcons[item.labelKey];
          return (
            <button
              key={item.labelKey}
              type="button"
              aria-pressed={activePublicLabel === item.labelKey}
              onClick={() => onOpenExistingPage(item.nav, item.labelKey)}
              data-testid="jiuwenmate-rail-existing-item"
              data-variant={item.labelKey}
            >
              <Icon aria-hidden />
              <span>{t(item.labelKey)}</span>
            </button>
          );
        })}
      </nav>
      <div className="jiuwenmate-rail__spacer" />
      <JiuwenMateModeSwitch active label={t('jiuwenmate.mode.exit')} onToggle={onExit} iconOnly />
    </aside>
  );
}

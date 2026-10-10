import { Plus } from 'lucide-react';
import type { HTMLAttributes, ReactNode } from 'react';

export type InfoCardProps = Omit<HTMLAttributes<HTMLDivElement>, 'children' | 'title'> & {
  /** 第一行左侧带背景色的块（如首字符色块头像），背景样式由调用方提供 */
  leading: ReactNode;
  title: ReactNode;
  /** 第一行最右侧加号的点击回调；不传则不渲染加号 */
  onPlusClick?: () => void;
  /** 第二行描述，超过两行截断 */
  description?: ReactNode;
  /** 追加行（如标签行），渲染在描述下方 */
  children?: ReactNode;
};

export function InfoCard({
  leading,
  title,
  onPlusClick,
  description,
  children,
  className,
  onClick,
  style,
  ...props
}: InfoCardProps) {
  return (
    <div
      {...props}
      onClick={onClick}
      style={style}
      className={
        'group relative text-left border border-border bg-panel hover:bg-card rounded-[8px] pt-6 pb-4 px-4 flex flex-col min-w-0 overflow-visible' +
        (onClick ? ' cursor-pointer' : '') +
        (className ? ` ${className}` : '')
      }
    >
      <div className="flex flex-shrink-0 items-center gap-3">
        {leading}
        <span className="min-w-0 flex-1 truncate text-sm font-semibold leading-5 text-text-strong">{title}</span>
        {onPlusClick && (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onPlusClick();
            }}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] text-text-muted transition-colors hover:bg-secondary hover:text-text"
          >
            <Plus size={16} />
          </button>
        )}
      </div>
      {description != null && description !== '' && (
        <div className="mt-4 line-clamp-2 text-xs text-text-muted">{description}</div>
      )}
      {children}
    </div>
  );
}

import { useId } from 'react';

export interface LoadingSpinnerProps {
  size?: number;
  testId?: string;
}

export function LoadingSpinner({ size = 16, testId }: LoadingSpinnerProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const maskId = `loading-mask-${uid}`;
  const clipId = `loading-clip-${uid}`;
  const gradientId = `loading-gradient-${uid}`;
  const filterId = `loading-filter-${uid}`;

  return (
    <svg
      viewBox="0 0 16 16"
      width={size}
      height={size}
      fill="none"
      aria-hidden="true"
      className="shrink-0 text-text-meta animate-spin"
      data-testid={testId}
    >
      <mask id={maskId} width={14} height={14} x={1} y={1} maskUnits="userSpaceOnUse">
        <g filter={`url(#${filterId})`}>
          <path
            d="M8 1C8.44043 1 8.79747 1.35704 8.79747 1.79747C8.79747 2.2379 8.44043 2.59494 8 2.59494C5.01486 2.59494 2.59494 5.01486 2.59494 8C2.59494 10.9851 5.01486 13.4051 8 13.4051C10.9851 13.4051 13.4051 10.9851 13.4051 8C13.4051 7.55957 13.7621 7.20253 14.2025 7.20253C14.643 7.20253 15 7.55957 15 8C15 11.866 11.866 15 8 15C4.134 15 1 11.866 1 8C1 4.134 4.134 1 8 1Z"
            fill={`url(#${gradientId})`}
            fillRule="nonzero"
          />
        </g>
      </mask>
      <defs>
        <clipPath id={clipId}>
          <rect width={16} height={16} x={0} y={0} fill="rgb(255,255,255)" />
        </clipPath>
        <linearGradient
          id={gradientId}
          x1={18.9547691}
          x2={4.75360775}
          y1={8.00000095}
          y2={0.999997139}
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="rgb(255,255,255)" offset={0.00095607515} stopOpacity={0} />
          <stop stopColor="rgb(255,254.745,254.745)" offset={0.975277901} stopOpacity={1} />
        </linearGradient>
        <filter id={filterId}>
          <feColorMatrix type="matrix" values="0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 1 0 " />
        </filter>
      </defs>
      <rect width={16} height={16} x={0} y={0} />
      <g clipPath={`url(#${clipId})`}>
        <rect width={16} height={16} x={0} y={0} />
        <g mask={`url(#${maskId})`}>
          <path d="M0 16L16 16L16 0L0 0L0 16Z" fill="currentColor" fillRule="evenodd" />
        </g>
      </g>
    </svg>
  );
}

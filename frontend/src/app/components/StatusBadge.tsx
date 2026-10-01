import type { InvoiceStatus } from '@/lib/types';

interface StatusBadgeProps {
  status: InvoiceStatus | string;
  className?: string;
}

interface BadgeConfig {
  label: string;
  badgeClass: string;
  dotClass: string;
}

export function StatusBadge({ status, className = '' }: StatusBadgeProps) {
  const normalizedStatus = status.toLowerCase();

  let config: BadgeConfig;

  switch (normalizedStatus) {
    case 'pending':
      config = {
        label: 'Pending',
        badgeClass:
          'bg-amber-500/10 text-amber-700 border-amber-300/60 dark:bg-amber-500/15 dark:text-amber-300 dark:border-amber-700/50',
        dotClass: 'bg-amber-500 shadow-[0_0_6px_rgba(245,158,11,0.4)]',
      };
      break;

    case 'completed':
    case 'approved':
      config = {
        label: normalizedStatus === 'approved' ? 'Approved' : 'Completed',
        badgeClass:
          'bg-emerald-500/10 text-emerald-700 border-emerald-300/60 dark:bg-emerald-500/15 dark:text-emerald-300 dark:border-emerald-700/50',
        dotClass: 'bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.4)]',
      };
      break;

    case 'skipped':
      config = {
        label: 'Skipped',
        badgeClass:
          'bg-zinc-500/10 text-zinc-600 border-zinc-300/60 dark:bg-zinc-500/15 dark:text-zinc-400 dark:border-zinc-700/50',
        dotClass: 'bg-zinc-400 dark:bg-zinc-500',
      };
      break;

    case 'failed':
    case 'rejected':
      config = {
        label: normalizedStatus === 'rejected' ? 'Rejected' : 'Failed',
        badgeClass:
          'bg-rose-500/10 text-rose-700 border-rose-300/60 dark:bg-rose-500/15 dark:text-rose-300 dark:border-rose-800/50',
        dotClass: 'bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.4)]',
      };
      break;

    case 'flagged':
      config = {
        label: 'Flagged',
        badgeClass:
          'bg-orange-500/10 text-orange-700 border-orange-300/60 dark:bg-orange-500/15 dark:text-orange-300 dark:border-orange-800/50',
        dotClass: 'bg-orange-500 shadow-[0_0_6px_rgba(249,115,22,0.4)]',
      };
      break;

    default:
      config = {
        label: status,
        badgeClass:
          'bg-zinc-500/10 text-zinc-600 border-zinc-300/60 dark:bg-zinc-500/15 dark:text-zinc-400 dark:border-zinc-700/50',
        dotClass: 'bg-zinc-400 dark:bg-zinc-500',
      };
      break;
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium tracking-tight shadow-[0_1px_2px_rgba(0,0,0,0.02)] transition-colors ${config.badgeClass} ${className}`}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${config.dotClass}`}
        aria-hidden="true"
      />
      <span>{config.label}</span>
    </span>
  );
}

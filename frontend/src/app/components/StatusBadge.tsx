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
  const normalizedStatus = (typeof status === 'string' ? status : String(status || 'unknown')).toLowerCase();

  let config: BadgeConfig;

  switch (normalizedStatus) {
    case 'pending':
      config = {
        label: 'Pending',
        badgeClass: 'bg-gradient-to-b from-amber-500/10 via-amber-500/8 to-amber-500/5 text-amber-800 border-t-amber-300/70 border-x-amber-400/40 border-b-amber-500/50 shadow-[inset_0_1px_0_rgba(255,255,255,0.7)]',
        dotClass: 'bg-amber-500 shadow-[0_0_6px_rgba(245,158,11,0.5)]',
      };
      break;

    case 'running':
    case 'in_progress':
      config = {
        label: 'Running',
        badgeClass: 'bg-gradient-to-b from-sky-500/15 via-sky-500/10 to-sky-500/5 text-sky-800 border-t-sky-300/80 border-x-sky-400/50 border-b-sky-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-sky-500 shadow-[0_0_6px_rgba(14,165,233,0.6)] animate-pulse',
      };
      break;

    case 'awaiting_approval':
      config = {
        label: 'Awaiting Approval',
        badgeClass: 'bg-gradient-to-b from-purple-500/15 via-purple-500/10 to-purple-500/5 text-purple-800 border-t-purple-300/80 border-x-purple-400/50 border-b-purple-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-purple-500 shadow-[0_0_6px_rgba(168,85,247,0.6)] animate-pulse',
      };
      break;

    case 'paused':
      config = {
        label: 'Paused',
        badgeClass: 'bg-gradient-to-b from-amber-500/15 via-amber-500/10 to-amber-500/5 text-amber-800 border-t-amber-300/80 border-x-amber-400/50 border-b-amber-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-amber-500',
      };
      break;

    case 'completed':
    case 'approved':
    case 'done':
      config = {
        label: normalizedStatus === 'approved' ? 'Approved' : 'Completed',
        badgeClass: 'bg-gradient-to-b from-emerald-500/15 via-emerald-500/10 to-emerald-500/5 text-emerald-800 border-t-emerald-300/80 border-x-emerald-400/50 border-b-emerald-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.5)]',
      };
      break;

    case 'skipped':
      config = {
        label: 'Skipped',
        badgeClass: 'bg-zinc-100 text-zinc-700 border-t-zinc-200 border-x-zinc-300/60 border-b-zinc-400/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-zinc-400',
      };
      break;

    case 'failed':
    case 'rejected':
    case 'error':
      config = {
        label: normalizedStatus === 'rejected' ? 'Rejected' : 'Failed',
        badgeClass: 'bg-gradient-to-b from-rose-500/15 via-rose-500/10 to-rose-500/5 text-rose-800 border-t-rose-300/80 border-x-rose-400/50 border-b-rose-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.5)]',
      };
      break;

    case 'session_lost':
      config = {
        label: 'Session Lost',
        badgeClass: 'bg-gradient-to-b from-amber-500/20 via-amber-500/15 to-amber-500/10 text-amber-900 border-t-amber-300 border-x-amber-400 border-b-amber-600 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-amber-500 shadow-[0_0_6px_rgba(245,158,11,0.6)] animate-ping',
      };
      break;

    case 'flagged':
    case 'stalled':
      config = {
        label: normalizedStatus === 'stalled' ? 'Stalled' : 'Flagged',
        badgeClass: 'bg-gradient-to-b from-orange-500/15 via-orange-500/10 to-orange-500/5 text-orange-800 border-t-orange-300/80 border-x-orange-400/50 border-b-orange-600/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]',
        dotClass: 'bg-orange-500 shadow-[0_0_6px_rgba(249,115,22,0.5)]',
      };
      break;

    case 'idle':
    default:
      config = {
        label: normalizedStatus === 'idle' ? 'Idle' : status,
        badgeClass: 'bg-gradient-to-b from-zinc-100 via-zinc-100/90 to-zinc-200/50 text-zinc-700 border-t-white border-x-zinc-200 border-b-zinc-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.9)]',
        dotClass: 'bg-zinc-400',
      };
      break;
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold tracking-tight shadow-2xs transition-all ${config.badgeClass} ${className}`}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${config.dotClass}`}
        aria-hidden="true"
      />
      <span>{config.label}</span>
    </span>
  );
}


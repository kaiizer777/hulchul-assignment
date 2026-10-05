'use client';

import React, { useState, useEffect } from 'react';
import { StatusBadge } from '../components/StatusBadge';
import { LiveStepExecutionLog, requestScrollToStep } from '../components/LiveStepExecutionLog';
import {
  useAgentStore,
  isTerminalStatus,
  PRESET_GOALS,
} from '../store/useAgentStore';

type DevLogEvent = 'landing' | 'invoice_created' | 'agent_run';

/**
 * Dev-only log hook (inline stub). The shared `@/lib/dev-logs` module is a
 * gitignored local-only file, so a static import would break fresh clones.
 * This stub keeps the same contract (dev-only, never throws, warns on
 * failure) and POSTs to `/api/dev-logs` when that local route exists.
 */
async function trackDevLog(
  event: DevLogEvent,
  metadata?: Record<string, string | number>,
): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  if (process.env.NODE_ENV !== 'development') return false;
  try {
    const res = await fetch('/api/dev-logs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event, metadata: metadata ?? {} }),
    });
    if (!res.ok) {
      console.warn(`[dev-logs] track ${event} failed: HTTP ${res.status}`);
      return false;
    }
    return true;
  } catch (err: unknown) {
    console.warn(`[dev-logs] track ${event} failed:`, err instanceof Error ? err.message : err);
    return false;
  }
}

const TERMINAL_FAILURE_COPY: Record<
  'failed' | 'stalled' | 'session_lost',
  { banner: string; message: string }
> = {
  failed: {
    banner: 'border-red-200 bg-red-50/90 text-red-800',
    message:
      'Run failed: the agent stopped with an error. Review the steps below, then start a fresh run with the same goal.',
  },
  stalled: {
    banner: 'border-orange-200 bg-orange-50/90 text-orange-800',
    message:
      'Run stalled: the agent hit the step limit or timed out. Start a fresh run with the same goal.',
  },
  session_lost: {
    banner: 'border-amber-300 bg-amber-50/90 text-amber-900',
    message:
      'Browser session lost: the remote Steel/Browserless CDP target was evicted mid-run. Bounded reattach was attempted and exhausted. Start a new run to retry.',
  },
};

export default function AgentControlPage() {
  const goal = useAgentStore((s) => s.goal);
  const selectedPresetId = useAgentStore((s) => s.selectedPresetId);
  const runId = useAgentStore((s) => s.runId);
  const status = useAgentStore((s) => s.status);
  const connectionState = useAgentStore((s) => s.connectionState);
  const steps = useAgentStore((s) => s.steps);
  const approvalData = useAgentStore((s) => s.approvalData);
  const verificationReport = useAgentStore((s) => s.verificationReport);
  const selectedScreenshot = useAgentStore((s) => s.selectedScreenshot);
  const autoScroll = useAgentStore((s) => s.autoScroll);
  const isStarting = useAgentStore((s) => s.isStarting);
  const isPausingOrResuming = useAgentStore((s) => s.isPausingOrResuming);
  const isSubmittingApproval = useAgentStore((s) => s.isSubmittingApproval);
  const isFetchingScreenshot = useAgentStore((s) => s.isFetchingScreenshot);
  const isFetchingVerification = useAgentStore((s) => s.isFetchingVerification);
  const error = useAgentStore((s) => s.error);
  const verificationError = useAgentStore((s) => s.verificationError);

  const setGoal = useAgentStore((s) => s.setGoal);
  const selectPreset = useAgentStore((s) => s.selectPreset);
  const startRun = useAgentStore((s) => s.startRun);
  const togglePause = useAgentStore((s) => s.togglePause);
  const submitApproval = useAgentStore((s) => s.submitApproval);
  const fetchVerification = useAgentStore((s) => s.fetchVerification);
  const viewScreenshot = useAgentStore((s) => s.viewScreenshot);
  const closeScreenshot = useAgentStore((s) => s.closeScreenshot);
  const clearError = useAgentStore((s) => s.clearError);
  const setAutoScroll = useAgentStore((s) => s.setAutoScroll);
  const toggleAutoScroll = useAgentStore((s) => s.toggleAutoScroll);

  const [copiedRunId, setCopiedRunId] = useState(false);

  // Client hydration sync with URL search params and local storage
  useEffect(() => {
    useAgentStore.getState().syncFromStorageOrUrl();
    return () => {
      useAgentStore.getState().resetRun();
    };
  }, []);

  const isTerminal = isTerminalStatus(status);
  const isBusy =
    isStarting ||
    status === 'running' ||
    status === 'paused' ||
    status === 'awaiting_approval';

  const handleFormSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isBusy || !goal.trim()) return;
    // Dev-only click event (fire-and-forget; must never block the run).
    void trackDevLog('agent_run', { goal: goal.trim().slice(0, 120) });
    await startRun();
  };

  // Honest new run: re-POSTs the same goal via startRun (fresh run_id,
  // cleared steps — no replayed or duplicated actions).
  const handleStartNewRun = () => {
    if (isBusy || isStarting || !goal.trim()) return;
    void trackDevLog('agent_run', { goal: goal.trim().slice(0, 120) });
    void startRun();
  };

  const copyRunIdToClipboard = async () => {
    if (!runId) return;
    try {
      await navigator.clipboard.writeText(runId);
      setCopiedRunId(true);
      setTimeout(() => setCopiedRunId(false), 2000);
    } catch {
      console.warn('Clipboard write failed');
    }
  };

  return (
    <div className="space-y-6 pb-14">
      {/* Page Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900">
              Agent Control & Live Monitor
            </h1>
            <StatusBadge status={status} />
            {connectionState === 'connecting' && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-600 border border-zinc-200">
                <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-pulse" />
                Connecting stream...
              </span>
            )}
            {connectionState === 'reconnecting' && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 border border-amber-200">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse" />
                Reconnecting stream...
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            Dispatch autonomous browser agents to process accounts payable, evaluate purchase orders, and manage approvals.
          </p>
        </div>

        {runId && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={copyRunIdToClipboard}
              className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-3 py-1.5 font-mono text-xs text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:bg-zinc-50 active:translate-y-[0.5px]"
              title="Click to copy full Run ID"
            >
              <svg className="h-3.5 w-3.5 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
              <span>Run: {runId.length > 16 ? `${runId.slice(0, 12)}...` : runId}</span>
              {copiedRunId ? (
                <span className="text-emerald-600 font-sans text-[10px] font-bold">Copied!</span>
              ) : (
                <span className="text-zinc-400 font-sans text-[10px]">Copy</span>
              )}
            </button>

            <button
              type="button"
              onClick={togglePause}
              disabled={isPausingOrResuming || isTerminal}
              className={`inline-flex items-center gap-2 rounded-lg border px-3.5 py-1.5 text-xs font-semibold shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all active:translate-y-[0.5px] disabled:opacity-50 ${
                status === 'paused'
                  ? 'border-t-emerald-200 border-x-emerald-300 border-b-emerald-400 bg-gradient-to-b from-emerald-50 to-emerald-100/70 text-emerald-800 hover:from-emerald-100 hover:to-emerald-200/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]'
                  : 'border-t-amber-200 border-x-amber-300 border-b-amber-400 bg-gradient-to-b from-amber-50 to-amber-100/70 text-amber-800 hover:from-amber-100 hover:to-amber-200/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]'
              }`}
            >
              {isPausingOrResuming ? (
                <>
                  <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Updating...</span>
                </>
              ) : status === 'paused' ? (
                <>
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Resume Agent</span>
                </>
              ) : (
                <>
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Pause Agent</span>
                </>
              )}
            </button>
          </div>
        )}
      </div>

      {/* Goal Dispatch Section */}
      <div className="rounded-2xl border border-zinc-200/90 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] transition-all">
        <form onSubmit={handleFormSubmit} className="space-y-4">
          <div>
            <div className="flex items-center justify-between">
              <label htmlFor="goal-input" className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-zinc-700">
                <span className="flex h-5 w-5 items-center justify-center rounded-md bg-zinc-100 text-zinc-700 border border-zinc-200 shadow-2xs">
                  <svg className="h-3 w-3 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                </span>
                Agent Goal & Instruction
              </label>
              <span className="text-[11px] text-zinc-400 font-medium">
                Press <kbd className="rounded border border-zinc-200 bg-zinc-100 px-1 py-0.5 font-mono text-[10px] text-zinc-600">Ctrl + Enter</kbd> to run
              </span>
            </div>
            <div className="mt-2.5">
              <textarea
                id="goal-input"
                rows={3}
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                    e.preventDefault();
                    handleFormSubmit(e);
                  }
                }}
                placeholder="Enter natural language instruction for the autonomous browser agent..."
                className="block w-full rounded-xl border border-zinc-300/80 bg-zinc-50/60 p-3.5 text-sm text-zinc-900 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.03)] transition-colors focus:border-zinc-800 focus:bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900/10 font-sans leading-relaxed"
              />
            </div>
          </div>

          {/* Preset Goal Suggestions */}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span className="text-xs font-semibold text-zinc-400">Suggestions:</span>
            {PRESET_GOALS.map((preset) => {
              const isSelected =
                goal.trim() === preset.goal.trim() || selectedPresetId === preset.id;
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => selectPreset(preset.id)}
                  className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-all active:translate-y-[0.5px] ${
                    isSelected
                      ? 'border-t-zinc-700 border-x-zinc-800 border-b-black bg-zinc-900 text-white shadow-xs font-semibold'
                      : 'border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-700 hover:border-zinc-300 hover:bg-zinc-100/80 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)]'
                  }`}
                >
                  {preset.goal}
                </button>
              );
            })}
          </div>

          <div className="flex flex-col gap-3 pt-3 sm:flex-row sm:items-center sm:justify-between border-t border-zinc-100">
            <div className="flex items-center gap-2 text-xs text-zinc-500">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span>Agent operates mock ERP via remote browser CDP (Steel / Browserless)</span>
            </div>

            <button
              type="submit"
              disabled={isBusy}
              className="inline-flex items-center justify-center gap-2 rounded-xl border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-6 py-2.5 text-sm font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(0,0,0,0.25)] transition-all hover:from-zinc-750 hover:to-zinc-900 active:translate-y-[0.5px] disabled:opacity-50"
            >
              {isStarting ? (
                <>
                  <svg className="h-4 w-4 animate-spin text-zinc-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Initializing...</span>
                </>
              ) : status === 'running' ? (
                <>
                  <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
                  <span>Agent Running...</span>
                </>
              ) : (
                <>
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Run Agent</span>
                </>
              )}
            </button>
          </div>
        </form>

        {error && (
          <div className="mt-4 flex items-center justify-between rounded-xl border border-red-200 bg-red-50/90 p-4 text-xs text-red-800 shadow-2xs">
            <span>{error}</span>
            <button
              type="button"
              onClick={clearError}
              className="text-red-700 font-semibold underline underline-offset-2 ml-3 shrink-0"
            >
              Dismiss
            </button>
          </div>
        )}

        {(status === 'failed' || status === 'stalled' || status === 'session_lost') && (
          <div
            className={`mt-4 flex flex-col gap-3 rounded-xl border p-4 text-xs shadow-2xs sm:flex-row sm:items-center sm:justify-between ${TERMINAL_FAILURE_COPY[status].banner}`}
          >
            <span>{TERMINAL_FAILURE_COPY[status].message}</span>
            <button
              type="button"
              onClick={handleStartNewRun}
              disabled={isBusy || isStarting || !goal.trim()}
              title="Starts a fresh run with the same goal (new run_id)"
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-black bg-gradient-to-b from-zinc-800 via-zinc-900 to-zinc-950 px-3.5 py-1.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_6px_rgba(0,0,0,0.25)] transition-all active:translate-y-[0.5px] disabled:opacity-50"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <span>{isStarting ? 'Starting...' : 'Start new run'}</span>
            </button>
          </div>
        )}
      </div>

      {/* Real-Time Step Log via SSE */}
      <LiveStepExecutionLog
        steps={steps}
        status={status}
        autoScroll={autoScroll}
        onToggleAutoScroll={toggleAutoScroll}
        onSetAutoScroll={setAutoScroll}
        onViewScreenshot={viewScreenshot}
        isFetchingScreenshot={isFetchingScreenshot}
      />

      {/* Verification Report Section (Phase 4) */}
      {(verificationReport ||
        isFetchingVerification ||
        verificationError ||
        (runId && isTerminal)) && (
        <div className="rounded-2xl border border-zinc-200/90 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] space-y-6">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-zinc-200/80">
            <div>
              <h3 className="text-base font-bold text-zinc-900 flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 shadow-2xs">
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </span>
                Phase 4: Verification & Evidence Report
              </h3>
              <p className="text-xs text-zinc-500 mt-0.5">
                Automated post-run comparison of actual ERP invoice states against expected seed rules.
              </p>
            </div>
            <button
              type="button"
              onClick={() => fetchVerification()}
              disabled={isFetchingVerification || !runId}
              className="inline-flex items-center gap-2 rounded-xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-3.5 py-2 text-xs font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)] hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-50"
            >
              {isFetchingVerification ? (
                <>
                  <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Refreshing...</span>
                </>
              ) : (
                <>
                  <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Refresh Report</span>
                </>
              )}
            </button>
          </div>

          {verificationError && (
            <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-xs text-red-800 shadow-2xs">
              {verificationError}
            </div>
          )}

          {verificationReport ? (
            <>
              {/* Summary Metrics Cards */}
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div className="rounded-xl border border-t-zinc-200 border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50/80 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)]">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">Total Invoices</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-zinc-900 tabular-nums">{verificationReport.total_invoices}</div>
                </div>
                <div className="rounded-xl border border-t-emerald-200 border-x-emerald-300 border-b-emerald-400 bg-gradient-to-b from-emerald-50/60 to-emerald-100/30 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)]">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-emerald-700">Passed</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-emerald-700 tabular-nums">{verificationReport.pass_count}</div>
                </div>
                <div className="rounded-xl border border-t-rose-200 border-x-rose-300 border-b-rose-400 bg-gradient-to-b from-rose-50/60 to-rose-100/30 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)]">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-rose-700">Failed / Mismatched</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-rose-700 tabular-nums">{verificationReport.fail_count}</div>
                </div>
                <div className="rounded-xl border border-t-amber-200 border-x-amber-300 border-b-amber-400 bg-gradient-to-b from-amber-50/60 to-amber-100/30 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)]">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-amber-700">Incomplete / Flagged</div>
                  <div className="mt-1 font-mono text-2xl font-bold text-amber-700 tabular-nums">{verificationReport.incomplete_count}</div>
                </div>
              </div>

              {/* Incomplete / Flagged Items Callout */}
              {verificationReport.incomplete_items.length > 0 && (
                <div className="rounded-xl border border-amber-200/90 bg-amber-50/70 p-4 shadow-2xs">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-amber-900 mb-2">
                    Incomplete / Flagged Items Requiring Attention ({verificationReport.incomplete_items.length})
                  </h4>
                  <div className="space-y-2">
                    {verificationReport.incomplete_items.map((item, idx) => (
                      <div key={idx} className="flex flex-col sm:flex-row sm:items-center justify-between text-xs gap-1 border-t border-amber-200/60 pt-2 font-mono">
                        <div>
                          <span className="font-bold text-zinc-900">{item.vendor}</span>
                          <span className="text-zinc-500 ml-2">({item.po_number || 'No PO'})</span>
                          <span className="ml-2 font-semibold text-emerald-700">₹{item.amount.toLocaleString()}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="rounded bg-amber-200 px-2 py-0.5 text-[10px] font-bold text-amber-900 uppercase">
                            {item.status}
                          </span>
                          <span className="text-zinc-600">{item.reason}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Verification Table */}
              <div className="overflow-x-auto rounded-xl border border-zinc-200 shadow-2xs">
                <table className="w-full text-left text-xs font-mono">
                  <thead className="border-b border-zinc-200 bg-zinc-50 text-zinc-700">
                    <tr>
                      <th className="p-3">ID / Vendor</th>
                      <th className="p-3">Amount</th>
                      <th className="p-3">PO Number</th>
                      <th className="p-3">Expected</th>
                      <th className="p-3">Actual</th>
                      <th className="p-3">Match</th>
                      <th className="p-3">Reason / Details</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-200 bg-white">
                    {verificationReport.verification_table.map((row, idx) => (
                      <tr key={idx} className="hover:bg-zinc-50/80 transition-colors">
                        <td className="p-3">
                          <div className="font-semibold text-zinc-900">{row.vendor}</div>
                          <div className="text-[10px] text-zinc-400">{row.invoice_id.slice(0, 8)}...</div>
                        </td>
                        <td className="p-3 font-semibold text-zinc-900">
                          ₹{row.amount.toLocaleString()}
                        </td>
                        <td className="p-3 text-zinc-700">
                          {row.po_number || <span className="text-zinc-400 italic">None</span>}
                        </td>
                        <td className="p-3">
                          <span className="rounded bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-800">
                            {row.expected_status}
                          </span>
                        </td>
                        <td className="p-3">
                          <span
                            className={`rounded px-2 py-0.5 text-[10px] font-medium ${
                              row.actual_status === 'completed'
                                ? 'bg-emerald-100 text-emerald-800'
                                : row.actual_status === 'flagged'
                                ? 'bg-amber-100 text-amber-800'
                                : 'bg-rose-100 text-rose-800'
                            }`}
                          >
                            {row.actual_status}
                          </span>
                        </td>
                        <td className="p-3">
                          {row.pass_fail ? (
                            <span className="inline-flex items-center gap-1 font-bold text-emerald-600">
                              ✅ Pass
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 font-bold text-rose-600">
                              ❌ Fail
                            </span>
                          )}
                        </td>
                        <td className="p-3 text-zinc-600">
                          <div>{row.reason}</div>
                          <div className="text-[10px] text-zinc-400 mt-0.5 font-sans">
                            Class: {row.classification}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Failed Step Screenshots Inline Evidence */}
              {verificationReport.failed_steps.length > 0 && (
                <div className="space-y-3 pt-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-900">
                    Failed Step Screenshots & Evidence ({verificationReport.failed_steps.length})
                  </h4>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    {verificationReport.failed_steps.map((step, idx) => {
                      const hasLogStep =
                        Boolean(step.step_id) && steps.some((s) => s.step_id === step.step_id);
                      return (
                      <div key={idx} className="rounded-xl border border-red-200 bg-red-50/50 p-4 space-y-3 shadow-2xs">
                        <div className="flex items-center justify-between gap-2 text-xs font-mono">
                          <span className="font-bold text-red-900 truncate">{step.action}</span>
                          <span className="flex shrink-0 items-center gap-2">
                            <span className="text-zinc-400">{new Date(step.timestamp).toLocaleTimeString()}</span>
                            <button
                              type="button"
                              onClick={() => requestScrollToStep(step.step_id)}
                              disabled={!hasLogStep}
                              title={
                                hasLogStep
                                  ? 'Scroll to this step in the execution log'
                                  : 'Step not present in the current execution log'
                              }
                              className="inline-flex items-center gap-1 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-2 py-0.5 text-[11px] font-semibold text-zinc-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] hover:bg-zinc-100 active:translate-y-[0.5px] disabled:opacity-50 disabled:hover:bg-white"
                            >
                              View in log
                            </button>
                          </span>
                        </div>
                        <div className="text-xs font-mono text-red-800 bg-white/80 p-2.5 rounded-lg border border-red-200">
                          {step.result || 'Failed step'}
                        </div>
                        {step.screenshot_b64 ? (
                          <div
                            onClick={() => viewScreenshot(step.step_id)}
                            className="cursor-pointer overflow-hidden rounded-lg border border-red-200 bg-black group relative"
                          >
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={`data:image/png;base64,${step.screenshot_b64}`}
                              alt="Failed Step Screenshot"
                              className="h-36 w-full object-cover transition-transform group-hover:scale-105"
                            />
                            <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center text-white text-xs font-semibold">
                              Click to expand thumbnail
                            </div>
                          </div>
                        ) : (
                          <div className="text-[11px] text-zinc-400 italic">No screenshot captured for this step.</div>
                        )}
                      </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="flex flex-col items-center justify-center py-8 text-center text-zinc-400">
              <p className="text-xs">Verification report not loaded yet. Click &quot;Refresh Report&quot; above or wait for run completion.</p>
            </div>
          )}
        </div>
      )}

      {/* Approval Modal */}
      {status === 'awaiting_approval' && approvalData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/50 backdrop-blur-sm p-4">
          <div className="w-full max-w-lg rounded-2xl border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white p-6 shadow-2xl space-y-4">
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-amber-100 text-amber-700 border border-amber-200 shadow-2xs">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
              <div>
                <h3 className="text-lg font-bold text-zinc-900">
                  Human Approval Required
                </h3>
                <p className="text-xs text-zinc-500">
                  Agent paused because invoice amount exceeds approval threshold.
                </p>
              </div>
            </div>

            <div className="my-5 rounded-xl border border-zinc-200 bg-zinc-50/70 p-4 space-y-2.5 font-mono text-xs shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)]">
              <div className="flex justify-between">
                <span className="text-zinc-500">Vendor:</span>
                <span className="font-bold text-zinc-900">{approvalData.vendor || '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-500">Invoice Amount:</span>
                <span className="font-bold text-emerald-600">
                  {approvalData.amount != null ? `₹${approvalData.amount.toLocaleString()}` : '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-500">PO Number:</span>
                <span className="text-zinc-900">{approvalData.po_number || '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-500">Threshold:</span>
                <span className="text-zinc-900">
                  {approvalData.threshold != null ? `₹${approvalData.threshold.toLocaleString()}` : '—'}
                </span>
              </div>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                type="button"
                disabled={isSubmittingApproval || !approvalData.nonce}
                onClick={() => submitApproval('rejected')}
                className="rounded-xl border border-t-rose-200 border-x-rose-300 border-b-rose-400 bg-gradient-to-b from-rose-50 to-rose-100 px-4 py-2.5 text-xs font-semibold text-rose-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.04)] hover:bg-rose-100 active:translate-y-[0.5px] disabled:opacity-50"
              >
                {isSubmittingApproval ? 'Processing...' : 'Reject Invoice'}
              </button>

              <button
                type="button"
                disabled={isSubmittingApproval || !approvalData.nonce}
                onClick={() => submitApproval('approved')}
                className="rounded-xl border-t border-t-emerald-400 border-x border-x-emerald-600 border-b border-b-emerald-800 bg-gradient-to-b from-emerald-600 to-emerald-700 px-5 py-2.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_2px_4px_rgba(0,0,0,0.15)] hover:from-emerald-550 hover:to-emerald-650 active:translate-y-[0.5px] disabled:opacity-50"
              >
                {isSubmittingApproval ? 'Processing...' : 'Approve & Continue'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Screenshot Preview Modal */}
      {selectedScreenshot && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/60 backdrop-blur-sm p-4">
          <div className="w-full max-w-4xl rounded-2xl border border-zinc-200 bg-white p-6 shadow-2xl space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-zinc-200">
              <h3 className="text-sm font-bold text-zinc-900 flex items-center gap-2">
                <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                </svg>
                Step Screenshot Preview
              </h3>
              <button
                type="button"
                onClick={closeScreenshot}
                className="rounded-lg bg-zinc-100 p-1.5 text-zinc-500 hover:bg-zinc-200 hover:text-zinc-800 transition-colors"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="overflow-hidden rounded-xl border border-zinc-200 bg-zinc-100 flex items-center justify-center p-2">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={`data:image/png;base64,${selectedScreenshot}`}
                alt="Agent Step Screenshot"
                className="max-h-[70vh] w-auto object-contain rounded-lg shadow-sm"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

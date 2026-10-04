'use client';

import React, { useState, useMemo, useRef, useEffect } from 'react';

export interface StepEvent {
  type: string;
  run_id: string;
  step_id?: string;
  step_index?: number;
  action: string;
  result?: string;
  timestamp: string;
  has_screenshot?: boolean;
  error?: string;
}

interface LiveStepExecutionLogProps {
  steps: StepEvent[];
  status: string;
  autoScroll?: boolean;
  onToggleAutoScroll?: () => void;
  onSetAutoScroll?: (enabled: boolean) => void;
  onViewScreenshot: (stepIdentifier?: string) => void;
  isFetchingScreenshot?: boolean;
}

type StepCategory = 'all' | 'actions' | 'errors' | 'session';

export function LiveStepExecutionLog({
  steps,
  status,
  autoScroll = true,
  onToggleAutoScroll,
  onSetAutoScroll,
  onViewScreenshot,
  isFetchingScreenshot = false,
}: LiveStepExecutionLogProps) {
  const [filterCategory, setFilterCategory] = useState<StepCategory>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedStepIds, setExpandedStepIds] = useState<Record<string, boolean>>({});
  const [copiedStepId, setCopiedStepId] = useState<string | null>(null);
  const [copiedAllLogs, setCopiedAllLogs] = useState(false);

  const logContainerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isProgrammaticScrollRef = useRef(false);

  // Compute status counts
  const counts = useMemo(() => {
    let actionsCount = 0;
    let errorsCount = 0;
    let sessionCount = 0;

    for (const step of steps) {
      const isFail =
        step.type === 'step_failed' ||
        Boolean(step.error) ||
        Boolean(step.result && step.result.toLowerCase().includes('failed'));
      const isSession =
        step.type === 'session_lost' ||
        step.type === 'session_reattached' ||
        step.action.includes('session');

      if (isFail) errorsCount++;
      if (isSession) sessionCount++;
      if (!isSession) actionsCount++;
    }

    return {
      all: steps.length,
      actions: actionsCount,
      errors: errorsCount,
      session: sessionCount,
    };
  }, [steps]);

  // Filtered steps
  const filteredSteps = useMemo(() => {
    return steps.filter((st, idx) => {
      const isFail =
        st.type === 'step_failed' ||
        Boolean(st.error) ||
        Boolean(st.result && st.result.toLowerCase().includes('failed'));
      const isSession =
        st.type === 'session_lost' ||
        st.type === 'session_reattached' ||
        st.action.includes('session');

      if (filterCategory === 'errors' && !isFail) return false;
      if (filterCategory === 'session' && !isSession) return false;
      if (filterCategory === 'actions' && isSession) return false;

      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const stepNum = (st.step_index ?? idx + 1).toString();
        const actionMatch = st.action.toLowerCase().includes(q);
        const resultMatch = (st.result || '').toLowerCase().includes(q);
        const errorMatch = (st.error || '').toLowerCase().includes(q);
        const stepMatch = stepNum.includes(q);
        return actionMatch || resultMatch || errorMatch || stepMatch;
      }

      return true;
    });
  }, [steps, filterCategory, searchQuery]);

  // Auto-scroll handler on step arrival, filter change, search change, expansion toggle, or autoScroll toggle
  useEffect(() => {
    if (!autoScroll) return;

    const rafId = requestAnimationFrame(() => {
      isProgrammaticScrollRef.current = true;
      if (bottomRef.current && typeof bottomRef.current.scrollIntoView === 'function') {
        bottomRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
      }
      if (logContainerRef.current) {
        logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
      }
      setTimeout(() => {
        isProgrammaticScrollRef.current = false;
      }, 200);
    });

    return () => cancelAnimationFrame(rafId);
  }, [steps, filteredSteps.length, filterCategory, searchQuery, expandedStepIds, autoScroll]);

  // Handle manual scroll in log container
  const handleScroll = () => {
    if (!logContainerRef.current || isProgrammaticScrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = logContainerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight <= 45;
    if (onSetAutoScroll && isAtBottom !== autoScroll) {
      onSetAutoScroll(isAtBottom);
    }
  };

  const handleToggleAutoScroll = () => {
    if (onToggleAutoScroll) {
      onToggleAutoScroll();
    } else if (onSetAutoScroll) {
      onSetAutoScroll(!autoScroll);
    }
    if (!autoScroll) {
      requestAnimationFrame(() => {
        isProgrammaticScrollRef.current = true;
        if (bottomRef.current) {
          bottomRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
        }
        if (logContainerRef.current) {
          logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
        }
        setTimeout(() => {
          isProgrammaticScrollRef.current = false;
        }, 200);
      });
    }
  };

  // Toggle single step expansion
  const toggleStep = (key: string) => {
    setExpandedStepIds((prev) => ({
      ...prev,
      [key]: prev[key] === undefined ? false : !prev[key],
    }));
  };

  // Check if all current steps are expanded
  const areAllExpanded = useMemo(() => {
    if (steps.length === 0) return true;
    return steps.every((st, idx) => {
      const key = st.step_id || `step-${idx}`;
      return expandedStepIds[key] !== false;
    });
  }, [steps, expandedStepIds]);

  const toggleAllSteps = () => {
    const nextState = !areAllExpanded;
    const updated: Record<string, boolean> = {};
    steps.forEach((st, idx) => {
      const key = st.step_id || `step-${idx}`;
      updated[key] = nextState;
    });
    setExpandedStepIds(updated);
  };

  // Copy single step output
  const handleCopyStep = (key: string, content: string) => {
    navigator.clipboard.writeText(content);
    setCopiedStepId(key);
    setTimeout(() => setCopiedStepId(null), 1800);
  };

  // Copy entire log as structured text
  const handleCopyAllLogs = () => {
    if (steps.length === 0) return;
    const formatted = steps
      .map((st, idx) => {
        const num = st.step_index ?? idx + 1;
        const time = new Date(st.timestamp).toISOString();
        const output = st.result || st.error || 'Success';
        return `[Step #${num}] [${time}] [${st.action}] (Type: ${st.type})\n${output}\n`;
      })
      .join('\n---\n\n');

    navigator.clipboard.writeText(formatted);
    setCopiedAllLogs(true);
    setTimeout(() => setCopiedAllLogs(false), 2000);
  };

  // Helper for JSON detection & pretty printing
  const parseJsonSafe = (raw?: string) => {
    if (!raw) return null;
    const trimmed = raw.trim();
    if (
      (trimmed.startsWith('{') && trimmed.endsWith('}')) ||
      (trimmed.startsWith('[') && trimmed.endsWith(']'))
    ) {
      try {
        const parsed = JSON.parse(trimmed);
        return JSON.stringify(parsed, null, 2);
      } catch {
        return null;
      }
    }
    return null;
  };

  // Helper for action icon
  const renderActionIcon = (actionName: string, isFail: boolean, isSession: boolean) => {
    const act = actionName.toLowerCase();

    if (isFail) {
      return (
        <svg className="h-4 w-4 text-rose-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
      );
    }

    if (isSession) {
      return (
        <svg className="h-4 w-4 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
        </svg>
      );
    }

    if (act.includes('navigat') || act.includes('open') || act.includes('url') || act.includes('goto')) {
      return (
        <svg className="h-4 w-4 text-sky-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
        </svg>
      );
    }

    if (act.includes('click') || act.includes('press') || act.includes('select') || act.includes('submit')) {
      return (
        <svg className="h-4 w-4 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" />
        </svg>
      );
    }

    if (act.includes('type') || act.includes('fill') || act.includes('input') || act.includes('write')) {
      return (
        <svg className="h-4 w-4 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
        </svg>
      );
    }

    if (act.includes('evaluat') || act.includes('verif') || act.includes('check') || act.includes('extract') || act.includes('audit')) {
      return (
        <svg className="h-4 w-4 text-teal-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
        </svg>
      );
    }

    if (act.includes('wait') || act.includes('sleep') || act.includes('delay') || act.includes('pause')) {
      return (
        <svg className="h-4 w-4 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      );
    }

    return (
      <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
        <path strokeLinecap="round" strokeLinejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
      </svg>
    );
  };

  return (
    <div className="rounded-2xl border border-zinc-200/90 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.02),0_6px_16px_rgba(0,0,0,0.03)] overflow-hidden transition-all">
      {/* Log Header Section */}
      <div className="p-5 sm:p-6 border-b border-zinc-200/80 bg-gradient-to-b from-white via-white to-zinc-50/50">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-start sm:items-center gap-3">
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-zinc-950 text-white shadow-xs border border-zinc-800">
              <svg className="h-5 w-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2.5">
                <h3 className="text-base font-bold tracking-tight text-zinc-900">
                  Live Step Execution Log
                </h3>

                {status === 'running' && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 border border-emerald-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-emerald-800 shadow-2xs">
                    <span className="relative flex h-2 w-2">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                      <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                    </span>
                    Live Streaming
                  </span>
                )}

                {status === 'paused' && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 border border-amber-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-amber-800 shadow-2xs">
                    <span className="h-1.5 w-1.5 rounded-full bg-amber-500"></span>
                    Paused
                  </span>
                )}

                {status === 'awaiting_approval' && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-purple-50 border border-purple-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-purple-800 shadow-2xs">
                    <span className="h-1.5 w-1.5 rounded-full bg-purple-500 animate-pulse"></span>
                    Awaiting Approval
                  </span>
                )}

                {(status === 'done' || status === 'completed') && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 border border-emerald-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-emerald-800 shadow-2xs">
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-600"></span>
                    Execution Done
                  </span>
                )}

                {status === 'failed' && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-rose-50 border border-rose-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-rose-800 shadow-2xs">
                    <span className="h-1.5 w-1.5 rounded-full bg-rose-600"></span>
                    Failed
                  </span>
                )}

                {status === 'stalled' && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-orange-50 border border-orange-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-orange-800 shadow-2xs">
                    <span className="h-1.5 w-1.5 rounded-full bg-orange-600"></span>
                    Stalled
                  </span>
                )}

                {status === 'session_lost' && (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 border border-amber-300/80 px-2.5 py-0.5 text-[10px] font-bold tracking-wide uppercase text-amber-900 shadow-2xs">
                    <span className="h-1.5 w-1.5 rounded-full bg-amber-600 animate-pulse"></span>
                    Session Lost
                  </span>
                )}
              </div>
              <p className="text-xs text-zinc-500 mt-0.5 font-sans">
                Real-time SSE events streaming agent observations, actions, and results.
              </p>
            </div>
          </div>

          {/* Action Toolbar */}
          <div className="flex flex-wrap items-center gap-2">
            {/* Step Counter Badge */}
            <span className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-700 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-3 py-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.02)]">
              <span
                className={`h-2 w-2 rounded-full ${
                  status === 'running'
                    ? 'bg-emerald-500 animate-pulse shadow-[0_0_6px_rgba(16,185,129,0.7)]'
                    : status === 'done' || status === 'completed'
                    ? 'bg-emerald-500'
                    : status === 'failed'
                    ? 'bg-rose-500'
                    : status === 'stalled'
                    ? 'bg-orange-500'
                    : 'bg-zinc-400'
                }`}
              />
              {steps.length} {steps.length === 1 ? 'step' : 'steps'} logged
            </span>

            {/* Auto-scroll toggle */}
            <button
              type="button"
              onClick={handleToggleAutoScroll}
              title={autoScroll ? 'Auto-scroll is active' : 'Click to resume auto-scrolling'}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold transition-all active:translate-y-[0.5px] ${
                autoScroll
                  ? 'border-t-emerald-200 border-x-emerald-300 border-b-emerald-400 bg-gradient-to-b from-emerald-50 to-emerald-100/70 text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)]'
                  : 'border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 text-zinc-600 hover:bg-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)]'
              }`}
            >
              <svg className="h-3.5 w-3.5 text-current" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
              </svg>
              <span>{autoScroll ? 'Auto-scroll ON' : 'Resume Auto-scroll'}</span>
            </button>

            {/* Expand / Collapse All */}
            {steps.length > 0 && (
              <button
                type="button"
                onClick={toggleAllSteps}
                className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-1.5 text-xs font-semibold text-zinc-700 hover:bg-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)] active:translate-y-[0.5px]"
              >
                <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  {areAllExpanded ? (
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 8h16M4 16h16" />
                  ) : (
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16m-7 6h7" />
                  )}
                </svg>
                <span>{areAllExpanded ? 'Collapse All' : 'Expand All'}</span>
              </button>
            )}

            {/* Copy Logs Button */}
            {steps.length > 0 && (
              <button
                type="button"
                onClick={handleCopyAllLogs}
                className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-1.5 text-xs font-semibold text-zinc-700 hover:bg-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.02)] active:translate-y-[0.5px]"
                title="Copy all steps to clipboard"
              >
                <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
                <span>{copiedAllLogs ? 'Copied All!' : 'Copy Logs'}</span>
              </button>
            )}
          </div>
        </div>

        {/* Filter and Search Bar */}
        {steps.length > 0 && (
          <div className="mt-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-3 border-t border-zinc-100">
            {/* Category Filter Pills */}
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                onClick={() => setFilterCategory('all')}
                className={`rounded-lg px-2.5 py-1 text-xs font-semibold transition-all ${
                  filterCategory === 'all'
                    ? 'bg-zinc-900 text-white shadow-xs'
                    : 'bg-zinc-100 text-zinc-600 hover:bg-zinc-200/70'
                }`}
              >
                All ({counts.all})
              </button>
              <button
                type="button"
                onClick={() => setFilterCategory('actions')}
                className={`rounded-lg px-2.5 py-1 text-xs font-semibold transition-all ${
                  filterCategory === 'actions'
                    ? 'bg-zinc-900 text-white shadow-xs'
                    : 'bg-zinc-100 text-zinc-600 hover:bg-zinc-200/70'
                }`}
              >
                Actions ({counts.actions})
              </button>
              {counts.errors > 0 && (
                <button
                  type="button"
                  onClick={() => setFilterCategory('errors')}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-semibold transition-all ${
                    filterCategory === 'errors'
                      ? 'bg-rose-600 text-white shadow-xs'
                      : 'bg-rose-50 text-rose-700 border border-rose-200/60 hover:bg-rose-100'
                  }`}
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-rose-500"></span>
                  Errors ({counts.errors})
                </button>
              )}
              {counts.session > 0 && (
                <button
                  type="button"
                  onClick={() => setFilterCategory('session')}
                  className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-semibold transition-all ${
                    filterCategory === 'session'
                      ? 'bg-amber-600 text-white shadow-xs'
                      : 'bg-amber-50 text-amber-800 border border-amber-200/60 hover:bg-amber-100'
                  }`}
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500"></span>
                  Session ({counts.session})
                </button>
              )}
            </div>

            {/* Search Input */}
            <div className="relative w-full sm:w-64">
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Filter steps by action or text..."
                className="w-full rounded-lg border border-zinc-200 bg-white py-1.5 pl-8 pr-7 text-xs text-zinc-800 placeholder-zinc-400 shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] focus:border-zinc-500 focus:outline-none focus:ring-1 focus:ring-zinc-400 font-sans"
              />
              <svg className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery('')}
                  className="absolute right-2 top-1.5 rounded p-0.5 text-zinc-400 hover:text-zinc-600"
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
                  </svg>
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Log Step Stream Container */}
      <div
        ref={logContainerRef}
        onScroll={handleScroll}
        className="max-h-[520px] min-h-[240px] overflow-y-auto p-4 sm:p-6 space-y-3 font-mono text-xs bg-zinc-50/30"
      >
        {steps.length === 0 ? (
          <div className="flex h-56 flex-col items-center justify-center text-center text-zinc-400 border border-dashed border-zinc-200/90 rounded-2xl bg-white p-8 shadow-2xs">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-zinc-200 bg-zinc-50 mb-3 shadow-2xs">
              <svg className="h-6 w-6 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
            </div>
            <span className="font-semibold text-zinc-800 text-sm">No execution steps recorded yet</span>
            <span className="text-xs text-zinc-400 max-w-sm mt-1 font-sans">
              Enter a goal in the prompt above and dispatch the agent to stream live browser actions and telemetry.
            </span>
          </div>
        ) : filteredSteps.length === 0 ? (
          <div className="flex h-40 flex-col items-center justify-center text-center text-zinc-400 border border-dashed border-zinc-200/80 rounded-xl bg-white p-6">
            <span className="font-semibold text-zinc-700">No steps match &quot;{searchQuery || filterCategory}&quot;</span>
            <button
              type="button"
              onClick={() => {
                setFilterCategory('all');
                setSearchQuery('');
              }}
              className="mt-2 text-xs font-semibold text-zinc-800 underline underline-offset-2"
            >
              Reset Filters
            </button>
          </div>
        ) : (
          filteredSteps.map((st, idx) => {
            const stepKey = st.step_id || `step-${idx}`;
            const isExpanded = expandedStepIds[stepKey] !== false;

            const isFail =
              st.type === 'step_failed' ||
              Boolean(st.error) ||
              Boolean(st.result && st.result.toLowerCase().includes('failed'));
            const isSession =
              st.type === 'session_lost' ||
              st.type === 'session_reattached' ||
              st.action.includes('session');
            const isSessionLost = st.type === 'session_lost';
            const isSessionReattached = st.type === 'session_reattached';

            const rawContent = st.result || st.error || 'Success';
            const formattedJson = parseJsonSafe(rawContent);
            const stepNum = st.step_index ?? idx + 1;
            const timeFormatted = new Date(st.timestamp).toLocaleTimeString();

            // Status Styling
            let cardClasses = 'border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white text-zinc-800';
            let statusBadge = (
              <span className="inline-flex items-center gap-1 rounded-md bg-emerald-50 border border-emerald-200/70 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
                COMPLETED
              </span>
            );

            if (isSessionLost) {
              cardClasses = 'border-t-amber-200 border-x-amber-300 border-b-amber-400 bg-amber-50/50 text-amber-950';
              statusBadge = (
                <span className="inline-flex items-center gap-1 rounded-md bg-amber-100 border border-amber-300 px-2 py-0.5 text-[10px] font-bold text-amber-900">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse"></span>
                  SESSION LOST
                </span>
              );
            } else if (isSessionReattached) {
              cardClasses = 'border-t-sky-200 border-x-sky-300 border-b-sky-400 bg-sky-50/40 text-sky-950';
              statusBadge = (
                <span className="inline-flex items-center gap-1 rounded-md bg-sky-100 border border-sky-300 px-2 py-0.5 text-[10px] font-bold text-sky-800">
                  <span className="h-1.5 w-1.5 rounded-full bg-sky-500"></span>
                  REATTACHED
                </span>
              );
            } else if (isFail) {
              cardClasses = 'border-t-rose-200 border-x-rose-300 border-b-rose-400 bg-rose-50/40 text-rose-950';
              statusBadge = (
                <span className="inline-flex items-center gap-1 rounded-md bg-rose-100 border border-rose-300 px-2 py-0.5 text-[10px] font-bold text-rose-800">
                  <span className="h-1.5 w-1.5 rounded-full bg-rose-500"></span>
                  FAILED
                </span>
              );
            }

            return (
              <div
                key={stepKey}
                className={`rounded-xl border shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02)] transition-all ${cardClasses}`}
              >
                {/* Step Header Row */}
                <div
                  onClick={() => toggleStep(stepKey)}
                  className="flex cursor-pointer select-none items-center justify-between p-3.5 sm:px-4 hover:bg-zinc-50/60 transition-colors rounded-t-xl"
                  role="button"
                  tabIndex={0}
                  aria-expanded={isExpanded}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggleStep(stepKey);
                    }
                  }}
                >
                  <div className="flex items-center gap-2.5 min-w-0">
                    <span className="flex h-6 min-w-6 px-1.5 items-center justify-center rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-100 text-[11px] font-bold text-zinc-700 shadow-2xs">
                      #{stepNum}
                    </span>

                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-zinc-100/90 border border-zinc-200">
                      {renderActionIcon(st.action, isFail, isSession)}
                    </span>

                    <span className="font-bold text-zinc-900 truncate text-xs sm:text-[13px]">
                      {st.action}
                    </span>

                    {statusBadge}
                  </div>

                  <div className="flex items-center gap-2 sm:gap-3 shrink-0">
                    {st.has_screenshot && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onViewScreenshot(st.step_id || st.step_index?.toString() || (idx + 1).toString());
                        }}
                        disabled={isFetchingScreenshot}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-50 px-2.5 py-1 text-[11px] font-semibold text-zinc-700 hover:bg-zinc-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.8),0_1px_2px_rgba(0,0,0,0.03)] active:translate-y-[0.5px] disabled:opacity-50"
                        title="View browser screenshot for this step"
                      >
                        <svg className="h-3.5 w-3.5 text-zinc-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                        </svg>
                        <span className="hidden xs:inline">Screenshot</span>
                      </button>
                    )}

                    <span className="text-[11px] text-zinc-400 font-mono hidden sm:inline" title={st.timestamp}>
                      {timeFormatted}
                    </span>

                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleCopyStep(stepKey, rawContent);
                      }}
                      className="rounded-md p-1 text-zinc-400 hover:text-zinc-600 hover:bg-zinc-100 transition-colors"
                      title="Copy output to clipboard"
                    >
                      {copiedStepId === stepKey ? (
                        <svg className="h-3.5 w-3.5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                        </svg>
                      ) : (
                        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                        </svg>
                      )}
                    </button>

                    <span className="text-zinc-400 p-0.5">
                      <svg
                        className={`h-4 w-4 transition-transform duration-150 ${isExpanded ? 'rotate-180' : ''}`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth="2"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                      </svg>
                    </span>
                  </div>
                </div>

                {/* Collapsible Step Content */}
                {isExpanded && (
                  <div className="border-t border-zinc-200/70 p-3 sm:p-4 bg-zinc-50/40 rounded-b-xl space-y-2">
                    {/* Header meta */}
                    <div className="flex flex-wrap items-center justify-between text-[11px] text-zinc-500 font-sans pb-1">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-zinc-700 bg-zinc-100 px-1.5 py-0.5 rounded border border-zinc-200 text-[10px]">
                          type:{st.type}
                        </span>
                        {st.step_id && (
                          <span className="font-mono text-zinc-400 text-[10px]">
                            id:{st.step_id}
                          </span>
                        )}
                      </div>
                      <span className="font-mono text-zinc-400">
                        {new Date(st.timestamp).toISOString()}
                      </span>
                    </div>

                    {/* Output / Payload Container */}
                    {formattedJson ? (
                      <div className="rounded-lg border border-zinc-800 bg-zinc-950 text-zinc-100 p-3.5 overflow-x-auto shadow-[inset_0_1px_2px_rgba(0,0,0,0.5)]">
                        <div className="flex items-center justify-between pb-2 mb-2 border-b border-zinc-800 text-[10px] text-zinc-400 font-sans">
                          <span className="font-semibold uppercase tracking-wider text-emerald-400 flex items-center gap-1.5">
                            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
                            Structured JSON Payload
                          </span>
                          <span>{formattedJson.split('\n').length} lines</span>
                        </div>
                        <pre className="font-mono text-[11px] leading-relaxed text-emerald-300/90 whitespace-pre">
                          {formattedJson}
                        </pre>
                      </div>
                    ) : (
                      <div
                        className={`rounded-lg border p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap shadow-[inset_0_1px_2px_rgba(0,0,0,0.02)] ${
                          isFail
                            ? 'bg-rose-50/70 border-rose-200 text-rose-900'
                            : isSession
                            ? 'bg-amber-50/70 border-amber-200 text-amber-900'
                            : 'bg-white border-zinc-200/90 text-zinc-800'
                        }`}
                      >
                        {rawContent}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
        <div ref={bottomRef} className="h-px w-full shrink-0" aria-hidden="true" />
      </div>
    </div>
  );
}

'use client';

import React, { useState, useEffect, useRef, useTransition } from 'react';
import { StatusBadge } from '../components/StatusBadge';

/**
 * Every agent request goes through this origin's own proxy instead of the
 * backend's absolute URL.
 *
 * `EventSource` cannot attach a session cookie to a cross-origin request, so a
 * stream opened against the backend host directly would always come back
 * unauthenticated. Routing through `/api/agent` keeps the browser same-origin,
 * which is what makes both the cookie and the stream work.
 */
const AGENT_API_BASE = '/api/agent';

/**
 * Builds the message for a request that failed below the HTTP layer, where no
 * response was ever produced.
 *
 * It deliberately does not claim the server never saw the request: a rejected
 * fetch cannot distinguish "never arrived" from "arrived and was refused", and
 * `POST /agent/run` returns 202 + run id immediately while the agent loop
 * continues in the background. Asserting no run started would invite a duplicate
 * submission of side-effecting work, so the operator is told to check the run
 * status instead.
 */
const agentProxyUnreachableMessage = (): string =>
  `Cannot read a response from the agent proxy at ${AGENT_API_BASE} on this origin. ` +
  'The request may have reached the backend, so the run status may be unknown: ' +
  'check it before retrying. This origin, its proxy, or the backend behind it may be down.';

/**
 * Interface representing a recorded agent step event.
 */
interface StepEvent {
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

/**
 * Interface representing approval gate details.
 */
interface ApprovalData {
  run_id: string;
  status: string;
  invoice_id?: string;
  vendor?: string;
  amount?: number;
  po_number?: string;
  threshold?: number;
  nonce?: string;
}

/**
 * Interface representing an individual invoice verification record.
 */
interface VerificationRow {
  invoice_id: string;
  vendor: string;
  amount: number;
  po_number?: string;
  expected_status: string;
  actual_status: string;
  pass_fail: boolean;
  reason: string;
  classification: string;
}

/**
 * Interface representing an incomplete or flagged invoice item.
 */
interface IncompleteItem {
  invoice_id: string;
  vendor: string;
  amount: number;
  po_number?: string;
  status: string;
  reason: string;
}

/**
 * Interface representing failed step execution evidence with screenshot.
 */
interface FailedStepEvidence {
  step_id: string;
  action: string;
  result?: string;
  screenshot_b64?: string;
  timestamp: string;
}

/**
 * Interface representing the comprehensive verification report for an agent run.
 */
interface VerificationReport {
  run_id: string;
  total_invoices: number;
  pass_count: number;
  fail_count: number;
  incomplete_count: number;
  verification_table: VerificationRow[];
  incomplete_items: IncompleteItem[];
  failed_steps: FailedStepEvidence[];
}

const DEFAULT_GOALS = [
  "Process all pending invoices",
  "Process only invoices from Vendor Acme",
  "Hold anything over ₹25,000 for approval"
];

/**
 * Marks the case where no HTTP response was ever produced.
 *
 * fetch() rejects with a TypeError for connection refusal, DNS failure, TLS
 * failure and CORS rejection, but a response body stream that dies mid-transfer
 * also rejects with a TypeError. The thrown value's class alone therefore cannot
 * distinguish "backend unreachable" from "backend answered, body unreadable", so
 * the distinction is made by where the rejection happened instead.
 */
class BackendUnreachableError extends Error {}

/**
 * Reads the JSON body of a response that was successfully received.
 *
 * @param res - A response with an ok status.
 * @returns The parsed body.
 */
const readJsonBody = async <T,>(res: Response): Promise<T> => {
  try {
    return (await res.json()) as T;
  } catch {
    throw new Error(
      `Backend returned a response that could not be read as JSON (HTTP ${res.status}).`
    );
  }
};

/**
 * AgentControlPage component provides the interactive UI for dispatching browser agent runs,
 * streaming real-time execution steps, managing pause/resume/approval states, and viewing screenshots.
 */
export default function AgentControlPage() {
  const [goal, setGoal] = useState("Process all pending invoices");
  const [runId, setRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("idle");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [isStarting, setIsStarting] = useState(false);
  const [isPausingOrResuming, setIsPausingOrResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Approval Modal State
  const [approvalData, setApprovalData] = useState<ApprovalData | null>(null);
  const [isSubmittingApproval, setIsSubmittingApproval] = useState(false);
  const submittedNonceRef = useRef<string | null>(null);

  // Screenshot Modal State
  const [selectedScreenshot, setSelectedScreenshot] = useState<string | null>(null);
  const [isFetchingScreenshot, setIsFetchingScreenshot] = useState(false);

  // Auto-scroll log ref
  const logContainerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  /**
   * Handles manual scroll container interaction to pause or resume auto-scrolling of step logs.
   */
  const handleScroll = () => {
    if (!logContainerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = logContainerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 40;
    setAutoScroll(isAtBottom);
  };

  // Auto-scroll effect
  useEffect(() => {
    if (autoScroll && logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [steps, autoScroll]);

  // Verification Report State (Phase 4)
  const [verificationReport, setVerificationReport] = useState<VerificationReport | null>(null);
  const [isFetchingVerification, setIsFetchingVerification] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(null);

  /**
   * Fetches the comprehensive verification report for the active agent run from the FastAPI backend.
   */
  const handleFetchVerification = async () => {
    if (!runId) return;
    setIsFetchingVerification(true);
    setVerificationError(null);
    try {
      let res: Response;
      try {
        res = await fetch(
          `${AGENT_API_BASE}/runs/${encodeURIComponent(runId)}/verification`,
          { credentials: 'include' }
        );
      } catch {
        throw new BackendUnreachableError();
      }
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Failed to fetch verification report (${res.status})`);
      }
      const data = await readJsonBody<VerificationReport>(res);
      setVerificationReport(data);
    } catch (err: unknown) {
      setVerificationError(
        err instanceof BackendUnreachableError
          ? agentProxyUnreachableMessage()
          : err instanceof Error
            ? err.message
            : 'Failed to fetch verification report'
      );
    } finally {
      setIsFetchingVerification(false);
    }
  };

  // Automatically fetch verification report when run completes or fails
  useEffect(() => {
    if (runId && (status === 'done' || status === 'failed')) {
      handleFetchVerification();
    }
  }, [runId, status]);

  /**
   * Dispatches a new agent run with the specified goal instruction.
   * Prevents starting while running, paused, or awaiting approval.
   */
  const handleRunAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!goal.trim() || isStarting || status === 'running' || status === 'paused' || status === 'awaiting_approval') return;

    setIsStarting(true);
    setError(null);
    setSteps([]);
    setStatus("running");
    submittedNonceRef.current = null;

    try {
      let res: Response;
      try {
        res = await fetch(`${AGENT_API_BASE}/run`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ goal: goal.trim() }),
        });
      } catch {
        throw new BackendUnreachableError();
      }

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Failed to start agent run (${res.status})`);
      }

      const data = await readJsonBody<{ run_id: string; status?: string }>(res);
      setRunId(data.run_id);
      setStatus(data.status || "running");
    } catch (err: unknown) {
      // Only a rejection from fetch() itself means no response was produced.
      // A body that arrived but could not be parsed is a different fault and
      // gets its own message, so "backend unreachable" stays trustworthy.
      setError(
        err instanceof BackendUnreachableError
          ? agentProxyUnreachableMessage()
          : err instanceof Error
            ? err.message
            : 'Failed to start agent run'
      );
      setStatus("failed");
    } finally {
      setIsStarting(false);
    }
  };

  // If we have a runId, connect to SSE stream and poll approval status
  useEffect(() => {
    if (!runId) return;

    let eventSource: EventSource | null = null;
    let pollInterval: NodeJS.Timeout | null = null;
    const abortController = new AbortController();

    try {
      // Same-origin, so the session cookie rides along automatically and the
      // stream needs no CORS preflight to stay open.
      eventSource = new EventSource(`${AGENT_API_BASE}/runs/${encodeURIComponent(runId)}/stream`);

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'status_change') {
            setStatus(data.status);
          } else if (data.type === 'step_complete' || data.type === 'step_failed') {
            setSteps((prev) => [...prev, data]);
          } else if (data.type === 'done') {
            setStatus('done');
          }
        } catch (e) {
          console.error('Failed to parse SSE message', e);
        }
      };

      eventSource.addEventListener('status_change', (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data);
          if (data.status) setStatus(data.status);
        } catch {}
      });

      eventSource.addEventListener('step_complete', (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data);
          setSteps((prev) => [...prev, data]);
        } catch {}
      });

      eventSource.addEventListener('step_failed', (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data);
          setSteps((prev) => [...prev, data]);
        } catch {}
      });

      eventSource.addEventListener('done', (event: MessageEvent) => {
        setStatus('done');
      });

      eventSource.addEventListener('needs_approval', (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data);
          if (data.nonce && data.nonce === submittedNonceRef.current) {
            return;
          }
          setStatus('awaiting_approval');
          setApprovalData(data);
        } catch {}
      });

      eventSource.onerror = (err) => {
        console.warn('SSE connection error or closed', err);
      };
    } catch (e) {
      console.error('Failed to establish EventSource connection', e);
    }

    // Poll approval endpoint periodically while run is active or awaiting approval
    pollInterval = setInterval(async () => {
      try {
        const res = await fetch(
          `${AGENT_API_BASE}/runs/${encodeURIComponent(runId)}/approval`,
          {
            signal: abortController.signal,
            credentials: 'include',
          }
        );
        if (res.ok) {
          const data = await res.json();
          if (data.pending && data.approval_data) {
            const nonce = data.approval_data.nonce;
            if (nonce && nonce === submittedNonceRef.current) {
              return;
            }
            setStatus('awaiting_approval');
            setApprovalData(data.approval_data);
          }
        }
      } catch (e) {
        if (e instanceof Error && e.name !== 'AbortError') {
          // ignore
        }
      }
    }, 2500);

    return () => {
      abortController.abort();
      if (eventSource) eventSource.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [runId]);

  /**
   * Toggles the pause or resume state of the active agent run.
   */
  const handleTogglePause = async () => {
    if (!runId || isPausingOrResuming) return;
    setIsPausingOrResuming(true);
    const isCurrentlyPaused = status === 'paused';
    const endpoint = isCurrentlyPaused ? 'resume' : 'pause';

    try {
      const res = await fetch(
        `${AGENT_API_BASE}/runs/${encodeURIComponent(runId)}/${endpoint}`,
        {
          method: 'POST',
          credentials: 'include',
        }
      );
      if (!res.ok) throw new Error(`Failed to ${endpoint} run`);
      setStatus(isCurrentlyPaused ? 'running' : 'paused');
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : `Failed to ${endpoint} agent run`);
    } finally {
      setIsPausingOrResuming(false);
    }
  };

  /**
   * Submits human approval decision ('approved' or 'rejected') for the pending approval gate.
   * @param decision - The approval decision to submit.
   */
  const handleApprovalDecision = async (decision: 'approved' | 'rejected') => {
    if (!runId || isSubmittingApproval || !approvalData?.nonce) return;
    setIsSubmittingApproval(true);
    submittedNonceRef.current = approvalData.nonce;

    try {
      const res = await fetch(
        `${AGENT_API_BASE}/runs/${encodeURIComponent(runId)}/approval`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            decision,
            nonce: approvalData.nonce,
          }),
        }
      );

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Failed to submit decision: ${decision}`);
      }

      setApprovalData(null);
      setStatus('running');
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : 'Failed to submit approval decision');
    } finally {
      setIsSubmittingApproval(false);
    }
  };

  /**
   * Fetches and displays the screenshot associated with a specific step ID or index.
   * @param stepIdentifier - The unique step ID or step index identifier.
   */
  const handleViewScreenshot = async (stepIdentifier?: string) => {
    if (!stepIdentifier) return;
    setIsFetchingScreenshot(true);
    try {
      const res = await fetch(
        `${AGENT_API_BASE}/steps/${encodeURIComponent(stepIdentifier)}`,
        { credentials: 'include' }
      );
      if (!res.ok) throw new Error('Failed to load screenshot');
      const data = await res.json();
      if (data.screenshot_b64) {
        setSelectedScreenshot(data.screenshot_b64);
      } else {
        alert('No screenshot captured for this step.');
      }
    } catch {
      alert('Failed to retrieve step screenshot.');
    } finally {
      setIsFetchingScreenshot(false);
    }
  };

  return (
    <div className="space-y-6 pb-12">
      {/* Page Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-50">
              Agent Control & Live Monitor
            </h1>
            <StatusBadge status={status} />
          </div>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            Dispatch autonomous browser agents to process accounts payable, evaluate purchase orders, and manage approvals.
          </p>
        </div>

        {runId && (
          <div className="flex items-center gap-2">
            <button
              onClick={handleTogglePause}
              disabled={isPausingOrResuming || status === 'done' || status === 'failed'}
              className={`inline-flex items-center gap-2 rounded-lg border px-3.5 py-2 text-xs font-semibold shadow-xs transition-all active:translate-y-[0.5px] disabled:opacity-50 ${
                status === 'paused'
                  ? 'border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'
                  : 'border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-300'
              }`}
            >
              {status === 'paused' ? (
                <>
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Resume Agent</span>
                </>
              ) : (
                <>
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>Pause Agent</span>
                </>
              )}
            </button>
            <span className="font-mono text-xs text-zinc-400 dark:text-zinc-500">
              Run: {runId.slice(0, 8)}...
            </span>
          </div>
        )}
      </div>

      {/* Goal Dispatch Section */}
      <div className="rounded-2xl border border-zinc-200/80 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02)] backdrop-blur-xs dark:border-zinc-800 dark:bg-zinc-900/60">
        <form onSubmit={handleRunAgent} className="space-y-4">
          <div>
            <label htmlFor="goal-input" className="block text-xs font-semibold uppercase tracking-wider text-zinc-600 dark:text-zinc-400">
              Agent Goal / Instruction
            </label>
            <div className="mt-2">
              <textarea
                id="goal-input"
                rows={3}
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="Enter plain English goal for the browser agent..."
                className="block w-full rounded-xl border border-zinc-200 bg-white p-3.5 text-sm text-zinc-900 placeholder-zinc-400 shadow-xs transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-zinc-700/80 dark:bg-zinc-950/70 dark:text-zinc-100 dark:placeholder-zinc-500 dark:focus:border-zinc-400 dark:focus:ring-zinc-400"
              />
            </div>
          </div>

          {/* Preset Goal Suggestion Buttons */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">Suggestions:</span>
            {DEFAULT_GOALS.map((suggestion, idx) => (
              <button
                key={idx}
                type="button"
                onClick={() => setGoal(suggestion)}
                className="rounded-lg border border-zinc-200/80 bg-zinc-50 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:border-zinc-300 hover:bg-zinc-100 dark:border-zinc-800 dark:bg-zinc-800/60 dark:text-zinc-300 dark:hover:border-zinc-700 dark:hover:bg-zinc-800"
              >
                {suggestion}
              </button>
            ))}
          </div>

          <div className="flex items-center justify-between pt-2">
            <div className="text-xs text-zinc-500 dark:text-zinc-400">
              Agent operates mock ERP via remote browser CDP.
            </div>

            <button
              type="submit"
              disabled={isStarting || status === 'running' || status === 'paused' || status === 'awaiting_approval'}
              className="inline-flex items-center justify-center gap-2 rounded-xl border-t border-t-zinc-700 border-x border-x-zinc-800 border-b border-b-zinc-950 bg-gradient-to-b from-zinc-800 to-zinc-900 px-5 py-2.5 text-sm font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_2px_4px_rgba(0,0,0,0.2)] transition-all hover:from-zinc-750 hover:to-zinc-850 active:translate-y-[0.5px] disabled:opacity-50 dark:border-t-white dark:border-x-zinc-200 dark:border-b-zinc-400 dark:from-zinc-100 dark:to-zinc-200 dark:text-zinc-900 dark:shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_2px_4px_rgba(0,0,0,0.1)]"
            >
              {isStarting ? (
                <>
                  <svg className="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
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
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-4 text-xs text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200">
            {error}
          </div>
        )}
      </div>

      {/* Real-Time Step Log via SSE */}
      <div className="rounded-2xl border border-zinc-200/80 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02)] backdrop-blur-xs dark:border-zinc-800 dark:bg-zinc-900/60">
        <div className="flex items-center justify-between pb-4 border-b border-zinc-200/80 dark:border-zinc-800">
          <div>
            <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              Live Step Execution Log
            </h3>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Real-time SSE events streaming agent observations, actions, and results.
            </p>
          </div>

          <div className="flex items-center gap-3">
            {!autoScroll && (
              <button
                onClick={() => setAutoScroll(true)}
                className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
              >
                Resume Auto-scroll
              </button>
            )}
            <span className="inline-flex items-center gap-1.5 font-mono text-xs text-zinc-500 dark:text-zinc-400">
              <span className={`h-2 w-2 rounded-full ${status === 'running' ? 'bg-emerald-500 animate-pulse' : 'bg-zinc-400'}`} />
              {steps.length} {steps.length === 1 ? 'step' : 'steps'} logged
            </span>
          </div>
        </div>

        <div
          ref={logContainerRef}
          onScroll={handleScroll}
          className="mt-4 max-h-[420px] min-h-[200px] overflow-y-auto space-y-3 font-mono text-xs pr-2"
        >
          {steps.length === 0 ? (
            <div className="flex h-48 flex-col items-center justify-center text-center text-zinc-400 dark:text-zinc-600">
              <svg className="h-8 w-8 mb-2 opacity-40" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
              </svg>
              <span>No execution steps recorded yet. Start an agent run above.</span>
            </div>
          ) : (
            steps.map((st, idx) => {
              const isFail = st.type === 'step_failed' || (st.result && st.result.toLowerCase().includes('failed'));
              return (
                <div
                  key={idx}
                  className={`rounded-xl border p-4 transition-all ${
                    isFail
                      ? 'border-red-200 bg-red-50/70 text-red-900 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-200'
                      : 'border-zinc-200/80 bg-zinc-50/70 text-zinc-800 dark:border-zinc-800 dark:bg-zinc-950/50 dark:text-zinc-200'
                  }`}
                >
                  <div className="flex items-center justify-between pb-2 border-b border-zinc-200/60 dark:border-zinc-800/80 text-[11px]">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-zinc-500 dark:text-zinc-400">
                        #{st.step_index || idx + 1}
                      </span>
                      <span className="font-semibold text-zinc-900 dark:text-zinc-100">
                        {st.action}
                      </span>
                    </div>
                    <div className="flex items-center gap-3">
                      {st.has_screenshot && (
                        <button
                          onClick={() => handleViewScreenshot(st.step_id || st.step_index?.toString() || (idx + 1).toString())}
                          disabled={isFetchingScreenshot}
                          className="inline-flex items-center gap-1 rounded bg-zinc-200/80 px-2 py-0.5 text-[10px] font-medium text-zinc-700 hover:bg-zinc-300 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700"
                        >
                          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                            <path strokeLinecap="round" strokeLinejoin="round" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                          </svg>
                          <span>Screenshot</span>
                        </button>
                      )}
                      <span className="text-zinc-400 dark:text-zinc-500">
                        {new Date(st.timestamp).toLocaleTimeString()}
                      </span>
                    </div>
                  </div>

                  <div className="mt-2.5 whitespace-pre-wrap text-xs font-mono leading-relaxed">
                    {st.result || st.error || 'Success'}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Verification Report Section (Phase 4) */}
      {(verificationReport || isFetchingVerification || verificationError || (runId && (status === 'done' || status === 'failed'))) && (
        <div className="rounded-2xl border border-zinc-200/80 bg-white p-6 shadow-[0_1px_3px_rgba(0,0,0,0.02)] backdrop-blur-xs dark:border-zinc-800 dark:bg-zinc-900/60 space-y-6">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between pb-4 border-b border-zinc-200/80 dark:border-zinc-800">
            <div>
              <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
                Phase 4: Verification & Evidence Report
              </h3>
              <p className="text-xs text-zinc-500 dark:text-zinc-400">
                Automated post-run comparison of actual ERP invoice states against expected seed rules.
              </p>
            </div>
            <button
              onClick={handleFetchVerification}
              disabled={isFetchingVerification || !runId}
              className="inline-flex items-center gap-2 rounded-xl border border-zinc-200 bg-zinc-50 px-3.5 py-2 text-xs font-semibold text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300 disabled:opacity-50"
            >
              {isFetchingVerification ? (
                <>
                  <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                  <span>Refreshing Verification...</span>
                </>
              ) : (
                <>
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
                  </svg>
                  <span>Refresh Report</span>
                </>
              )}
            </button>
          </div>

          {verificationError && (
            <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-xs text-red-800 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200">
              {verificationError}
            </div>
          )}

          {verificationReport ? (
            <>
              {/* Summary Metrics Cards */}
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div className="rounded-xl border border-zinc-200/80 bg-zinc-50/50 p-4 dark:border-zinc-800 dark:bg-zinc-950/40">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">Total Invoices</div>
                  <div className="mt-1 text-2xl font-bold text-zinc-900 dark:text-zinc-100">{verificationReport.total_invoices}</div>
                </div>
                <div className="rounded-xl border border-emerald-200/80 bg-emerald-50/50 p-4 dark:border-emerald-900/50 dark:bg-emerald-950/30">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-emerald-700 dark:text-emerald-400">Passed</div>
                  <div className="mt-1 text-2xl font-bold text-emerald-800 dark:text-emerald-300">{verificationReport.pass_count}</div>
                </div>
                <div className="rounded-xl border border-rose-200/80 bg-rose-50/50 p-4 dark:border-rose-900/50 dark:bg-rose-950/30">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-rose-700 dark:text-rose-400">Failed / Mismatched</div>
                  <div className="mt-1 text-2xl font-bold text-rose-800 dark:text-rose-300">{verificationReport.fail_count}</div>
                </div>
                <div className="rounded-xl border border-amber-200/80 bg-amber-50/50 p-4 dark:border-amber-900/50 dark:bg-amber-950/30">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-amber-700 dark:text-amber-400">Incomplete / Flagged</div>
                  <div className="mt-1 text-2xl font-bold text-amber-800 dark:text-amber-300">{verificationReport.incomplete_count}</div>
                </div>
              </div>

              {/* Incomplete / Flagged Items Callout */}
              {verificationReport.incomplete_items.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-4 dark:border-amber-900/50 dark:bg-amber-950/30">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-amber-900 dark:text-amber-300 mb-2">
                    Incomplete / Flagged Items Requiring Attention ({verificationReport.incomplete_items.length})
                  </h4>
                  <div className="space-y-2">
                    {verificationReport.incomplete_items.map((item, idx) => (
                      <div key={idx} className="flex flex-col sm:flex-row sm:items-center justify-between text-xs gap-1 border-t border-amber-200/60 dark:border-amber-900/40 pt-2 font-mono">
                        <div>
                          <span className="font-bold text-zinc-900 dark:text-zinc-100">{item.vendor}</span>
                          <span className="text-zinc-500 ml-2">({item.po_number || 'No PO'})</span>
                          <span className="ml-2 font-semibold text-emerald-700 dark:text-emerald-400">₹{item.amount.toLocaleString()}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="rounded bg-amber-200/70 px-2 py-0.5 text-[10px] font-semibold text-amber-900 dark:bg-amber-900 dark:text-amber-200 uppercase">
                            {item.status}
                          </span>
                          <span className="text-zinc-600 dark:text-zinc-400">{item.reason}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Verification Table */}
              <div className="overflow-x-auto rounded-xl border border-zinc-200 dark:border-zinc-800">
                <table className="w-full text-left text-xs font-mono">
                  <thead className="border-b border-zinc-200 bg-zinc-50 text-zinc-700 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300">
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
                  <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800 bg-white dark:bg-zinc-900">
                    {verificationReport.verification_table.map((row, idx) => (
                      <tr key={idx} className="hover:bg-zinc-50/50 dark:hover:bg-zinc-800/40">
                        <td className="p-3">
                          <div className="font-semibold text-zinc-900 dark:text-zinc-100">{row.vendor}</div>
                          <div className="text-[10px] text-zinc-400">{row.invoice_id.slice(0, 8)}...</div>
                        </td>
                        <td className="p-3 font-semibold text-zinc-900 dark:text-zinc-100">
                          ₹{row.amount.toLocaleString()}
                        </td>
                        <td className="p-3 text-zinc-700 dark:text-zinc-300">
                          {row.po_number || <span className="text-zinc-400 italic">None</span>}
                        </td>
                        <td className="p-3">
                          <span className="rounded bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200">
                            {row.expected_status}
                          </span>
                        </td>
                        <td className="p-3">
                          <span className={`rounded px-2 py-0.5 text-[10px] font-medium ${
                            row.actual_status === 'completed'
                              ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
                              : row.actual_status === 'flagged'
                              ? 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300'
                              : 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300'
                          }`}>
                            {row.actual_status}
                          </span>
                        </td>
                        <td className="p-3">
                          {row.pass_fail ? (
                            <span className="inline-flex items-center gap-1 font-bold text-emerald-600 dark:text-emerald-400">
                              ✅ Pass
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 font-bold text-rose-600 dark:text-rose-400">
                              ❌ Fail
                            </span>
                          )}
                        </td>
                        <td className="p-3 text-zinc-600 dark:text-zinc-400">
                          <div>{row.reason}</div>
                          <div className="text-[10px] text-zinc-400 mt-0.5 font-sans">Class: {row.classification}</div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Failed Step Screenshots Inline Evidence */}
              {verificationReport.failed_steps.length > 0 && (
                <div className="space-y-3 pt-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-zinc-900 dark:text-zinc-100">
                    Failed Step Screenshots & Evidence ({verificationReport.failed_steps.length})
                  </h4>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    {verificationReport.failed_steps.map((step, idx) => (
                      <div key={idx} className="rounded-xl border border-red-200 bg-red-50/50 p-4 space-y-3 dark:border-red-900/50 dark:bg-red-950/20">
                        <div className="flex items-center justify-between text-xs font-mono">
                          <span className="font-bold text-red-900 dark:text-red-200">{step.action}</span>
                          <span className="text-zinc-400">{new Date(step.timestamp).toLocaleTimeString()}</span>
                        </div>
                        <div className="text-xs font-mono text-red-800 dark:text-red-300 bg-white/50 dark:bg-black/30 p-2 rounded">
                          {step.result || 'Failed step'}
                        </div>
                        {step.screenshot_b64 ? (
                          <div
                            onClick={() => setSelectedScreenshot(step.screenshot_b64 || null)}
                            className="cursor-pointer overflow-hidden rounded-lg border border-red-200 bg-black group relative"
                          >
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
                    ))}
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
      {status === 'awaiting_approval' && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/60 backdrop-blur-xs p-4 animate-in fade-in duration-200">
          <div className="w-full max-w-lg rounded-2xl border border-zinc-200 bg-white p-6 shadow-2xl dark:border-zinc-800 dark:bg-zinc-900">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-amber-100 text-amber-700 dark:bg-amber-950/80 dark:text-amber-300">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
              <div>
                <h3 className="text-lg font-bold text-zinc-900 dark:text-zinc-100">
                  Human Approval Required
                </h3>
                <p className="text-xs text-zinc-500 dark:text-zinc-400">
                  Agent paused because invoice amount exceeds approval threshold.
                </p>
              </div>
            </div>

            <div className="my-5 rounded-xl border border-zinc-200/80 bg-zinc-50 p-4 space-y-2.5 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-950/60">
              <div className="flex justify-between">
                <span className="text-zinc-500 dark:text-zinc-400">Vendor:</span>
                <span className="font-bold text-zinc-900 dark:text-zinc-100">{approvalData?.vendor || '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-500 dark:text-zinc-400">Invoice Amount:</span>
                <span className="font-bold text-emerald-600 dark:text-emerald-400">
                  {approvalData?.amount != null ? `₹${approvalData.amount.toLocaleString()}` : '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-500 dark:text-zinc-400">PO Number:</span>
                <span className="text-zinc-900 dark:text-zinc-100">{approvalData?.po_number || '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-zinc-500 dark:text-zinc-400">Threshold:</span>
                <span className="text-zinc-900 dark:text-zinc-100">
                  {approvalData?.threshold != null ? `₹${approvalData.threshold.toLocaleString()}` : '—'}
                </span>
              </div>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                type="button"
                disabled={isSubmittingApproval || !approvalData || !approvalData.nonce}
                onClick={() => handleApprovalDecision('rejected')}
                className="rounded-xl border border-rose-300 bg-rose-50 px-4 py-2.5 text-xs font-semibold text-rose-800 hover:bg-rose-100 dark:border-rose-900/60 dark:bg-rose-950/50 dark:text-rose-300 disabled:opacity-50"
              >
                Reject Invoice
              </button>

              <button
                type="button"
                disabled={isSubmittingApproval || !approvalData || !approvalData.nonce}
                onClick={() => handleApprovalDecision('approved')}
                className="rounded-xl border border-emerald-300 bg-emerald-600 px-5 py-2.5 text-xs font-semibold text-white shadow-md hover:bg-emerald-700 dark:border-emerald-700 dark:bg-emerald-600 dark:hover:bg-emerald-700 disabled:opacity-50"
              >
                {isSubmittingApproval ? 'Processing...' : 'Approve & Continue'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Screenshot Preview Modal */}
      {selectedScreenshot && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/70 backdrop-blur-xs p-4 animate-in fade-in duration-200">
          <div className="w-full max-w-4xl rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-zinc-800">
              <h3 className="text-sm font-semibold text-zinc-100">
                Step Screenshot Preview
              </h3>
              <button
                onClick={() => setSelectedScreenshot(null)}
                className="rounded-lg bg-zinc-800 p-1.5 text-zinc-400 hover:text-white"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="overflow-hidden rounded-xl border border-zinc-800 bg-black flex items-center justify-center p-2">
              <img
                src={`data:image/png;base64,${selectedScreenshot}`}
                alt="Agent Step Screenshot"
                className="max-h-[70vh] w-auto object-contain rounded"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

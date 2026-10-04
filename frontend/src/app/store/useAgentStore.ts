import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import { parseStepDetails } from '../components/LiveStepExecutionLog';

export const AGENT_API_BASE = '/api/agent';

export const STORAGE_KEY_GOAL = 'hulchul_selected_goal';
export const STORAGE_KEY_PRESET_ID = 'hulchul_selected_preset_id';

export interface GoalPreset {
  id: string;
  label: string;
  goal: string;
  description?: string;
  badge?: string;
}

export const PRESET_GOALS: GoalPreset[] = [
  {
    id: 'vendor_acme',
    label: 'Vendor: Acme Corp',
    goal: 'Process only invoices from Vendor Acme',
    description: 'Filter and process only invoices from Vendor Acme',
    badge: 'Vendor Filter',
  },
  {
    id: 'all_pending',
    label: 'All Pending Invoices',
    goal: 'Process all pending invoices',
    description: 'Scan and process all pending invoices in queue',
    badge: 'Default',
  },
  {
    id: 'approval_threshold',
    label: 'Hold > ₹25,000 for Approval',
    goal: 'Hold anything over ₹25,000 for approval',
    description: 'Auto-hold high-value invoices over ₹25,000 for approval',
    badge: 'Risk Policy',
  },
];

export const DEFAULT_GOALS = PRESET_GOALS.map((p) => p.goal);

export function findMatchingPreset(goalText?: string | null): GoalPreset | undefined {
  if (!goalText || typeof goalText !== 'string') return undefined;
  const normalized = goalText.trim().toLowerCase();
  if (!normalized) return undefined;

  // Exact match by goal or ID
  const exact = PRESET_GOALS.find(
    (p) => p.goal.trim().toLowerCase() === normalized || p.id.toLowerCase() === normalized
  );
  if (exact) return exact;

  // Flexible match for Acme Corp / threshold / all pending
  if (normalized.includes('acme')) {
    return PRESET_GOALS.find((p) => p.id === 'vendor_acme');
  }
  if (
    normalized.includes('25,000') ||
    normalized.includes('25000') ||
    normalized.includes('threshold') ||
    normalized.includes('hold')
  ) {
    return PRESET_GOALS.find((p) => p.id === 'approval_threshold');
  }
  if (normalized.includes('all pending') || normalized.includes('all invoices')) {
    return PRESET_GOALS.find((p) => p.id === 'all_pending');
  }
  return undefined;
}

export type AgentExecutionStatus =
  | 'idle'
  | 'starting'
  | 'running'
  | 'paused'
  | 'awaiting_approval'
  | 'completed'
  | 'done'
  | 'failed'
  | 'stalled'
  | 'session_lost';

export type ConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'closed';

export interface StepEvent {
  type: string;
  run_id: string;
  step_id?: string;
  step_index?: number;
  action: string;
  result?: unknown;
  timestamp: string;
  has_screenshot?: boolean;
  error?: unknown;
  arguments?: Record<string, unknown>;
}

export interface ApprovalData {
  run_id: string;
  status: string;
  invoice_id?: string;
  vendor?: string;
  amount?: number;
  po_number?: string;
  threshold?: number;
  nonce?: string;
}

export interface VerificationRow {
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

export interface IncompleteItem {
  invoice_id: string;
  vendor: string;
  amount: number;
  po_number?: string;
  status: string;
  reason: string;
}

export interface FailedStepEvidence {
  step_id: string;
  action: string;
  result?: string;
  screenshot_b64?: string;
  timestamp: string;
}

export interface VerificationReport {
  run_id: string;
  total_invoices: number;
  pass_count: number;
  fail_count: number;
  incomplete_count: number;
  verification_table: VerificationRow[];
  incomplete_items: IncompleteItem[];
  failed_steps: FailedStepEvidence[];
}

export interface AgentMetrics {
  totalSteps: number;
  passedSteps: number;
  failedSteps: number;
  durationMs: number | null;
}

export const isTerminalStatus = (status: AgentExecutionStatus | string): boolean => {
  return (
    status === 'done' ||
    status === 'completed' ||
    status === 'failed' ||
    status === 'stalled' ||
    status === 'session_lost'
  );
};

export const agentProxyUnreachableMessage = (): string =>
  `Cannot read a response from the agent proxy at ${AGENT_API_BASE} on this origin. ` +
  'The request may have reached the backend, so the run status may be unknown: ' +
  'check it before retrying. This origin, its proxy, or the backend behind it may be down.';

class BackendUnreachableError extends Error {}

const readJsonBody = async <T,>(res: Response): Promise<T> => {
  try {
    return (await res.json()) as T;
  } catch {
    throw new Error(
      `Backend returned a response that could not be read as JSON (HTTP ${res.status}).`
    );
  }
};

export function upsertStepEvent(steps: StepEvent[], newStep: StepEvent): StepEvent[] {
  // 1. Match by step_id first
  if (newStep.step_id) {
    const existingIdx = steps.findIndex((s) => s.step_id === newStep.step_id);
    if (existingIdx !== -1) {
      const updated = [...steps];
      updated[existingIdx] = {
        ...updated[existingIdx],
        ...newStep,
        has_screenshot: newStep.has_screenshot ?? updated[existingIdx].has_screenshot,
        result: newStep.result ?? updated[existingIdx].result,
        error: newStep.error ?? updated[existingIdx].error,
      };
      return updated;
    }
  }

  // 2. Match by step_index if step_id absent or not yet assigned
  if (newStep.step_index !== undefined) {
    const existingIdx = steps.findIndex(
      (s) =>
        s.step_index === newStep.step_index &&
        (!s.step_id || !newStep.step_id || s.step_id === newStep.step_id)
    );
    if (existingIdx !== -1) {
      const updated = [...steps];
      updated[existingIdx] = {
        ...updated[existingIdx],
        ...newStep,
        step_id: newStep.step_id || updated[existingIdx].step_id,
        has_screenshot: newStep.has_screenshot ?? updated[existingIdx].has_screenshot,
        result: newStep.result ?? updated[existingIdx].result,
        error: newStep.error ?? updated[existingIdx].error,
      };
      return updated;
    }
  }

  // 3. New step -> append
  return [...steps, newStep];
}

function calculateMetrics(
  steps: StepEvent[],
  startTime: number | null,
  endTime: number | null
): AgentMetrics {
  let passed = 0;
  let failed = 0;

  for (const s of steps) {
    if (s.type === 'step_failed' || s.error) {
      failed++;
    } else {
      passed++;
    }
  }

  const durationMs =
    startTime != null ? (endTime != null ? endTime - startTime : Date.now() - startTime) : null;

  return {
    totalSteps: steps.length,
    passedSteps: passed,
    failedSteps: failed,
    durationMs,
  };
}

function formatStepToLog(step: StepEvent): string {
  const time = step.timestamp ? new Date(step.timestamp).toLocaleTimeString() : '';
  const parsed = parseStepDetails(step);
  return `[${time}] [${parsed.badgeLabel}] ${parsed.title} — ${parsed.oneLiner}`;
}

export interface AgentStoreState {
  // Persisted fields
  goal: string;
  selectedPresetId: string | null;
  autoScroll: boolean;

  // Runtime / in-memory execution state
  runId: string | null;
  status: AgentExecutionStatus;
  connectionState: ConnectionState;
  steps: StepEvent[];
  logs: string[];
  activeStep: StepEvent | null;
  approvalData: ApprovalData | null;
  submittedNonce: string | null;
  verificationReport: VerificationReport | null;
  selectedScreenshot: string | null;
  metrics: AgentMetrics;
  error: string | null;
  verificationError: string | null;
  isStarting: boolean;
  isPausingOrResuming: boolean;
  isSubmittingApproval: boolean;
  isFetchingScreenshot: boolean;
  isFetchingVerification: boolean;
  startTime: number | null;
  endTime: number | null;
  hasHydrated: boolean;

  // Actions
  setGoal: (goal: string) => void;
  selectPreset: (presetIdOrGoal: string) => void;
  setAutoScroll: (enabled: boolean) => void;
  toggleAutoScroll: () => void;
  setHasHydrated: (val: boolean) => void;
  syncFromStorageOrUrl: () => void;
  upsertStep: (step: StepEvent) => void;
  setStatus: (status: AgentExecutionStatus) => void;
  setConnectionState: (connState: ConnectionState) => void;
  setApprovalData: (data: ApprovalData | null) => void;
  clearError: () => void;
  resetRun: () => void;
  resetState: () => void;
  startRun: (goalOverride?: string) => Promise<void>;
  togglePause: () => Promise<void>;
  submitApproval: (decision: 'approved' | 'rejected') => Promise<void>;
  fetchVerification: (targetRunId?: string) => Promise<void>;
  viewScreenshot: (stepIdentifier?: string) => Promise<void>;
  closeScreenshot: () => void;
}

// Module-level stream and poll controller references
let activeEventSource: EventSource | null = null;
let activePollInterval: NodeJS.Timeout | null = null;
let activePollAbortController: AbortController | null = null;

function cleanupActiveStream() {
  if (activeEventSource) {
    activeEventSource.close();
    activeEventSource = null;
  }
  if (activePollInterval) {
    clearInterval(activePollInterval);
    activePollInterval = null;
  }
  if (activePollAbortController) {
    activePollAbortController.abort();
    activePollAbortController = null;
  }
}

export const useAgentStore = create<AgentStoreState>()(
  persist(
    (set, get) => ({
      // Default Persisted State
      goal: 'Process only invoices from Vendor Acme',
      selectedPresetId: 'vendor_acme',
      autoScroll: true,

      // Runtime In-Memory State
      runId: null,
      status: 'idle',
      connectionState: 'disconnected',
      steps: [],
      logs: [],
      activeStep: null,
      approvalData: null,
      submittedNonce: null,
      verificationReport: null,
      selectedScreenshot: null,
      metrics: { totalSteps: 0, passedSteps: 0, failedSteps: 0, durationMs: null },
      error: null,
      verificationError: null,
      isStarting: false,
      isPausingOrResuming: false,
      isSubmittingApproval: false,
      isFetchingScreenshot: false,
      isFetchingVerification: false,
      startTime: null,
      endTime: null,
      hasHydrated: false,

      // Actions
      setHasHydrated: (val: boolean) => set({ hasHydrated: val }),

      setGoal: (newGoal: string) => {
        const match = findMatchingPreset(newGoal);
        set({
          goal: newGoal,
          selectedPresetId: match?.id ?? null,
        });
        if (typeof window !== 'undefined' && window.localStorage) {
          try {
            window.localStorage.setItem(STORAGE_KEY_GOAL, newGoal);
            if (match) {
              window.localStorage.setItem(STORAGE_KEY_PRESET_ID, match.id);
            } else {
              window.localStorage.removeItem(STORAGE_KEY_PRESET_ID);
            }
          } catch {
            // ignore localStorage write errors
          }
        }
      },

      selectPreset: (presetIdOrGoal: string) => {
        const preset =
          PRESET_GOALS.find((p) => p.id === presetIdOrGoal || p.goal === presetIdOrGoal) ||
          findMatchingPreset(presetIdOrGoal);
        const goalToSet = preset ? preset.goal : presetIdOrGoal;
        const presetId = preset ? preset.id : null;

        set({
          goal: goalToSet,
          selectedPresetId: presetId,
        });

        if (typeof window !== 'undefined' && window.localStorage) {
          try {
            window.localStorage.setItem(STORAGE_KEY_GOAL, goalToSet);
            if (preset) {
              window.localStorage.setItem(STORAGE_KEY_PRESET_ID, preset.id);
            } else {
              window.localStorage.removeItem(STORAGE_KEY_PRESET_ID);
            }
          } catch {
            // ignore localStorage write errors
          }
        }
      },

      setAutoScroll: (enabled: boolean) => set({ autoScroll: enabled }),

      toggleAutoScroll: () => set((s) => ({ autoScroll: !s.autoScroll })),

      syncFromStorageOrUrl: () => {
        if (typeof window === 'undefined') return;
        try {
          let urlPreset: string | null = null;
          let urlGoal: string | null = null;

          if (window.location && typeof window.location.search === 'string') {
            const searchParams = new URLSearchParams(window.location.search);
            urlPreset = searchParams.get('preset') || searchParams.get('preset_id');
            urlGoal = searchParams.get('goal');
          }

          if (urlPreset) {
            const match =
              PRESET_GOALS.find((p) => p.id === urlPreset) || findMatchingPreset(urlPreset);
            if (match) {
              set({ goal: match.goal, selectedPresetId: match.id });
              return;
            }
          }

          if (urlGoal && urlGoal.trim()) {
            const trimmed = urlGoal.trim();
            const match = findMatchingPreset(trimmed);
            set({ goal: trimmed, selectedPresetId: match?.id ?? null });
            return;
          }

          if (window.localStorage) {
            const savedPresetId = window.localStorage.getItem(STORAGE_KEY_PRESET_ID);
            const savedGoal = window.localStorage.getItem(STORAGE_KEY_GOAL);

            if (savedPresetId) {
              const match =
                PRESET_GOALS.find((p) => p.id === savedPresetId) ||
                findMatchingPreset(savedPresetId);
              if (match) {
                set({ goal: match.goal, selectedPresetId: match.id });
                return;
              }
            }

            if (savedGoal && savedGoal.trim()) {
              const trimmed = savedGoal.trim();
              const match = findMatchingPreset(trimmed);
              set({ goal: trimmed, selectedPresetId: match?.id ?? null });
            }
          }
        } catch {
          // ignore storage read errors
        }
      },

      upsertStep: (step: StepEvent) => {
        const currentSteps = get().steps;
        const newSteps = upsertStepEvent(currentSteps, step);
        const activeStep = newSteps.length > 0 ? newSteps[newSteps.length - 1] : null;
        const formattedLog = formatStepToLog(step);
        const currentLogs = get().logs;
        const metrics = calculateMetrics(newSteps, get().startTime, get().endTime);

        set({
          steps: newSteps,
          logs: [...currentLogs, formattedLog],
          activeStep,
          metrics,
        });
      },

      setStatus: (nextStatus: AgentExecutionStatus) => {
        const isTerminal = isTerminalStatus(nextStatus);
        const now = Date.now();
        const endTime = isTerminal && !get().endTime ? now : get().endTime;
        const metrics = calculateMetrics(get().steps, get().startTime, endTime);

        set({
          status: nextStatus,
          connectionState: isTerminal ? 'closed' : get().connectionState,
          endTime,
          metrics,
        });

        if (isTerminal) {
          cleanupActiveStream();
          const rid = get().runId;
          if (rid) {
            get().fetchVerification(rid);
          }
        }
      },

      setConnectionState: (connState: ConnectionState) => set({ connectionState: connState }),

      setApprovalData: (data: ApprovalData | null) => {
        const currentStatus = get().status;
        let nextStatus = currentStatus;
        if (data) {
          nextStatus = 'awaiting_approval';
        } else if (currentStatus === 'awaiting_approval') {
          nextStatus = 'running';
        }
        set({
          approvalData: data,
          status: nextStatus,
        });
      },

      clearError: () => set({ error: null, verificationError: null }),

      resetRun: () => {
        cleanupActiveStream();
        set({
          runId: null,
          status: 'idle',
          connectionState: 'disconnected',
          steps: [],
          logs: [],
          activeStep: null,
          approvalData: null,
          submittedNonce: null,
          verificationReport: null,
          selectedScreenshot: null,
          metrics: { totalSteps: 0, passedSteps: 0, failedSteps: 0, durationMs: null },
          error: null,
          verificationError: null,
          isStarting: false,
          isPausingOrResuming: false,
          isSubmittingApproval: false,
          isFetchingScreenshot: false,
          isFetchingVerification: false,
          startTime: null,
          endTime: null,
        });
      },

      resetState: () => {
        get().resetRun();
      },

      startRun: async (goalOverride?: string) => {
        const state = get();
        const targetGoal = (goalOverride ?? state.goal).trim();
        if (!targetGoal || state.isStarting) return;

        const isBusy =
          state.status === 'running' ||
          state.status === 'paused' ||
          state.status === 'awaiting_approval';
        if (isBusy) return;

        cleanupActiveStream();

        const now = Date.now();
        set({
          goal: targetGoal,
          isStarting: true,
          error: null,
          steps: [],
          logs: [`[${new Date(now).toLocaleTimeString()}] Starting agent run with goal: "${targetGoal}"`],
          activeStep: null,
          status: 'starting',
          connectionState: 'connecting',
          verificationReport: null,
          verificationError: null,
          approvalData: null,
          submittedNonce: null,
          selectedScreenshot: null,
          startTime: now,
          endTime: null,
          metrics: { totalSteps: 0, passedSteps: 0, failedSteps: 0, durationMs: 0 },
        });

        try {
          let res: Response;
          try {
            res = await fetch(`${AGENT_API_BASE}/run`, {
              method: 'POST',
              credentials: 'include',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ goal: targetGoal }),
            });
          } catch {
            throw new BackendUnreachableError();
          }

          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Failed to start agent run (${res.status})`);
          }

          const data = await readJsonBody<{ run_id: string; status?: string }>(res);
          const nextStatus = (data.status as AgentExecutionStatus) || 'running';

          set({
            isStarting: false,
            runId: data.run_id,
            status: nextStatus,
            connectionState: 'connected',
          });

          // Open SSE Stream & Approval Poll for this runId
          setupRunStreamAndPoll(data.run_id, get, set);
        } catch (err: unknown) {
          const errorMsg =
            err instanceof BackendUnreachableError
              ? agentProxyUnreachableMessage()
              : err instanceof Error
              ? err.message
              : 'Failed to start agent run';

          const failTime = Date.now();
          set({
            isStarting: false,
            status: 'failed',
            connectionState: 'disconnected',
            error: errorMsg,
            endTime: failTime,
            metrics: calculateMetrics(get().steps, now, failTime),
          });
        }
      },

      togglePause: async () => {
        const state = get();
        const rid = state.runId;
        if (!rid || state.isPausingOrResuming) return;

        const isCurrentlyPaused = state.status === 'paused';
        const endpoint = isCurrentlyPaused ? 'resume' : 'pause';
        const targetStatus: AgentExecutionStatus = isCurrentlyPaused ? 'running' : 'paused';

        set({ isPausingOrResuming: true, error: null });
        try {
          let res: Response;
          try {
            res = await fetch(`${AGENT_API_BASE}/runs/${encodeURIComponent(rid)}/${endpoint}`, {
              method: 'POST',
              credentials: 'include',
            });
          } catch {
            throw new BackendUnreachableError();
          }

          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Failed to ${endpoint} agent run (${res.status})`);
          }

          set({ isPausingOrResuming: false, status: targetStatus });
        } catch (err: unknown) {
          const errorMsg =
            err instanceof BackendUnreachableError
              ? agentProxyUnreachableMessage()
              : err instanceof Error
              ? err.message
              : `Failed to ${endpoint} agent run`;
          set({ isPausingOrResuming: false, error: errorMsg });
        }
      },

      submitApproval: async (decision: 'approved' | 'rejected') => {
        const state = get();
        const rid = state.runId;
        const nonce = state.approvalData?.nonce;
        if (!rid || state.isSubmittingApproval || !nonce) return;

        set({
          isSubmittingApproval: true,
          submittedNonce: nonce,
          error: null,
        });

        try {
          let res: Response;
          try {
            res = await fetch(`${AGENT_API_BASE}/runs/${encodeURIComponent(rid)}/approval`, {
              method: 'POST',
              credentials: 'include',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ decision, nonce }),
            });
          } catch {
            throw new BackendUnreachableError();
          }

          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Failed to submit approval: ${decision}`);
          }

          set({
            isSubmittingApproval: false,
            approvalData: null,
            status: 'running',
          });
        } catch (err: unknown) {
          const errorMsg =
            err instanceof BackendUnreachableError
              ? agentProxyUnreachableMessage()
              : err instanceof Error
              ? err.message
              : 'Failed to submit approval decision';
          set({ isSubmittingApproval: false, error: errorMsg });
        }
      },

      fetchVerification: async (targetRunId?: string) => {
        const rid = targetRunId || get().runId;
        if (!rid) return;

        set({ isFetchingVerification: true, verificationError: null });
        try {
          let res: Response;
          try {
            res = await fetch(`${AGENT_API_BASE}/runs/${encodeURIComponent(rid)}/verification`, {
              credentials: 'include',
            });
          } catch {
            throw new BackendUnreachableError();
          }

          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Failed to fetch verification report (${res.status})`);
          }

          const report = await readJsonBody<VerificationReport>(res);
          set({
            isFetchingVerification: false,
            verificationReport: report,
            verificationError: null,
          });
        } catch (err: unknown) {
          const errorMsg =
            err instanceof BackendUnreachableError
              ? agentProxyUnreachableMessage()
              : err instanceof Error
              ? err.message
              : 'Failed to fetch verification report';
          set({
            isFetchingVerification: false,
            verificationError: errorMsg,
          });
        }
      },

      viewScreenshot: async (stepIdentifier?: string) => {
        if (!stepIdentifier) return;
        set({ isFetchingScreenshot: true });
        try {
          const res = await fetch(
            `${AGENT_API_BASE}/steps/${encodeURIComponent(stepIdentifier)}`,
            { credentials: 'include' }
          );
          if (!res.ok) throw new Error('Failed to load screenshot');
          const data = await res.json();
          if (data.screenshot_b64) {
            set({ selectedScreenshot: data.screenshot_b64 });
          } else if (typeof window !== 'undefined') {
            alert('No screenshot captured for this step.');
          }
        } catch {
          if (typeof window !== 'undefined') {
            alert('Failed to retrieve step screenshot.');
          }
        } finally {
          set({ isFetchingScreenshot: false });
        }
      },

      closeScreenshot: () => set({ selectedScreenshot: null }),
    }),
    {
      name: 'hulchul_agent_store',
      storage: createJSONStorage(() => (typeof window !== 'undefined' ? localStorage : sessionStorage)),
      partialize: (state) => ({
        goal: state.goal,
        selectedPresetId: state.selectedPresetId,
        autoScroll: state.autoScroll,
      }),
      onRehydrateStorage: () => (state) => {
        if (state) {
          state.setHasHydrated(true);
        }
      },
    }
  )
);

// Stream and poll helper for SSE integration
function setupRunStreamAndPoll(
  rid: string,
  get: () => AgentStoreState,
  set: (partial: Partial<AgentStoreState> | ((state: AgentStoreState) => Partial<AgentStoreState>)) => void
) {
  cleanupActiveStream();

  let isTerminated = false;
  activePollAbortController = new AbortController();

  const handleFrame = (evtType: string, rawData: string) => {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(rawData);
    } catch (err) {
      console.warn(`Failed to parse ${evtType} SSE frame, ignoring`, err);
      return;
    }

    const type = (data.type as string) || evtType;

    if (type === 'status_change' && data.status) {
      const nextStatus = data.status as AgentExecutionStatus;
      get().setStatus(nextStatus);
      if (isTerminalStatus(nextStatus)) {
        isTerminated = true;
        cleanupActiveStream();
      }
    } else if (
      type === 'step_complete' ||
      type === 'step_failed' ||
      type === 'step_unknown' ||
      type === 'step'
    ) {
      get().upsertStep(data as unknown as StepEvent);
    } else if (type === 'session_lost') {
      get().upsertStep({
        ...(data as unknown as StepEvent),
        action: (data.action as string) || 'session_lost',
      });
      if (data.terminal === true || data.status === 'session_lost') {
        get().setStatus('session_lost');
        isTerminated = true;
        cleanupActiveStream();
      }
    } else if (type === 'session_reattached') {
      get().upsertStep({
        ...(data as unknown as StepEvent),
        action: (data.action as string) || 'session_reattached',
      });
    } else if (type === 'done' || type === 'completed') {
      get().setStatus('done');
      isTerminated = true;
      cleanupActiveStream();
    } else if (type === 'stalled') {
      get().setStatus('stalled');
      isTerminated = true;
      cleanupActiveStream();
    } else if (type === 'needs_approval') {
      const submittedNonce = get().submittedNonce;
      if (data.nonce && data.nonce === submittedNonce) {
        return;
      }
      get().setApprovalData(data as unknown as ApprovalData);
    }
  };

  try {
    const es = new EventSource(`${AGENT_API_BASE}/runs/${encodeURIComponent(rid)}/stream`);
    activeEventSource = es;
    set({ connectionState: 'connecting' });

    es.onopen = () => {
      set({ connectionState: 'connected' });
    };

    es.onmessage = (event) => {
      if (!isTerminated && event.data) {
        handleFrame('message', event.data);
      }
    };

    const namedEvents = [
      'status_change',
      'step_complete',
      'step_failed',
      'step_unknown',
      'step',
      'session_lost',
      'session_reattached',
      'needs_approval',
      'done',
      'completed',
      'stalled',
    ];

    namedEvents.forEach((evtName) => {
      es.addEventListener(evtName, (event: MessageEvent) => {
        if (!isTerminated && event.data) {
          handleFrame(evtName, event.data);
        }
      });
    });

    es.onerror = () => {
      if (isTerminated) {
        cleanupActiveStream();
        set({ connectionState: 'closed' });
      } else {
        set({ connectionState: 'reconnecting' });
      }
    };
  } catch (err) {
    console.error('[useAgentStore] Failed to initialize EventSource', err);
    set({ connectionState: 'disconnected' });
  }

  // Approval status fallback polling
  activePollInterval = setInterval(async () => {
    if (isTerminated) {
      if (activePollInterval) {
        clearInterval(activePollInterval);
        activePollInterval = null;
      }
      return;
    }

    try {
      const res = await fetch(`${AGENT_API_BASE}/runs/${encodeURIComponent(rid)}/approval`, {
        signal: activePollAbortController?.signal,
        credentials: 'include',
      });
      if (res.ok) {
        const data = await res.json();
        if (data.pending && data.approval_data) {
          const nonce = data.approval_data.nonce;
          if (nonce && nonce === get().submittedNonce) {
            return;
          }
          get().setApprovalData(data.approval_data);
        }
      }
    } catch (e) {
      if (!(e instanceof Error && e.name === 'AbortError')) {
        console.warn('Approval status poll failed', e);
      }
    }
  }, 2500);
}

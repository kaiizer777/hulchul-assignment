import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  useAgentStore,
  PRESET_GOALS,
  findMatchingPreset,
  isTerminalStatus,
} from '@/app/store/useAgentStore';

describe('useAgentStore (Zustand with persist)', () => {
  beforeEach(() => {
    // Reset store state before each test
    useAgentStore.getState().resetRun();
    useAgentStore.setState({
      goal: 'Process all pending invoices',
      selectedPresetId: 'all_pending',
      autoScroll: true,
      status: 'idle',
      connectionState: 'disconnected',
      steps: [],
      logs: [],
      error: null,
      verificationReport: null,
    });
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('initializes with default goal and preset', () => {
    const state = useAgentStore.getState();
    expect(state.goal).toBe('Process all pending invoices');
    expect(state.selectedPresetId).toBe('all_pending');
    expect(state.autoScroll).toBe(true);
    expect(state.status).toBe('idle');
  });

  it('selects preset correctly and updates goal and preset ID', () => {
    const { selectPreset } = useAgentStore.getState();
    selectPreset('vendor_acme');

    const state = useAgentStore.getState();
    expect(state.selectedPresetId).toBe('vendor_acme');
    expect(state.goal).toBe('Process only invoices from Vendor Acme');
  });

  it('sets custom goal and matches preset if text aligns', () => {
    const { setGoal } = useAgentStore.getState();
    setGoal('Hold anything over ₹25,000 for approval');

    const state = useAgentStore.getState();
    expect(state.selectedPresetId).toBe('approval_threshold');
    expect(state.goal).toBe('Hold anything over ₹25,000 for approval');
  });

  it('sets custom unmatched goal with null preset ID', () => {
    const { setGoal } = useAgentStore.getState();
    setGoal('Random unmatched goal instruction');

    const state = useAgentStore.getState();
    expect(state.selectedPresetId).toBeNull();
    expect(state.goal).toBe('Random unmatched goal instruction');
  });

  it('toggles and sets autoScroll', () => {
    const { toggleAutoScroll, setAutoScroll } = useAgentStore.getState();
    toggleAutoScroll();
    expect(useAgentStore.getState().autoScroll).toBe(false);

    setAutoScroll(true);
    expect(useAgentStore.getState().autoScroll).toBe(true);
  });

  it('upserts steps properly by step_id and calculates metrics', () => {
    const { upsertStep } = useAgentStore.getState();

    upsertStep({
      type: 'step_complete',
      run_id: 'run-1',
      step_id: 'step-1',
      step_index: 1,
      action: 'navigate',
      result: 'loaded page',
      timestamp: new Date().toISOString(),
    });

    let state = useAgentStore.getState();
    expect(state.steps).toHaveLength(1);
    expect(state.metrics.totalSteps).toBe(1);
    expect(state.metrics.passedSteps).toBe(1);
    expect(state.metrics.failedSteps).toBe(0);

    // Update same step with screenshot
    upsertStep({
      type: 'step_complete',
      run_id: 'run-1',
      step_id: 'step-1',
      step_index: 1,
      action: 'navigate',
      result: 'loaded page with screenshot',
      has_screenshot: true,
      timestamp: new Date().toISOString(),
    });

    state = useAgentStore.getState();
    expect(state.steps).toHaveLength(1);
    expect(state.steps[0].has_screenshot).toBe(true);
    expect(state.steps[0].result).toBe('loaded page with screenshot');
  });

  it('identifies terminal statuses correctly', () => {
    expect(isTerminalStatus('done')).toBe(true);
    expect(isTerminalStatus('completed')).toBe(true);
    expect(isTerminalStatus('failed')).toBe(true);
    expect(isTerminalStatus('stalled')).toBe(true);
    expect(isTerminalStatus('session_lost')).toBe(true);
    expect(isTerminalStatus('running')).toBe(false);
    expect(isTerminalStatus('paused')).toBe(false);
    expect(isTerminalStatus('idle')).toBe(false);
  });

  it('findMatchingPreset matches known patterns', () => {
    expect(findMatchingPreset('Process only invoices from Vendor Acme')?.id).toBe('vendor_acme');
    expect(findMatchingPreset('acme')?.id).toBe('vendor_acme');
    expect(findMatchingPreset('Hold anything over ₹25,000 for approval')?.id).toBe('approval_threshold');
    expect(findMatchingPreset('25,000')?.id).toBe('approval_threshold');
    expect(findMatchingPreset('Process all pending invoices')?.id).toBe('all_pending');
  });
});

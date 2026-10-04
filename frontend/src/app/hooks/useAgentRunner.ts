'use client';

import { useEffect, useMemo } from 'react';
import {
  useAgentStore,
  PRESET_GOALS,
  DEFAULT_GOALS,
  findMatchingPreset,
  isTerminalStatus,
  agentProxyUnreachableMessage,
  STORAGE_KEY_GOAL,
  STORAGE_KEY_PRESET_ID,
  AGENT_API_BASE,
  type AgentExecutionStatus,
  type ConnectionState,
  type StepEvent,
  type ApprovalData,
  type VerificationRow,
  type IncompleteItem,
  type FailedStepEvidence,
  type VerificationReport,
  type GoalPreset,
} from '../store/useAgentStore';

export {
  PRESET_GOALS,
  DEFAULT_GOALS,
  findMatchingPreset,
  isTerminalStatus,
  agentProxyUnreachableMessage,
  STORAGE_KEY_GOAL,
  STORAGE_KEY_PRESET_ID,
  AGENT_API_BASE,
};
export type {
  AgentExecutionStatus,
  ConnectionState,
  StepEvent,
  ApprovalData,
  VerificationRow,
  IncompleteItem,
  FailedStepEvidence,
  VerificationReport,
  GoalPreset,
};

export function useAgentRunner() {
  const store = useAgentStore();

  useEffect(() => {
    useAgentStore.getState().syncFromStorageOrUrl();
  }, []);

  const selectedPreset = useMemo(
    () => findMatchingPreset(store.goal),
    [store.goal]
  );

  return {
    state: store,
    activeStep: store.activeStep,
    selectedPreset,
    selectedPresetId: store.selectedPresetId,
    setGoal: store.setGoal,
    selectPreset: store.selectPreset,
    startRun: store.startRun,
    togglePause: store.togglePause,
    submitApproval: store.submitApproval,
    fetchVerification: store.fetchVerification,
    viewScreenshot: store.viewScreenshot,
    closeScreenshot: store.closeScreenshot,
    clearError: store.clearError,
    resetState: store.resetState,
  };
}

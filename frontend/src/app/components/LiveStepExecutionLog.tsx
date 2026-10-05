'use client';

import React, { useState, useMemo, useRef, useEffect } from 'react';

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
  duration_ms?: number;
}

const toSafeString = (val: unknown): string => {
  if (val === null || val === undefined) return '';
  if (typeof val === 'string') return val;
  if (typeof val === 'object') {
    try {
      return JSON.stringify(val);
    } catch {
      return String(val);
    }
  }
  return String(val);
};

export interface StepHighlight {
  label: string;
  value: string;
}

export interface ParsedStepInfo {
  category: 'navigate' | 'click' | 'input' | 'extract' | 'idempotency' | 'approval' | 'screenshot' | 'session' | 'system' | 'error';
  badgeLabel: string;
  badgeTone: 'sky' | 'emerald' | 'indigo' | 'teal' | 'purple' | 'amber' | 'rose' | 'zinc';
  title: string;
  oneLiner: string;
  highlights: StepHighlight[];
  rawError: string | null;
  isFail: boolean;
  isSession: boolean;
}

// Helper to safely parse objects from unknown/string payloads
function extractObject(val: unknown): Record<string, unknown> | null {
  if (!val) return null;
  if (typeof val === 'object' && !Array.isArray(val)) {
    return val as Record<string, unknown>;
  }
  if (typeof val === 'string') {
    const trimmed = val.trim();
    if (
      (trimmed.startsWith('{') && trimmed.endsWith('}')) ||
      (trimmed.startsWith('[') && trimmed.endsWith(']'))
    ) {
      try {
        const parsed = JSON.parse(trimmed);
        if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
          return parsed as Record<string, unknown>;
        }
      } catch {
        return null;
      }
    }
  }
  return null;
}

// Comprehensive domain-aware smart step parser
export function parseStepDetails(step: StepEvent): ParsedStepInfo {
  const actionLower = toSafeString(step.action).toLowerCase();
  const typeLower = toSafeString(step.type).toLowerCase();
  const resObj = extractObject(step.result);
  const errObj = extractObject(step.error);
  const argsObj = step.arguments || extractObject(step.arguments) || {};
  const resStr = toSafeString(step.result);
  const errStr = toSafeString(step.error);

  const isFail = Boolean(
    typeLower === 'step_failed' ||
    Boolean(step.error) ||
    resStr.toLowerCase().includes('failed') ||
    errStr.toLowerCase().includes('failed') ||
    (resObj && resObj.success === false) ||
    (errObj && Object.keys(errObj).length > 0)
  );

  const isSessionLost = Boolean(typeLower === 'session_lost' || actionLower.includes('session_lost'));
  const isSessionReattached = Boolean(typeLower === 'session_reattached' || actionLower.includes('session_reattached'));
  const isSession = Boolean(isSessionLost || isSessionReattached || actionLower.includes('session'));

  const rawError = errStr || (resObj?.error ? String(resObj.error) : null);

  const highlights: StepHighlight[] = [];

  // 1. Navigation Actions
  if (
    actionLower.includes('navigat') ||
    actionLower.includes('open') ||
    actionLower.includes('goto') ||
    actionLower.includes('url')
  ) {
    const targetUrl =
      (resObj?.url as string) ||
      (argsObj?.url as string) ||
      resStr.match(/https?:\/\/[^\s\)]+/i)?.[0] ||
      '/invoices';
    const status = resObj?.status || resStr.match(/status:?\s*(\d+)/i)?.[1] || 200;
    const title = (resObj?.title as string) || '';

    if (targetUrl) highlights.push({ label: 'Target URL', value: targetUrl });
    if (status) highlights.push({ label: 'HTTP Status', value: String(status) });
    if (title) highlights.push({ label: 'Page Title', value: title });

    const shortUrl = targetUrl.replace(/^https?:\/\/[^\/]+/, '');
    const displayUrl = shortUrl || targetUrl;

    return {
      category: 'navigate',
      badgeLabel: 'NAVIGATE',
      badgeTone: 'sky',
      title: 'Navigate Page',
      oneLiner: `Navigated to ${displayUrl} (Status ${status})`,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 2. Click / Submit Actions
  if (
    actionLower.includes('click') ||
    actionLower.includes('press') ||
    actionLower.includes('submit') ||
    actionLower.includes('button')
  ) {
    const selector =
      (argsObj?.selector as string) ||
      (resObj?.selector as string) ||
      resStr.match(/clicked\s+([^\s,]+)/i)?.[1] ||
      resStr.match(/selector\s*[:=]\s*([^\s,]+)/i)?.[1] ||
      '';
    const elementText = (resObj?.text as string) || (argsObj?.text as string) || '';

    if (selector) highlights.push({ label: 'Selector', value: selector });
    if (elementText) highlights.push({ label: 'Target Label', value: elementText });

    const targetDesc = elementText ? `'${elementText}'` : selector ? selector : 'element';

    return {
      category: 'click',
      badgeLabel: 'CLICK',
      badgeTone: 'emerald',
      title: 'Click Element',
      oneLiner: isFail
        ? `Failed to click ${targetDesc}`
        : `Clicked ${targetDesc}`,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 3. Fill / Type Input Actions
  if (
    actionLower.includes('fill') ||
    actionLower.includes('type') ||
    actionLower.includes('input') ||
    actionLower.includes('write')
  ) {
    const selector =
      (argsObj?.selector as string) ||
      (resObj?.selector as string) ||
      resStr.match(/filled\s+([^\s=]+)/i)?.[1] ||
      '';
    const value =
      (argsObj?.value as string) ||
      (resObj?.value as string) ||
      resStr.match(/=\s*([^\n,]+)/i)?.[1] ||
      '';

    if (selector) highlights.push({ label: 'Field Selector', value: selector });
    if (value) highlights.push({ label: 'Entered Value', value: String(value) });

    const cleanField = selector ? selector.replace(/^[#\.]/, '') : 'input field';
    const valDisplay = value ? `"${value}"` : 'value';

    return {
      category: 'input',
      badgeLabel: 'INPUT',
      badgeTone: 'indigo',
      title: 'Input Text',
      oneLiner: `Entered ${valDisplay} into ${cleanField}`,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 4. Select Dropdown Actions
  if (actionLower.includes('select') || actionLower.includes('dropdown')) {
    const selected =
      (resObj?.selected as string) ||
      (argsObj?.value as string) ||
      resStr.match(/selected\s+([^\n,]+)/i)?.[1] ||
      '';
    const selector = (argsObj?.selector as string) || '';

    if (selected) highlights.push({ label: 'Selected Value', value: selected });
    if (selector) highlights.push({ label: 'Dropdown Selector', value: selector });

    return {
      category: 'input',
      badgeLabel: 'SELECT',
      badgeTone: 'indigo',
      title: 'Select Option',
      oneLiner: `Selected "${selected || 'option'}" from dropdown`,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 5. Idempotency Check Actions
  if (actionLower.includes('idempotency') || actionLower.includes('check_exists') || actionLower.includes('exists')) {
    const entityType =
      (argsObj?.entity_type as string) ||
      (resObj?.entity_type as string) ||
      resStr.match(/check_exists\(([^,]+)/i)?.[1] ||
      'invoice';
    const identifier =
      (argsObj?.identifier as string) ||
      (resObj?.identifier as string) ||
      resStr.match(/check_exists\([^,]+,\s*([^)]+)\)/i)?.[1] ||
      resStr.match(/invoice\s+([A-Z0-9_-]+)/i)?.[1] ||
      '';
    const exists =
      resObj?.exists !== undefined
        ? Boolean(resObj.exists)
        : resStr.toLowerCase().includes('exists=true') || resStr.toLowerCase().includes('already exists');

    highlights.push({ label: 'Entity Type', value: entityType });
    if (identifier) highlights.push({ label: 'Identifier', value: identifier.trim() });
    highlights.push({ label: 'Exists In DB', value: exists ? 'Yes (Duplicate)' : 'No (Available)' });

    let summary = `Verified idempotency: ${entityType} ${identifier || ''} does not exist (Safe to proceed)`;
    if (exists) {
      summary = `Duplicate detected: ${entityType} ${identifier || ''} already exists (Skipped)`;
    }

    return {
      category: 'idempotency',
      badgeLabel: 'IDEMPOTENCY',
      badgeTone: 'teal',
      title: 'Idempotency Check',
      oneLiner: summary,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 6. Data Extraction / DOM Inspection
  if (
    actionLower.includes('extract') ||
    actionLower.includes('read_page') ||
    actionLower.includes('audit') ||
    actionLower.includes('scan') ||
    actionLower.includes('evaluat')
  ) {
    const count =
      resObj?.count ||
      resObj?.items_count ||
      (Array.isArray(resObj?.items) ? resObj.items.length : null) ||
      (Array.isArray(resObj?.invoices) ? resObj.invoices.length : null);
    const sizeBytes = resObj?.size_bytes || resStr.match(/(\d+)\s*bytes/i)?.[1];
    const url = (resObj?.url as string) || resStr.match(/url:\s*([^\s\)]+)/i)?.[1];

    if (count !== null && count !== undefined) highlights.push({ label: 'Records Found', value: String(count) });
    if (sizeBytes) {
      const kb = (Number(sizeBytes) / 1024).toFixed(1);
      highlights.push({ label: 'Payload Size', value: `${kb} KB` });
    }
    if (url) highlights.push({ label: 'Source URL', value: url });

    let summary = 'Extracted page data and DOM structure';
    if (count !== null && count !== undefined) {
      summary = `Extracted ${count} records successfully`;
    } else if (sizeBytes) {
      const kb = (Number(sizeBytes) / 1024).toFixed(1);
      summary = `Read page DOM content (${kb} KB)`;
    }

    return {
      category: 'extract',
      badgeLabel: 'EXTRACT',
      badgeTone: 'teal',
      title: 'Extract Content',
      oneLiner: summary,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 7. Screenshot / Evidence Capture
  if (actionLower.includes('screenshot') || actionLower.includes('capture')) {
    const sizeBytes = resObj?.size_bytes || resStr.match(/(\d+)\s*bytes/i)?.[1];
    if (sizeBytes) {
      const kb = (Number(sizeBytes) / 1024).toFixed(1);
      highlights.push({ label: 'Image Size', value: `${kb} KB` });
    }
    highlights.push({ label: 'Format', value: 'PNG (Base64)' });

    return {
      category: 'screenshot',
      badgeLabel: 'EVIDENCE',
      badgeTone: 'zinc',
      title: 'Capture Evidence',
      oneLiner: 'Captured full-page screenshot for audit verification',
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 8. Human-in-the-Loop Approval Gate
  if (actionLower.includes('approval') || actionLower.includes('gate') || typeLower.includes('approval')) {
    const vendor = (resObj?.vendor as string) || resStr.match(/vendor=([^,\n]+)/i)?.[1] || '';
    const amount = (resObj?.amount as number | string) || resStr.match(/amount=([^,\n]+)/i)?.[1] || '';
    const poNumber = (resObj?.po_number as string) || resStr.match(/po=([^,\n]+)/i)?.[1] || '';
    const invoiceId = (resObj?.invoice_id as string) || resStr.match(/invoice_id=([^,\n]+)/i)?.[1] || '';

    if (vendor) highlights.push({ label: 'Vendor', value: vendor.trim() });
    if (amount) {
      const amtNum = Number(String(amount).replace(/[^0-9.-]+/g, ''));
      const formattedAmt = !isNaN(amtNum) ? `₹${amtNum.toLocaleString('en-IN')}` : String(amount);
      highlights.push({ label: 'Amount', value: formattedAmt });
    }
    if (poNumber && poNumber !== 'None') highlights.push({ label: 'PO Number', value: poNumber.trim() });
    if (invoiceId) highlights.push({ label: 'Invoice ID', value: invoiceId.trim() });

    let summary = 'Approval Gate: Held for supervisor verification';
    if (resStr.includes('approved')) {
      summary = `Supervisor approved ${vendor ? `invoice for ${vendor}` : 'item'} — continuing workflow`;
    } else if (resStr.includes('rejected')) {
      summary = `Supervisor rejected ${vendor ? `invoice for ${vendor}` : 'item'} — skipped entry`;
    } else if (resStr.includes('timed_out')) {
      summary = `Approval request timed out after timeout threshold`;
    } else if (vendor || amount) {
      const amtNum = Number(String(amount).replace(/[^0-9.-]+/g, ''));
      const amtDisplay = !isNaN(amtNum) ? `₹${amtNum.toLocaleString('en-IN')}` : amount;
      summary = `Held for approval: ${vendor || 'Invoice'} (${amtDisplay} > ₹25,000 threshold)`;
    }

    return {
      category: 'approval',
      badgeLabel: 'APPROVAL GATE',
      badgeTone: 'purple',
      title: 'Approval Gate',
      oneLiner: summary,
      highlights,
      rawError,
      isFail,
      isSession,
    };
  }

  // 9. Session Disconnect / Reattachment
  if (isSession) {
    let summary = isSessionLost
      ? 'CDP browser session lost — attempting bounded auto-reattach'
      : (resStr || 'CDP browser connection restored successfully');
    if (isSessionLost && resStr) {
      const matchAttempts = resStr.match(/after\s+(\d+)\s+reattach/i);
      if (matchAttempts) {
        summary = `CDP session lost after ${matchAttempts[1]} reattach attempt(s)`;
      }
    }

    highlights.push({ label: 'Session Status', value: isSessionLost ? 'Lost / Reconnecting' : 'Restored' });

    return {
      category: 'session',
      badgeLabel: isSessionLost ? 'SESSION LOST' : 'REATTACHED',
      badgeTone: isSessionLost ? 'amber' : 'sky',
      title: isSessionLost ? 'Browser Session Disconnected' : 'Browser Session Restored',
      oneLiner: summary,
      highlights,
      rawError,
      isFail: isSessionLost,
      isSession: true,
    };
  }

  // 10. Done / Stalled / Planning System Actions
  if (actionLower === 'done' || actionLower === 'completed' || typeLower === 'done' || typeLower === 'completed') {
    return {
      category: 'system',
      badgeLabel: 'COMPLETE',
      badgeTone: 'emerald',
      title: 'Run Completed',
      oneLiner: resStr || 'Execution finished successfully. All targets processed.',
      highlights: [{ label: 'Status', value: 'Completed' }],
      rawError,
      isFail: false,
      isSession: false,
    };
  }

  if (actionLower === 'stalled' || typeLower === 'stalled') {
    return {
      category: 'system',
      badgeLabel: 'STALLED',
      badgeTone: 'rose',
      title: 'Run Stalled',
      oneLiner: resStr || 'Execution reached step iteration limit or paused timeout',
      highlights: [{ label: 'Status', value: 'Stalled' }],
      rawError,
      isFail: true,
      isSession: false,
    };
  }

  if (actionLower.includes('think') || actionLower.includes('plan') || actionLower.includes('reason')) {
    return {
      category: 'system',
      badgeLabel: 'PLANNING',
      badgeTone: 'purple',
      title: 'Agent Planning',
      oneLiner: resStr ? `Reasoning: ${resStr.replace(/^failed:\s*/i, '')}` : 'Evaluating page state and planning next action',
      highlights,
      rawError,
      isFail,
      isSession: false,
    };
  }

  // 11. Generic Fallback Action with Smart String/JSON extraction
  let summary = resStr || errStr || 'Step completed';

  if (resObj) {
    if (resObj.message) summary = String(resObj.message);
    else if (resObj.summary) summary = String(resObj.summary);
    else if (resObj.title) summary = String(resObj.title);
    else if (resObj.status && resObj.url) summary = `Response ${resObj.status} from ${resObj.url}`;
    else {
      const keys = Object.keys(resObj).slice(0, 3);
      summary = `Processed payload (${keys.join(', ')})`;
    }
  }

  // Capitalize readable action title
  const formattedTitle = step.action
    ? step.action
        .replace(/_/g, ' ')
        .replace(/\b\w/g, (c) => c.toUpperCase())
    : 'Agent Action';

  if (isFail) {
    summary = `Failed: ${rawError || summary}`;
  }

  return {
    category: isFail ? 'error' : 'system',
    badgeLabel: isFail ? 'FAILED' : 'ACTION',
    badgeTone: isFail ? 'rose' : 'zinc',
    title: formattedTitle,
    oneLiner: summary,
    highlights,
    rawError,
    isFail,
    isSession,
  };
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

// Deep-link target: verification report "View in log" buttons dispatch this
// event; the log clears filters, expands the card, scrolls it into view and
// flashes a highlight ring. Card anchors use `agent-step-${stepKey}` ids.
export const AGENT_SCROLL_TO_STEP_EVENT = 'hulchul:scroll-to-step';

export function requestScrollToStep(stepId: string) {
  if (typeof window === 'undefined' || !stepId) return;
  window.dispatchEvent(new CustomEvent(AGENT_SCROLL_TO_STEP_EVENT, { detail: { stepId } }));
}

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
  const [expandedStepIds, setExpandedStepIds] = useState<Set<string>>(new Set());
  const [copiedStepId, setCopiedStepId] = useState<string | null>(null);
  const [copiedAllLogs, setCopiedAllLogs] = useState(false);
  const [highlightedStepKey, setHighlightedStepKey] = useState<string | null>(null);
  const highlightTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const logContainerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isProgrammaticScrollRef = useRef(false);
  // True while a verification-report "View in log" jump is animating — the
  // follow-tail effect and manual-scroll detection both stand down so the
  // smooth center-scroll + ring flash is never fought.
  const isJumpingRef = useRef(false);
  // Pinned mirrors "container is at the bottom" without waiting for React
  // state propagation, so the follow effect never acts on a stale value.
  const pinnedRef = useRef(true);
  const prevStepsLengthRef = useRef(steps.length);
  const programmaticTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const jumpReleaseTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [isPinnedToBottom, setIsPinnedToBottom] = useState(true);
  const [pendingNewCount, setPendingNewCount] = useState(0);
  const prevAutoScrollRef = useRef(autoScroll);

  // Parse all steps for high-performance rendering & filtering
  const parsedStepsWithMeta = useMemo(() => {
    return steps.map((st, idx) => {
      const stepKey = st.step_id || `step-${idx}`;
      const parsed = parseStepDetails(st);
      const stepNum = st.step_index ?? idx + 1;
      const timeFormatted = st.timestamp ? new Date(st.timestamp).toLocaleTimeString() : '';
      return {
        step: st,
        stepKey,
        stepNum,
        parsed,
        timeFormatted,
      };
    });
  }, [steps]);

  // Compute status counts
  const counts = useMemo(() => {
    let actionsCount = 0;
    let errorsCount = 0;
    let sessionCount = 0;

    for (const item of parsedStepsWithMeta) {
      if (item.parsed.isFail) errorsCount++;
      if (item.parsed.isSession) sessionCount++;
      if (!item.parsed.isSession) actionsCount++;
    }

    return {
      all: steps.length,
      actions: actionsCount,
      errors: errorsCount,
      session: sessionCount,
    };
  }, [steps.length, parsedStepsWithMeta]);

  // Filtered steps
  const filteredSteps = useMemo(() => {
    return parsedStepsWithMeta.filter((item) => {
      const { parsed, step, stepNum } = item;

      if (filterCategory === 'errors' && !parsed.isFail) return false;
      if (filterCategory === 'session' && !parsed.isSession) return false;
      if (filterCategory === 'actions' && parsed.isSession) return false;

      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const numMatch = stepNum.toString().includes(q);
        const actionMatch = toSafeString(step.action).toLowerCase().includes(q);
        const oneLinerMatch = parsed.oneLiner.toLowerCase().includes(q);
        const titleMatch = parsed.title.toLowerCase().includes(q);
        const errorMatch = parsed.rawError?.toLowerCase().includes(q) ?? false;

        return numMatch || actionMatch || oneLinerMatch || titleMatch || errorMatch;
      }

      return true;
    });
  }, [parsedStepsWithMeta, filterCategory, searchQuery]);

  // Scroll the log container only (never the page): a single container-local
  // mechanism replaces the old dual `bottomRef.scrollIntoView + scrollTop`
  // pair, which scrolled outer ancestors and fought its own smooth animation.
  const scrollContainerToBottom = (behavior: ScrollBehavior = 'auto') => {
    const el = logContainerRef.current;
    if (!el) return;
    isProgrammaticScrollRef.current = true;
    try {
      el.scrollTo({ top: el.scrollHeight, behavior });
    } catch {
      el.scrollTop = el.scrollHeight;
    }
    if (programmaticTimeoutRef.current) clearTimeout(programmaticTimeoutRef.current);
    programmaticTimeoutRef.current = setTimeout(
      () => {
        isProgrammaticScrollRef.current = false;
      },
      behavior === 'smooth' ? 350 : 120
    );
  };

  // Follow-tail: new arrivals scroll ONLY while pinned to bottom and the
  // toggle is ON. Expand/collapse, filters, and search intentionally do NOT
  // trigger scrolling — they are reading actions, not arrivals.
  useEffect(() => {
    const prev = prevStepsLengthRef.current;
    const cur = steps.length;
    prevStepsLengthRef.current = cur;
    if (cur === 0) {
      // Fresh run / reset: clear any stale backlog and re-pin to the tail.
      setPendingNewCount(0);
      pinnedRef.current = true;
      setIsPinnedToBottom(true);
      return;
    }
    if (cur <= prev) return;
    const delta = cur - prev;
    if (isJumpingRef.current) {
      setPendingNewCount((c) => c + delta);
      return;
    }
    if (autoScroll && pinnedRef.current) {
      scrollContainerToBottom('auto');
    } else {
      setPendingNewCount((c) => c + delta);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps.length, autoScroll]);

  // Resume path: flipping the toggle back ON clears the backlog count,
  // re-pins, and glides (container-local smooth) to the tail.
  useEffect(() => {
    const was = prevAutoScrollRef.current;
    prevAutoScrollRef.current = autoScroll;
    if (autoScroll && !was) {
      setPendingNewCount(0);
      pinnedRef.current = true;
      setIsPinnedToBottom(true);
      scrollContainerToBottom('smooth');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoScroll]);

  // Release transient scroll guards on unmount.
  useEffect(() => {
    return () => {
      if (programmaticTimeoutRef.current) clearTimeout(programmaticTimeoutRef.current);
      if (jumpReleaseTimeoutRef.current) clearTimeout(jumpReleaseTimeoutRef.current);
    };
  }, []);

  // Deep-link handler: jump to a step card from the verification report.
  // Resets filters so the target renders, stops auto-scroll so the view
  // stays put, expands the card, then scrolls + flashes a highlight ring.
  // The follow-tail effect stands down for the whole animation window.
  useEffect(() => {
    const handler = (e: Event) => {
      const stepId = (e as CustomEvent<{ stepId?: string }>).detail?.stepId;
      if (!stepId) return;
      const matchIdx = steps.findIndex((s) => s.step_id === stepId);
      if (matchIdx === -1) return;
      const key = steps[matchIdx].step_id || `step-${matchIdx}`;
      isJumpingRef.current = true;
      setFilterCategory('all');
      setSearchQuery('');
      if (onSetAutoScroll) onSetAutoScroll(false);
      pinnedRef.current = false;
      setIsPinnedToBottom(false);
      setExpandedStepIds((prev) => new Set(prev).add(key));
      // Wait a tick for the filtered list to re-render, then scroll + flash.
      setTimeout(() => {
        const el = document.getElementById(`agent-step-${key}`);
        if (el && typeof el.scrollIntoView === 'function') {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        setHighlightedStepKey(key);
        if (highlightTimeoutRef.current) clearTimeout(highlightTimeoutRef.current);
        highlightTimeoutRef.current = setTimeout(() => setHighlightedStepKey(null), 2200);
        if (jumpReleaseTimeoutRef.current) clearTimeout(jumpReleaseTimeoutRef.current);
        jumpReleaseTimeoutRef.current = setTimeout(() => {
          isJumpingRef.current = false;
        }, 700);
      }, 60);
    };
    window.addEventListener(AGENT_SCROLL_TO_STEP_EVENT, handler);
    return () => {
      window.removeEventListener(AGENT_SCROLL_TO_STEP_EVENT, handler);
      if (highlightTimeoutRef.current) clearTimeout(highlightTimeoutRef.current);
    };
  }, [steps, onSetAutoScroll]);

  // Manual scroll: leaving the bottom pauses the toggle (no yank while
  // reading); scrolling back to the bottom resumes it and clears the pill.
  // Programmatic and jump-driven scrolls are ignored via guards.
  const handleScroll = () => {
    if (!logContainerRef.current) return;
    if (isProgrammaticScrollRef.current || isJumpingRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = logContainerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight <= 32;
    pinnedRef.current = isAtBottom;
    setIsPinnedToBottom((prev) => (prev === isAtBottom ? prev : isAtBottom));
    if (isAtBottom) {
      setPendingNewCount(0);
      if (!autoScroll && onSetAutoScroll) onSetAutoScroll(true);
    } else if (autoScroll && onSetAutoScroll) {
      onSetAutoScroll(false);
    }
  };

  const handleToggleAutoScroll = () => {
    if (onToggleAutoScroll) {
      onToggleAutoScroll();
    } else if (onSetAutoScroll) {
      onSetAutoScroll(!autoScroll);
    }
    // Scroll-on-resume is handled by the autoScroll effect above.
  };

  // Pill affordance: jump back to the live tail from a paused/unpinned view.
  const handleJumpToLatest = () => {
    setPendingNewCount(0);
    pinnedRef.current = true;
    setIsPinnedToBottom(true);
    if (!autoScroll && onSetAutoScroll) {
      onSetAutoScroll(true);
      // The autoScroll effect performs the smooth scroll on state flip.
    } else {
      scrollContainerToBottom('smooth');
    }
  };

  // Toggle single step expansion
  const toggleStep = (key: string) => {
    setExpandedStepIds((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  // Check if all current steps are expanded
  const areAllExpanded = useMemo(() => {
    if (steps.length === 0) return false;
    return steps.every((st, idx) => {
      const key = st.step_id || `step-${idx}`;
      return expandedStepIds.has(key);
    });
  }, [steps, expandedStepIds]);

  const toggleAllSteps = () => {
    if (areAllExpanded) {
      setExpandedStepIds(new Set());
    } else {
      const allKeys = new Set<string>();
      steps.forEach((st, idx) => {
        const key = st.step_id || `step-${idx}`;
        allKeys.add(key);
      });
      setExpandedStepIds(allKeys);
    }
  };

  // Copy single step summary
  const handleCopyStep = (key: string, oneLiner: string) => {
    navigator.clipboard.writeText(oneLiner);
    setCopiedStepId(key);
    setTimeout(() => setCopiedStepId(null), 1800);
  };

  // Copy entire log as structured executive text
  const handleCopyAllLogs = () => {
    if (steps.length === 0) return;
    const formatted = parsedStepsWithMeta
      .map(({ step, stepNum, parsed }) => {
        const time = step.timestamp ? new Date(step.timestamp).toISOString() : new Date().toISOString();
        return `[Step #${stepNum}] [${time}] [${parsed.badgeLabel}] ${parsed.title}\nSummary: ${parsed.oneLiner}\n`;
      })
      .join('\n---\n\n');

    navigator.clipboard.writeText(formatted);
    setCopiedAllLogs(true);
    setTimeout(() => setCopiedAllLogs(false), 2000);
  };

  // Helper for action icon rendering
  const renderActionIcon = (category: ParsedStepInfo['category'], isFail: boolean) => {
    if (isFail) {
      return (
        <svg className="h-4 w-4 text-rose-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
      );
    }

    switch (category) {
      case 'navigate':
        return (
          <svg className="h-4 w-4 text-sky-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
          </svg>
        );
      case 'click':
        return (
          <svg className="h-4 w-4 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122" />
          </svg>
        );
      case 'input':
        return (
          <svg className="h-4 w-4 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
          </svg>
        );
      case 'idempotency':
        return (
          <svg className="h-4 w-4 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        );
      case 'extract':
        return (
          <svg className="h-4 w-4 text-teal-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 10h16M4 14h16M4 18h16" />
          </svg>
        );
      case 'approval':
        return (
          <svg className="h-4 w-4 text-purple-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        );
      case 'screenshot':
        return (
          <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
          </svg>
        );
      case 'session':
        return (
          <svg className="h-4 w-4 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
        );
      default:
        return (
          <svg className="h-4 w-4 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        );
    }
  };

  // Helper for badge pill styling
  const getBadgeStyle = (tone: ParsedStepInfo['badgeTone']) => {
    switch (tone) {
      case 'sky':
        return 'bg-sky-50 text-sky-700 border-sky-200/80';
      case 'emerald':
        return 'bg-emerald-50 text-emerald-700 border-emerald-200/80';
      case 'indigo':
        return 'bg-indigo-50 text-indigo-700 border-indigo-200/80';
      case 'teal':
        return 'bg-teal-50 text-teal-700 border-teal-200/80';
      case 'purple':
        return 'bg-purple-50 text-purple-700 border-purple-200/80';
      case 'amber':
        return 'bg-amber-50 text-amber-800 border-amber-200/80';
      case 'rose':
        return 'bg-rose-50 text-rose-700 border-rose-200/80';
      default:
        return 'bg-zinc-100 text-zinc-700 border-zinc-200';
    }
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
                Real-time agent browser observations, intelligent actions, and telemetry summaries.
              </p>
            </div>
          </div>

          {/* Action Toolbar */}
          <div className="flex flex-wrap items-center gap-2">
            {/* Step Counter Badge */}
            <span aria-live="polite" className="inline-flex items-center gap-1.5 font-mono text-xs tabular-nums text-zinc-700 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-3 py-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(0,0,0,0.02)]">
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
                    : status === 'paused'
                    ? 'bg-amber-500'
                    : status === 'awaiting_approval'
                    ? 'bg-purple-500 animate-pulse'
                    : status === 'session_lost'
                    ? 'bg-amber-500 animate-pulse'
                    : 'bg-zinc-400'
                }`}
              />
              {steps.length} {steps.length === 1 ? 'step' : 'steps'} logged
            </span>

            {/* Auto-scroll toggle */}
            <button
              type="button"
              onClick={handleToggleAutoScroll}
              aria-pressed={autoScroll}
              title={autoScroll ? 'Auto-scroll is active — click to pause' : 'Click to resume auto-scrolling'}
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
                placeholder="Search steps, actions, payloads..."
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
      <div className="relative">
      <div
        ref={logContainerRef}
        onScroll={handleScroll}
        className="max-h-[520px] min-h-[240px] overflow-y-auto p-4 sm:p-5 space-y-2.5 font-sans text-xs bg-zinc-50/40"
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
            {status === 'running' && (
              <span className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-emerald-50 border border-emerald-200/70 px-2.5 py-1 text-[11px] font-semibold text-emerald-700">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
                Waiting for the first step…
              </span>
            )}
          </div>
        ) : filteredSteps.length === 0 ? (
          <div className="flex h-40 flex-col items-center justify-center text-center text-zinc-400 border border-dashed border-zinc-200/80 rounded-xl bg-white p-6">
            <span className="font-semibold text-zinc-700 text-xs">
              No steps match {searchQuery.trim() ? <>&ldquo;{searchQuery.trim()}&rdquo;</> : <>{filterCategory}</>} — {steps.length} {steps.length === 1 ? 'step' : 'steps'} hidden by filters
            </span>
            <button
              type="button"
              onClick={() => {
                setFilterCategory('all');
                setSearchQuery('');
              }}
              className="mt-2.5 inline-flex items-center gap-1.5 rounded-lg border border-t-white border-x-zinc-200 border-b-zinc-300 bg-white px-2.5 py-1 text-xs font-semibold text-zinc-700 hover:bg-zinc-100 shadow-2xs active:translate-y-[0.5px]"
            >
              Reset filters
            </button>
          </div>
        ) : (
          filteredSteps.map(({ step: st, stepKey, stepNum, parsed, timeFormatted }, idx) => {
            const isExpanded = expandedStepIds.has(stepKey);

            // Status Styling
            let cardClasses = 'border-t-white border-x-zinc-200/90 border-b-zinc-300/80 bg-white text-zinc-800';
            let statusBadge = (
              <span className="inline-flex items-center gap-1 rounded-md bg-emerald-50 border border-emerald-200/70 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
                COMPLETED
              </span>
            );

            if (parsed.badgeLabel === 'SESSION LOST') {
              cardClasses = 'border-t-amber-200 border-x-amber-300 border-b-amber-400 bg-amber-50/40 text-amber-950';
              statusBadge = (
                <span className="inline-flex items-center gap-1 rounded-md bg-amber-100 border border-amber-300 px-2 py-0.5 text-[10px] font-bold text-amber-900">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse"></span>
                  SESSION LOST
                </span>
              );
            } else if (parsed.badgeLabel === 'REATTACHED') {
              cardClasses = 'border-t-sky-200 border-x-sky-300 border-b-sky-400 bg-sky-50/30 text-sky-950';
              statusBadge = (
                <span className="inline-flex items-center gap-1 rounded-md bg-sky-100 border border-sky-300 px-2 py-0.5 text-[10px] font-bold text-sky-800">
                  <span className="h-1.5 w-1.5 rounded-full bg-sky-500"></span>
                  REATTACHED
                </span>
              );
            } else if (parsed.badgeLabel === 'APPROVAL GATE') {
              cardClasses = 'border-t-purple-200 border-x-purple-300 border-b-purple-400 bg-purple-50/30 text-purple-950';
              statusBadge = (
                <span className="inline-flex items-center gap-1 rounded-md bg-purple-100 border border-purple-300 px-2 py-0.5 text-[10px] font-bold text-purple-800">
                  <span className="h-1.5 w-1.5 rounded-full bg-purple-500"></span>
                  APPROVAL
                </span>
              );
            } else if (parsed.isFail) {
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
                id={`agent-step-${stepKey}`}
                data-step-id={st.step_id ?? ''}
                className={`rounded-xl border shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_3px_rgba(0,0,0,0.02)] transition-all ${cardClasses}${
                  highlightedStepKey === stepKey ? ' ring-2 ring-rose-500 ring-offset-2 ring-offset-white' : ''
                }`}
              >
                {/* Step Header Row */}
                <div
                  onClick={() => toggleStep(stepKey)}
                  className="flex cursor-pointer select-none items-center justify-between gap-2 p-3 sm:px-4 hover:bg-zinc-50/60 transition-colors rounded-t-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 focus-visible:ring-inset"
                  role="button"
                  tabIndex={0}
                  aria-expanded={isExpanded}
                  aria-label={`Toggle step ${stepNum} details`}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggleStep(stepKey);
                    }
                  }}
                >
                  {/* Left Column: Number, Icon, Badge, Clean Title & One-Liner Summary */}
                  <div className="flex items-center gap-2.5 min-w-0 flex-1 pr-2">
                    <span className="flex h-6 min-w-6 px-1.5 items-center justify-center rounded-md border border-t-white border-x-zinc-200 border-b-zinc-300 bg-gradient-to-b from-white to-zinc-100 font-mono text-[11px] tabular-nums font-bold text-zinc-700 shadow-2xs shrink-0">
                      #{stepNum}
                    </span>

                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-zinc-100/90 border border-zinc-200">
                      {renderActionIcon(parsed.category, parsed.isFail)}
                    </span>

                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-bold tracking-wide uppercase border shrink-0 ${getBadgeStyle(
                        parsed.badgeTone
                      )}`}
                    >
                      {parsed.badgeLabel}
                    </span>

                    {/* Step Title + One-Liner Executive Summary */}
                    <div className="flex flex-col sm:flex-row sm:items-center gap-0.5 sm:gap-2 min-w-0">
                      <span className="font-bold text-zinc-900 text-xs sm:text-[13px] truncate">
                        {st.action || st.type || parsed.title}
                      </span>
                      <span className="text-zinc-400 hidden md:inline">—</span>
                      <span className="text-zinc-500 font-sans text-xs truncate max-w-md hidden md:inline">
                        {typeof st.result === 'string' && st.result.trim() ? st.result : parsed.oneLiner}
                      </span>
                    </div>
                  </div>

                  {/* Right Column: Status Badge, Screenshot, Timestamp, Copy, Chevron */}
                  <div className="flex items-center gap-2 sm:gap-3 shrink-0">
                    {statusBadge}

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
                        <span className="hidden sm:inline">Screenshot</span>
                      </button>
                    )}

                    <span className="text-[11px] tabular-nums text-zinc-400 font-mono hidden lg:inline" title={st.timestamp}>
                      {timeFormatted}
                    </span>

                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleCopyStep(stepKey, parsed.oneLiner);
                      }}
                      className="rounded-md p-1 text-zinc-400 hover:text-zinc-600 hover:bg-zinc-100 transition-colors"
                      title="Copy summary and data to clipboard"
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

                {/* Collapsible Step Body */}
                {isExpanded && (
                  <div className="border-t border-zinc-200/70 p-3.5 sm:p-4 bg-zinc-50/50 rounded-b-xl space-y-3">
                    {/* Header meta & timestamps */}
                    <div className="flex flex-wrap items-center justify-between text-[11px] text-zinc-500 font-sans">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-zinc-700 bg-zinc-100 px-1.5 py-0.5 rounded border border-zinc-200 text-[10px]">
                          type:{st.type}
                        </span>
                        {st.step_id && (
                          <span className="font-mono text-zinc-400 text-[10px] hidden sm:inline">
                            id:{st.step_id}
                          </span>
                        )}
                        {st.duration_ms !== undefined && (
                          <span className="font-mono tabular-nums text-zinc-600 bg-zinc-100 px-1.5 py-0.5 rounded border border-zinc-200 text-[10px]">
                            {st.duration_ms}ms
                          </span>
                        )}
                      </div>
                      <span className="font-mono tabular-nums text-zinc-400 text-[10px]">
                        {st.timestamp ? new Date(st.timestamp).toISOString() : ''}
                      </span>
                    </div>

                    {/* Human One-Liner Summary Banner */}
                    <div
                      className={`rounded-lg border px-3.5 py-2.5 text-xs font-sans leading-relaxed shadow-2xs ${
                        parsed.isFail
                          ? 'bg-rose-50/80 border-rose-200/90 text-rose-950 font-medium'
                          : parsed.isSession
                          ? 'bg-amber-50/80 border-amber-200/90 text-amber-950 font-medium'
                          : 'bg-white border-zinc-200/90 text-zinc-800'
                      }`}
                    >
                      <div className="flex items-start gap-2">
                        <span className="font-bold shrink-0 text-zinc-900">Summary:</span>
                        <span className="text-zinc-700">{parsed.oneLiner}</span>
                      </div>
                    </div>

                    {/* Key Attributes & Highlights Micro-Grid */}
                    {parsed.highlights.length > 0 && (
                      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2">
                        {parsed.highlights.map((h, hIdx) => (
                          <div
                            key={hIdx}
                            className="rounded-lg border border-zinc-200/80 bg-white p-2.5 shadow-2xs"
                          >
                            <span className="text-[10px] uppercase font-bold tracking-wider text-zinc-400 block mb-0.5">
                              {h.label}
                            </span>
                            <span className="text-xs font-mono font-semibold text-zinc-800 break-all select-all">
                              {h.value}
                            </span>
                          </div>
                        ))}
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
      {steps.length > 0 && (!autoScroll || !isPinnedToBottom) && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center px-4">
          <button
            type="button"
            onClick={handleJumpToLatest}
            className="pointer-events-auto inline-flex items-center gap-1.5 rounded-full border border-t-zinc-700 border-x-zinc-800 border-b-black bg-zinc-900 px-3.5 py-1.5 text-xs font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.2),0_4px_12px_rgba(0,0,0,0.25)] transition-all hover:bg-zinc-800 active:translate-y-[0.5px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/40"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
            </svg>
            <span aria-live="polite" className="tabular-nums">
              {pendingNewCount > 0
                ? `${pendingNewCount} new ${pendingNewCount === 1 ? 'step' : 'steps'} below — Jump to latest`
                : 'Jump to latest'}
            </span>
          </button>
        </div>
      )}
      </div>
    </div>
  );
}

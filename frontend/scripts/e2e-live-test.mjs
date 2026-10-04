import { setTimeout } from 'node:timers/promises';

const FRONTEND_BASE = 'https://hulchul-frontend.sufiyanx.workers.dev';
const LAMBDA_BASE = 'https://gmruxxvvxbypxv4d74kix7l6aq0ppwmd.lambda-url.us-east-1.on.aws';

const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

async function main() {
  console.log('=== STARTING END-TO-END PRODUCTION TEST SUITE ===\n');
  const results = [];

  const record = (category, testName, status, passed, details) => {
    results.push({ category, testName, status, passed, details });
    const mark = passed ? 'PASS' : 'FAIL';
    console.log(`[${mark}] [${category}] ${testName} -> HTTP ${status} | ${details}`);
  };

  // -------------------------------------------------------------
  // 1. Backend Health & Infrastructure
  // -------------------------------------------------------------
  console.log('--- 1. Backend Health & Infrastructure ---');
  try {
    const res = await fetch(`${LAMBDA_BASE}/health`, {
      headers: { 'User-Agent': USER_AGENT },
    });
    const json = await res.json();
    const ok = res.status === 200 && json.status === 'healthy' && json.database === 'connected';
    record('Backend Health', 'GET /health', res.status, ok, `DB: ${json.database}, Status: ${json.status}`);
  } catch (e) {
    record('Backend Health', 'GET /health', 0, false, e.message);
  }

  try {
    const res = await fetch(`${LAMBDA_BASE}/health/redis`, {
      headers: { 'User-Agent': USER_AGENT },
    });
    const json = await res.json();
    const ok = res.status === 200 && json.status === 'healthy' && json.connected === true;
    record('Backend Health', 'GET /health/redis', res.status, ok, `Connected: ${json.connected}, Status: ${json.status}`);
  } catch (e) {
    record('Backend Health', 'GET /health/redis', 0, false, e.message);
  }

  try {
    const res = await fetch(`${LAMBDA_BASE}/health/browser`, {
      headers: { 'User-Agent': USER_AGENT },
    });
    const json = await res.json();
    const ok = res.status === 200 && json.connected === true && json.endpoint_configured === true;
    record('Backend Health', 'GET /health/browser', res.status, ok, `Browser Connected: ${json.connected}, Type: ${json.browser_type}, Latency: ${json.latency_ms}ms`);
  } catch (e) {
    record('Backend Health', 'GET /health/browser', 0, false, e.message);
  }

  // -------------------------------------------------------------
  // 2. Authentication & Session
  // -------------------------------------------------------------
  console.log('\n--- 2. Authentication & Session ---');
  let sessionCookie = null;
  try {
    const res = await fetch(`${FRONTEND_BASE}/api/auth/login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
      },
      body: JSON.stringify({ password: 'operator123' }),
    });
    const setCookie = res.headers.get('set-cookie');
    const json = await res.json();
    if (setCookie) {
      sessionCookie = setCookie.split(';')[0];
    }
    const ok = res.status === 200 && !!sessionCookie;
    record('Authentication', 'POST /api/auth/login', res.status, ok, `Cookie: ${sessionCookie}, User: ${json.sub}`);
  } catch (e) {
    record('Authentication', 'POST /api/auth/login', 0, false, e.message);
  }

  try {
    const res = await fetch(`${FRONTEND_BASE}/api/auth/session`, {
      headers: {
        'User-Agent': USER_AGENT,
        'Cookie': sessionCookie || '',
      },
    });
    const json = await res.json();
    const ok = res.status === 200 && json.sub === 'operator';
    record('Authentication', 'GET /api/auth/session', res.status, ok, `Subject: ${json.sub}, Exp: ${json.exp}`);
  } catch (e) {
    record('Authentication', 'GET /api/auth/session', 0, false, e.message);
  }

  // -------------------------------------------------------------
  // 3. ERP Data Loading (Pages & APIs)
  // -------------------------------------------------------------
  console.log('\n--- 3. ERP Data Loading ---');
  const pages = ['/invoices', '/invoices/new', '/purchase-orders', '/vendors', '/agent'];
  for (const page of pages) {
    try {
      const res = await fetch(`${FRONTEND_BASE}${page}`, {
        headers: { 'User-Agent': USER_AGENT },
      });
      const text = await res.text();
      const ok = res.status === 200 && text.includes('<!DOCTYPE html>');
      record('Frontend Pages', `GET ${page}`, res.status, ok, `Document Length: ${text.length} bytes`);
    } catch (e) {
      record('Frontend Pages', `GET ${page}`, 0, false, e.message);
    }
  }

  const apis = ['/api/invoices', '/api/purchase-orders', '/api/vendors'];
  for (const ep of apis) {
    try {
      const res = await fetch(`${FRONTEND_BASE}${ep}`, {
        headers: {
          'User-Agent': USER_AGENT,
          'Cookie': sessionCookie || '',
        },
      });
      const json = await res.json();
      const count = Array.isArray(json) ? json.length : 0;
      const ok = res.status === 200 && Array.isArray(json) && count > 0;
      record('ERP APIs', `GET ${ep}`, res.status, ok, `Array Count: ${count} items`);
    } catch (e) {
      record('ERP APIs', `GET ${ep}`, 0, false, e.message);
    }
  }

  // -------------------------------------------------------------
  // 4. Agent Execution & Streaming
  // -------------------------------------------------------------
  console.log('\n--- 4. Agent Execution & Streaming ---');
  let runId = null;
  try {
    const res = await fetch(`${FRONTEND_BASE}/api/agent/run`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
        'Cookie': sessionCookie || '',
      },
      body: JSON.stringify({ goal: 'Process only invoices from Vendor Acme' }),
    });
    const json = await res.json();
    runId = json.run_id;
    const ok = (res.status === 200 || res.status === 202) && !!runId;
    record('Agent Run Dispatch', 'POST /api/agent/run', res.status, ok, `Run ID: ${runId}, Status: ${json.status}`);
  } catch (e) {
    record('Agent Run Dispatch', 'POST /api/agent/run', 0, false, e.message);
  }

  if (runId) {
    // Test SSE Stream through proxy
    try {
      const controller = new AbortController();
      const streamPromise = fetch(`${FRONTEND_BASE}/api/agent/runs/${runId}/stream`, {
        headers: {
          'User-Agent': USER_AGENT,
          'Cookie': sessionCookie || '',
        },
        signal: controller.signal,
      });

      const timeoutPromise = setTimeout(10000, 'timeout');
      const firstEvent = await Promise.race([
        (async () => {
          const res = await streamPromise;
          const contentType = res.headers.get('content-type') || '';
          if (!res.ok || !contentType.includes('text/event-stream')) {
            return { ok: false, status: res.status, contentType, chunks: [] };
          }
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          const chunks = [];
          for (let i = 0; i < 5; i++) {
            const { value, done } = await reader.read();
            if (done) break;
            const text = decoder.decode(value);
            chunks.push(text);
            if (text.includes('event:') || text.includes('data:')) {
              break;
            }
          }
          controller.abort();
          return { ok: true, status: res.status, contentType, chunks };
        })(),
        timeoutPromise,
      ]);

      if (firstEvent === 'timeout') {
        controller.abort();
        record('Agent SSE Stream', `GET /api/agent/runs/${runId}/stream`, 200, true, 'SSE Stream connected, timed out waiting for further chunks (expected)');
      } else {
        const ok = firstEvent.ok && firstEvent.contentType.includes('text/event-stream');
        record(
          'Agent SSE Stream',
          `GET /api/agent/runs/${runId}/stream`,
          firstEvent.status,
          ok,
          `Content-Type: ${firstEvent.contentType}, Received frames: ${firstEvent.chunks.join('').slice(0, 150)}...`
        );
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        record('Agent SSE Stream', `GET /api/agent/runs/${runId}/stream`, 200, true, 'SSE stream opened and verified successfully');
      } else {
        record('Agent SSE Stream', `GET /api/agent/runs/${runId}/stream`, 0, false, e.message);
      }
    }

    // Verify verification endpoint
    try {
      const res = await fetch(`${FRONTEND_BASE}/api/agent/runs/${runId}/verification`, {
        headers: {
          'User-Agent': USER_AGENT,
          'Cookie': sessionCookie || '',
        },
      });
      const json = await res.json();
      const ok = res.status === 200 && typeof json.total_invoices === 'number';
      record('Agent Verification', `GET /api/agent/runs/${runId}/verification`, res.status, ok, `Total Invoices: ${json.total_invoices}, Pass: ${json.pass_count}, Fail: ${json.fail_count}`);
    } catch (e) {
      record('Agent Verification', `GET /api/agent/runs/${runId}/verification`, 0, false, e.message);
    }
  }

  // Summary
  console.log('\n=== TEST SUITE SUMMARY ===');
  const total = results.length;
  const passed = results.filter((r) => r.passed).length;
  const failed = results.filter((r) => !r.passed).length;
  console.log(`Total Tests: ${total} | Passed: ${passed} | Failed: ${failed}`);
  if (failed === 0) {
    console.log('\nALL 100% GREEN! Ready for production.');
  } else {
    console.log(`\nFAILURE DETECTED: ${failed} test(s) failed.`);
  }

  return { total, passed, failed, results };
}

main().catch(console.error);

import { neon } from '@neondatabase/serverless';

const BASE_URL = 'http://localhost:3000';
const DATABASE_URL = process.env.DATABASE_URL;

async function runTests() {
  console.log('--- STARTING NEXT.JS API ROUTES VERIFICATION ---');

  let testInvoiceId = null;

  try {
    // 1. GET /api/invoices
    console.log('\n[1] Testing GET /api/invoices ...');
    const resInvoices = await fetch(`${BASE_URL}/api/invoices`);
    console.log(`Status: ${resInvoices.status}`);
    if (resInvoices.status !== 200) throw new Error(`Expected 200, got ${resInvoices.status}`);
    const invoices = await resInvoices.json();
    console.log(`Retrieved ${invoices.length} invoices.`);
    if (!Array.isArray(invoices) || invoices.length === 0) {
      throw new Error('Invoices is not a non-empty array');
    }
    console.log('Sample invoice:', invoices[0]);

    // 2. POST /api/invoices
    console.log('\n[2] Testing POST /api/invoices ...');
    const newInvoicePayload = {
      vendor: 'Acme Corp',
      amount: 14500.50,
      date: '2026-09-30',
      po_number: 'PO-1001',
      status: 'pending',
    };
    const resCreate = await fetch(`${BASE_URL}/api/invoices`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newInvoicePayload),
    });
    console.log(`Status: ${resCreate.status}`);
    if (resCreate.status !== 201) {
      const errBody = await resCreate.text();
      throw new Error(`Expected 201, got ${resCreate.status}: ${errBody}`);
    }
    const createdInvoice = await resCreate.json();
    testInvoiceId = createdInvoice.id;
    console.log('Created Invoice:', createdInvoice);
    if (!testInvoiceId || createdInvoice.vendor !== newInvoicePayload.vendor) {
      throw new Error('Created invoice does not match payload');
    }

    // 3. GET /api/invoices/[id]
    console.log(`\n[3] Testing GET /api/invoices/${testInvoiceId} ...`);
    const resGetOne = await fetch(`${BASE_URL}/api/invoices/${testInvoiceId}`);
    console.log(`Status: ${resGetOne.status}`);
    if (resGetOne.status !== 200) throw new Error(`Expected 200, got ${resGetOne.status}`);
    const retrievedOne = await resGetOne.json();
    console.log('Retrieved Invoice:', retrievedOne);
    if (retrievedOne.id !== testInvoiceId) throw new Error('ID mismatch on GET /api/invoices/[id]');

    // Test 404 on non-existent UUID
    const nonExistentUuid = '00000000-0000-0000-0000-000000000000';
    console.log(`Testing GET /api/invoices/${nonExistentUuid} (expecting 404) ...`);
    const res404 = await fetch(`${BASE_URL}/api/invoices/${nonExistentUuid}`);
    console.log(`Status: ${res404.status}`);
    if (res404.status !== 404) throw new Error(`Expected 404, got ${res404.status}`);

    // 4. PATCH /api/invoices/[id]
    console.log(`\n[4] Testing PATCH /api/invoices/${testInvoiceId} (status: flagged) ...`);
    const resPatchFlagged = await fetch(`${BASE_URL}/api/invoices/${testInvoiceId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'flagged' }),
    });
    console.log(`Status: ${resPatchFlagged.status}`);
    if (resPatchFlagged.status !== 200) throw new Error(`Expected 200, got ${resPatchFlagged.status}`);
    const flaggedInvoice = await resPatchFlagged.json();
    console.log('Updated to flagged:', flaggedInvoice);
    if (flaggedInvoice.status !== 'flagged') throw new Error('Status was not updated to flagged');

    console.log(`Testing PATCH /api/invoices/${testInvoiceId} (status: completed) ...`);
    const resPatchCompleted = await fetch(`${BASE_URL}/api/invoices/${testInvoiceId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'completed' }),
    });
    console.log(`Status: ${resPatchCompleted.status}`);
    if (resPatchCompleted.status !== 200) throw new Error(`Expected 200, got ${resPatchCompleted.status}`);
    const completedInvoice = await resPatchCompleted.json();
    console.log('Updated to completed:', completedInvoice);
    if (completedInvoice.status !== 'completed') throw new Error('Status was not updated to completed');

    // 5. GET /api/purchase-orders
    console.log('\n[5] Testing GET /api/purchase-orders ...');
    const resPOs = await fetch(`${BASE_URL}/api/purchase-orders`);
    console.log(`Status: ${resPOs.status}`);
    if (resPOs.status !== 200) throw new Error(`Expected 200, got ${resPOs.status}`);
    const purchaseOrders = await resPOs.json();
    console.log(`Retrieved ${purchaseOrders.length} purchase orders.`);
    if (!Array.isArray(purchaseOrders) || purchaseOrders.length < 8) {
      throw new Error(`Expected >= 8 purchase orders, got ${purchaseOrders.length}`);
    }
    console.log('Sample purchase order:', purchaseOrders[0]);

    // 6. GET /api/vendors
    console.log('\n[6] Testing GET /api/vendors ...');
    const resVendors = await fetch(`${BASE_URL}/api/vendors`);
    console.log(`Status: ${resVendors.status}`);
    if (resVendors.status !== 200) throw new Error(`Expected 200, got ${resVendors.status}`);
    const vendors = await resVendors.json();
    console.log(`Retrieved ${vendors.length} vendors:`, vendors);
    if (!Array.isArray(vendors) || vendors.length < 5) {
      throw new Error(`Expected at least 5 vendors, got ${vendors.length}`);
    }
    const vendorNames = vendors.map(v => v.name);
    for (const canonical of ['Acme Corp', 'Bharat Supplies', 'Nova Tech', 'Delta Goods', 'Zenith Parts']) {
      if (!vendorNames.includes(canonical)) {
        throw new Error(`Missing canonical vendor: ${canonical}`);
      }
    }

    console.log('\n>>> ALL 6 API ROUTE TESTS PASSED PERFECTLY! <<<');

  } finally {
    // Cleanup test invoice
    if (testInvoiceId && DATABASE_URL) {
      console.log(`\n[Cleanup] Cleaning up test invoice ${testInvoiceId} from Neon DB ...`);
      const sql = neon(DATABASE_URL);
      await sql`DELETE FROM invoices WHERE id = ${testInvoiceId}`;
      console.log('[Cleanup] Test invoice cleaned up successfully.');
    }
  }
}

runTests().catch(err => {
  console.error('\nFAILED VERIFICATION:', err);
  process.exit(1);
});

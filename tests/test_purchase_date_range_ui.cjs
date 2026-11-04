/* Run: node tests/test_purchase_date_range_ui.cjs */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../app/templates/index.html'), 'utf8');
const code = html.slice(html.indexOf('function app() {')).split('{% endraw %}')[0];
const context = { URLSearchParams, Intl, Date, console };
vm.createContext(context);
vm.runInContext(code, context); // Also verifies the complete app() JS syntax.
(async () => {
  const state = context.app();
  assert.equal(state.purchaseStartDate, '');
  assert.equal(state.purchaseEndDate, '');
  assert.equal(state.purchaseQuery().toString(), '');
  state.goTab('purchase');
  assert.equal(state.purchaseQuery().toString(), '');
  state.purchaseStartDate = '2026-08-14';
  assert.equal(state.purchaseQuery().toString(), 'start_date=2026-08-14');
  state.purchaseEndDate = '2026-08-15';
  assert.equal(state.purchaseQuery().toString(), 'start_date=2026-08-14&end_date=2026-08-15');
  state.purchaseStartDate = '';
  assert.equal(state.purchaseQuery().toString(), 'end_date=2026-08-15');
  state.purchaseStartDate = '2026-08-16';
  assert.throws(() => state.purchaseQuery(), /开始日期不能晚于结束日期/);
  let calls = 0;
  context.fetch = async () => { calls++; throw Error('should not fetch'); };
  await state.exportPurchase();
  assert.equal(calls, 0);
  assert.equal(state.purchaseBusy, false);
  assert.match(state.purchaseResult, /开始日期不能晚于结束日期/);
  state.purchaseStartDate = '2026-08-14';
  context.fetch = async (url, options) => {
    calls++;
    assert.equal(url, '/api/exports/purchase?start_date=2026-08-14&end_date=2026-08-15');
    assert.equal(options.credentials, 'include');
    return { ok: false, json: async () => ({detail: '日期无效'}) };
  };
  await state.exportPurchase();
  assert.match(state.purchaseResult, /日期无效/);
  assert.equal(state.purchaseBusy, false);
  let clicked = false;
  const anchor = {click() { clicked = true; }};
  context.document = {createElement: () => anchor, body: {appendChild() {}, removeChild() {}}};
  context.URL = {createObjectURL: () => 'blob:test', revokeObjectURL() {}};
  context.fetch = async () => ({
    ok: true,
    headers: {get: key => ({
      'X-Export-Summary': JSON.stringify({date_range: '2026-08-14 至 2026-08-15', row_count: 0}),
      'X-Export-Row-Count': '0',
    }[key] || '')},
    blob: async () => ({}),
  });
  await state.exportPurchase();
  assert.equal(clicked, true);
  assert.match(state.purchaseResult, /无采购需求/);
  assert.equal(state.purchaseSummary.date_range, '2026-08-14 至 2026-08-15');
  assert.equal(state.purchaseBusy, false);
  assert.match(html, /@media \(max-width: 600px\) \{ \.purchase-date-range \{ grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(html, /for="purchase-start-date"/);
  assert.match(html, /for="purchase-end-date"/);
  console.log('PASS: date controls, default/all, single bounds, range URL, reverse guard, server errors, empty download, mobile CSS');
})().catch(error => { console.error(error); process.exitCode = 1; });

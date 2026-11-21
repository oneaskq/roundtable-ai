const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/discussion-timing.js'), 'utf8'), {window});
test('timings skip questions, subtract the confirmed pause, and survive a reload', () => {
  const run = {workflow:'decision_v1', status:'completed', created_at:'2026-01-01T12:00:00Z',
    finished_at:'2026-01-01T12:01:16Z', decision:{clarification_wait_ms:35000}, discussion:[
      {kind:'assistant',role:'clarifier',content:'问题',created_at:'2026-01-01T12:00:03Z'},
      {kind:'assistant',role:'analysis_p1',content:'建议',created_at:'2026-01-01T12:00:46Z'}]};
  assert.deepEqual(Array.from(window.RoundtableTiming.metrics(run)), ['首条判断 11 秒','最终报告 41 秒']);
  run.status = 'paused'; run.discussion = []; assert.equal(window.RoundtableTiming.metrics(run).length, 0);
});

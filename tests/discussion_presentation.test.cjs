const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window: {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'discussion-presentation.js'), 'utf8'), context);
const {waiting, analysisPreview} = context.window.RoundtableDiscussion;

test('the wait uses running members, does not predict unstarted calls, and clears after the run', () => {
  const run = {status:'running', steps:[
    {role:'analysis_p1', provider:'DeepSeek', status:'running'},
    {role:'analysis_p2', provider:'Kimi', status:'running'},
    {role:'analysis_p3', provider:'千问', status:'pending'},
  ]};
  const name = value => value;
  assert.equal(waiting(run, false, name).text, 'DeepSeek、Kimi正在独立分析');
  run.decision = {phase:'research'};
  assert.match(waiting(run, false, name).text, /公开资料/);
  for (const status of ['awaiting_input', 'paused', 'completed', 'failed', 'needs_review']) {
    assert.equal(waiting({...run, status}, false, name).busy, false);
  }
  assert.equal(waiting(null, true, name).busy, true);
  assert.equal(waiting(null, false, name).text, '');
});

test('clarification waits only for explicitly addressed outstanding questions', () => {
  const note = {kind:'user', target_provider:'千问', status:'mention_queued'};
  const run = {status:'awaiting_input', discussion:[note]};
  assert.match(waiting(run, false, s => s).text, /千问/);
  note.status = 'mention_running';
  assert.equal(waiting(run, false, s => s).busy, true);
  note.status = 'answered';
  assert.equal(waiting(run, false, s => s).busy, false);
  note.status = 'mention_failed';
  assert.equal(waiting(run, false, s => s).busy, false);
});

test('long independent analyses quote original text, including old labeled messages', () => {
  const content = '若能每周到岗三天，建议先确认导师安排。' + '返校优先。'.repeat(60) + '\n关键主张：\n- 先问是否灵活排班。';
  const item = {kind:'assistant', role:'analysis_p1', content};
  const result = analysisPreview(item,{workflow:'decision_v1'});
  assert.deepEqual(Array.from(result), ['若能每周到岗三天，建议先确认导师安排。','先问是否灵活排班。']);
  assert.deepEqual(Array.from(analysisPreview({...item,role:undefined,label:'独立分析 · A'},{})), Array.from(result));
  for (const role of ['mention_10', 'quick_p1', 'clarifier', 'clarifier_peer','review_p1','synthesizer']) {
    assert.equal(analysisPreview({...item,role},{}),null);
  }
  assert.equal(analysisPreview(item,{decision:{response_kind:'direct_member'}}),null);
  assert.equal(analysisPreview({...item,content:'简短建议'},{}),null);
  assert.equal(analysisPreview({...item,kind:'user'},{}),null);
});

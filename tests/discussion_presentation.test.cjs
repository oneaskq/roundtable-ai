const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window: {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'discussion-presentation.js'), 'utf8'), context);
const {waiting, turnPresentation} = context.window.RoundtableDiscussion;

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

test('reviews and responses show the reason and its source once, keeping the original in the record', () => {
  const reason = '这要看导师周五是否值班。' + '只有满足你的课表才能选。'.repeat(7);
  const item = {kind:'assistant', content: '核验记录\n- ' + reason + '\n- 另一条有条件的意见。',
    turn: {text: reason, detail_label:'完整核验（2 条）', reply_to:{kind:'member',label:'Kimi',text:'可以问能不能周五到岗。'}}};
  for (const role of ['review_p1','response_p2','extra_review','extra_response']) {
    const result = turnPresentation({...item, role});
    assert.equal(result.reference.label, '回应 Kimi');
    assert.equal(result.reference.text, item.turn.reply_to.text);
    assert.equal(result.text, reason);
    assert.equal(result.detail, '');
  }
  assert.ok(item.content.includes('另一条有条件的意见'));
  assert.equal(turnPresentation({...item,kind:'user'}), null);
  assert.equal(turnPresentation({...item,turn:null}), null);
});

test('a direct response stands alone while other stage notes remain expandable', () => {
  const text = '按你刚说的时间，可以先选周五。';
  const turn = {text, detail_label:'完整发言', reply_to:{kind:'user',label:'你',text:'我只有周五有空。'}};
  const item = {kind:'assistant', turn, content:text};
  assert.equal(turnPresentation(item).detail, '');
  assert.equal(turnPresentation(item).reference.label, '回复你');
  assert.equal(turnPresentation({...item,content:text+'\n\n核验记录'}).detail, '核验记录');
  assert.equal(turnPresentation({...item,turn:{text:'',detail_label:'查看原文'}}).detail, text);
});

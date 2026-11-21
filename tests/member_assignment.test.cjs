const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('chat attributes each analysis to its actual assignment and handles old rounds', () => {
  const app = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
  const source = app.slice(app.indexOf('function teamChatMessage('), app.indexOf('function teamChatHistory('));
  function el(tag, props, children = []) {
    return {tag, ...props, children: children.filter(Boolean), append() {}, addEventListener() {}};
  }
  const ctx = {el, providerName: x => x, dateText: () => '', teamChatAvatar: () => el('aside', {}),
    messageIcon: () => el('svg', {}), window: {}, document: {createTextNode: x => x}};
  vm.runInNewContext(source, ctx);
  const run = {decision: {participants: [
    {id: 'p2', assignment: {label: '成本与投入', task: '核对机会成本'}},
    {id: 'p1', assignment: {label: '成长机会', task: '比较学习机会', focus: '导师反馈'}},
  ]}};
  const first = {kind: 'assistant', role: 'analysis_p1', provider: 'A'};
  const second = {kind: 'assistant', role: 'analysis_p2', provider: 'B'};
  const meta = (item, round, members = [item]) =>
    ctx.teamChatMessage(item, round, '', members).children[1].children[0].children;
  assert.equal(meta(first, run)[1].text, '成长机会');
  assert.match(meta(first, run)[0].title, /比较学习机会.*导师反馈/);
  assert.equal(meta(first, run, [first, second])[0].text, 'A（成长机会）、B（成本与投入）');
  assert.equal(meta(first, {})[0].text, 'A');
  assert.equal(meta(first, {}).length, 1);
  assert.equal(meta({...first, role: 'review_p1'}, run).length, 1);
});

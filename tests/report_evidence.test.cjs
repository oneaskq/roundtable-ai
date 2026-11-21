const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = name => fs.readFileSync(path.join(__dirname, '..', 'static', name), 'utf8');
class Element {
  constructor(tag, text = '') { this.tag = tag; this.textContent = text; this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this[key] = value; }
  find(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.find(tag)]); }
}
const window = {};
const document = {createElement: tag => new Element(tag), createTextNode: text => new Element('text', text),
  createDocumentFragment: () => new Element('fragment')};
vm.runInNewContext(read('message-format.js'), {document, window, URL});
const render = window.RoundtableMessage.render;
const state = {
  evidence: [
    {id: 'E1', type: 'file', name: '报价单', excerpt: '<b>仅六个月</b>', scope: '适用期'},
    {id: 'E2', type: 'calculation', status: 'executed', expression: '100*6', result: '600'},
  ],
  report_references: [
    {section: '主要依据', line: '六个月的报价', ids: ['E1', 'E2']},
    {section: '重要分歧与核验', line: '计算有误', ids: ['E2'], legacy_match: true},
  ],
};

test('a cited claim exposes the original excerpt and the actual calculation in place', () => {
  const tree = render('## 主要依据\n- 六个月的报价（依据 E1、E2）', state);
  assert.deepEqual(tree.find('summary').map(el => el.textContent), ['报价单', '计算过程']);
  assert.equal(tree.find('blockquote')[0].textContent, '<b>仅六个月</b>');
  assert.equal(tree.find('b').length, 0);
  assert.equal(tree.find('code')[0].textContent, '100*6 = 600');
  assert.equal(tree.find('details').every(el => !el.open), true);
});

test('a retrieved web citation exposes its checked excerpt, retrieval date and original page', () => {
  const evidence = [{id: 'E1', type: 'web', name: '公开条目', excerpt: '原文摘录',
    retrieved_at: '2026-01-01T00:00:00+00:00', attribution: '条目作者', url: 'https://example.org/page'}];
  const report_references = [{section: '主要依据', line: '有原文', ids: ['E1']}];
  const tree = render('## 主要依据\n- 有原文（依据 E1）', {evidence, report_references});
  assert.equal(tree.find('blockquote')[0].textContent, '原文摘录');
  assert.ok(tree.find('small')[0].textContent.includes('读取时间'));
  assert.equal(tree.find('a')[0].href, 'https://example.org/page');
});

test('only an unambiguous saved finding matches a legacy line without IDs', () => {
  const old = '## 重要分歧与核验\n- 计算有误\n  回应：接受纠错';
  assert.equal(render(old, state).find('details').length, 1);
  const conflicting = {...state, report_references: [{...state.report_references[1], legacy_match: false}]};
  assert.equal(render(old, conflicting).find('details').length, 0);
  assert.equal(render(old.replace('计算有误', '另一项计算'), state).find('details').length, 0);
});

test('a multi-line claim must match through its last line', () => {
  const row = {section: '主要依据', line: '总价已列明\n包含六个月', ids: ['E1']};
  const saved = {...state, report_references: [row]};
  const quoted = '## 主要依据\n- 总价已列明\n包含六个月（依据 E1）';
  assert.equal(render(quoted, saved).find('details').length, 1);
  assert.equal(render(quoted.replace('六个月', '一年'), saved).find('details').length, 0);
});

test('other sections, unknown IDs, ordinary replies and unexecuted calculations never gain a source', () => {
  const line = '## 主要依据\n- 六个月的报价（依据 E1、E2）';
  for (const tree of [render(line.replace('主要依据', '下一步'), state), render(line.replace('E2', 'E9'), state),
    render(line), render(line, {...state, evidence: [state.evidence[0], {...state.evidence[1], status: 'not_run'}]})]) {
    assert.equal(tree.find('details').length, 0);
  }
});

test('the timeline groups only adjacent equivalent contributions and preserves an interjection', () => {
  const app = read('app.js');
  const controller = app.slice(app.indexOf('function renderDiscussionTimeline('), app.indexOf('function teamChatMessage('));
  const ctx = {teamChatMessage: (item, run, label, members) => [label, members.map(m => m.role).join(',')]};
  vm.runInNewContext(controller, ctx);
  const run = {id: 'round', steps: [{role:'analysis_p1', output:'same'}, {role:'analysis_p2', output:'same'},
    {role:'analysis_p3', output:'other source'}]};
  const turn = role => ({run, item: {role, content:'相同可见文本'}});
  const result = ctx.renderDiscussionTimeline([turn('analysis_p1'), turn('analysis_p2'), turn('analysis_p3'),
    {node: '补充'}, turn('analysis_p1')]);
  assert.deepEqual(Array.from(result, row => typeof row === 'string' ? row : Array.from(row)),
    [['独立分析','analysis_p1,analysis_p2'], ['', 'analysis_p3'], '补充', ['独立分析','analysis_p1']]);
});

test('completed reviews use their checked claim and response identity rather than visible text or raw claim IDs', () => {
  const app = read('app.js');
  const controller = app.slice(app.indexOf('function renderDiscussionTimeline('), app.indexOf('function teamChatMessage('));
  const ctx = {teamChatMessage: (item, run, label, members) => members.map(m => m.role).join(',')};
  vm.runInNewContext(controller, ctx);
  const run = {id:'offers', steps:[]};
  const review = (n, equivalence) => ({run, item:{role:'review_p' + n, content:'岗位职责可从乙的录用通知中核对。', equivalence}});
  const same = [review(1, 'same'),review(2, 'same'),review(3, 'same')];
  assert.deepEqual(Array.from(ctx.renderDiscussionTimeline(same)), ['review_p1,review_p2,review_p3']);
  for (const changed of ['different', undefined]) {
    assert.deepEqual(Array.from(ctx.renderDiscussionTimeline([same[0],review(2, changed),same[2]])),
      ['review_p1','review_p2','review_p3']);
  }
});

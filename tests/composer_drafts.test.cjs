// Exercises the actual browser draft controller with a synthetic in-memory API.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const start = source.indexOf('function pendingDraftKey(');
const end = source.indexOf('function resizeComposer()', start);
if (start < 0 || end < 0) throw new Error('Draft controller was not found.');
const controller = source.slice(start, end);

function storage() {
  const data = new Map();
  return { get length() { return data.size; }, key(index) { return [...data.keys()][index] ?? null; },
    getItem(key) { return data.get(key) ?? null; }, setItem(key, value) { data.set(key, String(value)); },
    removeItem(key) { data.delete(key); } };
}

function fixture({ sharedStorage = storage(), rows = new Map(), deferred = false, network = {} } = {}) {
  const pending = [];
  const nodes = {'#message-input': {value: '', focus() {}}, '#composer-project': {value: ''},
    '#draft-conflict': {hidden: true}, '#draft-conflict-preview': {}, '#draft-status': {textContent: '', hidden: true}};
  function apply(body) {
    const key = body.conversation_id || 'new'; const old = rows.get(key);
    if (old && old.content === body.content && old.project_id === body.project_id) return {saved: true, revision: old.updated_at};
    if ((body.base_revision ?? null) !== (old?.updated_at ?? null)) {
      throw Object.assign(new Error('conflict'), {code: 'draft_conflict', draft: old});
    }
    const revision = String(Number(old?.updated_at || 0) + 1);
    rows.set(key, {...body, updated_at: revision}); return {saved: true, revision};
  }
  async function api(_url, options = {}) {
    if (network.offline) throw Error('offline');
    if (options.method !== 'POST') return {drafts: [...rows.values()]};
    if (deferred) return new Promise((resolve, reject) => pending.push(() => {
      try { resolve(apply(options.body)); } catch (error) { reject(error); }
    }));
    const saved = apply(options.body);
    if (network.loseAck) { network.loseAck = false; throw Error('response lost'); }
    return saved;
  }
  const state = {user: {id: 'synthetic-user'}, conversation: null, projects: [], draftsReady: true,
    drafts: new Map(), draftWriter: randomUUID(), draftSequence: 0, draftConflicts: new Map(),
    draftRevisions: new Map(), draftSaving: new Map(), draftPending: new Map(), draftTimers: new Map()};
  const context = vm.createContext({state, sessionStorage: sharedStorage, api,
    renderModelControls() {}, restoreDraft() { nodes['#message-input'].value = state.drafts.get('new')?.text || ''; },
    $: (selector) => nodes[selector], setTimeout: () => 1, clearTimeout: () => {},
    JSON, Map, Promise, Math, Number});
  vm.runInContext(controller, context);
  return {context, state, nodes, rows, pending, sharedStorage};
}

test('typing while a save is in flight records the latest draft against the acknowledged revision', async () => {
  const f = fixture({deferred: true});
  f.nodes['#message-input'].value = '第一个'; f.context.rememberDraft();
  const first = f.context.flushDraft('new');
  f.nodes['#message-input'].value = '最后一个'; f.context.rememberDraft();
  const second = f.context.flushDraft('new');
  assert.equal(f.pending.length, 1);
  f.pending[0](); await new Promise(resolve => setImmediate(resolve)); assert.equal(f.pending.length, 2);
  f.pending[1](); await Promise.all([first, second]);
  assert.equal(f.rows.get('new').content, '最后一个');
  assert.equal(f.state.draftPending.size, 0);
});

test('refresh replays an unacknowledged edit before restoring drafts', async () => {
  const first = fixture();
  first.nodes['#message-input'].value = '刷新前未提交的文字'; first.context.rememberDraft();
  const refreshed = fixture({sharedStorage: first.sharedStorage, rows: first.rows});
  await refreshed.context.loadDrafts();
  assert.equal(refreshed.state.drafts.get('new').text, '刷新前未提交的文字');
  assert.equal(refreshed.rows.get('new').content, '刷新前未提交的文字');
});

test('a successful submission only clears the text that was actually sent', async () => {
  const f = fixture();
  f.nodes['#message-input'].value = '原内容'; f.context.rememberDraft();
  f.nodes['#message-input'].value = '继续输入的新内容'; f.context.rememberDraft();
  f.context.forgetDraft('new', '原内容');
  assert.equal(f.state.drafts.get('new').text, '继续输入的新内容');
  f.context.forgetDraft('new', '继续输入的新内容');
  await f.context.flushDraft('new');
  assert.equal(f.rows.get('new').content, '');
});

test('conflicting drafts survive a refresh and can be explicitly resolved using either version', async () => {
  for (const useLocal of [false, true]) {
    const a = fixture();
    const b = fixture({rows: a.rows});
    a.nodes['#message-input'].value = 'A 待保存'; a.context.rememberDraft();
    b.nodes['#message-input'].value = 'B 已保存'; b.context.rememberDraft();
    await b.context.flushDraft('new'); await a.context.flushDraft('new');
    assert.equal(a.rows.get('new').content, 'B 已保存');
    assert.equal(a.nodes['#draft-conflict'].hidden, false);
    assert.match(a.nodes['#draft-conflict-preview'].textContent, /B 已保存/);
    a.nodes['#message-input'].value = 'A 修改后'; a.context.rememberDraft();
    const refreshed = fixture({rows: a.rows, sharedStorage: a.sharedStorage});
    await refreshed.context.loadDrafts();
    assert.equal(refreshed.state.drafts.get('new').text, 'A 修改后');
    assert.equal(refreshed.state.draftConflicts.get('new').content, 'B 已保存');
    refreshed.context.resolveDraftConflict(useLocal);
    await refreshed.context.flushDraft('new');
    assert.equal(a.rows.get('new').content, useLocal ? 'A 修改后' : 'B 已保存');
    assert.equal(refreshed.state.drafts.get('new').text, a.rows.get('new').content);
    assert.equal(refreshed.state.draftPending.size, 0);
  }
});

test('legacy pending text survives refresh when another page has a different server version', async () => {
  const f = fixture();
  f.nodes['#message-input'].value = '已保存'; f.context.rememberDraft(); await f.context.flushDraft('new');
  f.sharedStorage.setItem('roundtable:pending-draft:synthetic-user:new', JSON.stringify({
    content: '旧页面未确认', conversation_id: null, project_id: null, sequence: 2, writer_id: f.state.draftWriter
  }));
  const refreshed = fixture({rows: f.rows, sharedStorage: f.sharedStorage});
  await refreshed.context.loadDrafts();
  assert.equal(f.rows.get('new').content, '已保存');
  assert.equal(refreshed.state.drafts.get('new').text, '旧页面未确认');
  assert.equal(refreshed.state.draftConflicts.get('new').content, '已保存');
});

test('an offline refresh restores this page’s pending text and checks its revision at reconnection', async () => {
  const a = fixture();
  a.nodes['#message-input'].value = '离线编辑'; a.context.rememberDraft();
  const network = {offline: true};
  const refreshed = fixture({rows: a.rows, sharedStorage: a.sharedStorage, network});
  await refreshed.context.loadDrafts();
  assert.equal(refreshed.state.drafts.get('new').text, '离线编辑');
  const b = fixture({rows: a.rows});
  b.nodes['#message-input'].value = '其他页面在线编辑'; b.context.rememberDraft();
  await b.context.flushDraft('new');
  network.offline = false;
  await refreshed.context.flushDraft('new');
  assert.equal(refreshed.state.draftConflicts.get('new').content, '其他页面在线编辑');
  assert.equal(refreshed.state.drafts.get('new').text, '离线编辑');
});

test('a newer edit can continue after a lost acknowledgement for the same page’s prior save', async () => {
  const f = fixture({network: {loseAck: true}});
  f.nodes['#message-input'].value = '第一段'; f.context.rememberDraft(); await f.context.flushDraft('new');
  assert.equal(f.state.draftPending.size, 1);
  f.nodes['#message-input'].value = '第一段和第二段'; f.context.rememberDraft();
  await f.context.flushDraft('new'); await f.context.flushDraft('new');
  assert.equal(f.state.draftConflicts.size, 0);
  assert.equal(f.state.draftPending.size, 0);
  assert.equal(f.rows.get('new').content, '第一段和第二段');
});

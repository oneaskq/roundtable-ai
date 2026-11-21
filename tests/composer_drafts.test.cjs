// Exercises the actual browser draft controller with a synthetic in-memory API.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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

function fixture({ sharedStorage = storage(), rows = new Map(), deferred = false } = {}) {
  const pending = [];
  const nodes = {'#message-input': {value: ''}, '#composer-project': {value: ''},
                 '#draft-status': {textContent: '', hidden: true}};
  function apply(body) {
    const key = body.conversation_id || 'new'; const old = rows.get(key);
    if (old && old.writer_id === body.writer_id && body.sequence <= old.sequence)
      return {saved: false, stale: true, sequence: old.sequence};
    rows.set(key, {...body}); return {saved: true};
  }
  async function api(_url, options = {}) {
    if (options.method !== 'POST') return {drafts: [...rows.values()].filter((row) => row.content).map((row) => ({
      conversation_id: row.conversation_id, project_id: row.project_id, content: row.content}))};
    if (deferred) return new Promise((resolve) => pending.push(() => resolve(apply(options.body))));
    return apply(options.body);
  }
  const state = {user: {id: 'synthetic-user'}, conversation: null, draftsReady: true,
    drafts: new Map(), draftWriter: '01234567-89ab-4cde-8fab-0123456789ab',
    draftSequence: new Map(), draftPending: new Map(), draftTimers: new Map()};
  const context = vm.createContext({state, sessionStorage: sharedStorage, api,
    $: (selector) => nodes[selector], setTimeout: () => 1, clearTimeout: () => {},
    JSON, Map, Promise, Math, Number});
  vm.runInContext(controller, context);
  return {context, state, nodes, rows, pending, sharedStorage};
}

test('out-of-order saves retain the latest text', async () => {
  const f = fixture({deferred: true});
  f.nodes['#message-input'].value = '第一个'; f.context.rememberDraft();
  const first = f.context.flushDraft('new');
  f.nodes['#message-input'].value = '最后一个'; f.context.rememberDraft();
  const second = f.context.flushDraft('new');
  f.pending[1](); await second; f.pending[0](); await first;
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

test('a cloned tab rebases an older sequence instead of silently losing input', async () => {
  const f = fixture();
  f.rows.set('new', {conversation_id: null, project_id: null, content: '另一个标签的内容',
    writer_id: f.state.draftWriter, sequence: 5});
  f.nodes['#message-input'].value = '此标签的新文字'; f.context.rememberDraft();
  await f.context.flushDraft('new');
  assert.equal(f.state.draftPending.get('new').payload.sequence, 6);
  await f.context.flushDraft('new');
  assert.equal(f.rows.get('new').content, '此标签的新文字');
});

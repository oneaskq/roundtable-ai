const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const getRun = script.slice(script.indexOf('function interjectionRun('), script.indexOf('function renderInterjectionControls('));
const send = script.slice(script.indexOf('async function sendInterjection('), script.indexOf('function decisionRecord('));
const clarify = script.slice(script.indexOf('async function submitClarification('), script.indexOf('async function requestReply('));

function setup(store = new Map()) {
  const run = {id: 'round', status: 'running', workflow: 'decision_v1'};
  const input = {value: '补充条件'};
  const target = {value: ''};
  const state = {user: {id: 'u'}, view: 'roundtable', authVersion: 1, viewVersion: 1, conversation: {id: 'chat'},
    teamChatDrafts: new Map(), teamChatSending: new Map(), drafts: new Map(), replyFeedback: {}};
  const calls = [];
  let finish;
  let serial = 0;
  const ctx = vm.createContext({state, AbortSignal, currentTask: () => ({runs: [run]}), allTeamRuns: () => [run],
    sessionStorage: {getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key)},
    $: (selector) => selector === '#message-input' ? input : target,
    crypto: {randomUUID: () => String(++serial)},
    renderInterjectionControls() {}, renderModelControls() {}, rememberDraft() {}, forgetDraft() {},
    resizeComposer() {}, showToast() {}, reloadConversation: async () => {},
    api: (url, args) => { calls.push({url, body: args.body}); return new Promise(resolve => { finish = resolve; }); }
  });
  vm.runInContext(getRun + send + clarify, ctx);
  return {ctx, run, input, state, target, calls, ack: () => finish({accepted: true})};
}

test('live composer sends once to the existing round and keeps newly typed text', async () => {
  const env = setup();
  const run = env.ctx.interjectionRun();
  const first = env.ctx.sendInterjection(run, env.input.value);
  await env.ctx.sendInterjection(run, env.input.value);
  assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].url, '/api/conversations/chat/team-messages');
  assert.equal(env.calls[0].body.run_id, 'round');
  env.input.value = '下一条未发送的补充';
  env.ack(); await first;
  assert.equal(env.input.value, '下一条未发送的补充');
});

test('draft begun during discussion stays an interjection if the report just arrived', () => {
  const {ctx, run, state, input} = setup();
  ctx.saveInterjectionDraft('chat', {runId: run.id, target: '', text: input.value});
  run.status = 'completed';
  assert.equal(ctx.interjectionRun(), run);
  state.conversation = {id: 'another-chat'};
  assert.equal(ctx.interjectionRun(), null);
  state.conversation = {id: 'chat'};
  run.status = 'awaiting_input';
  assert.equal(ctx.interjectionRun(), null);
  run.status = 'completed'; input.value = '';
  assert.equal(ctx.interjectionRun(), null);
});

test('refresh after lost acknowledgement preserves the request, recipient and original run', async () => {
  const store = new Map();
  const first = setup(store); first.target.value = 'kimi';
  void first.ctx.sendInterjection(first.run, first.input.value);
  const savedId = first.calls[0].body.client_request_id;
  const fresh = setup(store);
  fresh.run.status = 'completed';
  assert.equal(fresh.ctx.interjectionRun(), fresh.run);
  // A pending replay retains the recipient even while capabilities reload.
  fresh.target.value = '';
  const replay = fresh.ctx.sendInterjection(fresh.run, fresh.input.value);
  assert.equal(fresh.calls[0].body.target_provider, 'kimi');
  assert.equal(fresh.calls[0].body.client_request_id, savedId);
  fresh.ack(); await replay;
  assert.equal(fresh.input.value, '');
  assert.equal(store.size, 0);
});

test('a lost interjection acknowledgement never becomes a clarification answer', () => {
  const {ctx, run, input} = setup();
  ctx.saveInterjectionDraft('chat', {runId: run.id, target: '', text: input.value, receipt: 'pending'});
  run.status = 'awaiting_input';
  assert.equal(ctx.interjectionRun(), run);
});

test('replaying a clarification after reload uses the original confirmation endpoint and run', async () => {
  const store = new Map();
  const old = setup(store); old.run.status = 'awaiting_input'; old.run.max_calls = 14;
  void old.ctx.submitClarification(old.input.value);
  const fresh = setup(store);
  fresh.run.status = 'completed';
  assert.equal(fresh.ctx.interjectionRun(), null);
  const saved = fresh.ctx.interjectionDraft();
  assert.equal(saved.kind, 'clarification');
  const retry = fresh.ctx.submitClarification(fresh.input.value, {id: saved.runId, max_calls: saved.maxCalls});
  assert.deepEqual({...fresh.calls[0].body}, {...old.calls[0].body});
  assert.equal(fresh.calls[0].url, '/api/conversations/chat/confirm-decision');
  fresh.ack(); await retry;
  assert.equal(store.size, 0);
});

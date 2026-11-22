const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const getRun = script.slice(script.indexOf('function interjectionRun('), script.indexOf('function renderInterjectionControls('));
const send = script.slice(script.indexOf('async function sendInterjection('), script.indexOf('function decisionRecord('));
const clarify = script.slice(script.indexOf('async function submitClarification('), script.indexOf('async function requestReply('));
const lookupWait = script.slice(script.indexOf('function lookupOnlyWait('), script.indexOf('function monitoringCurrent('));
const monitor = script.slice(script.indexOf('function resumeReplyMonitoring('), script.indexOf('function replyContextCurrent('));

function setup(store = new Map()) {
  const run = {id: 'round', status: 'running', workflow: 'decision_v1'};
  const input = {value: '补充条件', focus() { this.focused = true; }};
  const target = {value: '', options: [{value: ''}, {value: 'kimi'}, {value: 'qwen'}]};
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
    pendingSubmission: () => null, memberReplyPending: () => false,
    api: (url, args) => { calls.push({url, body: args.body}); return new Promise(resolve => { finish = resolve; }); }
  });
  vm.runInContext(getRun + send + clarify + lookupWait + monitor, ctx);
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

test('replying to a published remark keeps the draft and sends its real reference only on submit', async () => {
  const store = new Map();
  const env = setup(store);
  env.ctx.selectMemberReply(env.run.id, 'kimi', 'published-step');
  assert.equal(env.calls.length, 0);
  assert.equal(env.input.focused, true);
  assert.equal(env.target.value, 'kimi');
  assert.equal(env.input.value, '补充条件');
  void env.ctx.sendInterjection(env.run, env.input.value);
  const request = env.calls[0].body;
  assert.equal(request.reply_to_step, 'published-step');
  assert.equal(request.target_provider, 'kimi');
  const fresh = setup(store);
  fresh.ctx.selectMemberReply(fresh.run.id, 'qwen', 'different-step');
  const retry = fresh.ctx.sendInterjection(fresh.run, fresh.input.value);
  assert.deepEqual({...fresh.calls[0].body}, {...request});
  fresh.ack(); await retry;
});

test('stale and unavailable reply buttons cannot point the draft at a different round or member', () => {
  const env = setup();
  env.ctx.selectMemberReply('older-round', 'kimi', 'older-step');
  env.ctx.selectMemberReply(env.run.id, 'unavailable', 'wrong-step');
  env.run.status = 'completed';
  env.ctx.selectMemberReply(env.run.id, 'kimi', 'old-step');
  assert.equal(env.ctx.interjectionDraft(), null);
  assert.equal(env.target.value, '');
  assert.equal(env.calls.length, 0);
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
  assert.equal(old.calls[0].body.auto_public_search, true);
  assert.deepEqual({...fresh.calls[0].body}, {...old.calls[0].body});
  assert.equal(fresh.calls[0].url, '/api/conversations/chat/confirm-decision');
  fresh.ack(); await retry;
  assert.equal(store.size, 0);
});

test('opening an old lookup-only wait resumes once and leaves an unsent answer untouched', () => {
  const {ctx, run, input, calls, ack} = setup();
  run.status = 'awaiting_input';
  run.max_calls = 14;
  run.decision = {brief_draft: {public_search_query: '公开岗位要求', questions: []}};
  ctx.resumeReplyMonitoring();
  ctx.resumeReplyMonitoring();
  assert.equal(calls.length, 1);
  assert.deepEqual({...calls[0].body}, {run_id: run.id, answer: '', confirm_continue: true,
    expected_max_calls: 14, auto_public_search: true, resume_lookup_only: true});
  assert.equal(input.value, '补充条件');
  ack();
});

test('a real question, a pending team message, or an unacknowledged answer prevents automatic resume', () => {
  const {ctx, run, calls} = setup();
  run.status = 'awaiting_input';
  run.decision = {brief_draft: {public_search_requested: true, questions: ['什么时候决定？']}};
  ctx.resumeReplyMonitoring();
  run.decision.brief_draft.questions = [];
  run.discussion = [{kind: 'user', status: 'mention_queued'}];
  ctx.resumeReplyMonitoring();
  run.discussion = [];
  ctx.saveInterjectionDraft('chat', {runId: run.id, kind: 'clarification', text: '一周之内', receipt: 'old-request'});
  ctx.resumeReplyMonitoring();
  assert.deepEqual(calls, []);
});

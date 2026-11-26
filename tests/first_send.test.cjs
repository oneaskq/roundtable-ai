// Exercise the actual consent and send handlers, including lost acknowledgements.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const fs = require('node:fs');
const path = require('node:path');
const app = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
function section(start, end) {
  const from = app.indexOf(start), to = app.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from);
  return app.slice(from, to);
}
function setup({loseAck = false, beforeResponse, formatCheck = () => ({requested_formats: []})} = {}) {
  const nodes = new Map(), storage = new Map(), posts = [], runs = [], acceptedGoals = [];
  const state = {user:{id:'user1'}, view:'roundtable', viewVersion:1, authVersion:1, messages:[], tasks:[],
    messageSaveIds:new Map(), drafts:new Map(), replyFeedback:{}, conversation:null, aiConsentFor:null};
  state.capabilities = {team:{revision:'team', default_member_ids:['a','b'],
    members:[{id:'a',ready:true},{id:'b',ready:true}]}};
  function $(key) {
    if (!nodes.has(key)) nodes.set(key, {value:'', listeners:{}, focus(){},
      addEventListener(type, callback) { this.listeners[type] = callback; },
      requestSubmit() { return submit(); }});
    return nodes.get(key);
  }
  let requested;
  const submit = async (id = '') => $('#composer').listeners.submit({preventDefault(){},submitter:{id}});
  $('#composer').requestSubmit = () => { requested = submit(); };
  const noop = () => {};
  const c = vm.createContext({state, $, crypto, TextEncoder, Uint8Array, window: {},
    sessionStorage:{getItem:k=>storage.get(k) || null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    requestReply: (_reuse, goal) => { runs.push(state.conversation?.id); acceptedGoals.push(goal); }, interjectionRun:()=>null, interjectionDraft:()=>null, pendingSubmission:()=>null,
    currentTask:()=>state.tasks[0], allTeamRuns:()=>state.tasks.flatMap(task=>task.runs || []),
    modelIdentity:()=>[state.deliveryMode, state.conversation?.sources_revision || 'team'].join(':'),
    sourcesUI:{getState:()=>({busy:false})}, modelName:()=>'Test A、Test B', deliveryLimit:()=>14,
    renderChat:noop,renderModelControls:noop,resizeComposer:noop,rememberConversation:noop,
    rememberDraft:noop,forgetDraft:noop,reloadConversation:async()=>{},refreshWorkspace:async()=>{},showToast:noop,
    api:async(url, opt)=>{
      if (url === '/api/delivery-format') return formatCheck(opt.body.content);
      posts.push({url,body:opt.body});
      if (loseAck && posts.length === 1) throw new Error('lost response');
      if (beforeResponse) await beforeResponse();
      return {conversation: {id: state.conversation?.id || opt.body.client_message_id, sources_revision:state.conversation?.sources_revision},
        message:{id:opt.body.client_message_id},task:{id:'t',status:'waiting',goal:opt.body.content}};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/delivery-intent.js'), 'utf8'), c);
  vm.runInContext(section('function deliveryLimit(', 'function providerName('), c);
  vm.runInContext(section('function hasTeamConsent()', 'function renderParticipantOptions('), c);
  vm.runInContext(section('function closeAiConsent()', 'function pendingDraftKey('), c);
  vm.runInContext(section('function promptAIConsent(', '$("#refresh-connection").addEventListener'), c);
  vm.runInContext(section('$("#request-reply").addEventListener', '$("#skip-clarification").addEventListener'), c);
  vm.runInContext(section('$("#composer").addEventListener("submit"', '$("#pause-task").addEventListener'), c);
  const pagehide = app.indexOf('window.addEventListener("pagehide"');
  vm.runInContext(app.slice(app.lastIndexOf('$("#message-input").addEventListener("input"', pagehide), pagehide), c);
  const confirm = async () => { $('#ai-consent-confirm').listeners.click(); await requested; };
  $('#message-input').value = '比较两份实习说明';
  return {state,$,posts,runs,acceptedGoals,submit,confirm,context:c};
}
const workbookGoal = '生成一份 XLSX 工作簿';
const csvOffer = {requested_formats:['XLSX'], replacement_formats:['CSV'], replacement_goal:'生成一份 CSV 表格'};

test('an unavailable output requires an explicit alternative and fresh cost consent', async () => {
  const f = setup({formatCheck: content => { assert.equal(content, workbookGoal); return csvOffer; }});
  f.state.deliveryMode = 'files';
  f.state.aiConsentFor = 'files:team';  // Prior consent to a different file request.
  f.$('#message-input').value = workbookGoal;
  await f.submit();
  assert.equal(f.$('#ai-consent-confirm').textContent, '改用 CSV');
  assert.match(f.$('#ai-consent-text').textContent, /XLSX/);
  f.$('#ai-consent-cancel').listeners.click();
  assert.equal(f.$('#message-input').value, workbookGoal);
  assert.deepEqual(f.posts, []);
  await f.submit();
  await f.confirm();
  assert.deepEqual(f.posts, []);
  assert.deepEqual(f.runs, []);
  assert.match(f.$('#ai-consent-text').textContent, /已选择交付 CSV[\s\S]*10 次/);
  await f.confirm();
  assert.equal(f.posts[0].body.content, workbookGoal);
  assert.deepEqual(f.acceptedGoals, [csvOffer.replacement_goal]);
  assert.equal(f.runs.length, 1);
});

test('a saved request keeps its original message, and checking an input file allows ordinary discussion', async () => {
  const f = setup({formatCheck: content => content === workbookGoal ? csvOffer : {requested_formats:[]}});
  f.state.conversation = {id:'saved'};
  f.state.tasks = [{id:'t', goal:workbookGoal, status:'waiting'}];
  await f.$('#request-reply').listeners.click();
  assert.equal(f.$('#ai-consent-confirm').textContent, '改用 CSV');
  await f.confirm();
  await f.confirm();
  assert.deepEqual(f.posts, []);
  assert.deepEqual(f.acceptedGoals, [csvOffer.replacement_goal]);
  f.$('#message-input').value = '分析上传的 PDF';
  await f.submit();
  assert.equal(f.state.deliveryMode, 'text');
  assert.match(f.$('#ai-consent-text').textContent, /14 次/);
});

test('a late format check cannot act on a changed question or an unrelated conversation', async () => {
  for (const change of ['text','conversation']) {
    let resolve;
    const f = setup({formatCheck: () => new Promise(done => {resolve = done;})});
    f.$('#message-input').value = workbookGoal;
    const checking = f.submit();
    if (change === 'text') f.$('#message-input').value = '新问题';
    else f.state.conversation = {id:'different'};
    resolve(csvOffer);
    await checking;
    assert.ok(!f.state.pendingConsent);
    assert.deepEqual(f.runs, []);
    assert.deepEqual(f.posts, []);
  }
});

test('retry carries the exact chosen format; a pre-upgrade receipt rejected for its format is released', async () => {
  for (const accepted of [true, false]) {
    const f = setup();
    f.state.conversation = {id:'saved'};
    f.state.tasks = [{id:'t', goal:workbookGoal, status:'waiting'}];
    const body = {client_request_id:'same-request', expected_task:'t',
      ...(accepted ? {accepted_delivery_goal:csvOffer.replacement_goal} : {})};
    let pending = {id:'same-request', body};
    const submissions = [];
    Object.assign(f.context, {
      AbortController, setTimeout, clearTimeout,
      pendingSubmission: () => pending,
      forgetSubmission: () => { pending = null; },
      clearReplyMonitoring() {}, replyContextCurrent: () => true, scheduleReplyPoll() {},
      renderReplyFeedback() {},
      api: async (_url, options) => {
        submissions.push(options.body);
        if (!accepted) throw Object.assign(new Error('请选择交付格式'), {status:422, code:'unsupported_delivery_format'});
        return {queued:true, run:{id:'run'}, task:{id:'t'}};
      },
    });
    vm.runInContext(section('async function requestReply(', 'async function checkPendingSubmission('), f.context);
    await f.context.requestReply(true);
    assert.deepEqual(submissions, [body]);
    assert.equal(pending, null);
  }
});
test('a receipt after a finished recommendation saves immediately without team consent or a new run', async () => {
  const f = setup();
  f.state.conversation = {id:'completed-chat'};
  f.state.messages = [{id:'report',role:'assistant',content:'先确认到货日期。'}];
  f.state.tasks = [{id:'done',status:'completed',runs:[{decision:{result_message_id:'report',acknowledgements:['可以的']}}]}];
  f.$('#message-input').value = '可以的。';
  await f.submit();
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].body.content, '可以的。');
  assert.deepEqual(f.runs, []);
});

test('cancel preserves question and attachments; save only never starts a run', async () => {
  const f = setup();
  f.state.conversation = {id:'with-materials',sources_revision:'rev1',included_sources_count:2};
  await f.submit();
  assert.match(f.$('#ai-consent-text').textContent, /2 份材料/);
  f.$('#ai-consent-cancel').listeners.click();
  assert.equal(f.$('#message-input').value, '比较两份实习说明');
  assert.equal(f.posts.length, 0);
  assert.equal(f.state.conversation.included_sources_count, 2);
  await f.submit();
  await f.submit('ai-consent-save');
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].url, '/api/conversations/with-materials');
  assert.deepEqual(f.runs, []);
});
test('asking for a downloadable file in chat uses file collaboration and its cost before sending', async () => {
  const f = setup();
  f.$('#message-input').value = '把计划导出为一份 CSV 文件';
  await f.submit();
  assert.equal(f.state.deliveryMode, 'files');
  assert.equal(f.posts.length, 0);
  assert.match(f.$('#ai-consent-text').textContent, /可下载的文件.*10 次/);
  await f.confirm();
  assert.deepEqual(f.runs, [f.posts[0].body.client_message_id]);

  f.$('#message-input').value = '解释一下刚刚这个 CSV 里的两列是什么意思';
  await f.submit();
  assert.equal(f.state.deliveryMode, 'text');
  assert.match(f.$('#ai-consent-text').textContent, /14 次/);
  assert.equal(f.posts.length, 1);
});
test('an exhausted hour saves the question once and a later explicit start uses that same question', async () => {
  const f = setup();
  f.state.capabilities.hourly_budget = {remaining:0, resets_at:new Date(Date.now() + 60000).toISOString()};
  await f.submit();
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].body.content, '比较两份实习说明');
  assert.deepEqual(f.runs, []);
  assert.ok(!f.state.pendingConsent);
  f.$('#request-reply').listeners.click();
  assert.ok(!f.state.pendingConsent);

  f.state.capabilities.hourly_budget.resets_at = new Date(Date.now() - 1000).toISOString();
  f.$('#request-reply').listeners.click();
  assert.equal(f.state.consentAction, 'saved');
  assert.deepEqual(f.runs, []);
  await f.confirm();
  assert.equal(f.posts.length, 1);
  assert.deepEqual(f.runs, [f.state.conversation.id]);
});
test('quota used up during a confirmed save does not start a new discussion', async () => {
  const f = setup({beforeResponse() {
    f.state.capabilities.hourly_budget = {remaining:0, resets_at:new Date(Date.now() + 60000).toISOString()};
  }});
  await f.submit();
  await f.confirm();
  assert.equal(f.posts.length, 1);
  assert.deepEqual(f.runs, []);
  f.$('#message-input').value = '把新条件也保存下来';
  await f.submit();
  assert.equal(f.posts[1].url, '/api/conversations/' + f.state.conversation.id);
  assert.deepEqual(f.runs, []);
});
test('the saved start button becomes available at reset without starting a run', () => {
  const {state, $, runs, context} = setup();
  state.conversation = {id:'saved'};
  state.tasks = [{id:'t',status:'waiting'}];
  $('#reply-feedback').classList = {toggle(){}};
  vm.runInContext(section('function renderReplyFeedback()', 'function stopReplyWaiting('), context);
  context.renderModelControls = context.renderReplyFeedback;
  let now = Date.now();
  context.Date = class extends Date { static now() { return now; } };
  let expire;
  context.clearTimeout = () => {};
  context.setTimeout = (callback, delay) => {
    expire = () => { now += delay; callback(); };
    return 1;
  };
  vm.runInContext(section('let hourlyBudgetTimer;', 'async function refreshCapabilities('), context);
  context.applyCapabilities({...state.capabilities, hourly_budget:{remaining:0, resets_at:new Date(now + 5000).toISOString()}});
  assert.equal($('#request-reply').hidden, false);
  assert.equal($('#request-reply').disabled, true);
  assert.match($('#reply-feedback-text').textContent, /问题已保存.*额度恢复/);
  expire();
  assert.equal($('#request-reply').disabled, false);
  assert.match($('#reply-feedback-text').textContent, /可以开始团队协作/);
  assert.deepEqual(runs, []);
});
test('changing text, materials, or conversation invalidates the confirmation', async () => {
  for (const change of ['text','sources','conversation','view']) {
    const f = setup();
    f.state.conversation = {id:'A',sources_revision:'before'};
    await f.submit();
    if (change === 'text') { f.$('#message-input').value = '另一个问题'; f.$('#message-input').listeners.input(); }
    if (change === 'sources') f.state.conversation.sources_revision = 'after';
    if (change === 'conversation') f.state.conversation = {id:'B',sources_revision:'before'};
    if (change === 'view') f.state.viewVersion++;
    await f.confirm();
    assert.deepEqual(f.posts, [], change);
    assert.deepEqual(f.runs, [], change);
  }
});
test('one confirmation directly submits once; a lost save uses the same message id on retry', async () => {
  const f = setup({loseAck:true});
  await f.submit();
  assert.equal(f.posts.length, 0);
  await f.confirm();
  assert.equal(f.$('#message-input').value, '比较两份实习说明');
  assert.deepEqual(f.runs, []);
  await f.submit();
  assert.equal(f.posts.length, 2);
  assert.equal(f.posts[0].body.client_message_id, f.posts[1].body.client_message_id);
  assert.equal(f.posts[0].url, '/api/conversations');
  assert.deepEqual(f.runs, [f.posts[0].body.client_message_id]);
  await f.confirm();  // Stale confirmation has no action.
  assert.equal(f.posts.length, 2);
});
test('navigating while a save is in flight leaves the next conversation untouched', async () => {
  let release, entered;
  const enteredPost = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const f = setup({beforeResponse: async () => { entered(); await gate; }});
  await f.submit();
  const confirmation = f.confirm();
  await enteredPost;
  f.state.conversation = {id:'other'};
  f.state.viewVersion++;
  f.$('#message-input').value = '另一个对话的草稿';
  release();
  await confirmation;
  assert.equal(f.state.conversation.id, 'other');
  assert.equal(f.$('#message-input').value, '另一个对话的草稿');
  assert.deepEqual(f.runs, []);
  assert.deepEqual(f.state.messages, []);
  assert.equal(f.state.messageSaveIds.size, 1);  // Reopening the original draft can safely retry.
});

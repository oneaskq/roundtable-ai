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
function setup({loseAck = false, beforeResponse} = {}) {
  const nodes = new Map(), storage = new Map(), posts = [], runs = [];
  const state = {user:{id:'user1'}, view:'roundtable', viewVersion:1, authVersion:1, messages:[], tasks:[],
    messageSaveIds:new Map(), drafts:new Map(), replyFeedback:{}, conversation:null, aiEnabled:false};
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
  const c = vm.createContext({state, $, crypto, TextEncoder, Uint8Array,
    sessionStorage:{getItem:k=>storage.get(k) || null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    requestReply: () => runs.push(state.conversation?.id), interjectionRun:()=>null, interjectionDraft:()=>null, pendingSubmission:()=>null,
    currentTask:()=>state.tasks[0], chatAvailable:()=>true, modelIdentity:()=>state.conversation?.sources_revision || 'team',
    sourcesUI:{getState:()=>({busy:false})}, modelName:()=>'Test A、Test B', deliveryLimit:()=>14,
    renderChat:noop,renderModelControls:noop,resizeComposer:noop,rememberConversation:noop,
    rememberDraft:noop,forgetDraft:noop,reloadConversation:async()=>{},refreshWorkspace:async()=>{},showToast:noop,
    api:async(url, opt)=>{
      posts.push({url,body:opt.body});
      if (loseAck && posts.length === 1) throw new Error('lost response');
      if (beforeResponse) await beforeResponse();
      return {conversation: {id: state.conversation?.id || opt.body.client_message_id, sources_revision:state.conversation?.sources_revision},
        message:{id:opt.body.client_message_id},task:{id:'t',status:'waiting'}};
    }});
  vm.runInContext(section('function closeAiConsent()', 'function pendingDraftKey('), c);
  vm.runInContext(section('function promptAIConsent(', '$("#refresh-connection").addEventListener'), c);
  vm.runInContext(section('$("#composer").addEventListener("submit"', '$("#pause-task").addEventListener'), c);
  const pagehide = app.indexOf('window.addEventListener("pagehide"');
  vm.runInContext(app.slice(app.lastIndexOf('$("#message-input").addEventListener("input"', pagehide), pagehide), c);
  const confirm = async () => { $('#ai-consent-confirm').listeners.click(); await requested; };
  $('#message-input').value = '比较两份实习说明';
  return {state,$,posts,runs,submit,confirm};
}
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

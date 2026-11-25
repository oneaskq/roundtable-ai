const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const surface = () => ({
    dataset: {}, style: {}, children: [], scrollTop: 0, hidden: true, offsetTop: 0, offsetHeight: 10,
    addEventListener(name, handler) { this[name] = handler; },
    setAttribute(key, value) { this[key] = value; },
    removeAttribute(key) { delete this[key]; },
    contains(node) { return this.children.includes(node); },
    replaceChildren(...nodes) { this.children = nodes; },
    getBoundingClientRect() { return {top: 0, bottom: 200, height: 200}; },
  });
  let pending, nodes = [], jumps = 0;
  const panel = surface(), list = surface(), preview = surface(), scroller = surface();
  Object.assign(scroller, {clientHeight: 200, scrollHeight: 1200,
    scrollTo({top}) { this.scrollTop = Math.min(1000, Math.max(0, top)); }});
  list.clientHeight = 200;
  const runtime = {window: {}, document: {createElement: surface, activeElement: null},
    requestAnimationFrame(fn) { pending = fn; return 1; }, ResizeObserver: class { observe() {} }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'conversation-outline.js'), 'utf8'), runtime);
  const outline = runtime.window.RoundtableOutline.attach({panel, list, preview, scroller, content: surface(),
    messages: {querySelectorAll() { return nodes; }}, beforeJump() { jumps++; }});
  function turns(labels) {
    nodes = labels.map((label, index) => ({dataset: index % 2 ? {userTurn: String(index)} : {message: String(index)},
      querySelector() { return {textContent: label}; },
      getBoundingClientRect() { return {top: index * 300 - scroller.scrollTop}; }}));
    outline.refresh(); pending();
  }
  return {turns, panel, list, preview, scroller, jumps: () => jumps, paint() { pending(); }};
}

test('outline marks follow the reading position and jump to an intervening user turn', () => {
  const f = fixture();
  f.turns(['第一问', '中途补充', '最后一问']);
  assert.equal(f.panel.hidden, false);
  const [first, second, last] = f.list.children;
  assert.equal(first['aria-current'], 'location');
  assert.match(second['aria-label'], /中途补充/);
  assert.equal(second['aria-controls'], 'chat-turn-1');
  second.pointerenter({pointerType: 'mouse'});
  assert.equal(f.preview.textContent, '中途补充');
  second.click({detail: 0});
  assert.equal(f.scroller.scrollTop, 284);
  assert.equal(second['aria-current'], 'location');
  assert.equal(f.jumps(), 1);
  f.scroller.scrollTop = 1000;
  f.scroller.scroll(); f.paint();
  assert.equal(last['aria-current'], 'location');
  assert.equal(second['aria-current'], undefined);
});

test('short or switched conversations hide the outline and clear previous previews', () => {
  const f = fixture();
  f.turns(['第一问', '第二问']);
  f.list.children[1].pointerenter({pointerType: 'mouse'});
  f.turns([]);
  assert.equal(f.panel.hidden, true);
  assert.equal(f.preview.hidden, true);
  assert.equal(f.list.children.length, 0);
  f.scroller.scrollHeight = 230;
  f.turns(['新的一问', '简单补充']);
  assert.equal(f.panel.hidden, true);
});

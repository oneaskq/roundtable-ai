const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/delivery-intent.js'), 'utf8'), {window});
const {requestedMode} = window.RoundtableDelivery;

test('explicit output and download requests select the file workflow', () => {
  for (const message of ['生成一个可以下载的文件', '给我一份 CSV', '做一个 HTML 网页',
    '读取报价单，并将结果整理成文件发我', '把上一段写成一份文档',
    '用 Markdown 格式导出', '请提供一个可以下载的版本', '请打包发给我', 'Please export this as a CSV file']) {
    assert.equal(requestedMode(message), 'files', message);
  }
});

test('discussion, source material, examples and questions stay in text', () => {
  for (const message of ['比较两种方案', '分析这份 PDF', '请根据附件生成摘要',
    '给我看看这个 CSV 的内容', '我需要这份 PDF 的摘要', '怎么生成可下载的文件？', '用 Markdown 回答即可',
    '我写了一个脚本，帮我检查', '对比这两个文件，写一段总结', 'Write a summary of the attached PDF',
    '解释这段引用：\n> 请生成教程文件', '不要生成文件，直接文字说明',
    '请制作工作簿，但这轮只要文字说明']) {
    assert.equal(requestedMode(message), 'text', message);
  }
});

test('file revisions retain their workflow while unrelated follow-ups return to discussion', () => {
  assert.equal(requestedMode('把标题改成招聘计划', true), 'files');
  assert.equal(requestedMode('修订上一版', true), 'files');
  assert.equal(requestedMode('再优化一下封面', true), 'files');
  assert.equal(requestedMode('把标题改成招聘计划'), 'text');
  assert.equal(requestedMode('这个计划为什么选周三？', true), 'text');
  assert.equal(requestedMode('改成文字说明', true), 'text');
});

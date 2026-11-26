(() => {
  "use strict";

  // A named input or a request for advice is not a request for a download.
  const questionAboutFiles = /怎么|如何|为什么|什么是|能否介绍|能否解释|\b(?:how (?:to|do|can i)|why|explain how)\b/i;
  const referenceMaterial = /^(?:先|再|简单|详细|帮我|我)?(?:看|读|检查|分析|归纳|总结|解读|解释|介绍)|关于|有关|对于|上传|附上|\b(?:about|summary of|analysis of|attached|uploaded|existing|explain|summari[sz]e|review)\b/i;
  const textOnly = /(?:只要|只需|只用|仅需|直接用|改用|改成)(?:在对话中|在这里)?(?:文字|文本|口头)|(?:不需要|不必|不用|不要|无需).{0,14}(?:文件|附件|下载|导出)|\b(?:text only|no (?:files?|attachments?)|don't (?:create|generate|export))\b/i;
  const editArtifact = /^(?:请|麻烦|帮我|请帮我|再|继续|把|将|\s)*(?:修改|调整|修订|更新|优化|完善|改成|改为|改一下|改掉|改下|增加|添加|加上|删掉|删除|修复|替换|重做|重新打包)|(?:把|将).{1,28}(?:改成|改为|改一下|替换成|换成)|\b(?:update|revise|fix|modify) (?:the|this|that|previous|last) (?:file|page|script|bundle)\b/i;
  const deliveryVerb = /生成|制作|导出|保存[为成]|整理[成为]|转[成为]|转换为|输出|交付|打包|创建|编写|提供|想要|需要|写|给我|发我|发给我|做|\b(?:create|build|generate|write|export|deliver|provide|send|give me|save as|convert to)\b/gi;
  const fileTarget = /可(?:以)?下载|下载版|文件(?!名|中|里|内|格式|内容|清单|大小|类型|结构|区别|用途|列表|路径|上传|下载)|附件|文档|压缩包|网页|脚本|源代码|工作簿|电子表格|演示文稿|幻灯片|(?:^|[^a-z0-9])(?:md|markdown|txt|csv|json|html|css|js|py|zip|pdf|pptx?|docx?|xlsx?|excel|word)(?![a-z0-9])|\b(?:file|spreadsheet|downloadable|script|webpage|website|slides|archive)s?\b/i;
  const formatRequest = /(?:以|用|按|为|成|as(?: an?)?)\s*(?:markdown|csv|json|html|txt|pdf|word|excel|pptx?|docx?|xlsx?)\s*(?:文件|附件|文档|表格|格式)?\s*(?:形式)?\s*(?:下载|交付|导出|发给我|发我|给我)|(?:发|给)(?:我)?(?:个|一份)?可下载/i;

  function requestedMode(content, previousFiles = false) {
    const text = String(content || "").normalize("NFKC")
      .replace(/^\s*\x60\x60\x60[\s\S]*?(?:^\s*\x60\x60\x60|$(?![\s\S]))/gm, "")
      .replace(/^\s*>.*$/gm, "");
    let mode = "text";
    for (const part of text.split(/[，,。；;！？!?\n]|然后|并且|并(?:将|把)|但(?:是)?/)) {
      const clause = part.trim();
      if (!clause) continue;
      if (questionAboutFiles.test(clause)) continue;
      if (textOnly.test(clause) || /(?:不必|不用|不要|无需|不需要)(?:再|专门|额外)?(?:生成|制作|导出|打包|创建)/.test(clause)) {
        mode = "text"; continue;
      }
      if (formatRequest.test(clause) || /打包.{0,6}(?:下载|发给我|给我)/.test(clause) ||
          (previousFiles && editArtifact.test(clause))) {
        mode = "files"; continue;
      }
      for (const verb of clause.matchAll(deliveryVerb)) {
        const object = clause.slice(verb.index + verb[0].length, verb.index + verb[0].length + 65);
        const target = object.match(fileTarget);
        if (!target || /^(?:了|好的|完的|过的|的)/.test(object) ||
            referenceMaterial.test(object.slice(0, target.index).trimStart())) continue;
        if (/^\s*(?:文件|文档)?[里中内]?的(?:摘要|总结|内容|分析|简介|用法|解释|示例)/.test(object.slice(target.index + target[0].length))) continue;
        mode = "files"; break;
      }
    }
    return mode;
  }
  // The server decides whether a format describes an input or a requested output.
  // This inexpensive hint avoids a local preflight for ordinary conversation.
  function mentionsOfficeFormat(content) {
    return /pdf|docx?|xlsx?|pptx?|excel|word|powerpoint|工作簿|电子表格|演示文稿|幻灯片/i.test(String(content || "").normalize("NFKC"));
  }
  window.RoundtableDelivery = {requestedMode, mentionsOfficeFormat};
})();

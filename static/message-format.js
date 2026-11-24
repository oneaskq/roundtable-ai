/* Small, DOM-only message formatter. Model output is always untrusted text. */
(() => {
  "use strict";

  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };

  function safeLink(value) {
    if (!/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f]/.test(value)) return null;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      return url.href;
    } catch { return null; }
  }

  function link(label, destination) {
    const href = safeLink(destination);
    if (!href) return null;
    const anchor = node("a", "message-link", label);
    anchor.href = href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  function appendInline(parent, text, depth = 0) {
    if (depth > 8) { parent.append(document.createTextNode(text)); return; }
    let index = 0;
    let plain = "";
    const flush = () => { if (plain) { parent.append(document.createTextNode(plain)); plain = ""; } };
    while (index < text.length) {
      const char = text[index];
      if (char === "\\" && /[\\`*_[\]{}()#+.!>~-]/.test(text[index + 1] || "")) {
        plain += text[index + 1]; index += 2; continue;
      }
      if (char === "\n") { flush(); parent.append(node("br")); index++; continue; }
      if (char === "`") {
        let length = 1;
        while (text[index + length] === "`") length++;
        const delimiter = "`".repeat(length);
        const end = text.indexOf(delimiter, index + length);
        if (end >= 0) {
          flush();
          let content = text.slice(index + length, end).replace(/\n/g, " ");
          if (/^ .* $/.test(content) && /[^ ]/.test(content)) content = content.slice(1, -1);
          parent.append(node("code", "message-inline-code", content));
          index = end + length; continue;
        }
        plain += delimiter; index += length; continue;
      }
      if ((char === "*" || char === "_") && text[index + 1] === char) {
        const delimiter = char + char;
        const end = text.indexOf(delimiter, index + 2);
        if (end > index + 2) {
          flush(); const strong = node("strong");
          appendInline(strong, text.slice(index + 2, end), depth + 1);
          parent.append(strong); index = end + 2; continue;
        }
      }
      // A bounded label scan prevents repeated unmatched '[' from doing quadratic work.
      if (char === "[" && text[index - 1] !== "!") {
        const closing = text.slice(index + 1, index + 2049).indexOf("](");
        if (closing >= 0) {
          const labelEnd = index + 1 + closing;
          let end = labelEnd + 2;
          let nesting = 1;
          while (end < text.length && end - labelEnd < 4096 && nesting) {
            if (text[end] === "(") nesting++;
            if (text[end] === ")") nesting--;
            if (nesting) end++;
          }
          if (!nesting) {
            const anchor = link(text.slice(index + 1, labelEnd), text.slice(labelEnd + 2, end));
            if (anchor) { flush(); parent.append(anchor); index = end + 1; continue; }
          }
        }
      }
      if (char === "<") {
        const end = text.indexOf(">", index + 1);
        if (end >= 0 && end - index < 4096) {
          const destination = text.slice(index + 1, end);
          const anchor = link(destination, destination);
          if (anchor) { flush(); parent.append(anchor); index = end + 1; continue; }
        }
      }
      plain += char; index++;
    }
    flush();
  }

  function codeBlock(source, language) {
    const block = node("section", "message-code-block");
    const header = node("div", "message-code-header");
    const copy = node("button", "message-code-copy", "复制代码");
    copy.type = "button";
    copy.setAttribute("aria-label", "复制代码");
    copy.setAttribute("aria-live", "polite");
    let resetTimer;
    copy.addEventListener("click", async () => {
      if (copy.disabled) return;
      copy.disabled = true;
      clearTimeout(resetTimer);
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(source);
        copy.textContent = "已复制";
      } catch { copy.textContent = "复制失败，请选中文本"; }
      finally {
        copy.disabled = false;
        resetTimer = setTimeout(() => { copy.textContent = "复制代码"; }, 3000);
      }
    });
    const label = language.trim().split(/\s+/)[0].slice(0, 40) || "代码";
    header.append(node("span", "message-code-language", label), copy);
    const pre = node("pre");
    pre.setAttribute("tabindex", "0");
    pre.setAttribute("aria-label", `${label}代码`);
    pre.append(node("code", "", source));
    block.append(header, pre);
    return block;
  }

  const fence = (line) => line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
  const listItem = (line) => line.match(/^ {0,3}([-+*]|\d{1,9}[.)])\s+(.+)$/);
  const heading = (line) => line.match(/^ {0,3}(#{1,6})\s+(.+)$/);
  const rule = (line) => /^ {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line);
  const quote = (line) => /^ {0,3}> ?/.test(line);
  const startsBlock = (line) => !line.trim() || fence(line) || heading(line) || rule(line) || listItem(line) || quote(line);

  function reportSources(decision) {
    const evidence = new Map();
    for (const entry of decision.evidence || []) {
      if (entry?.id) evidence.set(entry.id, evidence.has(entry.id) ? null : entry);
    }
    const references = new Map();
    for (const row of decision.report_references || []) {
      if (!Array.isArray(row?.ids) || typeof row.line !== "string") continue;
      const entries = row.ids.map(id => evidence.get(id));
      if (!entries.length || entries.some(entry => !entry ||
        (entry.type === "calculation" ? entry.status !== "executed" || !entry.expression || entry.result == null : !entry.excerpt))) continue;
      const suffix = "（依据 " + row.ids.join("、") + "）";
      const keys = [row.line + suffix];
      // Older reports identified this same checked claim without printing its IDs.
      if (row.legacy_match) keys.push(row.line);
      for (const key of keys) {
        const parts = key.replace(/\r\n?/g, "\n").split("\n");
        const id = row.section + "\n" + parts[0];
        if (!references.has(id)) references.set(id, []);
        references.get(id).push({line: row.line, entries, suffix, parts});
      }
    }
    return references;
  }

  function sourceDetail(entry) {
    const calculation = entry.type === "calculation";
    const detail = node("details", "report-evidence");
    detail.append(node("summary", "", calculation ? "计算过程" : entry.name || "查看原文"));
    detail.append(node(calculation ? "code" : "blockquote", "", calculation
      ? entry.expression + " = " + entry.result : entry.excerpt));
    if (entry.scope) detail.append(node("p", "", entry.scope));
    if (entry.type === "web" && entry.url) {
      if (entry.retrieved_at) detail.append(node("small", "", "读取时间 " + entry.retrieved_at));
      if (entry.attribution) detail.append(node("small", "", entry.attribution));
      const origin = link("打开原网页", entry.url);
      if (origin) detail.append(origin);
    }
    return detail;
  }

  function blocks(lines, depth = 0, references = null) {
    const result = document.createDocumentFragment();
    let section = "";
    for (let index = 0; index < lines.length;) {
      const line = lines[index];
      if (!line.trim()) { index++; continue; }
      const opening = fence(line);
      if (opening) {
        const delimiter = opening[1];
        let end = index + 1;
        while (end < lines.length) {
          const close = lines[end].match(/^ {0,3}(`{3,}|~{3,})\s*$/);
          if (close && close[1][0] === delimiter[0] && close[1].length >= delimiter.length) break;
          end++;
        }
        result.append(codeBlock(lines.slice(index + 1, end).join("\n"), opening[2]));
        index = end < lines.length ? end + 1 : end; continue;
      }
      const title = heading(line);
      if (title) {
        section = title[1] === "##" ? title[2] : "";
        const element = node(`h${title[1].length}`);
        const label = references && section === "重要分歧与核验" ? "成员核验记录" : title[2].replace(/\s+#+\s*$/, "");
        appendInline(element, label);
        result.append(element); index++; continue;
      }
      if (rule(line)) { result.append(node("hr")); index++; continue; }
      if (quote(line) && depth < 8) {
        const quoted = [];
        while (index < lines.length && quote(lines[index])) quoted.push(lines[index++].replace(/^ {0,3}> ?/, ""));
        const element = node("blockquote"); element.append(blocks(quoted, depth + 1)); result.append(element); continue;
      }
      const first = listItem(line);
      if (first) {
        const ordered = /^\d/.test(first[1]);
        const list = node(ordered ? "ol" : "ul");
        if (ordered && parseInt(first[1], 10) !== 1) list.setAttribute("start", String(parseInt(first[1], 10)));
        while (index < lines.length) {
          const item = listItem(lines[index]);
          if (!item || /^\d/.test(item[1]) !== ordered) break;
          index++;
          const candidates = references?.get(section + "\n" + item[2]) || [];
          const matches = candidates.filter(candidate => candidate.parts.slice(1)
            .every((line, offset) => line === lines[index + offset]));
          const reference = matches.length && matches.every(match => match.line === matches[0].line &&
            match.suffix === matches[0].suffix) ? matches[0] : null;
          if (reference) index += reference.parts.length - 1;
          const content = [reference ? reference.line : item[2]];
          while (index < lines.length && /^ {2,}\S/.test(lines[index]) && !startsBlock(lines[index])) content.push(lines[index++].trimStart());
          const element = node("li");
          appendInline(element, content.join("\n"));
          if (reference) element.append(...reference.entries.map(sourceDetail));
          list.append(element);
        }
        result.append(list); continue;
      }
      const paragraph = [line]; index++;
      while (index < lines.length && !startsBlock(lines[index])) paragraph.push(lines[index++]);
      const element = node("p"); appendInline(element, paragraph.join("\n")); result.append(element);
    }
    return result;
  }

  window.RoundtableMessage = Object.freeze({
    render(text, decision = null, expanded = false) {
      const source = String(text ?? "").replace(/\r\n?/g, "\n");
      const references = decision ? reportSources(decision) : null;
      const recommendation = decision?.summary?.recommendation;
      if (typeof recommendation === "string" && recommendation.trim()) {
        const advice = recommendation.replace(/\r\n?/g, "\n").trim();
        const lead = "## 当前建议与条件\n" + advice;
        // Only fold the remainder when the saved advice matches this message.
        if (source.startsWith(lead + "\n\n") && source.slice(lead.length).trim()) {
          const result = blocks(advice.split("\n"));
          const detail = node("details", "report-details");
          detail.open = expanded;
          detail.append(node("summary", "", "查看完整报告"), blocks(source.slice(lead.length).split("\n"), 0, references));
          result.append(detail);
          return result;
        }
      }
      return blocks(source.split("\n"), 0, references);
    },
  });
})();

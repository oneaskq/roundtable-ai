(() => {
  "use strict";
  let selected = null;
  function button(label, className) {
    const node = document.createElement("button");
    node.type = "button"; node.className = className; node.textContent = label;
    return node;
  }
  const trigger = button("追问选中内容", "selection-followup");
  trigger.hidden = true;
  document.body.append(trigger);

  function compose(article, excerpt) {
    const reports = [...document.querySelectorAll("#messages article[data-report-id]")];
    const number = reports.indexOf(article) + 1;
    const input = document.querySelector("#message-input");
    if (!number || !input || input.disabled) return;
    const draft = input.value.replace(/^引用第 [1-9]\d* 份圆桌报告[：:]\n(?:>[^\n]*(?:\n|$))+\n?/, "");
    const text = "引用第 " + number + " 份圆桌报告：\n" +
      excerpt.trim().split("\n").map(line => "> " + line).join("\n") + "\n\n" + draft;
    if (text.length > input.maxLength) {
      input.setCustomValidity("引用与草稿超过当前输入上限，请缩短选段或草稿。");
      input.reportValidity(); input.setCustomValidity("");
      return;
    }
    input.value = text;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    window.getSelection()?.removeAllRanges();
    trigger.hidden = true;
    input.focus();
    input.setSelectionRange(text.length, text.length);
  }

  function updateSelection() {
    const selection = window.getSelection();
    selected = null; trigger.hidden = true;
    if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return;
    const range = selection.getRangeAt(0);
    const parent = node => node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    const start = parent(range.startContainer);
    const end = parent(range.endContainer);
    const content = start?.closest(".report-content");
    if (!content || !content.contains(end) || start.closest(".report-evidence") || end.closest(".report-evidence")) return;
    const article = content.closest("article[data-report-id]");
    const excerpt = selection.toString().trim();
    if (!article || !excerpt || excerpt.length > 2000) return;
    const rect = range.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) return;
    selected = {article, excerpt};
    trigger.hidden = false;
    trigger.style.left = Math.max(8, Math.min(window.innerWidth - trigger.offsetWidth - 8, rect.left)) + "px";
    const above = rect.top - trigger.offsetHeight - 5;
    trigger.style.top = (above >= 8 ? above : Math.min(rect.bottom + 5, window.innerHeight - trigger.offsetHeight - 8)) + "px";
  }
  document.addEventListener("selectionchange", updateSelection);
  document.addEventListener("scroll", () => { if (!trigger.hidden) updateSelection(); }, true);
  window.addEventListener("resize", () => { if (!trigger.hidden) updateSelection(); });
  trigger.addEventListener("pointerdown", event => event.preventDefault());
  trigger.addEventListener("click", () => { if (selected) compose(selected.article, selected.excerpt); });

  function menuButton(content) {
    const action = button("选择一段追问", "text-button");
    action.addEventListener("click", () => {
      const menu = action.closest("details");
      if (menu) menu.open = false;
      const article = content.closest("article[data-report-id]");
      if (!article) return;
      const dialog = document.createElement("dialog");
      dialog.className = "report-followup-picker";
      dialog.setAttribute("aria-label", "选择要追问的段落");
      const header = document.createElement("div");
      header.className = "dialog-heading";
      const title = document.createElement("h2");
      title.textContent = "选择要追问的段落";
      const cancel = button("取消", "text-button");
      cancel.addEventListener("click", () => dialog.close());
      header.append(title, cancel); dialog.append(header);
      let picked = null;
      for (const paragraph of content.querySelectorAll("p, li")) {
        if (paragraph.closest(".report-evidence") || paragraph.querySelector("p, li")) continue;
        const copy = paragraph.cloneNode(true);
        copy.querySelectorAll(".report-evidence").forEach(node => node.remove());
        const value = copy.textContent.trim();
        // Long paragraphs stay selectable on touchscreens and with the keyboard.
        for (let index = 0; index < value.length; index += 1800) {
          const option = button(value.slice(index, index + 1800), "report-followup-option");
          option.addEventListener("click", () => { picked = option.textContent; dialog.close(); });
          dialog.append(option);
        }
      }
      dialog.addEventListener("close", () => {
        dialog.remove();
        if (picked) compose(article, picked);
        else menu?.querySelector("summary")?.focus();
      }, {once: true});
      document.body.append(dialog); dialog.showModal();
    });
    return action;
  }
  window.RoundtableFollowup = {menuButton};
})();

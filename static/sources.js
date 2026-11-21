"use strict";

// Reference text is always rendered as text. Loading a source never starts AI.
(() => {
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const safeLink = (value) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  };
  const number = (value) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "0";

  window.RoundtableSources = {
    create({ api, onDraftReady, onChange = () => {}, onMutationStart = () => {}, onBusyChange = () => {} }) {
      const byId = (id) => document.getElementById(id);
      const ui = Object.fromEntries([
        "sources-open", "source-chips", "sources-dialog", "sources-close", "sources-done", "sources-description",
        "sources-library", "source-upload", "source-file", "source-link-toggle", "source-link-form", "source-url", "source-fetch",
        "sources-list", "sources-count", "sources-status", "sources-refresh", "sources-error", "sources-cancel-request",
        "source-paste-toggle", "source-paste-form", "source-paste-name", "source-paste-text", "source-paste-save",
        "source-preview", "source-preview-back", "source-preview-title", "source-preview-meta", "source-preview-link", "source-preview-content",
      ].map((id) => [id, byId(id)]));
      let context = { conversationId: null, revision: null, running: false, blocked: false };
      let sources = [], revision = null, loaded = false, loading = false, busy = false;
      let generation = 0, listSequence = 0, detailSequence = 0, mutationSequence = 0;
      let listController = null, detailController = null, mutationController = null;
      let busySignature = "false:false", previewId = null, deletingId = null;
      let draftId = crypto.randomUUID();
      const current = (token) => token === generation && (context.conversationId || context.allowDraft) && !context.blocked;
      const getState = () => ({ revision, sources: sources.map((item) => ({ ...item })), loaded, loading, busy });
      const notifyBusy = () => {
        const signature = `${busy}:${loading}`;
        if (signature !== busySignature) { busySignature = signature; onBusyChange({ busy, loading }); }
      };
      const showError = (text = "") => {
        ui["sources-error"].textContent = text;
        ui["sources-error"].hidden = !text;
      };
      const editable = () => (context.conversationId || context.allowDraft) && !context.running && !context.blocked && !busy && !loading;
      const addPath = () => context.conversationId ? `/api/conversations/${context.conversationId}/sources`
        : `/api/draft-sources/${draftId}${context.projectId ? "?project=" + encodeURIComponent(context.projectId) : ""}`;
      const sourceLabel = (item) => `${item.name || "未命名材料"} · ${number(item.char_count)} 字符`;
      const resetPreview = () => {
        detailSequence++; detailController?.abort(); detailController = null; previewId = null;
        ui["source-preview"].hidden = true; ui["sources-library"].hidden = false;
        ui["source-preview-content"].textContent = ""; ui["source-preview-title"].textContent = "";
        ui["source-preview-meta"].textContent = ""; ui["source-preview-link"].hidden = true;
        ui["source-preview-link"].removeAttribute("href");
      };

      function render() {
        const selected = sources.filter((item) => item.included);
        ui["sources-open"].disabled = (!context.conversationId && !context.allowDraft) || context.blocked;
        ui["sources-open"].title = "添加或查看参考材料";
        ui["sources-open"].setAttribute("aria-label", selected.length ? `参考材料，已选 ${selected.length} 份` : "添加材料");
        ui["source-chips"].replaceChildren();
        for (const item of selected) {
          const chip = make("button", "source-chip", item.name || "参考材料");
          chip.type = "button"; chip.title = sourceLabel(item); chip.disabled = context.blocked;
          chip.addEventListener("click", () => open(item.id)); ui["source-chips"].append(chip);
        }
        ui["source-chips"].hidden = !selected.length;
        ui["sources-count"].textContent = sources.length ? `${selected.length} / ${sources.length} 份已选 · ${number(selected.reduce((sum, item) => sum + (Number(item.char_count) || 0), 0))} 字符` : "参考材料";
        const status = loading ? "正在读取材料…" : busy ? "正在处理材料…" : context.running ? "任务处理期间可预览材料，暂不能更改选择。" : "";
        ui["sources-status"].textContent = status; ui["sources-status"].hidden = !status;
        ui["source-upload"].disabled = !editable() || sources.length >= 8;
        ui["source-link-toggle"].disabled = !editable() || sources.length >= 8;
        ui["source-paste-toggle"].disabled = !editable() || sources.length >= 8;
        ui["source-paste-save"].disabled = !editable() || sources.length >= 8;
        ui["source-url"].disabled = !editable();
        ui["source-fetch"].disabled = !editable() || sources.length >= 8;
        ui["sources-refresh"].disabled = busy || loading || !context.conversationId || context.blocked;
        ui["sources-cancel-request"].hidden = !busy && !loading;
        const focusId = document.activeElement?.dataset?.sourceId;
        ui["sources-list"].replaceChildren();
        if (!sources.length && !loading && context.conversationId && !loaded) ui["sources-list"].append(make("p", "source-empty", "重新载入材料后即可使用。"));
        for (const item of sources) {
          const row = make("div", `source-row${item.included ? "" : " excluded"}`);
          const selectedBox = make("input"); selectedBox.type = "checkbox"; selectedBox.checked = Boolean(item.included);
          selectedBox.dataset.sourceId = item.id; selectedBox.disabled = !editable();
          selectedBox.setAttribute("aria-label", `${item.included ? "本轮不使用" : "本轮使用"}：${item.name || "材料"}`);
          selectedBox.addEventListener("change", () => changeSelection(item, selectedBox.checked));
          const body = make("div", "source-row-body");
          const name = make("button", "source-name", item.name || "未命名材料"); name.type = "button";
          name.addEventListener("click", () => preview(item.id));
          const url = safeLink(item.source_url);
          let origin = item.kind === "web" ? "网页" : "文件";
          if (url) origin = new URL(url).hostname;
          body.append(name, make("span", "source-row-meta", `${origin} · ${number(item.char_count)} 字符${item.included ? "" : " · 本轮未选"}`));
          row.append(selectedBox, body);
          if (deletingId === item.id) {
            const confirm = make("div", "source-delete-confirm");
            confirm.append(make("span", "source-delete-note", "删除后，历史协作中的来源记录和已生成内容仍保留。"));
            const yes = make("button", "text-button", "确认删除"); yes.type = "button";
            yes.setAttribute("aria-label", `确认删除：${item.name || "材料"}`); yes.disabled = !editable();
            yes.dataset.confirmId = item.id;
            yes.addEventListener("click", () => void mutate(`/api/sources/${encodeURIComponent(item.id)}`, undefined, () => { deletingId = null; }, "DELETE"));
            const cancel = make("button", "text-button", "取消"); cancel.type = "button";
            cancel.disabled = busy; cancel.addEventListener("click", () => {
              deletingId = null; render();
              Array.from(ui["sources-list"].querySelectorAll("button")).find((node) => node.dataset.deleteId === item.id)?.focus();
            });
            confirm.append(yes, cancel); row.append(confirm);
          } else {
            const remove = make("button", "source-remove", "删除"); remove.type = "button"; remove.disabled = !editable();
            remove.setAttribute("aria-label", `删除：${item.name || "材料"}`);
            remove.dataset.deleteId = item.id;
            remove.addEventListener("click", () => {
              deletingId = item.id; render();
              Array.from(ui["sources-list"].querySelectorAll("button")).find((node) => node.dataset.confirmId === item.id)?.focus();
            }); row.append(remove);
          }
          ui["sources-list"].append(row);
        }
        if (focusId) Array.from(ui["sources-list"].querySelectorAll("input")).find((input) => input.dataset.sourceId === focusId)?.focus();
      }

      function apply(data) {
        if (!data || !Array.isArray(data.sources) || typeof data.revision !== "string") throw new Error("材料列表格式无效，请重新载入。");
        sources = data.sources; revision = data.revision; context.revision = revision; loaded = true;
        render(); onChange({ ...data, ...getState(), conversationId: context.conversationId });
      }

      async function refresh() {
        if (!context.conversationId || context.blocked || busy) return getState();
        const token = generation, sequence = ++listSequence, conversationId = context.conversationId;
        listController?.abort(); listController = new AbortController();
        const controller = listController;
        loading = true; showError(); render(); notifyBusy();
        try {
          const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/sources`, { signal: controller.signal });
          if (current(token) && sequence === listSequence) apply(data);
        } catch (error) {
          if (current(token) && sequence === listSequence && error.name !== "AbortError") { loaded = false; showError(error.message || "暂时无法读取材料。"); }
        } finally {
          if (current(token) && sequence === listSequence) { loading = false; listController = null; render(); notifyBusy(); }
        }
        return getState();
      }

      function cancelWaiting(message = true) {
        listSequence++; mutationSequence++; listController?.abort(); mutationController?.abort();
        listController = null; mutationController = null;
        if (busy || loading) loaded = false;
        busy = false; loading = false;
        if (message) showError("已停止等待。重新载入可确认材料是否已保存。");
        render(); notifyBusy();
      }

      function close() {
        if (!busy) cancelWaiting(false);
        resetPreview(); ui["sources-dialog"].close();
        if (!ui["sources-open"].disabled) ui["sources-open"].focus();
      }

      async function open(sourceId = null) {
        if ((!context.conversationId && !context.allowDraft) || context.blocked) return;
        showError(); resetPreview();
        ui["sources-dialog"].showModal();
        const token = generation;
        await refresh();
        if (!current(token) || !ui["sources-dialog"].open) return;
        if (sourceId && sources.some((item) => item.id === sourceId)) await preview(sourceId);
      }

      async function preview(id) {
        if (!context.conversationId || context.blocked) return;
        const token = generation, sequence = ++detailSequence;
        detailController?.abort(); detailController = new AbortController();
        const controller = detailController; previewId = id;
        showError(); ui["sources-library"].hidden = true; ui["source-preview"].hidden = false;
        ui["source-preview-title"].textContent = sources.find((item) => item.id === id)?.name || "参考材料";
        ui["source-preview-meta"].textContent = "正在读取正文…"; ui["source-preview-content"].textContent = "";
        ui["source-preview-link"].hidden = true; ui["source-preview-link"].removeAttribute("href");
        ui["source-preview-back"].focus();
        try {
          const data = await api(`/api/sources/${encodeURIComponent(id)}`, { signal: controller.signal });
          if (!current(token) || sequence !== detailSequence || previewId !== id) return;
          const item = data.source;
          if (!item || typeof item.content !== "string") throw new Error("材料正文格式无效。");
          ui["source-preview-title"].textContent = item.name || "参考材料";
          ui["source-preview-content"].textContent = item.content;
          ui["source-preview-meta"].textContent = `${number(item.char_count ?? Array.from(item.content).length)} 字符 · ${item.metadata?.scope || (item.kind === "web" ? "网页正文" : "提取的文字")}`;
          const url = safeLink(item.source_url);
          if (url) { ui["source-preview-link"].href = url; ui["source-preview-link"].hidden = false; }
        } catch (error) {
          if (current(token) && sequence === detailSequence && error.name !== "AbortError") { ui["source-preview-meta"].textContent = "正文未能载入"; showError(error.message || "暂时无法读取正文。"); }
        }
      }

      async function mutate(path, body, afterSave, method = "POST") {
        if (!editable()) return;
        const token = generation, sequence = ++mutationSequence, conversationId = context.conversationId;
        const previousFocus = document.activeElement, previousSourceId = previousFocus?.dataset?.sourceId;
        listSequence++; listController?.abort(); listController = null; loading = false;
        mutationController = new AbortController(); const controller = mutationController;
        busy = true; showError(); render(); onMutationStart(); notifyBusy();
        try {
          const result = await api(path, { method, body, signal: controller.signal });
          if (!current(token) || sequence !== mutationSequence) return;
          if (!context.conversationId && result.conversation) {
            context.conversationId = result.conversation.id;
            context.revision = result.revision;
            onDraftReady(result.conversation);
          }
          const data = Array.isArray(result.sources) ? result : await api(`/api/conversations/${encodeURIComponent(conversationId)}/sources`, { signal: controller.signal });
          if (!current(token) || sequence !== mutationSequence) return;
          apply(data); afterSave?.(); return true;
        } catch (error) {
          if (current(token) && sequence === mutationSequence && error.name !== "AbortError") {
            loaded = false; showError(error.message || "材料未能保存，请重新载入确认。");
          }
        } finally {
          if (current(token) && sequence === mutationSequence) {
            busy = false; mutationController = null; render(); notifyBusy();
            if (ui["sources-dialog"].open) {
              const target = previousSourceId ? Array.from(ui["sources-list"].querySelectorAll("input")).find((input) => input.dataset.sourceId === previousSourceId) : previousFocus;
              if (target?.isConnected && !target.disabled) target.focus();
              else if (method === "DELETE") ui["source-upload"].focus();
            }
          }
        }
      }

      function changeSelection(item, included) {
        return mutate(`/api/sources/${encodeURIComponent(item.id)}`, { included });
      }

      function setContext(next) {
        const incoming = { conversationId: next?.conversationId || null, revision: next?.revision ?? null,
          running: Boolean(next?.running), blocked: Boolean(next?.blocked), allowDraft: Boolean(next?.allowDraft),
          projectId: next?.projectId || null };
        const switched = incoming.conversationId !== context.conversationId || (!incoming.conversationId &&
          (incoming.allowDraft !== context.allowDraft || incoming.projectId !== context.projectId));
        const unblocked = context.blocked && !incoming.blocked;
        const changed = incoming.revision !== null && incoming.revision !== context.revision;
        if (switched || incoming.blocked && !context.blocked) {
          generation++; listSequence++; detailSequence++; mutationSequence++;
          listController?.abort(); detailController?.abort(); mutationController?.abort();
          listController = null; detailController = null; mutationController = null;
          sources = []; revision = null; loaded = false; loading = false; busy = false; deletingId = null;
          showError(); resetPreview(); ui["sources-dialog"].close();
          ui["source-file"].value = ""; ui["source-url"].value = "";
          ui["source-link-form"].hidden = true; ui["source-link-toggle"].setAttribute("aria-expanded", "false");
          ui["source-paste-form"].hidden = true; ui["source-paste-toggle"].setAttribute("aria-expanded", "false");
          ui["source-paste-text"].value = ""; ui["source-paste-name"].value = "";
          draftId = crypto.randomUUID();
        }
        context = incoming; render(); notifyBusy();
        if (context.conversationId && !context.blocked && (switched || changed || unblocked) && !busy) void refresh();
      }

      ui["sources-open"].addEventListener("click", () => open());
      ui["sources-close"].addEventListener("click", close); ui["sources-done"].addEventListener("click", close);
      ui["sources-dialog"].addEventListener("cancel", (event) => { event.preventDefault(); close(); });
      ui["sources-refresh"].addEventListener("click", () => refresh());
      ui["sources-cancel-request"].addEventListener("click", () => cancelWaiting());
      ui["source-upload"].addEventListener("click", () => { if (editable()) ui["source-file"].click(); });
      ui["source-file"].addEventListener("change", async () => {
        const files = Array.from(ui["source-file"].files || []); ui["source-file"].value = "";
        if (!files.length || !editable()) return;
        if (sources.length + files.length > 8) { showError("每个对话最多 8 份材料，请减少选择。"); return; }
        const extensions = ui["source-file"].accept.split(",");
        for (const file of files) {
          if (file.size > 2 * 1024 * 1024) { showError(`${file.name} 超过 2 MB，请缩小后上传。`); return; }
          if (!extensions.some(ext => file.name.toLowerCase().endsWith(ext.trim()))) {
            showError(`${file.name} 的格式暂不支持，可以使用「粘贴文字」。`); return;
          }
        }
        const token = generation;
        for (const file of files) {
          if (!current(token) || !editable()) return;
          const body = new FormData(); body.append("file", file);
          const saved = await mutate(addPath(), body);
          if (!saved) {
            if (current(token) && ui["sources-error"].textContent) showError(`${file.name}：${ui["sources-error"].textContent}`);
            return;
          }
        }
      });
      ui["source-paste-toggle"].addEventListener("click", () => {
        if (!editable()) return;
        const form = ui["source-paste-form"]; form.hidden = !form.hidden;
        ui["source-paste-toggle"].setAttribute("aria-expanded", String(!form.hidden));
        if (!form.hidden) ui["source-paste-text"].focus();
      });
      ui["source-paste-form"].addEventListener("submit", (event) => {
        event.preventDefault(); if (!editable()) return;
        const content = ui["source-paste-text"].value;
        if (!content.trim()) { showError("请先粘贴要引用的文字。"); return; }
        const name = ui["source-paste-name"].value.trim() || "粘贴的材料";
        const body = new FormData();
        body.append("file", new Blob([content], {type:"text/plain;charset=utf-8"}), name + ".txt");
        void mutate(addPath(), body, () => {
          ui["source-paste-text"].value = ""; ui["source-paste-name"].value = "";
          ui["source-paste-form"].hidden = true; ui["source-paste-toggle"].setAttribute("aria-expanded", "false");
        });
      });
      ui["source-link-toggle"].addEventListener("click", () => {
        if (!editable()) return;
        ui["source-link-form"].hidden = !ui["source-link-form"].hidden;
        ui["source-link-toggle"].setAttribute("aria-expanded", String(!ui["source-link-form"].hidden));
        if (!ui["source-link-form"].hidden) ui["source-url"].focus();
      });
      ui["source-link-form"].addEventListener("submit", (event) => {
        event.preventDefault(); if (!editable()) return;
        const url = safeLink(ui["source-url"].value.trim());
        if (!url) { showError("请填写不含登录信息的公开 HTTPS 网页地址。"); return; }
        void mutate(addPath(), { url }, () => {
          ui["source-url"].value = ""; ui["source-link-form"].hidden = true; ui["source-link-toggle"].setAttribute("aria-expanded", "false");
        });
      });
      ui["source-preview-back"].addEventListener("click", () => { resetPreview(); showError(); ui["sources-refresh"].focus(); });
      render();
      return { setContext, refresh, reset: () => setContext(null), getState };
    },
  };
})();

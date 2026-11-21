"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { user: null, auth: null, view: "roundtable", mode: "login", projects: [], conversations: [], conversation: null, messages: [], tasks: [], dialogHandler: null, viewVersion: 0, authVersion: 0, workspaceVersion: 0 };
Object.assign(state, { capabilities: null, capabilityError: "", capabilityVersion: 0, conversationRevision: 0, aiEnabled: false, aiConsentFor: null, activeReply: null, activeSave: null, replyFeedback: {}, deliveryMode: "text", participantCount: "", artifactVersion: 0 });
state.drafts = new Map();
state.teamChatDrafts = new Map();
state.teamChatSending = new Map();
state.draftWriter = (() => {
  try { const saved = sessionStorage.getItem("roundtable:draft-writer"); if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved || "")) return saved;
    const value = crypto.randomUUID(); sessionStorage.setItem("roundtable:draft-writer", value); return value; }
  catch { return crypto.randomUUID(); }
})();
state.draftSequence = new Map();
state.draftPending = new Map();
state.draftTimers = new Map();
state.draftsReady = false;
state.pendingSubmissions = new Map();
state.acceptanceEditor = null;
state.acceptanceStatus = null;
state.pendingConsent = null;
state.messageSaveIds = new Map();
state.messageSignature = "";
state.messageEdit = null;
state.scrollConversation = null;
state.followLatest = true;
state.chatScrollFrame = null;
state.reportScrollTarget = null;
// Keep recovery credentials out of the address bar and browser history. Support
// older query-string links as well as new links whose secrets are in fragments.
function captureRecoveryLink() {
  const url = new URL(window.location.href);
  const fragment = new URLSearchParams(url.hash.slice(1));
  const source = fragment.has("reset_uid") || fragment.has("reset_token") ? fragment : url.searchParams;
  if (!source.has("reset_uid") && !source.has("reset_token")) return false;
  state.resetLink = { uid: source.get("reset_uid") || "", token: source.get("reset_token") || "" };
  url.searchParams.delete("reset_uid"); url.searchParams.delete("reset_token");
  if (source === fragment) url.hash = "";
  history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  return true;
}
captureRecoveryLink();
const viewNames = { roundtable: "圆桌", projects: "我的项目", memories: "个人记忆", capabilities: "能力进展", quality: "质量评估", data: "数据中心", security: "账号安全", release: "上线准备" };
let toastTimer;
let afterDialog = null;
let sourcesUI = null;
let qualityUI = null;
let sourcesResetting = false;
function resetSources() {
  sourcesResetting = true;
  try { sourcesUI?.reset(); } finally { sourcesResetting = false; }
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value !== undefined && value !== null) node.setAttribute(key, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message; toast.classList.toggle("error", error); toast.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 7500 : 4500);
}

function csrfToken() {
  const cookie = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith("csrftoken="));
  return cookie ? decodeURIComponent(cookie.slice(10)) : $("[name=csrfmiddlewaretoken]")?.value || "";
}

async function api(path, options = {}) {
  const method = options.method || "GET"; const authVersion = state.authVersion;
  const multipart = options.body instanceof FormData;
  const response = await fetch(path, {
    credentials: "same-origin", method,
    headers: { Accept: "application/json", ...(method !== "GET" ? { ...(!multipart ? { "Content-Type": "application/json" } : {}), "X-CSRFToken": csrfToken() } : {}) },
    ...(options.body !== undefined ? { body: multipart ? options.body : JSON.stringify(options.body) } : {}),
    ...(options.signal ? { signal: options.signal } : {}),
    ...(options.keepalive ? { keepalive: true } : {}),
  });
  let data = {};
  if (response.status !== 204) {
    try { data = await response.json(); } catch {
      const error = new Error("服务暂时无法响应，请稍后重试。"); error.status = response.status; throw error;
    }
  }
  if (authVersion !== state.authVersion) throw new Error("登录状态已更新，请重试。");
  if (!response.ok) {
    if (response.status === 401 && state.user && data.code !== "credential_invalid") { state.user = null; showAuth(); }
    const error = new Error(typeof data.error === "string" ? data.error : "操作未完成，请稍后重试。");
    error.status = response.status; error.code = data.code; throw error;
  }
  return data;
}

async function busy(button, action) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try { return await action(); } catch (error) { showToast(error.message || "网络连接失败，请重试。", true); }
  finally { if (button) button.disabled = false; }
}

function button(text, action, className = "button") {
  const result = el("button", { type: "button", class: className, text });
  result.addEventListener("click", () => busy(result, action)); return result;
}
function dateText(value) { if (!value) return ""; const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
function heading(title, description, action) { return el("div", { class: "page-heading" }, [el("div", {}, [el("h1", { text: title }), el("p", { text: description })]), action]); }
function notice(text, error = false) { return el("p", { class: `notice${error ? " error-notice" : ""}`, text }); }
function empty(title, description) { return el("div", { class: "empty-card" }, [el("span", { class: "empty-symbol simple-circle", "aria-hidden": "true" }), el("h2", { text: title }), el("p", { text: description })]); }
function exportLink(url, label = "导出") { return el("a", { href: url, class: "button", text: label, download: "" }); }
function isMfaBlocked() { return Boolean(state.auth?.mfa?.required && !state.auth?.mfa?.verified); }

function syncSidebarFocus() {
  const mobile = window.matchMedia("(max-width:760px)").matches;
  const open = $("#sidebar").classList.contains("open");
  const collapsed = document.body.classList.contains("sidebar-collapsed");
  $("#sidebar").inert = mobile ? !open : collapsed;
  $("#main-content").inert = mobile && open;
  $("#menu-toggle").setAttribute("aria-expanded", String(mobile ? open : !collapsed));
  $("#menu-toggle").setAttribute("aria-label", (mobile ? open : !collapsed) ? "收起导航" : "打开导航");
}
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#sidebar-shade").hidden = true; document.body.classList.remove("nav-open"); $("#menu-toggle").setAttribute("aria-expanded", "false"); syncSidebarFocus(); }
function rememberConversation() {
  if (!state.user) return;
  try {
    const key = `roundtable:conversation:${state.user.id}`;
    if (state.conversation) sessionStorage.setItem(key, state.conversation.id);
    else sessionStorage.removeItem(key);
  } catch { /* Browsing with storage disabled still works. */ }
}
function submissionKey(conversationId) { return `roundtable:pending-submission:${state.user?.id}:${conversationId}`; }
function pendingSubmission(conversationId) {
  if (!state.user || !conversationId) return null;
  if (state.pendingSubmissions.has(conversationId)) return state.pendingSubmissions.get(conversationId);
  try {
    const saved = JSON.parse(sessionStorage.getItem(submissionKey(conversationId)) || "null");
    if (saved && typeof saved.id === "string" && saved.body?.client_request_id === saved.id && saved.body?.background === true) {
      state.pendingSubmissions.set(conversationId, saved); return saved;
    }
  } catch { /* Keep working in memory if browser storage is unavailable. */ }
  return null;
}
function rememberSubmission(conversationId, value) {
  state.pendingSubmissions.set(conversationId, value);
  try { sessionStorage.setItem(submissionKey(conversationId), JSON.stringify(value)); return true; }
  catch { state.pendingSubmissions.delete(conversationId); return false; }
}
function forgetSubmission(conversationId, expectedId) {
  const existing = pendingSubmission(conversationId);
  if (!existing || existing.id !== expectedId) return;
  state.pendingSubmissions.delete(conversationId);
  try { sessionStorage.removeItem(submissionKey(conversationId)); } catch {}
}
function syncChatScroll() {
  const scroller = $("#chat-scroll");
  if (!scroller) return;
  state.followLatest = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
  if ($("#scroll-to-bottom")) $("#scroll-to-bottom").hidden = state.followLatest || !state.messages.length;
}
function scrollToLatest(smooth = false) {
  const scroller = $("#chat-scroll"); if (!scroller) return;
  cancelAutomaticChatScroll();
  state.followLatest = true;
  scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth && !window.matchMedia("(prefers-reduced-motion:reduce)").matches ? "smooth" : "instant" });
  if ($("#scroll-to-bottom")) $("#scroll-to-bottom").hidden = true;
}
function cancelAutomaticChatScroll() {
  if (state.chatScrollFrame !== null) cancelAnimationFrame(state.chatScrollFrame);
  state.chatScrollFrame = null;
  state.reportScrollTarget = null;
}
function closeAiConsent() {
  state.pendingConsent = null;
  state.consentAction = null;
  state.consentDraft = null;
  if ($("#ai-consent")) $("#ai-consent").hidden = true;
}
async function messageSaveReceipt(userId, draftKey, project, content) {
  const key = `roundtable:message-save:${userId}:${draftKey}`;
  const bytes = new TextEncoder().encode(JSON.stringify([project, content]));
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    byte => byte.toString(16).padStart(2, "0")).join("");
  let receipt = state.messageSaveIds.get(key);
  try { receipt ||= JSON.parse(sessionStorage.getItem(key)); } catch { /* Storage may be disabled. */ }
  if (receipt?.digest !== digest || !receipt?.id) {
    receipt = {id: crypto.randomUUID(), digest};
    try { sessionStorage.setItem(key, JSON.stringify(receipt)); } catch { /* Keep the key in memory for retries. */ }
  }
  state.messageSaveIds.set(key, receipt);
  return {id: receipt.id, clear() {
    state.messageSaveIds.delete(key);
    try { sessionStorage.removeItem(key); } catch { /* In-memory key is cleared. */ }
  }};
}
function pendingDraftKey(userId, key) { return `roundtable:pending-draft:${userId}:${key}`; }
function rememberPendingDraft(userId, key, payload) {
  try { sessionStorage.setItem(pendingDraftKey(userId, key), JSON.stringify(payload)); } catch { /* Server save remains available. */ }
}
function forgetPendingDraft(userId, key, payload) {
  try {
    const storageKey = pendingDraftKey(userId, key);
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    if (!payload || (saved?.writer_id === payload.writer_id && saved?.sequence === payload.sequence)) sessionStorage.removeItem(storageKey);
  } catch { /* Storage may be unavailable. */ }
}
function draftStatus(text = "") {
  const node = $("#draft-status"); if (!node) return;
  node.textContent = text; node.hidden = !text;
}
function queueDraft(key, draft) {
  if (!state.user || !state.draftsReady) return;
  const userId = state.user.id; const sequenceKey = `roundtable:draft-seq:${userId}:${key}`;
  let stored = 0; try { stored = Number(sessionStorage.getItem(sequenceKey)) || 0; } catch {}
  const sequence = Math.max(state.draftSequence.get(`${userId}:${key}`) || 0, stored) + 1;
  try { sessionStorage.setItem(sequenceKey, String(sequence)); } catch {}
  state.draftSequence.set(`${userId}:${key}`, sequence);
  const payload = { conversation_id: key === "new" ? null : key, project_id: key === "new" ? draft.project || null : null,
    content: draft.text, writer_id: state.draftWriter, sequence };
  state.draftPending.set(key, { userId, payload }); rememberPendingDraft(userId, key, payload);
  clearTimeout(state.draftTimers.get(key));
  state.draftTimers.set(key, setTimeout(() => { void flushDraft(key); }, 400));
}
async function flushDraft(key) {
  const pending = state.draftPending.get(key); if (!pending || state.user?.id !== pending.userId) return;
  clearTimeout(state.draftTimers.get(key)); state.draftTimers.delete(key);
  try {
    const result = await api("/api/drafts", { method: "POST", body: pending.payload });
    if (result.stale && state.draftPending.get(key) === pending) {
      state.draftSequence.set(`${pending.userId}:${key}`, Math.max(result.sequence || 0, state.draftSequence.get(`${pending.userId}:${key}`) || 0));
      queueDraft(key, {text: pending.payload.content, project: pending.payload.project_id || ""});
      return;
    }
    forgetPendingDraft(pending.userId, key, pending.payload);
    if (state.draftPending.get(key) === pending) state.draftPending.delete(key);
    if (!state.draftPending.size) draftStatus();
  } catch {
    if (state.user?.id === pending.userId) {
      draftStatus("草稿暂未保存，请检查网络；系统会继续尝试保存。");
      if (state.draftPending.get(key) === pending) state.draftTimers.set(key, setTimeout(() => { void flushDraft(key); }, 5000));
    }
  }
}
async function flushDrafts() { await Promise.all([...state.draftPending.keys()].map((key) => flushDraft(key))); }
async function loadDrafts() {
  const userId = state.user.id;
  // Replay a not-yet-acknowledged save from this tab before loading the server copy.
  try {
    const prefix = `roundtable:pending-draft:${userId}:`;
    for (let index = sessionStorage.length - 1; index >= 0; index--) {
      const key = sessionStorage.key(index); if (!key?.startsWith(prefix)) continue;
      const value = JSON.parse(sessionStorage.getItem(key));
      await api("/api/drafts", { method: "POST", body: value });
      if (state.user?.id !== userId) return;
      sessionStorage.removeItem(key);
    }
  } catch { /* A later save can retry; do not discard the stored input. */ }
  const data = await api("/api/drafts");
  if (state.user?.id !== userId) return;
  state.drafts.clear();
  for (const item of data.drafts || []) state.drafts.set(item.conversation_id || "new", {text: item.content, project: item.project_id || ""});
  // If replay failed, prefer the unsent text still held in this tab.
  try {
    const prefix = `roundtable:pending-draft:${userId}:`;
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index); if (!key?.startsWith(prefix)) continue;
      const value = JSON.parse(sessionStorage.getItem(key));
      if (typeof value?.content === "string") {
        const draftKey = key.slice(prefix.length);
        state.drafts.set(draftKey, {text: value.content, project: value.project_id || ""});
        state.draftPending.set(draftKey, {userId, payload: value});
        state.draftTimers.set(draftKey, setTimeout(() => { void flushDraft(draftKey); }, 2000));
        draftStatus("草稿尚未同步，正在重试。");
      }
    }
  } catch { /* The server copy still works. */ }
  state.draftsReady = true;
}
function forgetDraft(key, expectedText) {
  const draft = state.drafts.get(key);
  if (!draft || (expectedText !== undefined && draft.text !== expectedText)) return;
  state.drafts.delete(key); queueDraft(key, {text: "", project: ""});
}
function rememberDraft() {
  if (!state.user || !state.draftsReady) return;
  const key = state.conversation?.id || "new";
  const draft = {text: $("#message-input").value, project: state.conversation ? "" : $("#composer-project").value};
  const prior = state.drafts.get(key);
  if ((!prior && !draft.text && !draft.project) || (prior?.text === draft.text && prior?.project === draft.project)) return;
  state.drafts.set(key, draft); queueDraft(key, draft);
}
function resizeComposer() {
  const input = $("#message-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(180, Math.max(60, input.scrollHeight))}px`;
  input.style.overflowY = input.scrollHeight > 180 ? "auto" : "hidden";
  if ($("#composer-count")) $("#composer-count").textContent = `${input.value.length.toLocaleString()} / ${input.maxLength.toLocaleString()}`;
}
function restoreDraft() {
  const draft = state.drafts.get(state.conversation?.id || "new");
  $("#message-input").value = draft?.text || "";
  if (!state.conversation) $("#composer-project").value = draft?.project || "";
  resizeComposer();
}
function showAuth() {
  resetSources();
  qualityUI?.reset();
  closeAiConsent(); state.messageSignature = ""; state.messageEdit = null; state.scrollConversation = null;
  closeAcceptanceEditor(); state.acceptanceStatus = null;
  stopReplyWaiting("navigation");
  state.viewVersion++; state.authVersion++; state.workspaceVersion++;
  state.capabilityVersion++; state.capabilities = null; state.capabilityError = ""; state.aiEnabled = false; state.aiConsentFor = null; state.activeSave = null; state.replyFeedback = {};
  state.saveOnlyMode = false;
  for (const timer of state.draftTimers.values()) clearTimeout(timer);
  state.draftTimers.clear(); state.draftPending.clear(); state.draftsReady = false;
  draftStatus();
  state.drafts.clear(); if ($("#conversation-search")) $("#conversation-search").value = "";
  state.teamChatDrafts.clear(); state.teamChatSending.clear();
  state.pendingSubmissions.clear();
  state.deliveryMode = "text"; state.participantCount = ""; state.artifactVersion++; $("#delivery-mode").value = "text";
  state.user = null; state.auth = null; state.conversation = null; state.messages = []; state.tasks = []; state.projects = []; state.conversations = [];
  $("#admin-release-nav").hidden = true;
  $("#messages").replaceChildren(); $("#conversation-list").replaceChildren(); $("#artifacts-list").replaceChildren(); $("#work-log-list").replaceChildren(); delete $("#work-log-list").dataset.signature;
  $("#live-draft").hidden = true; $("#live-draft-content").textContent = "";
  $("#team-runs").replaceChildren(); delete $("#team-runs").dataset.signature; $("#team-members").replaceChildren(); $("#team-progress").hidden = true; $("#conversation-menu").open = false; $("#team-record").open = false;
  document.querySelectorAll(".content-view").forEach((view) => view.replaceChildren());
  $("#message-input").value = ""; $("#composer-project").replaceChildren(el("option", { value: "", text: "未归入项目" }));
  $("#ai-enabled").checked = false; $("#ai-enabled").disabled = true; $("#reply-feedback").hidden = true; $("#connection-help").open = false;
  closeDialog();
  $("#auth-screen").hidden = false; $("#app-shell").hidden = true; $("#loading-screen").hidden = true;
  $("#auth-password").value = ""; $("#auth-code").value = ""; $("#auth-code-field").hidden = true;
  closeSidebar();
}
function setAuthMode(mode) {
  const ordinary = mode === "login" || mode === "register";
  state.mode = mode; $("#tab-login").classList.toggle("active", mode === "login"); $("#tab-register").classList.toggle("active", mode === "register");
  $("#auth-heading").textContent = ({ login: "欢迎回来", register: "留一个位置给想法", recover: "找回密码", reset: "设置新密码" })[mode];
  $("#auth-description").textContent = ({ login: "继续你的想法与项目。", register: "创建属于你的圆桌空间。", recover: state.recoveryScope === "this_computer" ? "输入账号邮箱，并在这台电脑上打开收到的链接。" : "输入账号邮箱，我们会发送恢复链接。", reset: "设置新密码后，请重新登录。" })[mode];
  $("#auth-tabs").hidden = !ordinary; $("#auth-form").hidden = !ordinary;
  $("#auth-forgot").hidden = mode !== "login"; $("#auth-back").hidden = ordinary;
  $("#recovery-request-form").hidden = mode !== "recover"; $("#recovery-confirm-form").hidden = mode !== "reset";
  $("#auth-info").hidden = true; $("#recovery-request-error").hidden = true; $("#recovery-confirm-error").hidden = true;
  $("#auth-name-field").hidden = mode !== "register"; $("#register-note").hidden = mode !== "register";
  $("#auth-code-field").hidden = true; $("#auth-error").hidden = true;
  $("#auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("#auth-password").minLength = mode === "register" ? 12 : 1;
  $("#auth-submit").textContent = mode === "login" ? "登录圆桌" : "创建账号";
}

async function useAuth(data) {
  if (state.user?.id !== data.user?.id) state.drafts.clear();
  state.authVersion++; state.auth = data; state.user = data.user; state.recoveryScope = data.recovery_scope || null;
  if (!data.authenticated) { showAuth(); return; }
  $("#admin-release-nav").hidden = data.user.role !== "admin";
  $("#auth-screen").hidden = true; $("#app-shell").hidden = false; $("#loading-screen").hidden = true;
  $("#user-name").textContent = data.user.display_name || "我的账号"; $("#user-email").textContent = data.user.email || "";
  $("#user-avatar").textContent = (data.user.display_name || data.user.email || "你").slice(0, 1).toUpperCase();
  $("#auth-password").value = ""; $("#auth-code").value = "";
  if (isMfaBlocked()) { await switchView("security"); return; }
  $("#message-input").disabled = true;
  try { await loadDrafts(); }
  catch (error) { state.draftsReady = false; draftStatus(error.status === 404
    ? "草稿更新需要重启圆桌服务后生效，当前输入不会自动保存。"
    : "暂时无法读取或保存草稿，请检查网络后刷新。"); }
  try {
    await Promise.all([refreshWorkspace(), refreshCapabilities().catch(() => {})]);
    restoreDraft(); await switchView("roundtable");
  } finally { $("#message-input").disabled = false; renderModelControls(); }
  let previous; try { previous = sessionStorage.getItem(`roundtable:conversation:${state.user.id}`); } catch {}
  if (previous && state.conversations.some((item) => item.id === previous)) await openConversation(previous);
}

async function refreshWorkspace() {
  const version = ++state.workspaceVersion;
  const [projectData, conversationData] = await Promise.all([api("/api/projects"), api("/api/conversations")]);
  if (version !== state.workspaceVersion) return;
  state.projects = projectData.projects || []; state.conversations = conversationData.conversations || [];
  const selection = $("#composer-project").value;
  $("#composer-project").replaceChildren(el("option", { value: "", text: "未归入项目" }), ...state.projects.map((item) => el("option", { value: item.id, text: item.title })));
  if (state.projects.some((item) => item.id === selection)) $("#composer-project").value = selection;
  renderConversationList();
}

function renderConversationList() {
  const list = $("#conversation-list");
  if (!state.conversations.length) { list.replaceChildren(el("p", { class: "sidebar-empty", text: "你的想法，会留在这里。" })); return; }
  const query = ($("#conversation-search")?.value || "").trim().toLocaleLowerCase();
  const matches = state.conversations.filter((item) => (item.title || "新对话").toLocaleLowerCase().includes(query));
  if (!matches.length) { list.replaceChildren(el("p", {class: "sidebar-empty", text: "没有找到相关对话，试试其他关键词。"})); return; }
  list.replaceChildren(...matches.map((item) => {
    const entry = button(item.title || "新对话", () => openConversation(item.id), `conversation-item${state.conversation?.id === item.id ? " selected" : ""}`);
    entry.title = item.title || "新对话";
    if (state.conversation?.id === item.id) entry.setAttribute("aria-current", "page");
    return entry;
  }));
}

function chatAvailable() {
  const team = state.capabilities?.team;
  return team?.readiness === "available" && typeof team.revision === "string" && Boolean(team.revision);
}
function modelIdentity() {
  const revision = state.capabilities?.team?.revision; const cases = state.conversation?.acceptance_cases || [];
  return revision ? JSON.stringify([revision, state.deliveryMode === "text" ? "decision_v1" : "adaptive", state.deliveryMode, state.deliveryMode === "text" ? state.participantCount : "", cases,
    ...(state.conversation?.sources_count ? [state.conversation.id, state.conversation.sources_revision] : []),
    ...(cases.length ? [state.conversation?.id, currentTask()?.id || null, currentTask()?.goal || ""] : [])]) : "";
}
function deliveryLimit(mode = state.deliveryMode) {
  if (mode === "text") {
    const configured = state.capabilities?.team?.decision_max_calls;
    return Number.isInteger(configured) && configured > 0 ? configured : 14;
  }
  const configured = state.capabilities?.team?.adaptive_modes?.find((item) => item.id === (mode === "files" ? "files" : "text"))?.max_calls;
  return Number.isInteger(configured) && configured > 0 ? configured : mode === "files" ? 10 : 8;
}
function setDeliveryMode(mode) {
  mode = ["files", "text_adaptive"].includes(mode) ? mode : "text";
  if (mode !== state.deliveryMode) { state.aiEnabled = false; state.aiConsentFor = null; closeAiConsent(); }
  state.deliveryMode = mode; $("#delivery-mode").value = mode;
}
function restoreConversationMode() {
  const runs = state.tasks.filter(task => !task.superseded_by).flatMap((task) => task.runs || []).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const latest = runs[0];
  setDeliveryMode(state.conversation?.acceptance_cases?.length || latest?.delivery_mode === "files" ? "files" : latest && latest.workflow !== "decision_v1" ? "text_adaptive" : "text");
}
function providerName(provider) { return ({ deepseek: "DeepSeek", kimi: "Kimi", qwen: "千问", gateway: "AI Gateway" })[provider] || provider || "未配置服务"; }
function modelName() { return [...new Set((state.capabilities?.team?.roles || []).map((member) => `${member.provider_label || providerName(member.provider)} · ${member.model || "未配置模型"}`))].join("；") || "当前团队"; }
function applyCapabilities(data) {
  const previous = modelIdentity(); state.capabilities = data; state.capabilityError = "";
  if (previous !== modelIdentity()) { state.aiEnabled = false; state.aiConsentFor = null; }
  if (!chatAvailable()) state.aiEnabled = false;
  renderModelControls();
}
async function refreshCapabilities() {
  const version = ++state.capabilityVersion;
  try { const data = await api("/api/capabilities"); if (version === state.capabilityVersion) applyCapabilities(data); return data; }
  catch (error) { if (version === state.capabilityVersion) { state.capabilities = null; state.capabilityError = "暂时无法读取团队状态，请打开“团队与模型”刷新。需求仍可保存。"; state.aiEnabled = false; renderModelControls(); } throw error; }
}
function renderModelControls() {
  const team = state.capabilities?.team; const available = chatAvailable();
  const pending = Boolean(state.activeReply) && state.activeReply.conversationId === state.conversation?.id;
  const task = currentTask(); const running = pending || task?.status === "running";
  const clarifying = task?.runs?.[0]?.status === "awaiting_input" && !state.replyFeedback[state.conversation?.id]?.briefStale;
  const unresolved = Boolean(pendingSubmission(state.conversation?.id));
  if (state.pendingConsent && (state.pendingConsent !== modelIdentity() || running || !available)) closeAiConsent();
  if ((state.conversation?.acceptance_cases?.length || state.conversation?.sources_count) && state.aiConsentFor !== modelIdentity()) state.aiEnabled = false;
  const sourcesBusy = Boolean(sourcesUI?.getState().busy);
  $("#ai-enabled").checked = state.aiEnabled; $("#ai-enabled").disabled = !available || running || sourcesBusy || unresolved;
  $("#delivery-mode").value = state.deliveryMode; $("#delivery-mode").disabled = running || Boolean(state.activeSave);
  $("#participant-count-control").hidden = state.deliveryMode !== "text";
  $("#participant-count-note").hidden = state.deliveryMode !== "text";
  $("#participant-count").value = state.participantCount;
  $("#participant-count").disabled = running || Boolean(state.activeSave) || unresolved;
  $("#delivery-mode-note").textContent = state.deliveryMode === "files" ? "先规划与分工，再交付文件和检查记录；必需工具不可用时会说明原因。" : state.deliveryMode === "text_adaptive" ? "沿用按任务规划与分工的原文字流程。" : "需要更多信息时，成员会在对话里问你。";
  $("#message-input").placeholder = clarifying ? "回复圆桌成员…" : "向圆桌提问，或交代一个目标";
  $("#message-input").maxLength = clarifying ? 2000 : 12000;
  $("#connection-summary").textContent = state.capabilityError || team?.notice || (team ? "团队仍需完成模型连接与验收。" : "正在读取团队状态。");
  $("#team-members").replaceChildren(...(team?.roles || []).map((member) => el("div", { class: "team-member" }, [el("strong", { text: member.label || member.role }), el("span", { text: `${member.provider_label || providerName(member.provider)} · ${member.model || "未配置模型"}` }), el("small", { text: member.ready ? "已验收" : "待验收" })])));
  $("#connection-instructions").textContent = state.deliveryMode === "text" ? "团队先分别独立分析，再交叉核验和回应；模型分工及实际效果仍需实测比较。" : "主助手先安排2至4项成员工作，再按依赖组织执行、交叉审查与核验。具体成员任务会出现在协作记录中；最佳模型仍需实测比较。";
  $("#availability-text").textContent = clarifying ? "回复或跳过后，团队将按本轮已确认的额度继续。" : running ? "后台处理本轮任务 · 停止后可继续发送" : state.capabilityError || (available ? (state.aiEnabled ? `AI 已开启 · 最多 ${deliveryLimit()} 次调用，使用模型额度` : state.saveOnlyMode ? "仅保存到圆桌" : "首次发送时确认参与的模型和费用") : team?.configured ? "模型待验收 · 当前仅保存需求" : team ? "AI 未配置 · 当前仅保存需求" : "读取团队状态中…");
  if ($("#composer-options-label")) $("#composer-options-label").textContent = state.deliveryMode === "files" ? "多文件成果" : state.deliveryMode === "text_adaptive" ? "文字任务" : "文字讨论";
  const sendForDiscussion = available && (state.aiEnabled || !state.saveOnlyMode);
  const actionLabel = clarifying ? "发送回答并继续" : running ? "协作中" : task?.status === "paused" ? "保存补充" : sendForDiscussion ? "发送并开始团队协作" : "保存需求";
  $("#send-message").setAttribute("aria-label", actionLabel); $("#send-message").title = actionLabel;
  $("#composer-hint").textContent = `Enter ${actionLabel} · Shift + Enter 换行`;
  if ($("#send-label")) $("#send-label").textContent = state.activeSave ? "保存中…" : actionLabel;
  $("#send-message").disabled = Boolean(state.activeSave) || running || unresolved || sourcesBusy || !$("#message-input").value.trim();
  $("#delete-conversation").disabled = running || unresolved || Boolean(state.activeSave);
  $("#composer").setAttribute("aria-busy", String(Boolean(state.activeSave)));
  const editReason = messageEditBlockedReason();
  document.querySelectorAll(".message-edit-action").forEach((edit) => {
    edit.setAttribute("aria-disabled", String(Boolean(editReason)));
    edit.title = editReason || "编辑消息";
  });
  renderInterjectionControls();
  const budget = state.capabilities?.hourly_budget;
  const reset = budget?.resets_at ? new Date(budget.resets_at) : null;
  if (!running && !clarifying && available && !state.saveOnlyMode && reset > new Date() && budget.remaining < deliveryLimit()) {
    const when = reset.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
    $("#availability-text").textContent = `本小时还可调用 ${budget.remaining} 次，${when} 恢复。${budget.remaining ? "较长的讨论可能提前结束。" : "现在仍可保存问题。"}`;
  }
  renderAcceptanceControls(running);
  sourcesUI?.setContext({conversationId: state.view === "roundtable" ? state.conversation?.id || null : null,
    allowDraft: Boolean(state.user && state.view === "roundtable"),
    projectId: $("#composer-project").value, revision: state.conversation?.sources_revision,
    running: running || Boolean(state.activeSave), blocked: isMfaBlocked()});
  renderReplyFeedback();
}
function acceptanceContextCurrent(context) {
  return Boolean(state.user) && context.authVersion === state.authVersion && context.viewVersion === state.viewVersion && state.view === "roundtable" && context.conversationId === state.conversation?.id;
}
function renderAcceptanceControls(running = currentTask()?.status === "running") {
  $("#acceptance-controls").hidden = state.deliveryMode !== "files";
  $("#acceptance-open").disabled = !state.conversation || running || Boolean(state.activeSave);
  const cases = state.conversation?.acceptance_cases || [];
  const known = state.acceptanceStatus;
  const unavailable = known && acceptanceContextCurrent(known) && known.revision === state.conversation?.acceptance_revision && known.available === false;
  $("#acceptance-summary").textContent = !state.conversation ? "先保存需求" : cases.length
    ? `${cases.length} 个用例 · ${unavailable ? "环境待准备" : "已保存"}${running ? " · 本轮已锁定" : ""}`
    : "可选 · Python 输入输出用例";
  const editor = state.acceptanceEditor;
  if (editor && acceptanceContextCurrent(editor)) syncAcceptanceEditorControls(editor);
}
function closeAcceptanceEditor() {
  state.acceptanceEditor = null;
  $("#acceptance-dialog").close();
  $("#acceptance-cases").replaceChildren();
}
function acceptanceEditorCurrent(editor) {
  return state.acceptanceEditor === editor && acceptanceContextCurrent(editor) && $("#acceptance-dialog").open;
}
function syncAcceptanceEditorControls(editor) {
  const running = currentTask()?.status === "running" || state.activeReply?.conversationId === editor.conversationId;
  const disabled = editor.loading || editor.saving || running;
  $("#acceptance-form").setAttribute("aria-busy", String(editor.loading || editor.saving));
  $("#acceptance-save").disabled = disabled || !editor.loaded || editor.conflict;
  $("#acceptance-save").textContent = editor.saving ? "保存中…" : "保存用例";
  $("#acceptance-add").disabled = disabled || !editor.loaded || editor.cases.length >= 5;
  $("#acceptance-close").disabled = editor.saving;
  $("#acceptance-cancel").disabled = editor.saving;
  $("#acceptance-reload").disabled = disabled;
  $("#acceptance-cases").querySelectorAll("textarea, button").forEach((node) => { node.disabled = disabled; });
  $("#acceptance-count").textContent = editor.loaded ? `${editor.cases.length} / 5 个用例` : "";
  if (running) $("#acceptance-environment").textContent = "团队正在协作，暂不能修改用例。请在本轮结束后重新打开。";
}
function renderAcceptanceEditor(editor) {
  $("#acceptance-cases").replaceChildren(...editor.cases.map((item, index) => {
    const remove = button("移除", () => {
      if (!acceptanceEditorCurrent(editor) || editor.saving || editor.loading || currentTask()?.status === "running") return;
      editor.cases.splice(index, 1); renderAcceptanceEditor(editor);
    }, "text-button");
    remove.setAttribute("aria-label", `移除用例 ${index + 1}`);
    return el("fieldset", { class: "acceptance-case" }, [
      el("legend", { text: `用例 ${index + 1}` }),
      el("div", { class: "acceptance-case-heading" }, [el("span", { text: "保留原文中的空格与换行" }), remove]),
      ...[["stdin", "标准输入"], ["expected_stdout", "预期标准输出"]].map(([key, label]) => {
        const id = `acceptance-${index}-${key}`; const countId = `${id}-count`;
        const input = el("textarea", { id, rows: 4, maxlength: 2000, spellcheck: "false", "aria-describedby": countId });
        input.value = item[key];
        const count = el("small", { id: countId, class: "acceptance-field-count", text: `${item[key].length} / 2,000 字符${item[key].endsWith("\n") ? " · 末尾有换行" : ""}` });
        input.addEventListener("input", () => { item[key] = input.value; count.textContent = `${item[key].length} / 2,000 字符${item[key].endsWith("\n") ? " · 末尾有换行" : ""}`; });
        return el("label", { for: id }, [label, input, count]);
      }),
    ]);
  }));
  if (!editor.cases.length) $("#acceptance-cases").append(notice("尚无用例。保存后，本对话将不设置用户输入输出验收。"));
  syncAcceptanceEditorControls(editor);
}
async function loadAcceptanceEditor(editor) {
  if (!acceptanceEditorCurrent(editor) || editor.loading || editor.saving) return;
  editor.loading = true; $("#acceptance-error").hidden = true; $("#acceptance-reload").hidden = true;
  $("#acceptance-environment").textContent = "正在读取已保存的用例与运行环境状态…";
  syncAcceptanceEditorControls(editor);
  try {
    const data = await api(`/api/conversations/${encodeURIComponent(editor.conversationId)}/acceptance-cases`);
    if (!acceptanceEditorCurrent(editor)) return;
    if (!Array.isArray(data.cases) || typeof data.revision !== "string") throw new Error("验收用例暂时无法读取，请重试。");
    const previous = modelIdentity();
    state.conversationRevision++;
    state.conversation = { ...state.conversation, acceptance_cases: data.cases, acceptance_revision: data.revision };
    if (previous !== modelIdentity()) { state.aiEnabled = false; state.aiConsentFor = null; }
    state.acceptanceStatus = { conversationId: editor.conversationId, authVersion: editor.authVersion, viewVersion: editor.viewVersion, revision: data.revision, available: data.execution_available };
    editor.cases = data.cases.map((item) => ({ stdin: item.stdin, expected_stdout: item.expected_stdout }));
    editor.revision = data.revision; editor.loaded = true; editor.conflict = false;
    $("#acceptance-environment").textContent = data.execution_available === false
      ? "运行环境未准备好：可以保存标准，当前不会运行，也不能认定通过。保存用例不会调用模型。"
      : data.execution_available === true ? "运行环境可用。保存仅记录标准，不会调用模型或立即运行；下一轮多文件协作时按此版本验收。"
      : "保存仅记录标准，不会调用模型或立即运行。运行环境状态尚未确认，用例保持待运行。";
    renderAcceptanceEditor(editor); renderModelControls();
  } catch (error) {
    if (acceptanceEditorCurrent(editor)) { $("#acceptance-error").textContent = error.message || "暂时无法读取用例，请重试。"; $("#acceptance-error").hidden = false; $("#acceptance-reload").hidden = false; $("#acceptance-environment").textContent = "暂未读取到已保存的标准。"; }
  } finally { if (acceptanceEditorCurrent(editor)) { editor.loading = false; syncAcceptanceEditorControls(editor); } }
}
async function openAcceptanceEditor() {
  if (!state.conversation) { showToast("保存需求后可设置验收用例。"); return; }
  if (state.activeSave || state.activeReply?.conversationId === state.conversation.id || currentTask()?.status === "running") return;
  const editor = { conversationId: state.conversation.id, authVersion: state.authVersion, viewVersion: state.viewVersion, cases: [], revision: "", loaded: false, loading: false, saving: false, conflict: false };
  state.acceptanceEditor = editor; $("#acceptance-cases").replaceChildren(); $("#acceptance-dialog").showModal();
  await loadAcceptanceEditor(editor);
}
async function saveAcceptanceEditor(event) {
  event.preventDefault(); const editor = state.acceptanceEditor;
  if (!editor || !acceptanceEditorCurrent(editor) || editor.loading || editor.saving || !editor.loaded || editor.conflict || currentTask()?.status === "running" || state.activeReply?.conversationId === editor.conversationId) return;
  if (editor.cases.length > 5 || editor.cases.some((item) => item.stdin.length > 2000 || item.expected_stdout.length > 2000)) { $("#acceptance-error").textContent = "最多保存 5 个用例，每个输入和预期输出最多 2,000 字符。"; $("#acceptance-error").hidden = false; return; }
  editor.saving = true; $("#acceptance-error").hidden = true; syncAcceptanceEditorControls(editor);
  try {
    const data = await api(`/api/conversations/${encodeURIComponent(editor.conversationId)}/acceptance-cases`, { method: "PUT", body: { cases: editor.cases.map((item) => ({ stdin: item.stdin, expected_stdout: item.expected_stdout })), expected_revision: editor.revision } });
    if (!acceptanceEditorCurrent(editor)) return;
    if (data.conversation?.id !== editor.conversationId || !Array.isArray(data.cases) || typeof data.revision !== "string") throw new Error("保存结果暂时无法确认。请重新载入用例后检查。");
    state.conversationRevision++;
    state.conversation = { ...data.conversation, acceptance_cases: data.cases, acceptance_revision: data.revision };
    const available = typeof data.execution_available === "boolean" ? data.execution_available : state.acceptanceStatus?.available;
    state.acceptanceStatus = { conversationId: editor.conversationId, authVersion: editor.authVersion, viewVersion: editor.viewVersion, revision: data.revision, available };
    state.aiEnabled = false; state.aiConsentFor = null;
    closeAcceptanceEditor(); renderModelControls();
    $("#acceptance-open").focus();
  } catch (error) {
    if (!acceptanceEditorCurrent(editor)) return;
    editor.conflict = error.status === 409;
    $("#acceptance-error").textContent = editor.conflict ? `${error.message || "用例已更新或团队正在协作。"} 本次修改仍保留；请重新载入已保存用例后再编辑。` : error.message || "用例暂未保存，本次修改仍保留，请重试。";
    $("#acceptance-error").hidden = false; $("#acceptance-reload").hidden = false;
  } finally { if (acceptanceEditorCurrent(editor)) { editor.saving = false; syncAcceptanceEditorControls(editor); } }
}
function renderReplyFeedback() {
  const task = currentTask(); const id = state.conversation?.id; const pending = state.activeReply?.conversationId === id && Boolean(id);
  const feedback = id ? state.replyFeedback[id] : null;
  const unresolved = pendingSubmission(id);
  let text = feedback?.text || "";
  if (unresolved && !pending) text = "本次提交的回执尚未确认。请先核对状态；系统不会自动再次提交，已发出的请求可能使用额度。";
  const needsBrief = task?.runs?.[0]?.status === "awaiting_input" && !feedback?.briefStale;
  if (needsBrief) text = feedback?.error ? text : "";
  else if (task?.runs?.[0]?.status === "needs_review" && !text) text = "核验发现待修订项，可查看协作记录并补充要求。";
  else if (task?.status === "failed" && !text) text = task.runs?.[0]?.error || "本轮未完成。需求已保存，可查看记录后重新协作。";
  else if (task?.status === "waiting" && !text) {
    text = !chatAvailable() ? (state.capabilityError || (state.capabilities ? "需求已保存，团队尚未就绪。请在对话设置中查看团队与模型。" : "需求已保存，正在读取团队状态。"))
      : state.aiEnabled ? "需求已保存，可以开始协作。" : "需求已保存，尚未开始回复。请先开启 AI 团队。";
  }
  $("#reply-feedback").hidden = !text && !needsBrief; $("#reply-feedback-text").hidden = !text; $("#reply-feedback-text").textContent = text;
  $("#reply-feedback").classList.toggle("has-error", !pending && (feedback?.error || task?.status === "failed"));
  $("#request-reply").hidden = needsBrief || Boolean(unresolved) || !task || !["waiting", "failed"].includes(task.status) || pending || !chatAvailable();
  $("#skip-clarification").hidden = !needsBrief;
  $("#skip-clarification").disabled = Boolean(state.activeSave) || Boolean(state.activeReply);
  $("#request-reply").textContent = feedback?.briefStale ? "重新整理问题" : !state.aiEnabled ? "开启 AI 团队" : task?.status === "failed" || feedback?.error ? "重新协作" : "开始团队协作";
  $("#request-reply").disabled = !chatAvailable() || Boolean(state.activeSave) || Boolean(state.activeReply) || (!needsBrief && !state.aiEnabled && $("#ai-enabled").disabled);
  $("#retry-pending-submission").hidden = !unresolved || pending || !task || !["waiting", "failed"].includes(task.status) || task.id !== unresolved.body.expected_task;
  $("#retry-pending-submission").disabled = Boolean(state.activeSave) || Boolean(state.activeReply);
  $("#stop-waiting").hidden = true;
  $("#refresh-conversation").hidden = !id || (!unresolved && (!task || !feedback?.error));
  $("#refresh-conversation").textContent = unresolved ? "核对提交状态" : "刷新状态";
  $("#refresh-conversation").disabled = Boolean(state.activeSave);
}
function stopReplyWaiting(reason) {
  const request = state.activeReply; if (!request) return;
  state.activeReply = null; clearReplyMonitoring(request);
  if (reason === "pause") state.replyFeedback[request.conversationId] = { text: "本轮已停止。已发出的调用可能仍计费，迟到结果不会写入。" };
  request.controller.abort();
}
function clearReplyMonitoring(request) {
  request.monitoringStopped = true;
  clearTimeout(request.timeout); clearTimeout(request.pollTimer); clearTimeout(request.pollTimeout);
  request.pollController?.abort();
}
function scheduleReplyPoll(request) {
  clearTimeout(request.pollTimer);
  const run = currentTask()?.runs?.[0];
  const waitingForFirstReply = run?.workflow === "decision_v1" &&
    !(run.discussion || []).some(item => item.kind === "assistant" && item.content);
  const finishing = (run?.steps || []).some(step => step.role === "synthesizer" && step.status === "running");
  const interval = waitingForFirstReply || finishing ? 650 : 1500;
  request.pollTimer = setTimeout(async () => {
    if (request.monitoringStopped || state.activeReply !== request || !replyContextCurrent(request)) return;
    const revision = state.conversationRevision;
    request.pollController = new AbortController();
    request.pollTimeout = setTimeout(() => request.pollController?.abort(), 10000);
    try {
      let version = null;
      if (request.runId) {
        const progress = await api(`/api/conversations/${encodeURIComponent(request.conversationId)}/runs/${encodeURIComponent(request.runId)}/status`, { signal: request.pollController.signal });
        if (request.monitoringStopped || state.activeReply !== request || !replyContextCurrent(request) || revision !== state.conversationRevision) return;
        version = progress.version;
        if (version && version === request.progressVersion && progress.task_status === "running") {
          request.pollFailures = 0;
          if (state.replyFeedback[request.conversationId]?.pollError) { delete state.replyFeedback[request.conversationId]; renderReplyFeedback(); }
          return;
        }
      }
      const data = await api(`/api/conversations/${encodeURIComponent(request.conversationId)}`, { signal: request.pollController.signal });
      if (request.monitoringStopped || state.activeReply !== request || !replyContextCurrent(request) || revision !== state.conversationRevision) return;
      state.conversation = data.conversation; state.messages = data.messages || []; state.tasks = data.tasks || [];
      request.progressVersion = version;
      request.pollFailures = 0;
      if (state.replyFeedback[request.conversationId]?.pollError) delete state.replyFeedback[request.conversationId];
      if (currentTask()?.status !== "running") {
        clearReplyMonitoring(request); state.activeReply = null;
        renderChat(true); renderConversationList();
        if (!$("#artifacts-panel").hidden) await loadConversationArtifacts();
        void refreshWorkspace().catch(() => {});
        void refreshCapabilities().catch(() => {});
        return;
      }
      renderChat(true);
    } catch {
      if (request.monitoringStopped || state.activeReply !== request || !replyContextCurrent(request)) return;
      request.pollFailures = (request.pollFailures || 0) + 1;
      if (request.pollFailures >= 2) {
        state.replyFeedback[request.conversationId] = { text: "进度连接暂时中断，正在重连。不会重复提交任务。", error: true, pollError: true };
        renderReplyFeedback();
      }
    }
    finally {
      clearTimeout(request.pollTimeout);
      if (!request.monitoringStopped && state.activeReply === request && replyContextCurrent(request)) scheduleReplyPoll(request);
    }
  }, Math.min(10000, interval * (1 + (request.pollFailures || 0))));
}
function resumeReplyMonitoring() {
  if (state.view !== "roundtable" || !state.conversation || state.activeReply || currentTask()?.status !== "running") return;
  const request = {conversationId: state.conversation.id, taskId: currentTask().id, runId: currentTask().runs?.[0]?.id, viewVersion: state.viewVersion, authVersion: state.authVersion, controller: new AbortController(), submitted: true};
  state.activeReply = request; scheduleReplyPoll(request); renderModelControls();
}
function replyContextCurrent(request) {
  return state.user && request.authVersion === state.authVersion && request.viewVersion === state.viewVersion && state.view === "roundtable" && state.conversation?.id === request.conversationId;
}
async function reloadConversation(id, version = state.viewVersion) {
  const revision = state.conversationRevision;
  const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
  if (version !== state.viewVersion || revision !== state.conversationRevision || state.view !== "roundtable" || state.conversation?.id !== id) return false;
  state.conversation = data.conversation; state.messages = data.messages || []; state.tasks = data.tasks || [];
  if (["completed", "paused"].includes(currentTask()?.status)) delete state.replyFeedback[id];
  renderChat(true); renderConversationList(); resumeReplyMonitoring(); return true;
}
async function submitClarification(answer, previous = null) {
  const run = previous || currentTask()?.runs?.[0]; const conversationId = state.conversation?.id;
  if (!conversationId || !run || (!previous && (run.status !== "awaiting_input" || state.activeReply)) || state.activeSave) return;
  if (answer.length > 2000) { showToast("补充回答最多 2000 字。", true); return; }
  const operation = {version: state.viewVersion, authVersion: state.authVersion, conversationId};
  if (answer) saveInterjectionDraft(conversationId, {kind: "clarification", runId: run.id, text: answer, maxCalls: run.max_calls});
  state.activeSave = operation; state.conversationRevision++; renderModelControls();
  try {
    await api(`/api/conversations/${encodeURIComponent(conversationId)}/confirm-decision`, {method: "POST", signal: AbortSignal.timeout?.(30000),
      body: {run_id: run.id, answer, confirm_continue: true, expected_max_calls: run.max_calls}});
    if (operation.authVersion !== state.authVersion) return;
    if (answer && interjectionDraft(conversationId)?.kind === "clarification" && interjectionDraft(conversationId)?.text === answer) {
      saveInterjectionDraft(conversationId, null);
      const draftText = state.drafts.get(conversationId)?.text;
      if (draftText?.trim() === answer) forgetDraft(conversationId, draftText);
    }
    if (state.view !== "roundtable" || state.conversation?.id !== conversationId) return;
    if (answer && $("#message-input").value.trim() === answer) {
      const draftText = $("#message-input").value;
      forgetDraft(conversationId, draftText); $("#message-input").value = ""; rememberDraft();
    }
    delete state.replyFeedback[conversationId]; state.followLatest = true;
    await reloadConversation(conversationId, state.viewVersion);
  } catch (error) {
    if (operation.authVersion !== state.authVersion || operation.version !== state.viewVersion || state.conversation?.id !== conversationId) return;
    const stale = ["brief_stale", "sources_changed", "team_changed", "acceptance_changed"].includes(error.code);
    if (stale && interjectionDraft(conversationId)?.kind === "clarification") saveInterjectionDraft(conversationId, null);
    state.replyFeedback[conversationId] = {text: stale ? "需求或配置已改变，请重新整理问题。" : "回复状态暂未确认；可用相同内容重试或刷新查看，不会重复开始本轮。", error: true, briefStale: stale};
    renderReplyFeedback();
  } finally {
    if (state.activeSave === operation) state.activeSave = null;
    if (operation.authVersion === state.authVersion) renderModelControls();
  }
}
async function requestReply(reusePending = false) {
  const task = currentTask();
  if (task?.runs?.[0]?.status === "awaiting_input" && !state.replyFeedback[state.conversation?.id]?.briefStale) return;
  if (sourcesUI?.getState().busy) return;
  if (!state.conversation || state.activeReply || state.activeSave || !["waiting", "failed"].includes(task?.status)) return;
  const saved = pendingSubmission(state.conversation.id);
  if (reusePending ? (!saved || saved.body.expected_task !== task.id) : (saved || !chatAvailable() || !state.aiEnabled || state.aiConsentFor !== modelIdentity())) return;
  const clientRequestId = reusePending ? saved.id : crypto.randomUUID();
  const body = reusePending ? saved.body : { background: true, client_request_id: clientRequestId, confirm_cost: true,
    expected_team: state.capabilities.team.revision, expected_acceptance: state.conversation.acceptance_revision,
    expected_task: task.id, expected_task_updated_at: task.updated_at, expected_sources: state.conversation.sources_revision,
    delivery_mode: state.deliveryMode === "files" ? "files" : "text", workflow: state.deliveryMode === "text" ? "decision_v1" : "adaptive", expected_max_calls: deliveryLimit(),
    ...(state.deliveryMode === "text" && state.participantCount ? {participant_count: Number(state.participantCount)} : {}) };
  const request = { conversationId: state.conversation.id, taskId: task.id, viewVersion: state.viewVersion, authVersion: state.authVersion, controller: new AbortController(), clientRequestId };
  if (!reusePending && !rememberSubmission(request.conversationId, {id: clientRequestId, body})) {
    showToast("浏览器无法保存本次提交标识，请允许会话存储后重试；本轮未开始。", true); return;
  }
  state.conversationRevision++; state.activeReply = request; delete state.replyFeedback[request.conversationId]; task.status = "running"; renderChat();
  request.timeout = setTimeout(() => request.controller.abort(), 30000);
  try {
    const result = await api(`/api/conversations/${encodeURIComponent(request.conversationId)}/collaborate`, { method: "POST", body, signal: request.controller.signal });
    if (state.activeReply !== request || !replyContextCurrent(request)) return;
    if (!result.run?.id || !result.task?.id) throw new Error("提交回执不完整，请核对状态。");
    request.runId = result.run.id;
    forgetSubmission(request.conversationId, request.clientRequestId);
    clearTimeout(request.timeout);
    if (result.message && !state.messages.some((message) => message.id === result.message.id)) state.messages.push(result.message);
    if (result.task && result.run) result.task.runs = [result.run, ...(task.runs || []).filter((item) => item.id !== result.run.id)];
    if (result.task) state.tasks = [result.task, ...state.tasks.filter((item) => item.id !== result.task.id)];
    if (result.queued || ["queued", "running"].includes(result.run?.status)) {
      request.submitted = true;
      renderChat(true); scheduleReplyPoll(request);
      void refreshWorkspace().catch(() => {});
      return;
    }
    clearReplyMonitoring(request);
    renderChat();
    try { await reloadConversation(request.conversationId, request.viewVersion); } catch { if (replyContextCurrent(request)) showToast("结果已保存，协作记录暂未刷新。请稍后刷新对话状态。"); }
    if (state.activeReply !== request || !replyContextCurrent(request)) return;
    state.activeReply = null; renderChat();
    await refreshWorkspace().catch(() => {});
  } catch (error) {
    if (state.activeReply !== request || !replyContextCurrent(request)) return;
    if ((!reusePending && error.status >= 400 && error.status < 500 && error.status !== 408) ||
        (reusePending && error.code === "acceptance_task_changed")) forgetSubmission(request.conversationId, request.clientRequestId);
    else { state.aiEnabled = false; state.aiConsentFor = null; }
    state.activeReply = null;
    clearReplyMonitoring(request);
    const taskChanged = error.code === "acceptance_task_changed";
    const taskChangedText = "任务已更新，本次协作未启动。请核对最新需求与验收用例，重新确认 AI 团队后再开始。";
    if (["connection_changed", "team_changed", "acceptance_changed", "acceptance_task_changed", "sources_changed"].includes(error.code)) { state.aiEnabled = false; state.aiConsentFor = null; }
    state.replyFeedback[request.conversationId] = { text: taskChanged ? taskChangedText : ["connection_changed", "team_changed"].includes(error.code) ? "团队或模型连接已变更，协作已停止。你的原文已保存，请刷新团队状态，重新开启并确认后再开始；已经发出的请求可能产生费用。" : `${error.message || "网络连接中断。"} 你的原文已保存，无需再次发送。`, error: true };
    renderModelControls();
    try {
      await reloadConversation(request.conversationId, request.viewVersion);
      if (taskChanged && replyContextCurrent(request)) state.replyFeedback[request.conversationId] = { text: taskChangedText, error: true };
    }
    catch { if (replyContextCurrent(request)) state.replyFeedback[request.conversationId] = { text: taskChanged ? "任务已更新，本次协作未启动，暂未读取到最新需求。请先刷新对话状态，再核对需求与验收用例并重新确认 AI 团队。" : "暂时无法确认协作状态。你的原文已保存；模型服务可能仍在处理，请先刷新对话状态。", error: true }; }
    if (["capability_pending", "team_pending", "connection_changed", "team_changed", "hourly_budget_exhausted"].includes(error.code)) await refreshCapabilities().catch(() => {});
    if (pendingSubmission(request.conversationId) && replyContextCurrent(request)) {
      try { await checkPendingSubmission(request.conversationId); } catch { /* Keep the receipt marker for a later check. */ }
    }
    if (replyContextCurrent(request)) renderChat();
  } finally { clearTimeout(request.timeout); if (!request.submitted || state.activeReply !== request) clearReplyMonitoring(request); }
}

async function checkPendingSubmission(id) {
  const saved = pendingSubmission(id); if (!saved) { await reloadConversation(id); return; }
  const version = state.viewVersion; const authVersion = state.authVersion;
  const result = await api(`/api/conversations/${encodeURIComponent(id)}/submissions/${encodeURIComponent(saved.id)}`);
  if (authVersion !== state.authVersion || version !== state.viewVersion || state.conversation?.id !== id) return;
  if (result.found) {
    forgetSubmission(id, saved.id);
    state.replyFeedback[id] = {text: "已找到这次提交，下面显示当前状态；没有重复提交。"};
  }
  await reloadConversation(id, version);
  if (!result.found && version === state.viewVersion && state.conversation?.id === id) {
    state.replyFeedback[id] = {text: "暂未找到本次回执；原请求仍可能在途中。可以再次核对，或明确确认后继续同一次提交。", error: true};
    renderChat();
  }
}

async function switchView(view) {
  resetSources();
  closeAiConsent();
  closeAcceptanceEditor();
  rememberDraft();
  if (isMfaBlocked() && view !== "security") { showToast("请先完成账号的多因素验证。", true); view = "security"; }
  if (view === "release" && state.user?.role !== "admin") view = "roundtable";
  stopReplyWaiting("navigation"); state.view = view; const version = ++state.viewVersion; closeSidebar();
  for (const name of Object.keys(viewNames)) $(`#${name}-view`).hidden = name !== view;
  document.querySelectorAll(".nav-item").forEach((node) => { node.classList.toggle("active", node.dataset.view === view); if (node.dataset.view === view) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
  $("#view-title").textContent = ["security", "data"].includes(view) ? "个人设置" : "工作空间";
  $("#view-subtitle").textContent = viewNames[view];
  if (view === "roundtable") { renderChat(); resumeReplyMonitoring(); return; }
  const target = $(`#${view}-view`); target.replaceChildren(heading(viewNames[view], "正在读取你的内容…"));
  try {
    const render = { projects: projectsPage, memories: memoriesPage, capabilities: capabilitiesPage, quality: () => qualityUI.render(), data: dataPage, security: securityPage, release: releasePage }[view];
    const nodes = await render(); if (version === state.viewVersion) target.replaceChildren(...nodes);
  } catch (error) { if (version === state.viewVersion) target.replaceChildren(heading(viewNames[view], ""), notice(error.message, true), button("重新加载", () => switchView(view))); }
}

function taskLabel(task) {
  if (task?.runs?.[0]?.status === "queued") return "已排队";
  if (task?.status === "failed" && task?.runs?.[0]?.status === "needs_review") return "核验待修订";
  const labels = { waiting: "已保存", pending: "等待执行", draft: "草稿", paused: "已暂停", running: "协作中", completed: "已完成本轮", failed: "待处理", cancelled: "已取消", interrupted: "已中断" };
  return labels[task?.status] || "等待需求";
}
function currentTask() {
  const tasks = state.tasks.filter(task => !task.superseded_by);
  return tasks.find(task => ["draft", "waiting", "running", "paused"].includes(task.status)) || tasks[0];
}
function messageIcon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", kind === "copy" ? "M8 8h10a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2Zm8-3H5a2 2 0 0 0-2 2v10" : "M13.5 5.5 18.5 10.5M4 20l4.3-.8L19 8.5a2.1 2.1 0 0 0-3-3L5.3 16.2 4 20Z");
  svg.append(path); return svg;
}
function messageEditBlockedReason() {
  if (state.activeSave) return "正在保存，请稍后编辑消息。";
  if (state.activeReply || currentTask()?.status === "running") return "协作正在进行，结束或停止后可编辑消息。";
  if (pendingSubmission(state.conversation?.id)) return "请先核对上一轮提交状态，再编辑消息。";
  return "";
}
function beginMessageEdit(message) {
  if (!state.conversation || message.role !== "user" || message.superseded_by) return;
  const reason = messageEditBlockedReason();
  if (reason) { showToast(reason); return; }
  state.messageEdit = {id: message.id, conversationId: state.conversation.id, original: message.content || "", draft: message.content || "", requestId: crypto.randomUUID()};
  state.messageSignature = ""; renderChat(true);
  $(".message-edit-input")?.focus();
}
async function sendMessageEdit() {
  const edit = state.messageEdit; const task = currentTask();
  if (!edit || state.activeSave || state.activeReply || task?.status === "running" || pendingSubmission(edit.conversationId)) return;
  const content = edit.draft.trim();
  if (!content) { showToast("请输入修改后的消息。", true); return; }
  if (content === edit.original) { state.messageEdit = null; state.messageSignature = ""; renderChat(true); return; }
  const operation = {version: state.viewVersion, authVersion: state.authVersion, conversationId: edit.conversationId,
    wantReply: chatAvailable() && state.aiEnabled, model: modelIdentity()};
  state.activeSave = operation; state.conversationRevision++; renderModelControls();
  const save = $(".message-edit-save"); const cancel = $(".message-edit-cancel");
  if (save) save.disabled = true; if (cancel) cancel.disabled = true;
  let shouldReply = false;
  try {
    await api(`/api/messages/${encodeURIComponent(edit.id)}/edit`, {method: "POST", body: {content,
      expected_content: edit.original, client_edit_id: edit.requestId}});
    if (operation.authVersion !== state.authVersion || operation.version !== state.viewVersion || state.conversation?.id !== operation.conversationId) return;
    state.messageEdit = null; state.messageSignature = ""; state.followLatest = true;
    delete state.replyFeedback[operation.conversationId];
    await reloadConversation(operation.conversationId, operation.version);
    shouldReply = operation.wantReply && state.aiEnabled && chatAvailable() && operation.model === modelIdentity() && currentTask()?.status === "waiting";
    await refreshWorkspace().catch(() => {});
  } catch (error) {
    if (operation.authVersion === state.authVersion && operation.version === state.viewVersion) showToast(error.message || "修改未确认，请重试。", true);
  } finally {
    if (state.activeSave === operation) state.activeSave = null;
    if (save?.isConnected) save.disabled = false; if (cancel?.isConnected) cancel.disabled = false;
    if (operation.authVersion === state.authVersion) renderModelControls();
  }
  if (shouldReply && operation.authVersion === state.authVersion && operation.version === state.viewVersion && state.conversation?.id === operation.conversationId) await requestReply();
}
function renderChat(preserveArtifacts = false) {
  const scroller = $("#chat-scroll");
  const switched = state.scrollConversation !== (state.conversation?.id || null);
  const follow = switched || state.followLatest;
  if (switched) cancelAutomaticChatScroll();
  state.scrollConversation = state.conversation?.id || null;
  const hasConversation = Boolean(state.conversation);
  $("#chat-empty").hidden = hasConversation; $("#conversation-heading").hidden = !hasConversation;
  $("#conversation-title").textContent = state.conversation?.title || "新对话";
  $("#view-subtitle").textContent = state.conversation?.title || "圆桌";
  $("#composer-project").disabled = hasConversation;
  if (hasConversation) $("#composer-project").value = state.conversation.project_id || "";
  const task = currentTask();
  const runsForChat = allTeamRuns();
  const existingMessageIds = new Set([...$("#messages").querySelectorAll("[data-message]")].map(node => node.dataset.message));
  if (!switched && follow && !state.reportScrollTarget) {
    const report = runsForChat.find(run => run.workflow === "decision_v1" && run.status === "completed"
      && !["quick", "quick_group"].includes(run.decision?.response_kind)
      && run.decision?.result_message_id && !existingMessageIds.has(run.decision.result_message_id)
      && state.messages.some(message => message.id === run.decision.result_message_id && !message.superseded_by));
    if (report) state.reportScrollTarget = {conversationId: state.conversation?.id, messageId: report.decision.result_message_id};
  }
  const signature = JSON.stringify([state.messages.map((item) => [item.id, item.role, item.content, item.created_at, item.superseded_by, item.edited_from]), state.messageEdit?.id, task?.status, runsForChat.map(run => [run.id, run.status, run.decision?.response_kind, run.decision?.result_message_id, run.discussion])]);
  if (signature !== state.messageSignature || switched) {
    state.messageSignature = signature;
    const archived = state.messages.filter(message => message.superseded_by);
    const history = archived.length ? el("details", {class: "message-history"}, [el("summary", {text: `查看编辑前的对话（${archived.length} 条）`}),
      ...archived.map(message => el("div", {class: "message-history-entry"}, [el("strong", {text: message.role === "user" ? "你" : "圆桌"}), el("p", {text: message.content || ""})]))]) : null;
    const activeMessages = state.messages.filter(message => !message.superseded_by);
    const messages = activeMessages.map((message) => {
      const resultRun = runsForChat.find(run => run.workflow === 'decision_v1' && run.decision?.result_message_id === message.id);
      if (resultRun?.decision?.response_kind === 'quick_group') return null;
      const report = message.role === "assistant" && resultRun && resultRun.decision?.response_kind !== "quick" ? resultRun : null;
      const content = el("div", {class: report ? "message-content report-content" : "message-content"});
      if (message.role === "assistant" && window.RoundtableMessage) content.append(window.RoundtableMessage.render(message.content || "", report?.decision));
      else content.textContent = message.content || "";
      const copy = el("button", {type: "button", class: "message-copy message-action", title: "复制", "aria-label": message.role === "user" ? "复制消息" : "复制回复"}, [messageIcon("copy")]);
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(message.content || ""); showToast("已复制"); }
        catch { showToast("复制失败，请选中文字复制。", true); }
      });
      const edit = message.role === "user" ? el("button", {type: "button", class: "message-action message-edit-action", title: "编辑消息", "aria-label": "编辑消息"}, [messageIcon("edit")]) : null;
      edit?.addEventListener("click", () => beginMessageEdit(message));
      if (state.messageEdit?.id === message.id && state.messageEdit.conversationId === state.conversation?.id) {
        const input = el("textarea", {class: "message-edit-input", "aria-label": "编辑消息内容", maxlength: 12000, rows: 3});
        input.value = state.messageEdit.draft;
        input.addEventListener("input", () => { if (state.messageEdit?.id === message.id) { state.messageEdit.draft = input.value; state.messageEdit.requestId = crypto.randomUUID(); } });
        const cancel = button("取消", () => { state.messageEdit = null; state.messageSignature = ""; renderChat(true); }, "button message-edit-cancel");
        const save = button("发送修改", sendMessageEdit, "button primary message-edit-save");
        return el("article", {class: "message user is-editing", dataset: {message: message.id}}, [
          el("div", {class: "message-editor"}, [input, el("p", {text: "修改后从这条消息继续；编辑前的对话会保留在历史中。"}), el("div", {class: "message-edit-actions"}, [cancel, save])])]);
      }
      const reportMenu = report ? el("details", {class: "report-menu"}, [
        el("summary", {title: "报告操作", "aria-label": "报告操作", text: "⋯"}),
        el("div", {class: "report-menu-panel"}, [window.RoundtableFollowup?.menuButton(content) || null,
        button("下载这份报告", () => {
          const url = URL.createObjectURL(new Blob([message.content || ""], {type: "text/markdown;charset=utf-8"}));
          const link = el("a", {href: url, download: "圆桌报告.md"});
          document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
          reportMenu.open = false; reportMenu.querySelector("summary").focus();
        }, "text-button")])
      ]) : null;
      return el("article", {class: `message ${["user", "assistant", "system"].includes(message.role) ? message.role : "system"}`, dataset: report ? {message: message.id, reportId: report.id} : {message: message.id}}, [content,
        el("div", {class: "message-footer"}, [copy, edit, reportMenu, message.edited_from ? el("span", {class: "message-meta", text: "已编辑"}) : null,
          el("time", {class: "message-meta", datetime: message.created_at || "", text: dateText(message.created_at)})])]);
    });
    const timeline = messages.map((node, index) => node ? {node, time: activeMessages[index].created_at || '', order: 0} : null).filter(Boolean);
    for (const run of runsForChat) {
      if (run.workflow !== 'decision_v1' || run.decision?.response_kind === 'quick') continue;
      for (const item of run.discussion || []) {
        if (!item.content || item.status === 'forwarded') continue;
        if (item.kind !== 'user' && item.role === 'synthesizer') continue;
        timeline.push({item, run, time: item.created_at || run.created_at || '', order: item.order || 0});
      }
    }
    timeline.sort((a, b) => String(a.time).localeCompare(String(b.time)) || a.order - b.order);
    $("#messages").replaceChildren(...(history ? [history] : []), ...renderDiscussionTimeline(timeline));
  }
  $("#task-status").textContent = taskLabel(task); $("#task-controls").hidden = !task;
  $("#pause-task").hidden = !task || (!["paused", "running"].includes(task.status) && task.runs?.[0]?.status !== "awaiting_input");
  $("#pause-task").textContent = task?.status === "paused" ? "恢复待办" : "停止本轮";
  $("#work-log").hidden = !task;
  $("#team-record").hidden = !task && !runsForChat.length;
  $("#artifacts-toggle").hidden = !task;
  const logSignature = JSON.stringify(task?.events || []);
  if ($("#work-log-list").dataset.signature !== logSignature) {
    $("#work-log-list").dataset.signature = logSignature;
    $("#work-log-list").replaceChildren(...(task?.events || []).map((event) => el("div", { class: "log-entry" }, [el("span", { text: event.content }), el("time", { datetime: event.created_at || "", text: dateText(event.created_at) })])));
  }
  renderTeamProgress(task);
  if (switched || !preserveArtifacts) hideArtifacts();
  renderModelControls();
  resizeComposer();
  if (state.chatScrollFrame !== null) cancelAnimationFrame(state.chatScrollFrame);
  state.chatScrollFrame = null;
  if (scroller && (follow || state.reportScrollTarget)) {
    const conversationId = state.conversation?.id;
    state.chatScrollFrame = requestAnimationFrame(() => {
      state.chatScrollFrame = null;
      if (state.conversation?.id !== conversationId) return;
      const target = state.reportScrollTarget;
      state.reportScrollTarget = null;
      const reportNode = target?.conversationId === conversationId
        ? [...$("#messages").querySelectorAll("[data-message]")].find(node => node.dataset.message === target.messageId) : null;
      if (reportNode) {
        const heading = $("#conversation-heading");
        const headingHeight = heading.hidden ? 0 : heading.getBoundingClientRect().height;
        const top = scroller.scrollTop + reportNode.getBoundingClientRect().top - scroller.getBoundingClientRect().top - headingHeight - 12;
        scroller.scrollTo({top: Math.max(0, top), behavior: "instant"});
        syncChatScroll();
      } else scrollToLatest();
    });
  } else syncChatScroll();
}

function acceptanceText(label, value) {
  const content = typeof value === "string" ? value : "";
  return el("div", { class: "acceptance-original" }, [
    el("div", { class: "acceptance-original-label" }, [el("strong", { text: label }), el("small", { text: content.length ? `${content.length} 字符${content.endsWith("\n") ? " · 末尾有换行" : " · 末尾无换行"}` : "空字符串" })]),
    el("pre", { class: "artifact-source", "aria-label": label }, [el("code", { text: content })]),
  ]);
}
function acceptanceRunSnapshot(run, opened) {
  if (!Array.isArray(run.acceptance_cases) || !run.acceptance_cases.length) return null;
  const cases = run.acceptance_cases; const recordId = `${run.id}-acceptance`;
  const details = el("details", { class: "team-system-check acceptance-snapshot", dataset: { record: recordId } }, [
    el("summary", {}, [el("strong", { text: `用户提供的 ${cases.length} 个验收用例` }), el("span", { text: cases.length ? "查看本轮使用的原文" : "未设置输入输出验收" })]),
    el("p", { class: "team-scope", text: "这是本轮保存的用户标准，模型不能修改；后续修改对话用例不会改变这份记录。仅用于 Python main.py 的标准输入输出逐字比较，通过这些用例不代表整个程序已完成。" }),
    ...cases.map((item, index) => el("section", { class: "acceptance-original-case" }, [el("h4", { text: `用例 ${index + 1}` }), acceptanceText("标准输入", item.stdin), acceptanceText("预期标准输出", item.expected_stdout)])),
  ]);
  details.open = opened.has(recordId); return details;
}
function acceptanceExecution(report, expectedCount = 0) {
  const execution = report?.execution?.report || report?.execution;
  const checks = Array.isArray(execution?.checks) ? execution.checks : Array.isArray(report?.checks) ? report.checks : [];
  const stdio = checks.find((check) => check.tool === "python_stdio");
  if (!stdio && !expectedCount) return null;
  const outcomes = Array.isArray(stdio?.cases) ? stdio.cases : [];
  const requested = Number.isInteger(stdio?.requested_cases) ? stdio.requested_cases : 0;
  const count = Math.min(5, Math.max(expectedCount, requested, outcomes.length));
  const labels = { passed: "通过", failed: "未通过", not_run: "未运行" };
  return el("section", { class: "acceptance-results" }, [
    el("h4", { text: "用户验收用例 · 运行结果" }),
    el("p", { class: "artifact-scope", text: stdio?.detail || "本阶段尚无用户用例的运行记录，不能认定通过。" }),
    ...Array.from({ length: count }, (_, index) => {
      const outcome = outcomes[index]; const status = outcome?.status || "not_run";
      return el("details", { class: "acceptance-result" }, [
        el("summary", {}, [el("strong", { text: `用例 ${index + 1}` }), el("span", { class: "check-status", text: labels[status] || "未确认" })]),
        el("p", { class: "artifact-scope", text: outcome?.detail || (stdio?.status === "not_run" ? stdio.detail : "没有此用例的执行记录，未运行。") }),
        typeof outcome?.matched_stdout === "boolean" ? el("p", { class: "artifact-scope", text: outcome.matched_stdout ? "标准输出与预期原文完全一致。" : "标准输出与预期原文不一致。" }) : null,
        typeof outcome?.output === "string" ? acceptanceText("实际标准输出", outcome.output) : null,
        typeof outcome?.stderr === "string" ? acceptanceText("错误输出", outcome.stderr) : null,
      ]);
    }),
    el("p", { class: "artifact-scope", text: "以上只依据系统记录的实际运行结果。模型自行生成的测试或通过声明不能替代用户用例验收；通过用例也不代表整个程序已完成。" }),
  ]);
}

function teamChatAvatar(provider, isUser) {
  const avatar = el("span", { class: "team-chat-avatar", "aria-hidden": "true" });
  const fallback = el("span", { class: "team-chat-avatar-fallback", text: isUser ? "你" : ({deepseek: "D", kimi: "K", qwen: "千"})[provider] || "AI" });
  avatar.append(fallback);
  return avatar;
}
function renderDiscussionTimeline(timeline) {
  const nodes = [];
  let previousPhase = "";
  for (let index = 0; index < timeline.length; index++) {
    const {node, item, run} = timeline[index];
    if (node) { nodes.push(node); previousPhase = ""; continue; }
    const phase = /^(analysis|review|response)_p\d+$/.exec(item.role)?.[1];
    const phaseId = phase ? run.id + "/" + phase : "";
    const phaseLabel = phase ? ({analysis: "独立分析", review: "交叉核验", response: "回应质疑"})[phase] : null;
    const members = [item];
    if (phase) {
      const identity = entry => phase === "analysis" ? run.steps.find(step => step.role === entry.role)?.output : entry.equivalence;
      const output = identity(item);
      while (output && index + 1 < timeline.length) {
        const next = timeline[index + 1];
        if (next.run !== run || !next.item.role.startsWith(phase + "_p") || next.item.content !== item.content ||
            identity(next.item) !== output) break;
        members.push(next.item); index++;
      }
    }
    nodes.push(teamChatMessage(item, run, phaseId && phaseId === previousPhase ? "" : phaseLabel, members));
    previousPhase = phaseId;
  }
  return nodes;
}
function teamChatMessage(item, run, stage = null, members = [item]) {
  const isUser = item.kind === "user";
  const name = isUser ? "你" : members.map(member => providerName(member.provider)).join("、");
  const metadata = members.map(member => [member.model, member.label, dateText(member.created_at)].filter(Boolean).join(" · ")).join("\n");
  const label = stage ?? (item.role !== 'clarifier' && !item.role?.startsWith('quick_') ? item.label : "");
  const target = isUser && item.target_provider ? `@${providerName(item.target_provider)}` : "";
  const text = el("div", { class: "team-chat-bubble" });
  if (target) text.append(el("span", { class: "team-chat-mention", text: target + " " }));
  if (!isUser && window.RoundtableMessage) text.append(window.RoundtableMessage.render(item.content || ""));
  else text.append(document.createTextNode(item.content || ""));
  const copy = el("button", {type:"button", class:"message-action", title:"复制", "aria-label":"复制回复"}, [messageIcon("copy")]);
  copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(item.content || ""); showToast("已复制"); } catch { showToast("复制失败，请选中文字复制。", true); } });
  const status = !isUser ? "" : item.status === "pending" ? (["queued", "running", "awaiting_input"].includes(run.status) ? "等待后续讨论采用" : "本轮未处理，已留给下一轮") : item.status === "included" ? "已纳入讨论" : item.status === "answered" ? "成员已回应" : "";
  return el("article", { class: `team-chat-message${isUser ? " is-user" : ""}` }, [
    teamChatAvatar(item.provider, isUser),
    el("div", { class: "team-chat-message-body" }, [el("div", { class: "team-chat-meta" }, [el("strong", { text: name, title: metadata }), !isUser && label ? el("span", {text:label}) : null]), text, copy, status ? el("small", { class: "team-chat-status", text: status }) : null]),
  ]);
}
function teamChatHistory(run) {
  const discussion = Array.isArray(run.discussion) ? run.discussion : null;
  const items = discussion || (run.steps || []).filter((step) => step.status === "completed" && step.output && step.role !== "planner").map((step) => ({kind: "assistant", provider: step.provider, label: step.label, content: step.output}));
  return el("div", { class: "team-chat-history", "aria-label": "团队讨论记录" }, items.length
    ? items.filter((item) => item.content).map((item) => teamChatMessage(item, run))
    : [el("p", { class: "team-scope", text: "成员完成发言后会显示在这里。" })]);
}
function allTeamRuns() {
  return state.tasks.filter(task => !task.superseded_by).flatMap((task) => task.runs || []).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")) || String(b.id).localeCompare(String(a.id)));
}
function interjectionRun() {
  const draft = interjectionDraft();
  if (draft?.kind === "clarification" && draft.text === $("#message-input").value.trim()) return null;
  const latest = currentTask()?.runs?.[0];
  if (draft?.text === $("#message-input").value.trim()) {
    const origin = latest?.id === draft.runId ? latest : allTeamRuns().find(item => item.id === draft.runId);
    if (origin && (origin.status !== "awaiting_input" || draft.receipt)) return origin;
    if (!origin) return {id: draft.runId, status: "unavailable"};
  }
  if (!latest || !["decision_v1", "adaptive"].includes(latest.workflow)) return null;
  if (["running", "queued"].includes(latest.status)) return latest;
  return null;
}
function interjectionDraft(id = state.conversation?.id) {
  if (!id || !state.user) return null;
  if (state.teamChatDrafts.has(id)) return state.teamChatDrafts.get(id);
  try {
    const saved = JSON.parse(sessionStorage.getItem(`roundtable:interjection:${state.user.id}:${id}`) || "null");
    if (typeof saved?.runId === "string" && typeof saved.text === "string") {
      state.teamChatDrafts.set(id, saved); return saved;
    }
  } catch { /* In-memory use still works. */ }
  return null;
}
function saveInterjectionDraft(id, draft) {
  if (!id || !state.user) return;
  if (draft) state.teamChatDrafts.set(id, draft); else state.teamChatDrafts.delete(id);
  try {
    const key = `roundtable:interjection:${state.user.id}:${id}`;
    if (draft) sessionStorage.setItem(key, JSON.stringify(draft)); else sessionStorage.removeItem(key);
  } catch { /* In-memory use still works. */ }
}
function renderInterjectionControls() {
  const run = interjectionRun();
  const select = $("#team-chat-target");
  $("#team-chat-target-row").hidden = !run;
  const pendingAnswer = interjectionDraft();
  if (pendingAnswer?.kind === "clarification" && pendingAnswer.text === $("#message-input").value.trim() && !state.activeSave) {
    $("#send-message").disabled = false;
    $("#send-message").setAttribute("aria-label", "核对上次回答"); $("#send-message").title = "核对上次回答";
    $("#send-label").textContent = "核对上次回答";
    $("#availability-text").textContent = "上次回答尚未收到回执，按发送可核对同一轮。";
  }
  if (select.dataset.run !== (run?.id || "")) {
    const saved = interjectionDraft();
    const providers = [...new Set([...(run?.steps || []).map(step => step.provider),
      ...(state.capabilities?.team?.roles || []).map(role => role.provider),
      saved?.runId === run?.id ? saved?.target : ""].filter(Boolean))];
    select.replaceChildren(el("option", {value: "", text: "全体成员"}), ...providers.map(provider => el("option", {value: provider, text: providerName(provider)})));
    select.dataset.run = run?.id || "";
    const draft = interjectionDraft();
    if (run && draft?.runId === run.id) select.value = draft.target || "";
  }
  if (!run) return;
  const open = ["queued", "running", "awaiting_input"].includes(run.status);
  const closed = !open && !["completed", "failed", "needs_review"].includes(run.status);
  const sending = state.teamChatSending.has(run.id);
  const input = $("#message-input");
  input.placeholder = open ? "随时补充信息，或向团队提问…" : closed ? "这条补充属于已停止的讨论…" : "把这条补充留给下一轮…";
  input.maxLength = 2000;
  const label = open ? "发送给本轮团队" : closed ? "核对这条补充" : "保存给下一轮";
  $("#send-message").setAttribute("aria-label", label);
  $("#send-message").title = label;
  $("#send-label").textContent = sending ? "发送中…" : label;
  $("#send-message").disabled = sending || Boolean(state.activeSave) || !input.value.trim();
  $("#availability-text").textContent = open ? "讨论进行中，随时可以补充。" : closed ? "原轮次已停止，可以核对是否已送达。" : "本轮已结束，这条补充将留给下一轮。";
  $("#composer-hint").textContent = "Enter " + label + " · Shift + Enter 换行";
  $("#composer").setAttribute("aria-busy", String(sending));
  select.disabled = sending;
}
$("#message-input").addEventListener("input", () => {
  const id = state.conversation?.id;
  if (!id) return;
  const text = $("#message-input").value.trim();
  const select = $("#team-chat-target");
  const runId = select.dataset.run || interjectionRun()?.id;
  if (!text || !runId) saveInterjectionDraft(id, null);
  else {
    const draft = interjectionDraft(id);
    saveInterjectionDraft(id, {runId, text, target: select.value, receipt: draft?.text === text ? draft.receipt : null});
  }
});
$("#message-input").addEventListener("keydown", (event) => {
  if (event.key !== "@" || !interjectionRun() || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
  const before = event.currentTarget.value.slice(0, event.currentTarget.selectionStart);
  if (/[A-Za-z0-9._%+-]$/.test(before)) return;
  event.preventDefault();
  $("#composer-options").open = true;
  const select = $("#team-chat-target"); select.focus();
  try { select.showPicker?.(); } catch { /* Use keyboard selection when needed. */ }
});
$("#team-chat-target").addEventListener("change", () => {
  const draft = interjectionDraft();
  if (draft) saveInterjectionDraft(state.conversation.id, {...draft, target: $("#team-chat-target").value, receipt: null});
  $("#composer-options").open = false;
  $("#message-input").focus();
});
async function sendInterjection(run, content) {
  const conversationId = state.conversation?.id;
  const input = $("#message-input");
  const original = input.value;
  if (state.teamChatSending.has(run.id)) return;
  if (content.length > 2000) { showToast("每条补充最多 2000 字，可分两次发送。", true); return; }
  const draft = interjectionDraft(conversationId);
  const target = draft?.receipt && draft.runId === run.id && draft.text === content ? draft.target : $("#team-chat-target").value;
  const receipt = draft?.text === content && draft.target === target && draft.runId === run.id && draft.receipt || crypto.randomUUID();
  saveInterjectionDraft(conversationId, {runId: run.id, text: content, target, receipt});
  const request = {runId: run.id, auth: state.authVersion, version: state.viewVersion, receipt};
  const current = () => state.conversation?.id === conversationId && state.authVersion === request.auth && state.viewVersion === request.version;
  state.teamChatSending.set(run.id, request);
  renderInterjectionControls();
  try {
    const result = await api(`/api/conversations/${encodeURIComponent(conversationId)}/team-messages`, {method: "POST", signal: AbortSignal.timeout?.(30000),
      body: {run_id: run.id, content, target_provider: target, client_request_id: receipt}});
    if (state.authVersion !== request.auth) return;
    if (result?.accepted !== true) throw new Error("补充的保存状态尚未确认，请用原文重试。");
    if (interjectionDraft(conversationId)?.receipt === receipt) saveInterjectionDraft(conversationId, null);
    forgetDraft(conversationId, original);
    if (state.conversation?.id !== conversationId || state.view !== "roundtable") return;
    if (input.value === original) {
      input.value = ""; $("#team-chat-target").value = "";
      rememberDraft(); resizeComposer();
    }
    showToast(result.continuation_required ? "本轮已结束，补充已保存给下一轮。" : "补充已送达团队。");
    state.conversationRevision++;
    await reloadConversation(conversationId, state.viewVersion).catch(() => {});
  } catch (error) {
    if (current()) {
      if (error.code === "interjection_round_closed" || error.status === 404) {
        openForm("这条补充所属的讨论已结束", [], "转为当前草稿", async () => {
          if (!current() || input.value !== original) return;
          saveInterjectionDraft(conversationId, null); renderModelControls(); input.focus();
        }, "保留原文后，你可以修改并决定是否发送给当前讨论。");
      } else showToast(error.status && error.status < 500 ? error.message : "补充尚未收到回执，可以用原文重试。", true);
      if (error.status === 409) await reloadConversation(conversationId, request.version).catch(() => {});
    }
  } finally {
    if (state.teamChatSending.get(run.id) === request) state.teamChatSending.delete(run.id);
    if (current()) renderModelControls();
  }
}
function decisionRecord(run, opened) {
  const decision = run.decision;
  if (run.workflow !== "decision_v1" || !decision) return null;
  const recordId = `${run.id}-decision`;
  const brief = decision.brief || {};
  const names = {goal: "目标", metrics: "衡量指标", constraints: "硬约束", preferences: "偏好", assumptions: "假设", unknowns: "未知项", answers: "用户补充"};
  const briefRows = Object.entries(names).filter(([key]) => brief[key]).map(([key, label]) =>
    el("p", {}, [el("strong", {text: `${label}：`}), document.createTextNode(brief[key])]));
  const evidence = Array.isArray(decision.evidence) ? decision.evidence : [];
  const findings = [...(decision.reviews || []), ...(decision.extra_round?.reviews || [])];
  const responses = [...(decision.responses || []), ...(decision.extra_round?.responses || [])];
  const statuses = {supported: "依据支持", refuted: "依据反驳", unverified: "暂无法核实", user_decision: "待用户确定"};
  const evidenceNodes = evidence.map(item => {
    const label = item.type === "calculation" ? `系统计算 · ${item.status === "executed" ? "已执行" : "未执行"}` : item.type === "user" ? "用户提供" : "选定资料";
    const body = item.type === "calculation" ? `${item.expression} = ${item.result}` : item.excerpt;
    return el("li", {}, [el("strong", {text: `${item.id} · ${label}`}), el("p", {text: body || ""}),
      el("p", {class: "team-scope", text: [item.name, item.source, item.scope, item.url].filter(Boolean).join(" · ")})]);
  });
  const findingNodes = findings.map(item => {
    const claim = (decision.analyses || []).flatMap(a => a.claims || []).find(c => c.id === item.claim_id);
    const response = responses.find(r => r.finding_id === item.id);
    return el("li", {}, [el("strong", {text: `${statuses[item.verdict] || "暂无法核实"} · ${claim?.text || item.claim_id}`}),
      el("p", {text: item.reason || ""}), el("p", {class: "team-scope", text: `影响：${item.impact || "未说明"}；依据：${(item.evidence_ids || []).join("、") || "暂无可追溯依据"}`}),
      response ? el("p", {text: `提出者回应：${response.reason}`}) : el("p", {class: "team-scope", text: "尚无回应记录。"})]);
  });
  const detail = el("details", {class: "team-system-check", dataset: {record: recordId}}, [
    el("summary", {text: "问题说明、依据与分歧"}),
    briefRows.length ? el("div", {}, briefRows) : el("p", {class: "team-scope", text: "成员正在整理问题。"}),
    el("p", {class: "team-scope", text: "来源原文与实际计算分别记录；资料是否支持主张属于模型判断，不代表外部事实已经证实。"}),
    evidenceNodes.length ? el("ul", {}, evidenceNodes) : el("p", {class: "team-scope", text: "尚无可追溯的资料或计算记录。"}),
    findingNodes.length ? el("ul", {}, findingNodes) : el("p", {class: "team-scope", text: "尚无交叉核验记录。"}),
    decision.stop_reason ? el("p", {text: `停止原因：${decision.stop_reason}`}) : null,
  ]);
  detail.open = opened.has(recordId); return detail;
}
function renderTeamProgress(task) {
  const runs = allTeamRuns(); const latest = runs[0];
  const pending = state.activeReply?.conversationId === state.conversation?.id && Boolean(state.conversation);
  $("#team-progress").hidden = !runs.length && !pending && !task;
  const active = (latest?.steps || []).find((step) => step.status === "running");
  const labels = { queued: "已排队", running: "协作中", awaiting_input: "等你回复", completed: "成果已保存", failed: "本轮未完成", paused: "已暂停", needs_review: "核验待修订" };
  $("#live-draft").hidden = true;
  $("#live-draft-content").textContent = "";
  const attempts = latest?.status === "failed" ? (latest.calls_used ? ` · 已记录 ${latest.calls_used} 次调用尝试，可能计费；未自动重试` : " · 没有记录模型调用尝试；未自动重试") : "";
  const progress = !latest && pending ? "正在提交…" : latest?.status === "queued" ? "已排队 · 等待后台处理" : latest?.status === "running" ? `协作中${active ? ` · 正在${active.label || active.role}` : ""}` : latest?.status === "failed" ? `本轮未完成${attempts}` : "";
  // Keep the live region quiet unless the actual progress changes.
  if ($("#team-progress-text").textContent !== progress) $("#team-progress-text").textContent = progress;
  $("#team-progress-text").hidden = !progress;
  $("#team-progress").hidden = !progress;
  const recordSignature = JSON.stringify(runs.map((run) => ({...run, steps: (run.steps || []).map((step) => step.status === "running" ? {...step, output: ""} : step)})));
  if ($("#team-runs").dataset.signature === recordSignature) return;
  $("#team-runs").dataset.signature = recordSignature;
  const opened = new Set([...$("#team-runs").querySelectorAll("details[open]")].map((node) => node.dataset.record));
  const renderedRuns = runs.map((run, runIndex) => {
    const stepLabels = { pending: "等待处理", running: "处理中", completed: "已完成", failed: "未完成", skipped: "未执行", paused: "已暂停" };
    const isQuick = run.status === "completed" && ['quick', 'quick_group'].includes(run.decision?.response_kind);
    const steps = (run.steps || []).filter(step => !isQuick || step.status !== "skipped").map((step, index) => {
      const recordId = `${run.id}-${step.role}-${index}`;
      const detail = el("details", { class: `team-step${step.status === "running" ? " is-running" : ""}`, dataset: { record: recordId } }, [
        el("summary", {}, [el("span", { class: "team-step-number", text: String(index + 1), "aria-hidden": "true" }), el("span", { class: "team-step-title", text: step.label || step.role }), el("span", { class: "team-step-status", text: stepLabels[step.status] || "等待处理" })]),
        el("p", { class: "team-step-model", text: `${providerName(step.provider)} · ${step.model || "未配置模型"}` }),
        step.output && step.status !== "running" ? el("div", { class: "team-step-output", text: step.output }) : null,
        step.error ? el("p", { class: "team-step-error", text: step.error }) : null,
        !step.output && !step.error ? el("p", { class: "team-scope", text: "尚无阶段成果。" }) : null,
      ]);
      detail.open = opened.has(recordId); return detail;
    });
    const systemChecks = (Array.isArray(run.checks) ? run.checks : []).map((report, index) => {
      const recordId = `${run.id}-system-check-${report.stage || "unknown"}-${index}`;
      const stageLabel = ({ synthesizer: "首次文件", repair: "修订后文件" })[report.stage] || "阶段文件";
      const checkLabels = { passed: "通过", failed: "未通过", not_run: "未执行" };
      const checks = Array.isArray(report.checks) ? report.checks : [];
      const detail = el("details", { class: "team-system-check", dataset: { record: recordId } }, [
        el("summary", {}, [el("strong", { text: `系统检查 · ${stageLabel}` }), el("span", { text: report.passed === true ? "已执行的检查通过" : "发现待处理项" })]),
        el("p", { class: "team-scope", text: report.scope || "检查文件格式与部分源码语法；没有运行生成的程序。" }),
        checks.length ? el("ul", { class: "artifact-checks" }, checks.map((check) => el("li", { class: check.status === "failed" ? "check-failed" : "" }, [
          el("span", { class: "check-status", text: checkLabels[check.status] || "未确认" }),
          el("div", {}, [el("strong", { text: [check.path, check.check].filter(Boolean).join(" · ") || "文件检查" }), el("p", { text: check.detail || "" })]),
        ]))) : el("p", { class: "team-scope", text: "没有可展示的检查项目。" }),
        acceptanceExecution(report, run.acceptance_cases?.length || 0),
        el("p", { class: "team-scope", text: "每项检查分别记录通过、未通过或未执行。模型自行声明的测试结果不作为系统执行证据。" }),
      ]);
      detail.open = opened.has(recordId); return detail;
    });
    const plan = run.plan;
    let planView = null;
    if (plan && Array.isArray(plan.work_items)) {
      const recordId = `${run.id}-plan`;
      const names = { analysis: "分析", material_research: "材料研究", coding: "编程", critique: "质疑与审查", writing: "写作" };
      planView = el("details", { class: "team-system-check", dataset: { record: recordId } }, [
        el("summary", {}, [el("strong", { text: "任务计划与验收条件" }), el("span", { text: `${plan.work_items.length} 项分工` })]),
        el("p", { text: plan.goal || "" }),
        el("ul", {}, (plan.criteria || []).map((item) => el("li", { text: item }))),
        ...plan.work_items.map((item, index) => el("div", { class: "team-step" }, [el("strong", { text: `工作 ${index + 1} · ${names[item.specialty] || item.specialty}` }), el("p", { text: item.instruction }), el("p", { class: "team-scope", text: item.depends_on?.length ? `接续工作：${item.depends_on.map((id) => String(plan.work_items.findIndex((prior) => prior.id === id) + 1)).join("、")}` : "计划未列依赖；此流程的成员仍可能看到此前公开讨论。" })])),
      ]);
      planView.open = opened.has(recordId);
    }
    const sourceView = run.sources?.length ? el("details", { class: "team-system-check" }, [
      el("summary", { text: `本轮参考资料 · ${run.sources.length} 份` }),
      el("ul", {}, run.sources.map(source => el("li", {}, [
        el("strong", { text: `[${source.reference}] ${source.name}` }),
        el("span", { text: ` · ${source.char_count} 字符` }),
        source.kind === "web" ? el("p", { text: source.source_url }) : null,
      ]))), el("p", { class: "team-scope", text: "这里记录启动时选定的资料，后续选择不改变本轮记录。网页读取不代表事实核验。" }),
    ]) : null;
    const details = el("details", { class: "team-chat-details", dataset: { record: `${run.id}-details` } }, [el("summary", { text: "查看阶段记录与检查" }), isQuick ? null : decisionRecord(run, opened), sourceView, acceptanceRunSnapshot(run, opened), planView, ...steps, ...systemChecks]);
    details.open = opened.has(`${run.id}-details`);
    const historyId = `${run.id}-discussion`;
    const discussion = run.workflow === "decision_v1" ? null : el("details", { class: "team-chat-details", dataset: { record: historyId } }, [el("summary", { text: "查看团队发言" }), teamChatHistory(run)]);
    if (discussion) discussion.open = opened.has(historyId);
    return el("section", { class: "team-run" }, [el("div", { class: "team-run-heading" }, [el("span", { text: `${labels[run.status] || "等待处理"} · 已尝试 ${run.calls_used || 0}/${run.max_calls || 5} 次调用` }), el("time", { datetime: run.created_at || "", text: dateText(run.created_at) })]), discussion, details]);
  });
  const previous = runs.length > 1 ? el("details", { class: "team-run-history", dataset: { record: "previous-runs" } }, [el("summary", { text: `查看之前的记录（${runs.length - 1}）` }), ...renderedRuns.slice(1)]) : null;
  if (previous) previous.open = opened.has("previous-runs");
  $("#team-runs").replaceChildren(...[renderedRuns[0], previous].filter(Boolean));
}

async function openConversation(id) {
  closeAiConsent();
  resetSources();
  closeAcceptanceEditor();
  rememberDraft();
  stopReplyWaiting("navigation");
  if (state.conversation?.id !== id) { state.messageEdit = null; $("#conversation-menu").open = false; $("#team-record").open = false; $("#work-log").open = false; }
  const version = ++state.viewVersion;
  const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
  if (version !== state.viewVersion) return;
  state.conversation = data.conversation; state.messages = data.messages || []; state.tasks = data.tasks || [];
  rememberConversation();
  restoreDraft();
  restoreConversationMode();
  if (["completed", "paused"].includes(currentTask()?.status)) delete state.replyFeedback[id];
  await switchView("roundtable"); renderConversationList();
  if (pendingSubmission(id)) void checkPendingSubmission(id).catch(() => { if (state.conversation?.id === id) renderReplyFeedback(); });
}
function newConversation(options = {}) {
  closeAiConsent();
  resetSources();
  if (isMfaBlocked()) { showToast("请先完成账号的多因素验证。", true); return; }
  if (!options.skipRemember) rememberDraft();
  stopReplyWaiting("navigation"); state.conversation = null; state.messages = []; state.tasks = []; state.messageEdit = null;
  rememberConversation(); state.followLatest = true;
  setDeliveryMode("text");
  $("#conversation-menu").open = false; $("#team-record").open = false; $("#work-log").open = false;
  restoreDraft();
  switchView("roundtable"); renderConversationList(); $("#message-input").focus();
}

function hideArtifacts() {
  state.artifactVersion++; $("#artifacts-panel").hidden = true;
  $("#artifacts-toggle").textContent = "历史文件"; $("#artifacts-toggle").setAttribute("aria-expanded", "false");
}
function artifactContextCurrent(context) {
  return Boolean(state.user) && context.authVersion === state.authVersion && context.viewVersion === state.viewVersion && context.artifactVersion === state.artifactVersion && state.view === "roundtable" && state.conversation?.id === context.conversationId && !$("#artifacts-panel").hidden;
}
function renderBundleDetails(artifact) {
  const validation = artifact.validation || {}; const checks = Array.isArray(validation.checks) ? validation.checks : [];
  const labels = { passed: "通过", failed: "未通过", not_run: "未执行" };
  const checkList = el("ul", { class: "artifact-checks" }, checks.map((check) => el("li", { class: check.status === "failed" ? "check-failed" : "" }, [
    el("span", { class: "check-status", text: labels[check.status] || "未确认" }),
    el("div", {}, [el("strong", { text: [check.path, check.check].filter(Boolean).join(" · ") || "文件检查" }), el("p", { text: check.detail || "" })]),
  ])));
  const files = Array.isArray(artifact.files) ? artifact.files : [];
  const fileList = el("div", { class: "artifact-files" }, files.map((file, index) => {
    const content = el("div", { class: "artifact-file-body" });
    const detail = el("details", { class: "artifact-file" }, [el("summary", { text: file.path || `文件 ${index + 1}` }), content]);
    let rendered = false;
    detail.addEventListener("toggle", () => {
      if (!detail.open || rendered) return;
      rendered = true;
      content.append(exportLink(`/api/artifacts/${encodeURIComponent(artifact.id)}/files/${index}`, "下载此文件"), el("pre", { class: "artifact-source" }, [el("code", { text: typeof file.content === "string" ? file.content : "" })]));
    });
    return detail;
  }));
  return el("div", { class: "artifact-detail-content" }, [
    artifact.content ? el("p", { class: "artifact-summary", text: artifact.content }) : null,
    el("h4", { text: validation.passed === true ? "已执行的文件检查通过" : "文件检查仍有待处理项" }),
    el("p", { class: "artifact-scope", text: validation.scope || "仅检查文件格式与部分源码语法，不执行生成的程序。" }),
    checks.length ? checkList : notice("尚无可展示的文件检查记录。"),
    acceptanceExecution(validation),
    el("p", { class: "artifact-scope", text: validation.execution ? "运行情况和独立验收范围以逐项检查记录为准；生成测试的自报结果不能代替独立验收。点击文件名查看内容。" : "源码未实际运行，文件检查不能代替功能测试。点击文件名查看文本内容。" }),
    fileList,
  ]);
}
function artifactCard(artifact, context) {
  const bundle = artifact.kind === "bundle";
  const draft = artifact.review_status === "draft";
  const detailBody = el("div", { class: "artifact-detail-body" });
  const detail = bundle ? el("details", { class: "artifact-detail" }, [el("summary", { text: "查看文件与检查结果" }), detailBody]) : null;
  const card = el("section", { class: "artifact-card" }, [
    el("div", { class: "artifact-card-heading" }, [
      el("div", {}, [el("h3", { text: artifact.name || "成果文件" }), el("p", { class: "artifact-meta", text: [bundle ? `版本 ${artifact.revision || 1}` : "文字文件", bundle ? draft ? "待复核草稿" : "已交付" : "", dateText(artifact.created_at)].filter(Boolean).join(" · ") })]),
      exportLink(`/api/artifacts/${encodeURIComponent(artifact.id)}/download`, bundle ? "下载 ZIP" : "下载"),
    ]),
    bundle && draft ? notice("这是待复核版本，尚未通过全部检查或团队核验。可以在对话中补充修改要求。") : null,
    detail,
  ]);
  if (detail) {
    let loaded = false; let loading = false;
    const load = async () => {
      if (loaded || loading || !artifactContextCurrent(context) || !detail.isConnected) return;
      loading = true; detailBody.replaceChildren(notice("正在读取文件与检查结果…"));
      try {
        const data = await api(`/api/artifacts/${encodeURIComponent(artifact.id)}/detail`);
        if (!artifactContextCurrent(context) || !detail.isConnected) return;
        if (data.artifact?.id !== artifact.id || data.artifact?.kind !== "bundle") throw new Error("成果信息已改变，请重新打开成果列表。");
        detailBody.replaceChildren(renderBundleDetails(data.artifact)); loaded = true;
      } catch (error) {
        if (artifactContextCurrent(context) && detail.isConnected) detailBody.replaceChildren(notice(error.message || "暂时无法读取文件。", true), button("重新加载", load));
      } finally { loading = false; }
    };
    detail.addEventListener("toggle", () => { if (detail.open) void load(); });
  }
  return card;
}
async function loadConversationArtifacts() {
  if (!state.conversation) return;
  const panel = $("#artifacts-panel"); const list = $("#artifacts-list");
  const context = { conversationId: state.conversation.id, viewVersion: state.viewVersion, authVersion: state.authVersion, artifactVersion: ++state.artifactVersion };
  panel.hidden = false; list.replaceChildren(notice("正在读取本对话的成果与历史版本…"));
  $("#artifacts-toggle").textContent = "收起文件"; $("#artifacts-toggle").setAttribute("aria-expanded", "true");
  try {
    const data = await api(`/api/conversations/${encodeURIComponent(context.conversationId)}/artifacts`);
    if (!artifactContextCurrent(context)) return;
    const artifacts = (Array.isArray(data.artifacts) ? data.artifacts : []).slice().sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")) || (Number(b.revision) || 0) - (Number(a.revision) || 0));
    list.replaceChildren(el("h2", { class: "artifact-list-title", text: "本对话的成果" }), el("p", { class: "artifact-history-note", text: "新版本显示在前。历史版本会保留；继续修改时，直接在此对话补充要求。" }), ...(artifacts.length ? artifacts.map((artifact) => artifactCard(artifact, context)) : [notice("尚无成果文件。可以先生成需求文件，或选择成果形式并开始团队协作。") ]));
  } catch (error) {
    if (artifactContextCurrent(context)) list.replaceChildren(notice(error.message || "暂时无法读取成果。", true), button("重新加载成果", loadConversationArtifacts));
  }
}

function openForm(title, fields, submitLabel, handler, description) {
  state.dialogHandler = handler; $("#dialog-title").textContent = title; $("#dialog-submit").textContent = submitLabel;
  $("#dialog-submit").disabled = false; $("#dialog-error").hidden = true;
  const children = description ? [notice(description)] : [];
  for (const field of fields) {
    const input = el(field.multiline ? "textarea" : "input", { name: field.name, ...(field.multiline ? {} : { type: field.type || "text" }), ...(field.required === false ? {} : { required: "" }), ...(field.maxLength ? { maxlength: field.maxLength } : {}), ...(field.placeholder ? { placeholder: field.placeholder } : {}), ...(field.autocomplete ? { autocomplete: field.autocomplete } : {}) });
    input.value = field.value || ""; children.push(el("label", {}, [field.label, input]));
  }
  $("#dialog-fields").replaceChildren(...children); $("#form-dialog").showModal();
}

async function projectsPage() {
  const data = await api("/api/projects"); state.projects = data.projects || [];
  const add = button("＋ 新建项目", () => openForm("新建项目", [{ name: "title", label: "项目名称", maxLength: 120 }, { name: "description", label: "你希望完成什么", multiline: true, required: false, maxLength: 4000 }], "创建项目", async (values) => { await api("/api/projects", { method: "POST", body: values }); await refreshWorkspace(); await switchView("projects"); showToast("项目已创建"); }), "button primary");
  const nodes = [heading("我的项目", "把相关目标与对话放在一起，继续推进。", add)];
  if (!state.projects.length) { nodes.push(empty("给想法一个位置", "创建第一个项目，再把对话归入其中。", "▤")); return nodes; }
  nodes.push(el("div", { class: "card-grid" }, state.projects.map((project) => el("article", { class: "card" }, [el("h2", { text: project.title }), el("p", { text: project.description || "还没有项目说明。" }), el("div", { class: "card-footer" }, [el("small", { text: `创建于 ${dateText(project.created_at)}` }), el("div", {}, [button("开始对话", () => { newConversation(); $("#composer-project").value = project.id; }), document.createTextNode(" "), exportLink(`/api/projects/${encodeURIComponent(project.id)}/export`)])])]))));
  return nodes;
}

async function memoriesPage() {
  const data = await api("/api/memories");
  const edit = (memory = null) => openForm(memory ? "修改记忆" : "添加记忆", [{ name: "content", label: "希望保留的偏好或背景", multiline: true, value: memory?.content || "", maxLength: 4000 }], "保存记忆", async (values) => { await api(memory ? `/api/memories/${encodeURIComponent(memory.id)}` : "/api/memories", { method: memory ? "PATCH" : "POST", body: values }); await switchView("memories"); showToast("记忆已保存"); });
  const nodes = [heading("个人记忆", "你决定留下什么，也可以随时修改或删除。", button("＋ 添加记忆", () => edit(), "button primary")), notice("这些记忆目前仅保存在你的账号中。自动记忆和向模型提供记忆仍待交付。")];
  if (!data.memories?.length) { nodes.push(empty("让圆桌慢慢了解你", "可以先记下沟通偏好、工作习惯或项目背景。")); return nodes; }
  nodes.push(el("div", { class: "card-grid" }, data.memories.map((memory) => el("article", { class: "card" }, [el("p", { text: memory.content }), el("div", { class: "card-footer" }, [el("small", { text: `更新于 ${dateText(memory.updated_at)}` }), el("div", {}, [button("修改", () => edit(memory)), document.createTextNode(" "), button("删除", () => openForm("删除这条记忆", [], "确认删除", async () => { await api(`/api/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE" }); await switchView("memories"); showToast("记忆已删除"); }, "删除后，这条记忆将不再出现在你的个人记忆中。"), "button danger")])])]))));
  return nodes;
}

async function capabilitiesPage() {
  const data = await refreshCapabilities();
  const labels = { available: "已验证可用", pending: "待交付", partial: "部分可用", unavailable: "暂不可用", unverified: "待验证" };
  const team = data.team || {};
  const nodes = [heading("能力进展", "各项能力的接入与验证状态。"), el("section", { class: "card connection-card" }, [el("h2", { text: team.configured ? "文字协作团队" : "团队模型待配置" }), el("p", { text: team.notice || "请先完成团队中各模型的连接与验收，再刷新状态。" }), el("p", { text: modelName() }), button("查看团队与模型", async () => { await switchView("roundtable"); $("#composer-options").open = true; $("#connection-help").open = true; $("#connection-help").scrollIntoView({ block: "nearest" }); })])];
  if (data.material_tools?.length) nodes.push(el("section", { class: "card" }, [
    el("h2", { text: "已接通的资料工具" }),
    ...data.material_tools.map(tool => el("div", { class: "list-row" }, [el("div", {}, [el("h3", { text: tool.name }), el("p", { text: tool.description })])])),
    el("p", { text: "在对话中保存需求后，从输入框旁添加材料；只有选中的正文会在确认 AI 协作后发送给模型。" }),
  ]));
  nodes.push(el("div", { class: "card-grid" }, (data.capabilities || []).map((capability, index) => el("article", { class: "card" }, [el("div", { class: "capability-title" }, [el("div", {}, [el("p", { class: "eyebrow", text: `CAPABILITY ${String(index + 1).padStart(2, "0")}` }), el("h2", { text: capability.name })]), el("span", { class: "pending-badge", text: labels[capability.status] || "待交付" })]), el("p", { text: capability.reason || "尚未完成接入与验证。" }), ...(capability.subcapabilities || []).map((sub) => el("div", { class: "list-row" }, [el("div", {}, [el("h3", { text: sub.name }), sub.reason ? el("p", { text: sub.reason }) : null]), el("span", { class: "pending-badge", text: labels[sub.status] || "待交付" })]))]))));
  nodes.push(el("h2", { class: "section-title", text: "发布路径" }), notice("内部测试 → 小范围公测 → 正式开放。正式开放前，所有承诺首版提供的功能都必须通过对应验收。正式上线功能向所有普通用户开放。"));
  return nodes;
}

function row(title, description, actions) { return el("div", { class: "settings-row" }, [el("div", {}, [el("h3", { text: title }), el("p", { text: description })]), el("div", { class: "row-actions" }, Array.isArray(actions) ? actions : [actions])]); }

async function dataPage() {
  const data = await api("/api/data/overview");
  const names = { projects: "项目", conversations: "对话", messages: "消息", tasks: "任务", artifacts: "成果", memories: "记忆", quality_rounds: "质量评估" };
  const nodes = [heading("数据中心", "查看你的数据，决定如何保存和带走。", exportLink("/api/data/export", "↓ 导出全部数据")), el("div", { class: "stat-grid" }, Object.entries(data.counts || {}).filter(([key]) => names[key]).map(([key, count]) => el("div", { class: "stat-card" }, [el("strong", { text: String(count) }), el("span", { text: names[key] })])))];
  const policy = data.policy || {}; const retention = el("select", { "aria-label": "数据保留期限" }, [{ value: 0, title: "长期保留" }, { value: 30, title: "30 天" }, { value: 90, title: "90 天" }, { value: 365, title: "365 天" }].map((item) => el("option", { value: item.value, text: item.title }))); retention.value = String(policy.retention_days || 0);
  const automatic = el("input", { type: "checkbox", "aria-label": "自动清理超期对话" }); automatic.checked = Boolean(policy.auto_cleanup);
  const autoChoice = el("label", { class: "retention-choice" }, [automatic, "自动清理超期对话"]);
  nodes.push(el("section", { class: "card" }, [row("数据保留偏好", "适用于会话、消息、任务和成果；项目、记忆与质量评估单独管理。", [retention, autoChoice, button("保存", async () => {
    if (automatic.checked && Number(retention.value) === 0) throw new Error("请先选择 30、90 或 365 天的保留期限。");
    const result = await api("/api/data/policy", { method: "POST", body: { retention_days: Number(retention.value), auto_cleanup: automatic.checked, confirm_auto_cleanup: automatic.checked } });
    showToast(result.policy.notice); await switchView("data");
  })]), notice(`${policy.auto_cleanup ? "已开启自动清理，后台服务运行时会处理超期对话。" : "当前为手动清理；勾选并保存后，后台服务运行时会自动处理超期对话。"}进行中的任务会保留。历史备份、导出文件和外部服务需另行处理。`), row("个人数据导出", "导出项目、对话、任务、成果、记忆与质量评估；不会导出密码或认证密钥。", exportLink("/api/data/export", "导出 JSON"))]));
  nodes.push(notice(data.notice || "保留期限设置已保存为偏好。自动定期清理尚待交付，当前不会因设置期限而自动删除数据。"));
  const requests = data.deletion_requests || [];
  nodes.push(el("h2", { class: "section-title", text: "账号注销与删除申请" }), el("section", { class: "card" }, [row("申请注销账号", "提交后记录申请并等待处理。当前版本不会自动删除账号或数据。", button("提交申请", () => openForm("申请注销账号", [{ name: "password", label: "输入当前密码确认", type: "password", autocomplete: "current-password" }], "提交注销申请", async (values) => { const result = await api("/api/data/deletion-requests", { method: "POST", body: { password: values.password, confirm: true } }); await switchView("data"); showToast(result.notice || "申请已提交，等待处理。"); }, "此操作会创建注销与删除申请，并不立即删除数据。你可以在处理前撤回。"), "button danger")), ...requests.map((request) => {
    const labels = { pending: "等待处理", cancelled: "已撤回", completed: "已完成", processing: "处理中", rejected: "未获批准" };
    return el("div", { class: "list-row" }, [el("div", {}, [el("h3", { text: labels[request.status] || request.status }), el("p", { text: `申请时间 ${dateText(request.requested_at)}${request.processed_at ? ` · 处理时间 ${dateText(request.processed_at)}` : ""}` })]), request.status === "pending" ? button("撤回", async () => { await api(`/api/data/deletion-requests/${encodeURIComponent(request.id)}/cancel`, { method: "POST", body: {} }); await switchView("data"); showToast("申请已撤回"); }) : el("span", { class: "tag", text: "申请记录" })]);
  })]));
  const deletion = data.account_deletion || {};
  const removeAccount = button("删除账号", () => openForm("删除账号和当前数据", [
    { name: "password", label: "当前密码", type: "password", autocomplete: "current-password" },
    { name: "confirmation", label: "输入“删除我的账号”确认", maxLength: 20 },
  ], "确认删除", async (values) => {
    if (values.confirmation !== "删除我的账号") throw new Error("请完整输入确认文字。");
    const result = await api("/api/data/delete-account", { method: "POST", body: { password: values.password, confirmation: values.confirmation, acknowledge_backup: true } });
    try {
      for (let index = sessionStorage.length - 1; index >= 0; index--) {
        const key = sessionStorage.key(index);
        if (key?.startsWith(`roundtable:pending-draft:${state.user.id}:`) || key?.startsWith(`roundtable:draft-seq:${state.user.id}:`)) sessionStorage.removeItem(key);
      }
    } catch { /* Browser storage may be unavailable. */ }
    afterDialog = () => {
      showAuth(); setAuthMode("login");
      openForm("当前账号已注销", [], "知道了", async () => {}, `当前数据库中的账号及关联记录已删除。历史备份仍需人工处理，导出文件及外部服务的数据不在本次删除范围内。处理编号：${result.receipt}`);
    };
  }, "此操作不可撤销：将删除当前数据库中的账号、项目、对话、资料、成果、记忆、质量评估和安全记录。会保留不含账号内容的核对记录。历史备份、已导出文件及外部模型已收到的数据不会自动删除；恢复旧备份前需处理已注销账号。建议先导出需要保留的数据。"), "button danger");
  removeAccount.disabled = deletion.available === false;
  nodes.push(el("section", { class: "card" }, [row("立即注销并删除当前数据", deletion.available === false ? "当前是最后一名管理员，请先设置另一名管理员。" : "删除当前数据库中的账号及关联记录；历史备份仍需人工处理。", removeAccount)]));
  nodes.push(notice(deletion.backup_notice || "历史备份、已导出文件和外部服务的数据不会自动删除。"));
  return nodes;
}

function showRecoveryCodes(codes) {
  state.dialogHandler = null; $("#dialog-title").textContent = "保存你的恢复码"; $("#dialog-error").hidden = true;
  $("#dialog-fields").replaceChildren(notice("每个恢复码仅可使用一次。请复制并保存在安全的地方，关闭后不会再次显示。"), el("div", { class: "recovery-codes" }, codes.map((code) => el("code", { text: code }))));
  $("#dialog-submit").textContent = "我已保存"; $("#form-dialog").showModal();
}

function rotateRecoveryCodes() {
  openForm("更换恢复码", [
    { name: "password", label: "当前密码", type: "password", autocomplete: "current-password" },
    { name: "code", label: "验证器中的 6 位验证码", autocomplete: "one-time-code", maxLength: 6 },
  ], "生成新的恢复码", async (values) => {
    const data = await api("/api/auth/mfa/recovery/rotate", { method: "POST", body: values });
    afterDialog = () => showRecoveryCodes(data.recovery_codes || []);
  }, "更换后旧恢复码立即失效；新恢复码只显示一次，请妥善保存。请使用验证器验证码。");
}

function startMfaSetup() {
  openForm("设置验证器", [{ name: "password", label: "输入当前密码", type: "password", autocomplete: "current-password" }], "获取设置密钥", async (values) => {
    const data = await api("/api/auth/mfa/setup", { method: "POST", body: values });
    afterDialog = () => {
      openForm("连接验证器", [{ name: "code", label: "验证器中的 6 位验证码", autocomplete: "one-time-code", maxLength: 8 }], "确认开启", async (codes) => {
        const result = await api("/api/auth/mfa/enable", { method: "POST", body: codes });
        state.auth.mfa = { ...state.auth.mfa, enabled: true, required: false, verified: true, setup_required: false };
        afterDialog = () => {
          showRecoveryCodes(result.recovery_codes || []);
          busy(null, async () => { await switchView("security"); if (!isMfaBlocked()) await refreshWorkspace(); });
        };
      }, "在你使用的身份验证器中手动添加账号，输入下方设置密钥，再填写生成的验证码。");
      $("#dialog-fields").prepend(el("p", { class: "mono-box", text: data.secret }));
    };
  });
}

function verifyMfa() {
  openForm("完成安全验证", [{ name: "code", label: "验证器验证码或恢复码", autocomplete: "one-time-code", maxLength: 32 }], "验证", async (values) => {
    const auth = await api("/api/auth/mfa/verify", { method: "POST", body: values }); await useAuth(auth); showToast("验证通过");
  });
}

function changePassword() {
  openForm("修改登录密码", [
    { name: "current_password", label: "当前密码", type: "password", autocomplete: "current-password", maxLength: 1024 },
    { name: "new_password", label: "新密码（至少 12 位）", type: "password", autocomplete: "new-password", maxLength: 1024 },
    { name: "confirm_password", label: "再输入一次新密码", type: "password", autocomplete: "new-password", maxLength: 1024 },
  ], "保存新密码", async (values) => {
    if (values.new_password !== values.confirm_password) throw new Error("两次新密码不一致。");
    const result = await api("/api/auth/password/change", { method: "POST", body: values });
    afterDialog = () => { void switchView("security"); showToast(result.message || "密码已更新"); };
  }, "修改后，其他设备需要重新登录。");
}

async function releasePage() {
  const [{ report }, deletion] = await Promise.all([api("/api/release-readiness"), api("/api/data/deletion-backups")]);
  const labels = { passed: "已满足", failed: "待配置", pending: "待验收" };
  const sections = [heading("上线准备", "查看正式开放前的配置和验收进度。"),
    el("section", { class: "card" }, [row(report.ready ? "检查已通过" : "尚未达到正式开放条件",
      `已满足 ${report.counts.passed} 项 · 待配置 ${report.counts.failed} 项 · 待验收 ${report.counts.pending} 项`, el("span", { class: "tag", text: report.ready ? "已满足" : "准备中" }))]),
    notice("这是只读检查，不会调用模型、发送邮件或部署。配置通过也需要独立验收。"),
    el("section", { class: "card" }, [row("删除记录与备份", `账号注销 ${deletion.pending_count} 项 · 单项删除 ${deletion.record_receipts_count || 0} 项；历史备份不会自动清理。`,
      exportLink("/api/data/deletion-backups/ledger", "导出核对清单"))]),
    notice(deletion.notice || "备份与外部服务仍需分别处理。")];
  for (const [scope, title] of [["configuration", "环境配置"], ["acceptance", "实际验收"]]) {
    sections.push(el("h2", { class: "section-title", text: title }));
    sections.push(el("section", { class: "card" }, report.checks.filter((item) => item.scope === scope)
      .map((item) => row(item.name, item.detail, el("span", { class: "tag", text: labels[item.status] || "待检查" })))));
  }
  return sections;
}

async function securityPage() {
  const auth = await api("/api/auth/me"); state.auth = auth;
  const mfa = auth.mfa || {};
  const nodes = [heading("账号安全", "管理验证方式、登录设备与安全记录。"), el("section", { class: "card" }, [row("当前账号", `${auth.user?.email || ""}${auth.user?.role === "admin" ? " · 管理员" : ""}`, el("span", { class: "tag", text: "邮箱与密码" })), row("多因素认证", mfa.enabled ? "已开启验证器保护，登录时需额外验证。" : auth.user?.role === "admin" ? "管理员必须开启多因素认证后才能继续使用。" : "可使用验证器为账号增加一道保护。", mfa.enabled ? (isMfaBlocked() ? button("完成验证", verifyMfa, "button primary") : el("span", { class: "tag", text: "已开启" })) : button("设置验证器", startMfaSetup, "button primary"))])];
  if (isMfaBlocked()) { nodes.push(notice("请先完成多因素认证设置或验证，之后即可使用圆桌并管理设备。")); return nodes; }
  nodes.push(el("section", { class: "card" }, [row("登录密码", "建议使用至少 12 位的独立密码。", button("修改密码", changePassword))]));
  if (mfa.enabled) nodes.push(el("section", { class: "card" }, [row("恢复码", "验证器不可用时可使用，每个恢复码只能使用一次。", button("更换恢复码", rotateRecoveryCodes))]));
  const results = await Promise.allSettled([api("/api/auth/sessions"), api("/api/auth/security-events")]);
  nodes.push(el("h2", { class: "section-title", text: "登录设备" }));
  if (results[0].status === "fulfilled") {
    const sessions = results[0].value.sessions || [];
    nodes.push(sessions.length ? el("section", { class: "card" }, sessions.map((session) => el("div", { class: "list-row" }, [el("div", {}, [el("h3", { text: session.device || "浏览器会话" }), el("p", { text: `${session.ip_address || ""} · 最近活动 ${dateText(session.last_seen_at || session.created_at)}` })]), session.current ? el("span", { class: "tag", text: "当前设备" }) : session.active === false ? el("span", { class: "tag", text: "已失效" }) : button("退出此设备", async () => { await api(`/api/auth/sessions/${encodeURIComponent(session.id)}/revoke`, { method: "POST", body: {} }); await switchView("security"); showToast("该设备已退出"); })]))) : empty("暂无其他设备", "设备登录后会在这里显示。", "⊙"));
  } else nodes.push(notice(results[0].reason.message, true));
  nodes.push(el("h2", { class: "section-title", text: "近期安全记录" }));
  if (results[1].status === "fulfilled") {
    const events = results[1].value.events || [];
    nodes.push(events.length ? el("section", { class: "card" }, events.slice(0, 15).map((event) => el("div", { class: "list-row" }, [el("div", {}, [el("h3", { text: event.message || event.kind }), el("p", { text: dateText(event.created_at) })])]))) : empty("暂无安全记录", "登录与账号安全操作会记录在这里。", "⊙"));
  } else nodes.push(notice(results[1].reason.message, true));
  nodes.push(el("h2", { class: "section-title", text: "待交付保护" }), notice("邮件通知与邮箱账号恢复需接入并验证邮件服务。当前不能将安全记录视作已完成异常登录提醒。"));
  return nodes;
}

$("#tab-login").addEventListener("click", () => setAuthMode("login"));
$("#tab-register").addEventListener("click", () => setAuthMode("register"));
window.addEventListener("hashchange", () => {
  if (captureRecoveryLink()) { showAuth(); setAuthMode("reset"); $("#recovery-password").focus(); }
});
$("#auth-forgot").addEventListener("click", () => { $("#recovery-email").value = $("#auth-email").value.trim(); setAuthMode("recover"); $("#recovery-email").focus(); });
$("#auth-back").addEventListener("click", async () => {
  state.resetLink = null; $("#recovery-password").value = ""; $("#recovery-password-confirm").value = "";
  setAuthMode("login");
  try { const auth = await api("/api/auth/me"); if (auth.authenticated) await useAuth(auth); } catch { /* Login remains available. */ }
});
$("#recovery-request-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const submit = $("#recovery-request-submit"); if (submit.disabled) return;
  submit.disabled = true; $("#recovery-request-error").hidden = true;
  try {
    const result = await api("/api/auth/password-reset/request", { method: "POST", body: { email: $("#recovery-email").value.trim() } });
    $("#auth-info").textContent = result.message; $("#auth-info").hidden = false;
  } catch (error) { $("#recovery-request-error").textContent = error.message || "连接失败，请重试。"; $("#recovery-request-error").hidden = false; }
  finally { submit.disabled = false; }
});
$("#recovery-confirm-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const submit = $("#recovery-confirm-submit"); if (submit.disabled) return;
  submit.disabled = true; $("#recovery-confirm-error").hidden = true;
  try {
    const password = $("#recovery-password").value;
    if (!state.resetLink?.uid || !state.resetLink?.token) throw new Error("恢复链接无效，请重新申请。 ");
    if (password !== $("#recovery-password-confirm").value) throw new Error("两次新密码不一致。");
    await api("/api/auth/password-reset/confirm", { method: "POST", body: { ...state.resetLink, password } });
    state.resetLink = null; $("#recovery-password").value = ""; $("#recovery-password-confirm").value = "";
    setAuthMode("login"); $("#auth-info").textContent = "密码已更新，请重新登录。已启用的验证器仍然有效。"; $("#auth-info").hidden = false;
  } catch (error) { $("#recovery-confirm-error").textContent = error.message || "连接失败，请重试。"; $("#recovery-confirm-error").hidden = false; }
  finally { submit.disabled = false; }
});
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const submit = $("#auth-submit"); if (submit.disabled) return;
  submit.disabled = true; $("#auth-error").hidden = true;
  try {
    const payload = { email: $("#auth-email").value.trim(), password: $("#auth-password").value };
    if (state.mode === "register") payload.display_name = $("#auth-name").value.trim();
    if (!$("#auth-code-field").hidden && $("#auth-code").value.trim()) payload.code = $("#auth-code").value.trim();
    const data = await api(`/api/auth/${state.mode}`, { method: "POST", body: payload });
    if (data.mfa?.enabled && data.mfa?.required && !data.mfa?.verified) {
      state.auth = data; state.user = data.user; $("#auth-code-field").hidden = false; $("#auth-submit").textContent = "验证并登录";
      $("#auth-error").textContent = "请输入验证器验证码，也可以使用恢复码。"; $("#auth-error").hidden = false;
      if (payload.code) { const verified = await api("/api/auth/mfa/verify", { method: "POST", body: { code: payload.code } }); await useAuth(verified); }
      else $("#auth-code").focus();
    } else await useAuth(data);
  } catch (error) { $("#auth-error").textContent = error.message || "连接失败，请重试。"; $("#auth-error").hidden = false; }
  finally { submit.disabled = false; }
});

document.querySelectorAll(".nav-item").forEach((node) => node.addEventListener("click", () => switchView(node.dataset.view)));
$("#new-conversation").addEventListener("click", newConversation);
$("#menu-toggle").addEventListener("click", () => {
  if (!window.matchMedia("(max-width:760px)").matches) { document.body.classList.toggle("sidebar-collapsed"); syncSidebarFocus(); return; }
  if ($("#sidebar").classList.contains("open")) { closeSidebar(); return; }
  $("#sidebar").classList.add("open"); $("#sidebar-shade").hidden = false; document.body.classList.add("nav-open"); syncSidebarFocus(); $("#new-conversation").focus();
});
$("#sidebar-shade").addEventListener("click", () => { closeSidebar(); $("#menu-toggle").focus(); });
window.matchMedia("(max-width:760px)").addEventListener("change", () => { closeSidebar(); resizeComposer(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && $("#sidebar").classList.contains("open") && !$("#form-dialog").open && !$("#acceptance-dialog").open) { closeSidebar(); $("#menu-toggle").focus(); } });
$("#conversation-search")?.addEventListener("input", renderConversationList);
$("#composer-project").addEventListener("change", () => { rememberDraft(); renderModelControls(); });
$("#logout").addEventListener("click", () => busy($("#logout"), async () => { rememberDraft(); await flushDrafts(); await api("/api/auth/logout", { method: "POST", body: {} }); try { sessionStorage.removeItem(`roundtable:conversation:${state.user.id}`); } catch {} state.user = null; state.auth = null; state.projects = []; state.conversations = []; showAuth(); setAuthMode("login"); }));
document.querySelectorAll(".starter").forEach((node) => node.addEventListener("click", () => { const input = $("#message-input"); input.value = input.value.trim() ? input.value : node.dataset.prompt; rememberDraft(); resizeComposer(); renderModelControls(); input.focus(); }));
$("#delivery-mode").addEventListener("change", () => {
  if (state.activeReply || state.activeSave || currentTask()?.status === "running") { $("#delivery-mode").value = state.deliveryMode; return; }
  setDeliveryMode($("#delivery-mode").value); renderModelControls();
});
$("#participant-count").addEventListener("change", () => {
  if (state.activeReply || state.activeSave || currentTask()?.status === "running" || pendingSubmission(state.conversation?.id)) { $("#participant-count").value = state.participantCount; return; }
  state.participantCount = $("#participant-count").value;
  state.aiEnabled = false; state.aiConsentFor = null; closeAiConsent(); renderModelControls();
});
$("#acceptance-open").addEventListener("click", () => { void openAcceptanceEditor(); });
$("#acceptance-close").addEventListener("click", closeAcceptanceEditor);
$("#acceptance-cancel").addEventListener("click", closeAcceptanceEditor);
$("#acceptance-dialog").addEventListener("cancel", (event) => { event.preventDefault(); if (!state.acceptanceEditor?.saving) closeAcceptanceEditor(); });
$("#acceptance-add").addEventListener("click", () => {
  const editor = state.acceptanceEditor;
  if (!editor || !acceptanceEditorCurrent(editor) || !editor.loaded || editor.loading || editor.saving || editor.cases.length >= 5 || currentTask()?.status === "running") return;
  editor.cases.push({ stdin: "", expected_stdout: "" }); renderAcceptanceEditor(editor);
  $("#acceptance-cases").querySelector("fieldset:last-child textarea")?.focus();
});
$("#acceptance-reload").addEventListener("click", () => { if (state.acceptanceEditor) void loadAcceptanceEditor(state.acceptanceEditor); });
$("#acceptance-form").addEventListener("submit", saveAcceptanceEditor);
$("#ai-enabled").addEventListener("change", () => {
  if (pendingSubmission(state.conversation?.id)) { state.aiEnabled = false; renderModelControls(); return; }
  if (!$("#ai-enabled").checked) { state.aiEnabled = false; state.saveOnlyMode = true; closeAiConsent(); renderModelControls(); return; }
  if (!chatAvailable()) { state.aiEnabled = false; renderModelControls(); return; }
  const identity = modelIdentity();
  if (state.aiConsentFor === identity) { state.aiEnabled = true; state.saveOnlyMode = false; renderModelControls(); return; }
  $("#ai-enabled").checked = false;
  promptAIConsent("enable");
});
function promptAIConsent(action) {
  if (!chatAvailable() || state.activeSave || state.activeReply || sourcesUI?.getState().busy) return;
  state.pendingConsent = modelIdentity();
  state.consentAction = action;
  state.consentDraft = {conversationId: state.conversation?.id, version: state.viewVersion,
    text: action === "send" ? $("#message-input").value : null, taskId: currentTask()?.id};
  const count = state.conversation?.included_sources_count || 0;
  $("#ai-consent-text").textContent = `本次对话、相关成果和成员回复${count ? `，以及所选 ${count} 份材料的文字与出处` : ""}将发给 ${modelName()}。每轮最多调用 ${deliveryLimit()} 次，使用已配置账号的额度，可能产生费用。`;
  $("#ai-consent-save").hidden = action !== "send";
  $("#ai-consent-confirm").textContent = action === "enable" ? "开启 AI 团队" : "确认并开始讨论";
  $("#ai-consent").hidden = false;
  $("#composer-options").open = false;
  $("#ai-consent-confirm").focus();
}
$("#ai-consent-confirm")?.addEventListener("click", () => {
  const draft = state.consentDraft;
  if (!draft || draft.version !== state.viewVersion || draft.conversationId !== state.conversation?.id ||
      draft.taskId !== currentTask()?.id || (draft.text !== null && draft.text !== $("#message-input").value) ||
      !state.pendingConsent || state.pendingConsent !== modelIdentity() || !chatAvailable() || currentTask()?.status === "running") {
    closeAiConsent(); renderModelControls(); return;
  }
  const action = state.consentAction;
  state.aiConsentFor = state.pendingConsent; state.aiEnabled = true; state.saveOnlyMode = false; closeAiConsent(); renderModelControls();
  if ($("#composer-options")) $("#composer-options").open = false;
  $("#message-input").focus();
  if (action === "send") $("#composer").requestSubmit();
  else if (action === "saved") void requestReply();
});
$("#ai-consent-cancel")?.addEventListener("click", () => { closeAiConsent(); renderModelControls(); $("#message-input").focus(); });
$("#refresh-connection").addEventListener("click", () => busy($("#refresh-connection"), async () => { await refreshCapabilities(); }));
$("#copy-connection-command").addEventListener("click", () => busy($("#copy-connection-command"), async () => {
  if (!navigator.clipboard?.writeText) { showToast("请复制文件名：配置模型.cmd"); return; }
  try { await navigator.clipboard.writeText("配置模型.cmd"); showToast("已复制文件名：配置模型.cmd"); } catch { showToast("请复制文件名：配置模型.cmd"); }
}));
$("#request-reply").addEventListener("click", () => {
  if (!state.aiEnabled) {
    if (!$("#ai-enabled").disabled) promptAIConsent("saved");
    return;
  }
  void requestReply();
});
$("#skip-clarification").addEventListener("click", () => { void submitClarification(""); });
$("#delete-conversation").addEventListener("click", () => {
  const id = state.conversation?.id; const authVersion = state.authVersion;
  if (!id || state.activeReply || state.activeSave || pendingSubmission(id)) return;
  openForm("删除对话", [
    { name: "password", label: "当前密码", type: "password", autocomplete: "current-password" },
    { name: "confirmation", label: "输入“删除此对话”确认", maxLength: 20 },
  ], "确认删除", async (values) => {
    if (values.confirmation !== "删除此对话") throw new Error("请完整输入确认文字。");
    await api(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE", body: values });
    afterDialog = () => {
      if (state.authVersion !== authVersion || state.conversation?.id !== id) return;
      state.drafts.delete(id); clearTimeout(state.draftTimers.get(id)); state.draftTimers.delete(id); state.draftPending.delete(id);
      forgetPendingDraft(state.user.id, id); newConversation({skipRemember: true}); void refreshWorkspace().catch(() => {}); showToast("对话已从当前数据库删除。");
    };
  }, "此操作会删除当前数据库中的对话、消息、资料、任务和成果，无法撤销。已有备份、导出文件及外部模型已收到的数据不会自动删除。");
});
$("#retry-pending-submission").addEventListener("click", () => {
  const id = state.conversation?.id; const saved = pendingSubmission(id); const authVersion = state.authVersion;
  if (!saved) return;
  openForm("继续同一次提交", [], "确认继续", async () => {
    afterDialog = () => { if (state.authVersion === authVersion && state.conversation?.id === id && pendingSubmission(id)?.id === saved.id) void requestReply(true); };
  }, `将使用原来的提交编号。如果之前未进入后台，这次可能开始处理，最多 ${saved.body.expected_max_calls} 次模型调用并使用额度；已收到的同一次提交不会重复创建。`);
});
$("#stop-waiting").addEventListener("click", () => { stopReplyWaiting("user"); renderChat(); });
$("#refresh-conversation").addEventListener("click", () => busy($("#refresh-conversation"), async () => { if (state.conversation && !state.activeSave) await checkPendingSubmission(state.conversation.id); }));
$("#chat-scroll")?.addEventListener("scroll", syncChatScroll, {passive: true});
for (const type of ["wheel", "touchstart", "pointerdown", "keydown"]) {
  $("#chat-scroll")?.addEventListener(type, cancelAutomaticChatScroll, {passive: true});
}
$("#scroll-to-bottom")?.addEventListener("click", () => scrollToLatest(true));
document.addEventListener("pointerdown", (event) => {
  if ($("#acceptance-dialog").open || $("#form-dialog").open || $("#sources-dialog")?.open) return;
  const options = $("#composer-options");
  if (options?.open && !options.contains(event.target) && !$("#ai-consent").contains(event.target)) { options.open = false; closeAiConsent(); }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#composer-options")?.open && !$("#acceptance-dialog").open && !$("#sources-dialog")?.open) { $("#composer-options").open = false; closeAiConsent(); $("#composer-options summary").focus(); }
});
$("#message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $("#composer").requestSubmit(); } });
$("#message-input").addEventListener("input", () => {
  if (state.consentAction === "send") closeAiConsent();
  rememberDraft(); resizeComposer(); renderModelControls();
});
window.addEventListener("pagehide", () => {
  rememberDraft();
  for (const pending of state.draftPending.values()) {
    if (pending.userId !== state.user?.id) continue;
    void fetch("/api/drafts", {method: "POST", credentials: "same-origin", keepalive: true,
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()}, body: JSON.stringify(pending.payload)}).catch(() => {});
  }
});
$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault(); const content = $("#message-input").value.trim(); if (!content) return;
  const pendingAnswer = interjectionDraft();
  if (pendingAnswer?.kind === "clarification" && pendingAnswer.text === content) {
    await submitClarification(content, {id: pendingAnswer.runId, max_calls: pendingAnswer.maxCalls}); return;
  }
  const liveRun = interjectionRun();
  if (liveRun) { await sendInterjection(liveRun, content); return; }
  if (pendingSubmission(state.conversation?.id)) { showToast("请先核对上一轮提交状态。", true); return; }
  if (state.activeSave || state.activeReply || currentTask()?.status === "running") return;
  if (currentTask()?.runs?.[0]?.status === "awaiting_input" && !state.replyFeedback[state.conversation?.id]?.briefStale) {
    await submitClarification(content); return;
  }
  if (sourcesUI?.getState().busy) return;
  const saveOnly = event.submitter?.id === "ai-consent-save";
  if (saveOnly) {
    if (state.consentAction !== "send") return;
    closeAiConsent();
  } else if (chatAvailable() && !state.aiEnabled && !state.saveOnlyMode && currentTask()?.status !== "paused") {
    promptAIConsent("send"); return;
  }
  const operation = { version: state.viewVersion, authVersion: state.authVersion, wantReply: !saveOnly && chatAvailable() && state.aiEnabled, model: modelIdentity(), draftKey: state.conversation?.id || "new", draftText: $("#message-input").value };
  state.conversationRevision++; state.activeSave = operation; renderModelControls(); let shouldReply = false;
  let conversationId = state.conversation?.id;
  const contextCurrent = () => operation.authVersion === state.authVersion && operation.version === state.viewVersion && state.view === "roundtable" && state.conversation?.id === conversationId;
  try {
    const project = conversationId ? null : $("#composer-project").value || null;
    const receipt = await messageSaveReceipt(state.user.id, operation.draftKey, project, content);
    if (!contextCurrent()) return;
    const path = conversationId ? `/api/conversations/${encodeURIComponent(conversationId)}` : "/api/conversations";
    const saved = await api(path, {method:"POST", body: {content, client_message_id: receipt.id,
      ...(!conversationId ? {project_id: project} : {})}});
    if (!contextCurrent()) return;
    receipt.clear();
    if (!conversationId) {
      conversationId = saved.conversation.id;
      state.conversation = saved.conversation;
    }
    if (operation.authVersion === state.authVersion) forgetDraft(operation.draftKey, operation.draftText);
    if (contextCurrent()) {
      if (saved.conversation) state.conversation = saved.conversation;
      rememberConversation(); state.followLatest = true;
      if (saved.message && !state.messages.some(item => item.id === saved.message.id)) state.messages.push(saved.message);
      if (saved.task) state.tasks = [saved.task, ...state.tasks.filter((task) => task.id !== saved.task.id)];
      delete state.replyFeedback[conversationId];
      if ($("#message-input").value.trim() === content) $("#message-input").value = "";
      if (operation.draftKey === "new" && state.drafts.get("new")?.text === $("#message-input").value) {
        forgetDraft("new", $("#message-input").value);
      }
      rememberDraft(); resizeComposer();
      renderChat();
      try { await reloadConversation(conversationId, operation.version); } catch { /* The saved response already contains the user message and task. */ }
      shouldReply = contextCurrent() && operation.wantReply && state.aiEnabled && chatAvailable() && operation.model === modelIdentity() && currentTask()?.status === "waiting";
      if (contextCurrent()) $("#message-input").focus();
    }
    await refreshWorkspace().catch(() => {});
  } catch (error) { if (operation.authVersion === state.authVersion && operation.version === state.viewVersion) showToast(error.message || "需求暂未保存，请重试。", true); }
  finally { if (state.activeSave === operation) state.activeSave = null; if (operation.authVersion === state.authVersion) renderModelControls(); }
  if (shouldReply && contextCurrent()) await requestReply();
});
$("#pause-task").addEventListener("click", () => busy($("#pause-task"), async () => {
  const task = currentTask(); if (!task) return; const version = state.viewVersion; const conversationId = state.conversation.id;
  const revision = ++state.conversationRevision;
  const resuming = task.status === "paused";
  const wasQueued = task.runs?.[0]?.status === "queued" && !task.runs?.[0]?.calls_used;
  const result = await api(`/api/tasks/${encodeURIComponent(task.id)}/transition`, { method: "POST", body: { action: resuming ? "resume" : "pause" } });
  if (state.activeReply?.conversationId === conversationId) stopReplyWaiting("pause");
  if (version === state.viewVersion && revision === state.conversationRevision && state.conversation?.id === conversationId) {
    delete state.replyFeedback[conversationId];
    if (result.task) state.tasks = [result.task, ...state.tasks.filter((item) => item.id !== result.task.id)];
    renderChat(); await reloadConversation(conversationId, version);
    if (!resuming) state.replyFeedback[conversationId] = {text: wasQueued && currentTask()?.runs?.[0]?.calls_used === 0 ? "排队任务已取消，未调用模型。" : "本轮已停止。已发出的请求可能仍计费，迟到结果不会写入。"};
    renderReplyFeedback();
  }
}));
$("#brief-task").addEventListener("click", () => busy($("#brief-task"), async () => {
  const task = currentTask(); if (!task) return; const version = state.viewVersion; const authVersion = state.authVersion; const conversationId = state.conversation.id;
  await api(`/api/tasks/${encodeURIComponent(task.id)}/brief`, { method: "POST", body: {} });
  if (version !== state.viewVersion || authVersion !== state.authVersion || conversationId !== state.conversation?.id) return;
  if (!$("#artifacts-panel").hidden) await loadConversationArtifacts();
  showToast("需求已导出，可在“下载成果”中查看。");
}));
$("#artifacts-toggle").addEventListener("click", () => busy($("#artifacts-toggle"), async () => {
  if (!$("#artifacts-panel").hidden) { hideArtifacts(); return; }
  await loadConversationArtifacts();
}));
document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll(".conversation-menu[open], .report-menu[open]")) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const menu = event.target.closest?.(".conversation-menu[open], .report-menu[open]");
  if (!menu) return;
  event.preventDefault(); menu.open = false; menu.querySelector(":scope > summary")?.focus();
});
function closeDialog() { $("#form-dialog").close(); $("#dialog-fields").replaceChildren(); state.dialogHandler = null; afterDialog = null; }
$("#dialog-close").addEventListener("click", closeDialog); $("#dialog-cancel").addEventListener("click", closeDialog);
$("#form-dialog").addEventListener("cancel", (event) => { if ($("#dialog-submit").disabled) { event.preventDefault(); return; } $("#dialog-fields").replaceChildren(); state.dialogHandler = null; afterDialog = null; });
$("#dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const submit = $("#dialog-submit"); if (submit.disabled) return;
  if (!state.dialogHandler) { closeDialog(); return; }
  submit.disabled = true; $("#dialog-close").disabled = true; $("#dialog-cancel").disabled = true; $("#dialog-error").hidden = true;
  try { const handler = state.dialogHandler; const values = Object.fromEntries(new FormData(event.currentTarget)); await handler(values); const next = afterDialog; closeDialog(); if (next) next(); }
  catch (error) { $("#dialog-error").textContent = error.message || "操作未完成，请重试。"; $("#dialog-error").hidden = false; }
  finally { submit.disabled = false; $("#dialog-close").disabled = false; $("#dialog-cancel").disabled = false; }
});

sourcesUI = window.RoundtableSources?.create({api,
  onDraftReady(conversation) {
    state.conversation = conversation;
    forgetDraft("new", $("#message-input").value);
    rememberConversation(); rememberDraft(); renderChat(true);
    void refreshWorkspace().catch(() => {});
  },
  onMutationStart() { state.aiEnabled = false; state.aiConsentFor = null; closeAiConsent(); renderModelControls(); },
  onBusyChange() { if (!sourcesResetting) renderModelControls(); },
  onChange(data) {
    if (!state.conversation || data.conversationId !== state.conversation.id) return;
    if (state.conversation.sources_revision !== data.revision) {
      state.aiEnabled = false; state.aiConsentFor = null; closeAiConsent();
    }
    state.conversation.sources_revision = data.revision;
    state.conversation.sources_count = data.sources.length;
    state.conversation.included_sources_count = data.sources.filter(source => source.included).length;
    renderModelControls();
  },
});

qualityUI = window.RoundtableQuality.create({api, el, button, heading, notice, openForm,
  getVersion: () => state.viewVersion,
  isActive: () => Boolean(state.user) && state.view === "quality",
  refresh: () => switchView("quality"),
});

(async () => {
  try {
    const auth = await api("/api/auth/me");
    if (state.resetLink) { showAuth(); setAuthMode("reset"); $("#recovery-password").focus(); }
    else await useAuth(auth);
  }
  catch (error) { showAuth(); $("#auth-error").textContent = error.message || "无法连接本地服务，请稍后重试。"; $("#auth-error").hidden = false; }
})();

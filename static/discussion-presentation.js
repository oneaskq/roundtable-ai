(() => {
  "use strict";

  function waiting(run, submitting, providerName) {
    const notes = (run?.discussion || []).filter(item => item.kind === "user" && item.target_provider);
    const mentions = notes.filter(item => ["mention_queued", "mention_running"].includes(item.status));
    if (run?.status === "awaiting_input" && mentions.length) {
      const people = [...new Set(mentions.map(item => providerName(item.target_provider)))];
      return {busy: true, text: "等待" + people.join("、") + "回应你的提问"};
    }
    if (submitting) return {busy: true, text: "正在提交本轮问题"};
    if (run?.status === "queued") return {busy: true, text: "已排队，等待团队开始"};
    if (run?.status !== "running") return {busy: false, text: ""};
    if (run.decision?.phase === "research") return {busy: true, text: "正在查阅公开资料"};
    const steps = (run.steps || []).filter(step => step.status === "running");
    const groups = new Map();
    for (const step of steps) {
      const role = step.role || "";
      let action;
      if (role.startsWith("analysis_")) action = "独立分析";
      else if (role.startsWith("review_") || role === "extra_review") action = "交叉核验";
      else if (role.startsWith("response_") || role === "extra_response") action = "回应质疑";
      else if (role.startsWith("mention_") || role.startsWith("quick_")) action = "回复";
      else if (role === "clarifier" || role === "clarifier_peer") action = "梳理问题";
      else if (role === "synthesizer") action = "整理结论";
      else action = (step.label || "处理任务").split(" · ")[0];
      if (!groups.has(action)) groups.set(action, new Set());
      if (step.provider || step.model) groups.get(action).add(step.provider ? providerName(step.provider) : step.model);
    }
    const phrases = [...groups].map(([action, members]) => [...members].join("、") + "正在" + action);
    return {busy: true, text: phrases.join("；") || "团队正在处理本轮问题"};
  }

  function turnPresentation(item) {
    const turn = item.turn;
    if (item.kind !== "assistant" || !turn || typeof turn.text !== "string") return null;
    const reference = turn.reply_to;
    const toUser = reference?.kind === "user";
    let detail = item.content || "";
    if (detail === turn.text) detail = "";
    else if (toUser && detail.startsWith(turn.text + "\n\n")) detail = detail.slice(turn.text.length + 2);
    return {text: turn.text, decision: turn.decision, detail, detailLabel: turn.detail_label,
      reference: reference?.label && reference.text ? {
        label: toUser ? "回复你" : "回应 " + reference.label, text: reference.text,
      } : null};
  }

  window.RoundtableDiscussion = {waiting, turnPresentation};
})();

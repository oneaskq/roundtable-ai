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
    if (run.decision?.phase === "research") return {busy: true, text: "正在读取已授权的公开资料"};
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

  function analysisPreview(item, run) {
    if (item.kind !== "assistant" || (run.workflow && run.workflow !== "decision_v1") ||
        ["quick", "quick_group", "direct_member"].includes(run.decision?.response_kind)) return null;
    const independent = item.role ? /^analysis_p\d+$/.test(item.role) : /^独立分析(?:$|\s|·)/.test(item.label || "");
    const content = item.content || "";
    if (!independent || (Array.from(content).length <= 185 && content.trim().split(/\r?\n/).length <= 5)) return null;
    const [recommendation, claims = ""] = content.split(/\n\s*关键主张[：:]\s*\n/);
    const candidates = [recommendation.trim(), ...claims.split(/\n\s*[-*•]\s*/).map(line => line.replace(/^[-*•]\s*/, "").trim())];
    const excerpts = [];
    for (const candidate of candidates) {
      if (!candidate) continue;
      const sentence = candidate.match(/^[\s\S]*?[。！？](?:[”」）])?/)?.[0] || candidate.split("\n")[0];
      const letters = Array.from(sentence);
      const excerpt = letters.length > 70 ? letters.slice(0, 69).join("").trimEnd() + "…" : sentence;
      if (excerpt && !excerpts.includes(excerpt)) excerpts.push(excerpt);
      if (excerpts.length === 2) break;
    }
    return excerpts.length ? excerpts : null;
  }

  window.RoundtableDiscussion = {waiting, analysisPreview};
})();

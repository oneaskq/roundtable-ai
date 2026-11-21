(function () {
  "use strict";
  const time = value => value ? Date.parse(value) : NaN;
  function duration(ms) {
    const seconds = Math.max(0, Math.round(ms / 1000));
    return seconds < 60 ? seconds + " 秒" : Math.floor(seconds / 60) + " 分 " + seconds % 60 + " 秒";
  }
  function elapsed(run, end) {
    const start = time(run.created_at);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
    const pause = Number(run.decision?.clarification_wait_ms) || 0;
    return duration(end - start - pause);
  }
  function metrics(run) {
    if (run?.workflow !== "decision_v1") return [];
    const quick = ["quick", "quick_group", "direct_member"].includes(run.decision?.response_kind);
    const first = (run.discussion || []).filter(item => item.kind === "assistant" && item.content &&
      (item.role.startsWith("analysis_") || (quick && (item.role === "clarifier" || item.role.startsWith("quick_")))))
      .map(item => time(item.created_at)).filter(Number.isFinite).sort((a,b) => a-b)[0];
    const result = [];
    const initial = elapsed(run, first);
    if (initial) result.push("首条判断 " + initial);
    const final = run.status === "completed" && elapsed(run, time(run.finished_at));
    if (final) result.push((quick ? "整轮回复 " : "最终报告 ") + final);
    return result;
  }
  let timer;
  function monitor(element, run) {
    clearInterval(timer);
    if (!element) return;
    element.hidden = !run || run.workflow !== "decision_v1" || !["running","queued"].includes(run.status) || !Number.isFinite(time(run.created_at));
    if (element.hidden) { element.textContent = ""; return; }
    const first = metrics(run);
    const tick = () => {
      if (!element.isConnected || element.closest("[hidden]")) { clearInterval(timer); return; }
      element.textContent = ["已用 " + elapsed(run, Date.now()), ...first].join(" · ");
    };
    tick();
    timer = setInterval(tick, 1000);
  }
  window.RoundtableTiming = {metrics, monitor};
})();

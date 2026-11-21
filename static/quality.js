"use strict";

// Manual evaluations never initiate model requests or enable capabilities.
window.RoundtableQuality = {
  create({api, el, button, heading, notice, openForm, getVersion, isActive, refresh}) {
    let selectedCase = "arithmetic", activeRound = null;
    const active = (version) => isActive() && getVersion() === version;
    const scoreNames = {accuracy: "事实与约束", coverage: "任务覆盖", usefulness: "清晰与可用"};
    const statusNames = {collecting: "收集回复", reviewing: "人工评审", completed: "已完成评审"};
    const text = (tag, value, className = "") => el(tag, {text: value, ...(className ? {class: className} : {})});

    function promptPanel(case_) {
      const prompt = text("pre", case_.prompt, "quality-prompt");
      const feedback = text("span", "", "quality-copy-status");
      const copy = button("复制题目", async () => {
        try { await navigator.clipboard.writeText(case_.prompt); feedback.textContent = "已复制"; }
        catch { feedback.textContent = "请手动选择题目文字复制。"; }
      }, "text-button");
      return el("section", {class: "card quality-case"}, [
        text("p", `${case_.role} · ${case_.title}`, "quality-case-label"), prompt,
        text("p", case_.review_focus, "quality-hint"), el("div", {class: "quality-case-actions"}, [copy, feedback]),
      ]);
    }

    async function createRound(caseId, action) {
      const version = getVersion(); action.disabled = true;
      try {
        const result = await api("/api/quality/rounds", {method: "POST", body: {case_id: caseId}});
        if (active(version)) { activeRound = result.round.id; await refresh(); }
      } catch (error) { if (active(version)) { action.disabled = false; throw error; } }
    }

    function sampleCard(sample, round, rerender) {
      const checks = sample.checks || [];
      const node = el("article", {class: "card quality-sample"}, [
        el("div", {class: "quality-sample-heading"}, [
          text("h3", sample.alias),
          sample.model_label ? text("span", sample.model_label, "quality-model") : text("span", "模型名称暂时隐藏", "quality-model"),
        ]),
        text("pre", sample.output, "quality-output"),
        el("div", {class: "quality-checks"}, [text("strong", "客观检查"),
          ...checks.map(check => text("span", `${check.passed ? "✓" : "—"} ${check.name}`, check.passed ? "quality-check-pass" : "quality-check-fail"))]),
      ]);
      if (sample.reviewed_at) {
        const ratings = sample.review?.ratings || {};
        node.append(text("p", `人工评分：${Object.entries(scoreNames).map(([id, name]) => `${name} ${ratings[id] ?? "—"}/2`).join(" · ")}`, "quality-hint"));
        if (sample.review?.note) node.append(text("p", sample.review.note, "quality-review-note"));
      } else if (round.status === "reviewing") {
        const selects = {};
        const fields = (round.case.rubric || []).map((item) => {
          const select = el("select", {"aria-label": `${sample.alias} ${item.name}`, required: ""}, [
            el("option", {value: "", text: "请选择"}),
            el("option", {value: "0", text: "0 · 未满足"}),
            el("option", {value: "1", text: "1 · 部分满足"}),
            el("option", {value: "2", text: "2 · 满足"}),
          ]);
          selects[item.id] = select;
          return el("label", {class: "quality-rating"}, [text("span", item.name), select]);
        });
        const note = el("textarea", {rows: "2", maxlength: "1000", placeholder: "可填写关键理由（选填）", "aria-label": `${sample.alias} 评审备注`});
        const error = el("p", {class: "form-error", role: "alert", hidden: ""});
        const submit = el("button", {type: "submit", class: "button primary", text: "保存评分"});
        const form = el("form", {class: "quality-review-form"}, [el("div", {class: "quality-ratings"}, fields), note, error, submit]);
        form.addEventListener("submit", async (event) => {
          event.preventDefault(); if (submit.disabled) return;
          const version = getVersion(); submit.disabled = true; error.hidden = true;
          try {
            const ratings = Object.fromEntries(Object.entries(selects).map(([id, select]) => [id, Number(select.value)]));
            if (Object.values(selects).some(select => select.value === "")) throw new Error("请选择三项评分。");
            await api(`/api/quality/samples/${encodeURIComponent(sample.id)}/review`, {method: "POST", body: {ratings, note: note.value}});
            if (active(version)) await rerender();
          } catch (failure) { if (active(version)) { error.textContent = failure.message || "评分未保存，请重试。"; error.hidden = false; submit.disabled = false; } }
        });
        node.append(form);
      }
      return node;
    }

    function sampleForm(round, rerender) {
      const model = el("input", {maxlength: "80", required: "", placeholder: "例如：模型名称和版本", autocomplete: "off"});
      const output = el("textarea", {rows: "7", maxlength: "12000", required: "", placeholder: "粘贴这道题目的完整回复"});
      const error = el("p", {class: "form-error", role: "alert", hidden: ""});
      const submit = el("button", {type: "submit", class: "button primary", text: "添加回复"});
      const form = el("form", {class: "card quality-form"}, [text("h2", "添加一份回复"),
        text("p", "名称和回复由你提供，请勿填写密钥。每组最多3份。", "quality-hint"),
        el("label", {}, [text("span", "模型名称及版本"), model]),
        el("label", {}, [text("span", "回复内容"), output]), error, submit]);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (submit.disabled) return;
        const version = getVersion(); submit.disabled = true; error.hidden = true;
        try {
          await api(`/api/quality/rounds/${encodeURIComponent(round.id)}/samples`,
                    {method: "POST", body: {model_label: model.value, output: output.value}});
          if (active(version)) await rerender();
        } catch (failure) { if (active(version)) { error.textContent = failure.message || "回复未保存，请重试。"; error.hidden = false; submit.disabled = false; } }
      });
      return form;
    }

    function detailNodes(round) {
      const nodes = [heading("质量评估", "同一题目比较多份回复。"),
        el("div", {class: "quality-toolbar"}, [
          button("← 返回评估列表", async () => { activeRound = null; await refresh(); }, "text-button"),
          button("删除这组评估", () => openForm("删除这组评估", [], "确认删除", async () => {
            const version = getVersion();
            await api(`/api/quality/rounds/${encodeURIComponent(round.id)}`, {method: "DELETE"});
            if (active(version)) { activeRound = null; await refresh(); }
          }, "这组题目、回复和评分会被删除。"), "text-button"),
        ]),
        promptPanel(round.case),
        notice(round.status === "completed" ? "评分已完成。模型名称和回复由用户填写、来源未经验证；结果仅供本题参考，不会自动启用能力或调整分工。"
          : round.status === "reviewing" ? "评审时暂时隐藏模型名称。评分保存后不能修改，全部完成后显示名称；自行导入的评审者仍可能记得来源。"
          : "手动添加2至3份回复后开始评审，开始后不能再添加。评审时暂时隐藏模型名称；自行导入的评审者仍可能记得来源。"),
        text("h2", `回复 · ${(round.samples || []).length} 份`, "section-title"),
      ];
      const rerender = async () => { await refresh(); };
      if (!round.samples?.length) nodes.push(text("p", "还没有添加回复。", "quality-hint"));
      nodes.push(...(round.samples || []).map(sample => sampleCard(sample, round, rerender)));
      if (round.status === "collecting") {
        if ((round.samples || []).length < 3) nodes.push(sampleForm(round, rerender));
        const start = button("开始评审", async () => {
          const version = getVersion(); start.disabled = true;
          try { await api(`/api/quality/rounds/${encodeURIComponent(round.id)}/start`, {method: "POST", body: {}}); if (active(version)) await refresh(); }
          catch (error) { if (active(version)) { start.disabled = false; startError.textContent = error.message || "未能开始评审。"; startError.hidden = false; } }
        }, "button");
        start.disabled = (round.samples || []).length < 2;
        const startError = el("p", {class: "form-error", role: "alert", hidden: ""});
        nodes.push(el("div", {class: "quality-start"}, [start, text("span", "至少需要两份回复", "quality-hint"), startError]));
      }
      return nodes;
    }

    async function render() {
      if (activeRound) {
        const data = await api(`/api/quality/rounds/${encodeURIComponent(activeRound)}`);
        return detailNodes(data.round);
      }
      const [caseData, roundData] = await Promise.all([api("/api/quality/cases"), api("/api/quality/rounds")]);
      const cases = caseData.cases || [];
      const selector = el("select", {"aria-label": "选择评估题目"}, cases.map(item => el("option", {value: item.id, text: `${item.role} · ${item.title}`})));
      if (!cases.some(item => item.id === selectedCase)) selectedCase = cases[0]?.id;
      selector.value = selectedCase;
      const caseSlot = el("div");
      const showCase = () => { const selected = cases.find(item => item.id === selector.value); if (selected) caseSlot.replaceChildren(promptPanel(selected)); };
      selector.addEventListener("change", () => { selectedCase = selector.value; showCase(); }); showCase();
      const create = button("创建评估", async () => {
        const error = createError; error.hidden = true;
        try { await createRound(selector.value, create); }
        catch (failure) { error.textContent = failure.message || "未能创建评估。"; error.hidden = false; }
      }, "button primary");
      const createError = el("p", {class: "form-error", role: "alert", hidden: ""});
      const rounds = roundData.rounds || [];
      const nodes = [heading("质量评估", "用同一组虚构任务比较回复，记录人工判断。"),
        notice("这里只保存你手动提供的回复，不会连接模型或消耗额度。来源无法自动验证，评估不会自动启用模型。"),
        el("div", {class: "quality-select"}, [el("label", {}, [text("span", "选择评估题目"), selector]), create, createError]), caseSlot,
        text("h2", "我的评估", "section-title")];
      if (!rounds.length) nodes.push(text("p", "还没有评估记录。选择题目后即可创建。", "quality-hint"));
      else nodes.push(el("div", {class: "quality-history"}, rounds.map(item =>
        button(`${item.case_title} · ${statusNames[item.status] || "待处理"} · ${item.sample_count} 份回复`, async () => {
          activeRound = item.id; await refresh();
        }, "quality-history-item"))));
      return nodes;
    }
    return {render, reset() { activeRound = null; selectedCase = "arithmetic"; }};
  },
};

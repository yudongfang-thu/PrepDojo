/* PrepDojo 前端：无构建 SPA（CDN CodeMirror 失败自动降级 textarea） */
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));

// ---------- 编辑器（CodeMirror 优先，降级 textarea） ----------
let cm = null, fallbackTA = null;
function initEditor() {
  const holder = $("editor-holder");
  holder.innerHTML = "";
  if (typeof CodeMirror !== "undefined") {
    cm = CodeMirror(holder, {
      value: "", mode: "python", theme: "material-darker",
      lineNumbers: true, indentUnit: 4, tabSize: 4,
    });
    fallbackTA = null;
  } else {
    fallbackTA = document.createElement("textarea");
    fallbackTA.id = "fallback-editor";
    fallbackTA.spellcheck = false;
    holder.appendChild(fallbackTA);
    cm = null;
  }
}
function setEditorCode(code, lang) {
  if (cm) {
    cm.setValue(code);
    cm.setOption("mode", lang === "cpp" ? "text/x-c++src" : "python");
  } else if (fallbackTA) fallbackTA.value = code;
}
function getEditorCode() {
  return cm ? cm.getValue() : (fallbackTA ? fallbackTA.value : "");
}
const TEMPLATES = {
  python: "# 在此写代码，从标准输入读数据、向标准输出写结果\n",
  cpp: "#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    // 在此写代码\n    return 0;\n}\n",
};

// ---------- 导航 ----------
const pages = ["home", "coding", "quiz", "stats"];
function showPage(name) {
  pages.forEach(p => {
    const el = $("page-" + p);
    if (el) el.classList.toggle("hidden", p !== name);
    $("nav-" + p).classList.toggle("active", p === name);
  });
  if (name === "stats") loadStats();
  if (name === "home") loadHeroStats();
}
pages.forEach(p => $("nav-" + p).onclick = () => showPage(p));

async function loadHeroStats() {
  try {
    const [s, h] = await Promise.all([api("/api/stats"), api("/api/health")]);
    const ac = problems.filter(p => p.ever_ac).length;
    const items = [
      [s.cards, "八股题卡（来自你的资料）"],
      [s.learned_cards, "已学习"],
      [s.problems, "代码题"],
      [`${ac}`, "已攻克"],
      [h.llm_ready ? "已就绪" : "未配置", "AI（" + (h.model || "判题可用") + "）"],
    ];
    $("hero-stats").innerHTML = items.map(([v, k]) =>
      `<div class="hs"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("");
  } catch {}
}

// 大类卡片点击路由
document.querySelectorAll(".mode-card").forEach(card => {
  card.onclick = () => {
    const goto = card.dataset.goto;
    if (goto === "coding") showPage("coding");
    else if (goto === "quiz-learn") { showPage("quiz"); setQuizMode("learn"); }
  };
});

// ---------- LLM 徽标 ----------
async function refreshBadge() {
  try {
    const h = await api("/api/health");
    const b = $("llm-badge");
    if (h.llm_ready) { b.textContent = `● AI 已就绪（${h.model}）`; b.className = "on"; }
    else { b.textContent = "○ AI 未配置（判题可用）"; b.className = ""; }
  } catch {}
}

// ---------- 代码题 ----------
let problems = [], currentProblem = null, lastSubmission = null;

async function loadProblems() {
  const d = await api("/api/problems");
  problems = d.problems;
  const tb = $("problem-table").querySelector("tbody");
  tb.innerHTML = problems.map(p => {
    const st = p.ever_ac ? '<span title="已攻克">✅</span>'
      : (p.attempts > 0 ? `<span title="未通过（${p.attempts} 次提交）">❌</span>` : '<span class="muted" title="没做过">⬜</span>');
    return `
    <tr class="problem-row" data-id="${p.id}">
      <td>${st}</td>
      <td class="muted">${p.id}</td>
      <td>${esc(p.title)}</td>
      <td><span class="badge ${p.difficulty}">${{easy:"简单",medium:"中等",hard:"困难"}[p.difficulty]||p.difficulty}</span></td>
      <td>${p.tags.map(t => `<span class="badge tag">${esc(t)}</span>`).join("")}</td>
      <td class="muted">${p.n_cases}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll(".problem-row").forEach(tr => {
    tr.onclick = () => openProblem(tr.dataset.id);
  });
  const ac = problems.filter(p => p.ever_ac).length;
  const tried = problems.filter(p => p.attempts > 0).length;
  $("problem-stat-brief").textContent =
    `已攻克 ${ac}/${problems.length} · 做过 ${tried} · 错题 ${tried - ac}`;
}

$("wrong-drill-btn").onclick = async () => {
  const d = await api("/api/problems/wrong");
  if (!d.wrong.length) return alert("错题本是空的——提交过但未 AC 的题才会进错题本。");
  const pick = d.wrong[Math.floor(Math.random() * d.wrong.length)];
  openProblem(pick.id);
};

async function openProblem(pid) {
  currentProblem = await api("/api/problems/" + pid);
  $("problem-list-view").classList.add("hidden");
  $("problem-detail-view").classList.remove("hidden");
  $("pd-title").textContent = `${currentProblem.id} · ${currentProblem.title}`;
  $("pd-badges").innerHTML =
    `<span class="badge ${currentProblem.difficulty}">${{easy:"简单",medium:"中等",hard:"困难"}[currentProblem.difficulty]||currentProblem.difficulty}</span>` +
    currentProblem.tags.map(t => `<span class="badge tag">${esc(t)}</span>`).join("");
  let st = currentProblem.statement;
  if (currentProblem.samples && currentProblem.samples.length) {
    st += "\n\n【样例】\n" + currentProblem.samples.map(s =>
      `输入：\n${s.input}输出：\n${s.output}`).join("\n\n");
  }
  st += `\n\n（共 ${currentProblem.n_cases} 组测试用例；时限 ${currentProblem.time_limit_ms}ms）`;
  $("statement").textContent = st;
  const lang = $("lang-select").value;
  setEditorCode(TEMPLATES[lang], lang);
  $("result-area").innerHTML = "";
  lastSubmission = null;
  coachHistory = [];
  const cm = $("coach-messages");
  if (cm) cm.innerHTML = "";
}

$("back-btn").onclick = () => {
  $("problem-detail-view").classList.add("hidden");
  $("problem-list-view").classList.remove("hidden");
};
$("lang-select").onchange = () => {
  const lang = $("lang-select").value;
  if (!getEditorCode().trim() || getEditorCode() === TEMPLATES.python.trim() ||
      getEditorCode() === TEMPLATES.cpp.trim())
    setEditorCode(TEMPLATES[lang], lang);
};

$("submit-btn").onclick = async () => {
  const code = getEditorCode();
  if (!code.trim()) return alert("代码为空");
  const btn = $("submit-btn");
  btn.disabled = true; btn.textContent = "判题中…";
  $("result-area").innerHTML = '<p class="muted">沙箱运行中…</p>';
  try {
    const r = await api("/api/submit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ problem_id: currentProblem.id,
        language: $("lang-select").value, code }),
    });
    lastSubmission = r;
    renderResult(r);
  } catch (e) {
    $("result-area").innerHTML = `<p class="verdict-RE">提交失败：${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false; btn.textContent = "提交判题";
  }
};

function renderResult(r) {
  const v = `<span class="verdict-${r.verdict}">${r.verdict}</span>`;
  let html = `<p style="font-size:15px">判定：${v} · 最慢用例 ${r.max_time_ms}ms</p>`;
  if (r.compile_error) {
    html += `<p class="muted">编译错误：</p><pre>${esc(r.compile_error)}</pre>`;
  } else {
    html += "<table><thead><tr><th>用例</th><th>结果</th><th>耗时</th><th>详情</th></tr></thead><tbody>" +
      r.cases.map(c => {
        let det = "";
        if (c.verdict === "WA") det = `期望 ${esc(c.expected)} / 实际 ${esc(c.stdout)}`;
        else if (c.verdict === "RE") det = esc(c.stderr).slice(0, 200);
        return `<tr><td>#${c.idx}</td><td class="verdict-${c.verdict}">${c.verdict}</td><td>${c.time_ms}ms</td><td class="muted">${det}</td></tr>`;
      }).join("") + "</tbody></table>";
  }
  $("result-area").innerHTML = html;
}

// ---------- AI 判题（工具增强判定报告，SSE 流式） ----------
$("ai-judge-btn").onclick = async () => {
  if (!currentProblem) return;
  const code = getEditorCode();
  if (!code.trim()) return alert("编辑器代码为空");
  const btn = $("ai-judge-btn");
  btn.disabled = true; btn.textContent = "🤖 AI 判题中…";
  const area = $("result-area");
  area.insertAdjacentHTML("beforeend",
    '<div id="ai-judge-live" class="card" style="margin-top:14px"><b>🤖 AI 判题</b><div class="tool-log"></div></div>');
  const live = area.querySelector("#ai-judge-live .tool-log");
  try {
    const resp = await fetch(`/api/ai_judge/${currentProblem.id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, language: $("lang-select").value,
        last_submission_id: lastSubmission ? lastSubmission.submission_id : null }),
    });
    if (!resp.ok) {
      let msg = resp.statusText;
      try { msg = (await resp.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.event === "tool_start") {
          live.insertAdjacentHTML("beforeend",
            `<div class="coach-msg tool">⚙️ ${ev.name}(${(ev.args.code || ev.args.problem_id || "")
              .slice(0, 50).replace(/\n/g, "⏎")}…)</div>`);
        } else if (ev.event === "tool_done") {
          live.insertAdjacentHTML("beforeend",
            `<div class="coach-msg tool">✅ ${esc(ev.summary)}</div>`);
        } else if (ev.event === "report") {
          renderAiJudgeReport(live, ev.report);
        } else if (ev.event === "report_raw") {
          live.insertAdjacentHTML("beforeend",
            `<div class="coach-msg assistant">${esc(ev.text)}</div>`);
        } else if (ev.event === "error") {
          live.insertAdjacentHTML("beforeend",
            `<div class="coach-msg tool">❌ ${esc(ev.message)}</div>`);
        }
      }
    }
  } catch (e) {
    live.insertAdjacentHTML("beforeend", `<div class="coach-msg tool">❌ ${esc(e.message)}</div>`);
  } finally {
    btn.disabled = false; btn.textContent = "🤖 AI 判题";
  }
};

function renderAiJudgeReport(container, r) {
  const bs = r.better_solution || {};
  container.insertAdjacentHTML("beforeend", `
    <div class="card" style="margin-top:10px">
      <p style="font-size:16px"><b>判定：<span class="verdict-${esc(r.sandbox_verdict)}">${esc(r.sandbox_verdict)}</span></b>
      　<span class="muted">${esc(r.summary || "")}</span></p>
      <p>复杂度：时间 <b>${esc(r.complexity?.time || "未知")}</b> · 空间 <b>${esc(r.complexity?.space || "未知")}</b></p>
      <p>🔬 边界分析</p>
      <div class="per-point">${esc(r.boundary_analysis || "—")}</div>
      ${bs.exists ? `
      <p>🚀 更优解法：${esc(bs.name)}（${esc(bs.complexity)}）</p>
      <div class="per-point hit"><b>为什么更优：</b>${esc(bs.why_better)}<br><b>思路提示：</b>${esc(bs.hint)}</div>` : ""}
      ${r.related_knowledge && r.related_knowledge.length ? `
      <p>📚 知识点（更优解法背后）</p>
      ${r.related_knowledge.map(x=>`<div class="per-point">${esc(x)}</div>`).join("")}` : ""}
      ${r.interview_tips && r.interview_tips.length ? `
      <p>🎤 面试官视角</p>
      ${r.interview_tips.map(x=>`<div class="per-point">${esc(x)}</div>`).join("")}` : ""}
    </div>`);
}

// ---------- AI 讲题教练（SSE 流式 + 沙箱工具轨迹） ----------
let coachHistory = [];

function coachRender(role, text, cls) {
  const div = document.createElement("div");
  div.className = `coach-msg ${cls}`;
  div.textContent = text;
  $("coach-messages").appendChild(div);
  $("coach-messages").scrollTop = 1e9;
  return div;
}

$("ask-coach-btn").onclick = () => {
  const p = $("coach-panel");
  p.classList.toggle("hidden");
  if (!p.classList.contains("hidden")) $("coach-input").focus();
};

async function coachSend() {
  const input = $("coach-input");
  const text = input.value.trim();
  if (!text || !currentProblem) return;
  input.value = "";
  coachRender("user", text, "user");
  coachHistory.push({ role: "user", content: text });
  const btn = $("coach-send-btn");
  btn.disabled = true;
  let assistantDiv = coachRender("assistant", "思考中…", "assistant");
  let reply = "";
  try {
    const resp = await fetch(`/api/chat/problem/${currentProblem.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: coachHistory.slice(0, -1),
        code: getEditorCode(),
        language: $("lang-select").value,
        last_submission_id: lastSubmission ? lastSubmission.submission_id : null,
      }),
    });
    if (!resp.ok) {
      let msg = resp.statusText;
      try { msg = (await resp.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.event === "tool_start") {
          coachRender("tool", `⚙️ ${ev.name}(${(ev.args.code || ev.args.problem_id || "")
            .slice(0, 40).replace(/\n/g, "⏎")}…)`, "tool");
          assistantDiv = coachRender("assistant", "", "assistant");
          reply = "";
        } else if (ev.event === "tool_done") {
          coachRender("tool", `✅ ${ev.summary}`, "tool");
        } else if (ev.event === "reply") {
          reply = ev.text;
          assistantDiv.textContent = reply;
        } else if (ev.event === "error") {
          assistantDiv.textContent = "出错：" + ev.message;
        }
        $("coach-messages").scrollTop = 1e9;
      }
    }
    if (reply) coachHistory.push({ role: "assistant", content: reply });
  } catch (e) {
    assistantDiv.textContent = "请求失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}
$("coach-send-btn").onclick = coachSend;
$("coach-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.isComposing) coachSend();
});

// ---------- 八股：模式切换 ----------
function setQuizMode(mode) {
  const isLearn = mode === "learn";
  $("quiz-mode-learn").className = "btn" + (isLearn ? " primary" : "");
  $("quiz-mode-test").className = "btn" + (!isLearn ? " primary" : "");
  $("quiz-learn-view").classList.toggle("hidden", !isLearn);
  $("quiz-test-view").classList.toggle("hidden", isLearn);
  if (isLearn) refreshLearnProgress();
}
$("quiz-mode-learn").onclick = () => setQuizMode("learn");
$("quiz-mode-test").onclick = () => setQuizMode("test");

async function refreshLearnProgress() {
  try {
    const p = await api("/api/cards/progress");
    const pct = p.total ? Math.round(p.learned / p.total * 100) : 0;
    $("learn-progress").textContent = p.total
      ? `已学 ${p.learned}/${p.total}（${pct}%）` : "";
  } catch {}
}

// ---------- 八股：学习模式 ----------
let learnQueue = [], learnIdx = 0;

async function loadLearnTags() {
  try {
    const d = await api("/api/tags");
    d.tags.forEach(([t, n]) => {
      const o = document.createElement("option");
      o.value = t; o.textContent = `${t} (${n})`;
      $("learn-tag-select").appendChild(o);
    });
  } catch {}
}

$("learn-start-btn").onclick = async () => {
  const tags = $("learn-tag-select").value;
  const n = parseInt($("learn-num").value, 10);
  const d = await api(`/api/cards/learn?tags=${encodeURIComponent(tags)}&n=${n}`);
  if (!d.cards.length) {
    return alert("没有可学的卡：全部学完了（或题库为空，先 ingest）。");
  }
  learnQueue = d.cards; learnIdx = 0;
  $("learn-session").classList.remove("hidden");
  showLearnCard();
};

function showLearnCard() {
  const c = learnQueue[learnIdx];
  $("learn-progress-cnt").textContent = `第 ${learnIdx + 1} / ${learnQueue.length} 卡`;
  $("learn-question").innerHTML =
    `${c.topic_tags.map(t => `<span class="badge tag">${esc(t)}</span>`).join("")}` +
    (c.learned ? ` <span class="badge easy">已学</span>` : "") +
    `<br><br>${esc(c.question)}`;
  $("learn-answer").classList.add("hidden");
  $("learn-show-btn").classList.remove("hidden");
  $("learn-done-btn").classList.add("hidden");
  $("learn-later-btn").classList.add("hidden");
  $("learn-explain-area").innerHTML = "";
}

$("learn-show-btn").onclick = () => {
  const c = learnQueue[learnIdx];
  $("learn-answer").classList.remove("hidden");
  $("learn-show-btn").classList.add("hidden");
  $("learn-done-btn").classList.remove("hidden");
  $("learn-later-btn").classList.remove("hidden");
  $("learn-points").innerHTML = c.answer_points.map(p => `<div class="per-point">${esc(p)}</div>`).join("");
};

$("learn-explain-btn").onclick = async () => {
  const c = learnQueue[learnIdx];
  const btn = $("learn-explain-btn");
  btn.disabled = true; btn.textContent = "🧠 讲解生成中…";
  try {
    const r = await api(`/api/cards/${c.id}/explain`);
    const e = r.explanation;
    $("learn-explain-area").innerHTML = `
      <div class="card" style="margin-top:12px">
        <p><b>核心：</b>${esc(e.core)}</p>
        <p style="white-space:pre-wrap">${esc(e.expanded)}</p>
        ${e.analogy ? `<p>🔗 <b>类比：</b>${esc(e.analogy)}</p>` : ""}
        ${e.mnemonic ? `<p>📌 <b>记忆锚点：</b>${esc(e.mnemonic)}</p>` : ""}
        ${e.related && e.related.length ? `<p class="muted">相关：${e.related.map(esc).join(" · ")}</p>` : ""}
        ${r.cached ? '<p class="muted" style="font-size:11px">（缓存）</p>' : ""}
      </div>`;
  } catch (err) {
    alert("讲解失败：" + err.message);
  } finally {
    btn.disabled = false; btn.textContent = "🧠 AI 讲解";
  }
};

$("learn-done-btn").onclick = async () => {
  const c = learnQueue[learnIdx];
  try {
    await api(`/api/cards/${c.id}/learn`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ learned: true }),
    });
    c.learned = true;
    refreshLearnProgress();
  } catch (e) { alert("标记失败：" + e.message); }
  learnNext();
};

$("learn-later-btn").onclick = () => learnNext();
$("learn-next-btn").onclick = () => learnNext();
$("learn-prev-btn").onclick = () => {
  if (learnIdx > 0) { learnIdx -= 1; showLearnCard(); }
};
function learnNext() {
  learnIdx += 1;
  if (learnIdx >= learnQueue.length) {
    $("learn-session").classList.add("hidden");
    refreshLearnProgress();
    alert("本批学习完成！");
    return;
  }
  showLearnCard();
}
$("learn-exit-btn").onclick = () => {
  $("learn-session").classList.add("hidden");
  refreshLearnProgress();
};

// ---------- 八股：测验模式 ----------
let quizQueue = [], quizIdx = 0, quizLastAnswer = "";

async function loadTags() {
  try {
    const d = await api("/api/tags");
    const sel = $("quiz-tag-select");
    d.tags.forEach(([t, n]) => {
      const o = document.createElement("option");
      o.value = t; o.textContent = `${t} (${n})`;
      sel.appendChild(o);
    });
  } catch {}
}

$("quiz-start-btn").onclick = async () => {
  const tags = $("quiz-tag-select").value;
  const n = parseInt($("quiz-num").value, 10);
  const onlyLearned = $("quiz-only-learned").checked ? 1 : 0;
  const d = await api(`/api/cards/next?tags=${encodeURIComponent(tags)}&n=${n}&only_learned=${onlyLearned}`);
  if (!d.cards.length) {
    return alert("题库为空：请先用 `prepdojo ingest <知识目录>` 接入你的八股资料");
  }
  quizQueue = d.cards; quizIdx = 0;
  $("quiz-test-view").querySelector("#quiz-session").classList.remove("hidden");
  if (d.fallback) {
    alert("已学的卡暂时抽不出题，本次从全部卡里抽（学都没学的题分数低是正常的）。");
  }
  showQuizCard();
};

function showQuizCard() {
  const c = quizQueue[quizIdx];
  $("quiz-progress").textContent = `第 ${quizIdx + 1} / ${quizQueue.length} 题`;
  $("quiz-question").innerHTML =
    `${c.topic_tags.map(t => `<span class="badge tag">${esc(t)}</span>`).join("")}` +
    `<span class="muted" style="font-size:12px"> 难度${c.difficulty}</span><br><br>${esc(c.question)}`;
  $("quiz-answer").value = "";
  $("quiz-feedback").classList.add("hidden");
  $("quiz-grade-btn").disabled = false;
  $("quiz-next-btn").classList.add("hidden");
}

$("quiz-grade-btn").onclick = async () => {
  const ans = $("quiz-answer").value.trim();
  if (!ans) return alert("先作答再提交");
  const c = quizQueue[quizIdx];
  quizLastAnswer = ans;
  const btn = $("quiz-grade-btn");
  btn.disabled = true; btn.textContent = "AI 评分中…";
  try {
    const r = await api("/api/quiz/grade", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_id: c.id, answer: ans, style: $("quiz-style").value }),
    });
    renderQuizFeedback(r, c);
  } catch (e) {
    alert("评分失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "提交评分";
  }
};

function renderQuizFeedback(r, c) {
  const fb = $("quiz-feedback");
  fb.classList.remove("hidden");
  let html = `
    <div class="card" style="margin-top:14px">
      <div class="score-big">${r.score}<span class="muted" style="font-size:16px"> / 10</span></div>
      <p>${esc(r.overall || "")}</p>
      ${r.per_point ? r.per_point.map(p => `
        <div class="per-point ${p.covered ? "hit" : "miss"}">${p.covered ? "✅" : "❌"} ${esc(p.point)}
          <div class="muted">${esc(p.comment || "")}</div></div>`).join("") : ""}
      ${r.missed && r.missed.length ? `<p>📌 遗漏要点</p>` + r.missed.map(x=>`<div class="per-point miss">${esc(x)}</div>`).join("") : ""}
      ${r.extra_good && r.extra_good.length ? `<p>🌟 加分项</p>` + r.extra_good.map(x=>`<div class="per-point hit">${esc(x)}</div>`).join("") : ""}
      <p class="muted" style="margin-top:10px">参考要点（来自你的知识库）：</p>
      ${r.reference.map(x => `<div class="per-point">${esc(x)}</div>`).join("")}
    </div>`;
  if (r.follow_up) {
    html += `
    <div class="followup-box"><b>💬 追问：</b>${esc(r.follow_up)}</div>
    <textarea class="answer" id="followup-answer" placeholder="回答追问…"></textarea>
    <div class="toolbar"><button class="btn primary" id="followup-grade-btn">提交追问回答</button></div>
    <div id="followup-result"></div>`;
  }
  html += `<div class="toolbar"><button class="btn primary" id="quiz-next-inline">下一题</button></div>`;
  fb.innerHTML = html;
  const fBtn = $("followup-grade-btn");
  if (fBtn) fBtn.onclick = async () => {
    const fa = $("followup-answer").value.trim();
    if (!fa) return alert("先回答追问");
    fBtn.disabled = true; fBtn.textContent = "评分中…";
    try {
      const rr = await api("/api/quiz/followup", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card_id: c.id, question: r.follow_up, answer: fa,
          context_answer: quizLastAnswer, style: $("quiz-style").value }),
      });
      $("followup-result").innerHTML = `
        <div class="card"><b class="score-big" style="font-size:24px">${rr.score}/10</b>
        <p>${esc(rr.overall || "")}</p>
        <p class="muted">追问参考答案：</p><div class="per-point">${esc(rr.reference_answer || "")}</div></div>`;
    } catch (e) { alert("追问评分失败：" + e.message); }
    finally { fBtn.disabled = false; fBtn.textContent = "提交追问回答"; }
  };
  const next = $("quiz-next-inline");
  next.onclick = quizNext;
  $("quiz-next-btn").classList.remove("hidden");
  $("quiz-grade-btn").disabled = true;
}

function quizNext() {
  quizIdx += 1;
  if (quizIdx >= quizQueue.length) {
    $("quiz-session").classList.add("hidden");
    alert("本轮练习完成！");
    return;
  }
  showQuizCard();
}
$("quiz-next-btn").onclick = quizNext;
$("quiz-skip-btn").onclick = quizNext;
$("quiz-exit-btn").onclick = () => { $("quiz-session").classList.add("hidden"); };

// ---------- 概览 ----------
async function loadStats() {
  const s = await api("/api/stats");
  const ac = problems.filter(p => p.ever_ac).length;
  const items = [
    ["八股题卡", s.cards], ["已学习", s.learned_cards ?? "—"], ["代码题", s.problems],
    ["代码已攻克", `${ac}/${problems.length}`], ["提交次数", s.submissions], ["AC 次数", s.ac],
    ["八股练习", s.quiz_attempts], ["八股均分", s.quiz_avg_score ?? "—"],
  ];
  $("stats-grid").innerHTML = items.map(([k, v]) =>
    `<div class="card"><div class="num">${v}</div><div class="muted">${k}</div></div>`).join("");
}

// ---------- 启动 ----------
initEditor();
setEditorCode(TEMPLATES.python, "python");
refreshBadge();
loadProblems().then(loadHeroStats);
loadTags();
loadLearnTags();
setQuizMode("learn");
refreshLearnProgress();

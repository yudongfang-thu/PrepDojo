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
const pages = ["coding", "quiz", "stats"];
function showPage(name) {
  pages.forEach(p => {
    $("page-" + p).classList.toggle("hidden", p !== name);
    $("nav-" + p).classList.toggle("active", p === name);
  });
  if (name === "stats") loadStats();
}
pages.forEach(p => $("nav-" + p).onclick = () => showPage(p));

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
  tb.innerHTML = problems.map(p => `
    <tr class="problem-row" data-id="${p.id}">
      <td class="muted">${p.id}</td>
      <td>${esc(p.title)}</td>
      <td><span class="badge ${p.difficulty}">${{easy:"简单",medium:"中等",hard:"困难"}[p.difficulty]||p.difficulty}</span></td>
      <td>${p.tags.map(t => `<span class="badge tag">${esc(t)}</span>`).join("")}</td>
      <td class="muted">${p.n_cases}</td>
    </tr>`).join("");
  tb.querySelectorAll(".problem-row").forEach(tr => {
    tr.onclick = () => openProblem(tr.dataset.id);
  });
}

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
  $("review-btn").disabled = true;
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
    $("review-btn").disabled = false;
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

$("review-btn").onclick = async () => {
  if (!lastSubmission) return;
  const btn = $("review-btn");
  btn.disabled = true; btn.textContent = "AI 点评中…";
  try {
    const r = await api(`/api/review/${lastSubmission.submission_id}`, { method: "POST" });
    const rv = r.review;
    $("result-area").insertAdjacentHTML("beforeend", `
      <h2 class="sec" style="margin-top:16px">AI 点评</h2>
      <div class="card"><p><b>${esc(rv.summary || "")}</b></p>
      <p>复杂度：时间 ${esc(rv.complexity?.time || "未知")} · 空间 ${esc(rv.complexity?.space || "未知")}</p>
      ${rv.good_points ? "<p>✅ 做得好</p>" + rv.good_points.map(x=>`<div class="per-point hit">${esc(x)}</div>`).join("") : ""}
      ${rv.issues ? "<p>⚠️ 问题</p>" + rv.issues.map(x=>`<div class="per-point miss">${esc(x)}</div>`).join("") : ""}
      ${rv.interview_tips ? "<p>🎤 面试官可能追问</p>" + rv.interview_tips.map(x=>`<div class="per-point">${esc(x)}</div>`).join("") : ""}
      ${rv.improved_hint && rv.improved_hint !== "无" ? `<p class="muted">改进方向：${esc(rv.improved_hint)}</p>` : ""}
      </div>`);
  } catch (e) {
    alert("点评失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "AI 点评";
  }
};

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

// ---------- 八股 ----------
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
  const d = await api(`/api/cards/next?tags=${encodeURIComponent(tags)}&n=${n}`);
  if (!d.cards.length) {
    return alert("题库为空：请先用 `prepdojo ingest <知识目录>` 接入你的八股资料");
  }
  quizQueue = d.cards; quizIdx = 0;
  $("quiz-start").classList.add("hidden");
  $("quiz-session").classList.remove("hidden");
  $("quiz-feedback").classList.add("hidden");
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
    $("quiz-start").classList.remove("hidden");
    alert("本轮练习完成！");
    return;
  }
  showQuizCard();
}
$("quiz-next-btn").onclick = quizNext;
$("quiz-skip-btn").onclick = quizNext;
$("quiz-exit-btn").onclick = () => {
  $("quiz-session").classList.add("hidden");
  $("quiz-start").classList.remove("hidden");
};

// ---------- 概览 ----------
async function loadStats() {
  const s = await api("/api/stats");
  const items = [
    ["八股题卡", s.cards], ["知识来源文件", s.sources], ["代码题", s.problems],
    ["提交次数", s.submissions], ["AC 次数", s.ac],
    ["八股练习", s.quiz_attempts], ["八股均分", s.quiz_avg_score ?? "—"],
  ];
  $("stats-grid").innerHTML = items.map(([k, v]) =>
    `<div class="card"><div class="num">${v}</div><div class="muted">${k}</div></div>`).join("");
}

// ---------- 启动 ----------
initEditor();
setEditorCode(TEMPLATES.python, "python");
refreshBadge();
loadProblems();
loadTags();

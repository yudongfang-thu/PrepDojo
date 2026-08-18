/* PrepDojo 前端：无构建 SPA（本地 CodeMirror，失败自动降级 textarea） */
"use strict";

const $ = (id) => document.getElementById(id);

// ---------- 会话（多用户模式；单机模式 /api/me 恒返回 local） ----------
let currentUser = null;

function showLogin(msg) {
  ["home", "coding", "quiz", "kb", "settings", "stats"].forEach(p =>
    $("page-" + p).classList.add("hidden"));
  document.querySelector("header nav").style.display = "none"; // 未登录不显示菜单
  $("page-login").classList.remove("hidden");
  $("login-error").textContent = msg || "";
  $("login-username").focus();
}

function applyUserUI() {
  document.querySelector("header nav").style.display = "flex"; // 登录后恢复菜单
  const u = currentUser;
  if (!u || !u.multiuser) return; // 单机模式：不显示用户相关 UI
  $("user-badge").textContent = u.username + (u.is_admin ? " · 管理员" : "");
  $("logout-btn").classList.remove("hidden");
  if (!u.is_admin) $("nav-kb").classList.add("hidden"); // 知识库管理仅管理员
}

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (r.status === 401 && currentUser !== null) { showLogin("登录已过期，请重新登录"); }
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
// ---------- 代码补全：字典（关键字/内建）+ 文档词 ----------
const PY_WORDS = ("False None True and as assert async await break class continue def del elif else except "
  + "finally for from global if import in is lambda nonlocal not or pass raise return try while with yield "
  + "print len range enumerate map filter zip sorted sum min max abs reversed input int str float list dict set "
  + "tuple bool isinstance type any all divmod round pow ord chr hex bin format join split strip replace startswith "
  + "endswith append extend pop insert remove sort reverse keys values items get setdefault defaultdict deque heapify "
  + "heappush heappop bisect_left bisect_right Counter reduce inf").split(" ");
const CPP_WORDS = ("alignof auto bool break case catch char class const constexpr continue decltype default delete do "
  + "double else enum explicit extern false float for friend goto if inline int long mutable namespace new noexcept "
  + "nullptr operator private protected public return short signed sizeof static struct switch template this throw "
  + "true try typedef typename union unsigned using virtual void volatile while "
  + "include bits std vector string map set unordered_map unordered_set pair make_pair queue stack priority_queue deque "
  + "array list sort stable_sort reverse push_back pop_back emplace_back begin end front back size resize empty lower_bound "
  + "upper_bound binary_search min max abs swap cout cin endl ios sync_with_stdio tie accumulate iota INT_MAX INT_MIN "
  + "unique_ptr shared_ptr move forward iterator push make_heap pop_heap next_permutation gcd lcm memset substr find").split(" ");

function mergedHint(cmr) {
  const cur = cmr.getCursor();
  const line = cmr.getLine(cur.line);
  const m = /[\w]+$/.exec(line.slice(0, cur.ch));
  if (!m || m[0].length < 2) return null;
  const from = { line: cur.line, ch: cur.ch - m[0].length };
  const prefix = m[0];
  const dict = $("lang-select").value === "cpp" ? CPP_WORDS : PY_WORDS;
  const seen = new Set();
  const list = [];
  for (const w of dict) {
    if (w.startsWith(prefix) && !seen.has(w)) {
      seen.add(w);
      list.push({ text: w, displayText: w, className: "hint-kw" });
    }
  }
  try {
    const any = CodeMirror.hint.anyword(cmr);
    if (any && any.list) {
      for (const w of any.list) {
        if (!seen.has(w) && String(w).startsWith(prefix)) {
          seen.add(w);
          list.push({ text: w, displayText: String(w) });
        }
      }
    }
  } catch {}
  if (!list.length) return null;
  return { list, from, to: cur };
}

let _hintTimer = null;
function initEditor() {
  const holder = $("editor-holder");
  holder.innerHTML = "";
  if (typeof CodeMirror !== "undefined") {
    cm = CodeMirror(holder, {
      value: "", mode: "python", theme: "material-darker",
      lineNumbers: true, indentUnit: 4, tabSize: 4,
      extraKeys: {
        // Tab：多行选中→整块缩进；行首→缩进；代码中间→插入 4 空格
        "Tab": cmr => {
          const ca = cmr.state.completionActive;
          if (ca && ca.data && ca.data.list && ca.data.list.length) { ca.pick(); return; }
          if (cmr.somethingSelected() && cmr.getSelection().includes("\n")) {
            cmr.indentSelection("add");
          } else if (!cmr.somethingSelected() &&
                     /^\s*$/.test(cmr.getLine(cmr.getCursor().line).slice(0, cmr.getCursor().ch))) {
            cmr.execCommand("indentMore");
          } else {
            cmr.replaceSelection("    ");
          }
        },
        "Shift-Tab": cmr => cmr.indentSelection("subtract"),
        // Cmd/Ctrl+Enter：提交判题
        "Cmd-Enter": () => $("submit-btn").click(),
        "Ctrl-Enter": () => $("submit-btn").click(),
        "Ctrl-Space": cmr => cmr.showHint({ hint: mergedHint, completeSingle: false }),
      },
    });
    // 输入标识符 ≥2 字符后自动弹出补全（180ms 防抖）
    cm.on("inputRead", (_, ch) => {
      if (!/[A-Za-z_]/.test(ch.text && ch.text[0] || "")) return;
      clearTimeout(_hintTimer);
      _hintTimer = setTimeout(() => {
        if (!cm.state.completionActive) cm.showHint({ hint: mergedHint, completeSingle: false });
      }, 180);
    });
    cm.on("keydown", (_, e) => { if (e.key === "Tab") e.preventDefault(); });
    fallbackTA = null;
  } else {
    fallbackTA = document.createElement("textarea");
    fallbackTA.id = "fallback-editor";
    fallbackTA.spellcheck = false;
    // 降级 textarea：Tab 插入 4 空格 / Shift+Tab 反缩进，不让焦点跳出
    fallbackTA.addEventListener("keydown", e => {
      if (e.key === "Tab") {
        e.preventDefault();
        const ta = e.target, s = ta.selectionStart, en = ta.selectionEnd;
        if (e.shiftKey) {
          const lineStart = ta.value.lastIndexOf("\n", s - 1) + 1;
          const m = /^ {1,4}/.exec(ta.value.slice(lineStart));
          if (m && s === en && s >= lineStart && s <= lineStart + m[0].length) {
            ta.value = ta.value.slice(0, lineStart) + ta.value.slice(lineStart + m[0].length);
            ta.selectionStart = ta.selectionEnd = s - m[0].length;
          }
        } else if (s !== en && ta.value.slice(s, en).includes("\n")) {
          const ls = ta.value.lastIndexOf("\n", s - 1) + 1;
          const block = ta.value.slice(ls, en);
          ta.value = ta.value.slice(0, ls) + block.replace(/^/gm, "    ") + ta.value.slice(en);
          ta.selectionStart = ls; ta.selectionEnd = en + 4 * (block.split("\n").length);
        } else {
          ta.setRangeText("    ", s, en, "end");
        }
      }
    });
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
const pages = ["home", "coding", "quiz", "kb", "settings", "stats"];
function showPage(name) {
  // 多用户模式未登录：一律回登录页（登录后才见菜单内容）
  if (currentUser === undefined) { showLogin(); return; }
  pages.forEach(p => {
    const el = $("page-" + p);
    if (el) el.classList.toggle("hidden", p !== name);
    $("nav-" + p).classList.toggle("active", p === name);
  });
  if (name === "stats") loadStats();
  if (name === "home") loadHeroStats();
  if (name === "kb") loadSources();
  if (name === "settings") loadSettings();
  if (name === "coding" && cm) {
    // 编辑器在 hidden 容器中初始化会导致行号槽度量错误（行号与代码重叠），可见后必须 refresh
    setTimeout(() => cm.refresh(), 0);
  }
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
      <td>${(p.id.startsWith("cpg-") && currentUser && currentUser.is_admin) ? `<button class="btn" style="padding:2px 8px;font-size:11px" onclick="delProblem('${p.id}')">删除</button>` : ""}</td>
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
  if (cm) setTimeout(() => cm.refresh(), 0);
  $("problem-list-view").classList.add("hidden");
  $("problem-detail-view").classList.remove("hidden");
  $("pd-title").textContent = `${currentProblem.id} · ${currentProblem.title}`;
  $("pd-badges").innerHTML =
    `<span class="badge ${currentProblem.difficulty}">${{easy:"简单",medium:"中等",hard:"困难"}[currentProblem.difficulty]||currentProblem.difficulty}</span>` +
    currentProblem.tags.map(t => `<span class="badge tag">${esc(t)}</span>`).join("");
  let st = currentProblem.statement;
  if (currentProblem.samples && currentProblem.samples.length) {
    st += "\n\n【样例】\n" +
      currentProblem.samples.map(s =>
        `输入：\n${s.input}\n输出：\n${s.output}`).join("\n\n");
  }
  st += `\n\n（共 ${currentProblem.n_cases} 组测试用例；时限 ${currentProblem.time_limit_ms}ms）`;
  $("statement").textContent = st;
  const lang = $("lang-select").value;
  // 草稿优先 → 无草稿时从提交记录不限语言恢复 → 都没有才用模板
  const draft = localStorage.getItem(draftKey(currentProblem.id, lang));
  if (draft && draft.trim()) {
    setEditorCode(draft, lang);
  } else {
    setEditorCode(TEMPLATES[lang], lang); // 先设模板，再异步恢复
    api("/api/submissions/last/" + currentProblem.id)  // 不限语言，取最近一次提交
      .then(r => {
        if (r && r.code && r.code.trim()) {
          // 如果上次提交的语言和当前不同，自动切换下拉
          if (r.language && r.language !== lang) {
            $("lang-select").value = r.language;
          }
          setEditorCode(r.code, r.language || lang);
        }
      })
      .catch(() => {});
  }
  saveDraft();
  $("result-area").innerHTML = "";
  lastSubmission = null;
  coachHistory = [];
  const coachMsgs = $("coach-messages");
  if (coachMsgs) coachMsgs.innerHTML = "";
}

$("back-btn").onclick = () => {
  $("problem-detail-view").classList.add("hidden");
  $("problem-list-view").classList.remove("hidden");
};
// 草稿键：单机模式保持旧格式（老用户草稿不丢）；多用户按人隔离
function draftKey(pid, lang) {
  const u = currentUser && currentUser.username && currentUser.username !== "local"
    ? currentUser.username + "-" : "";
  return "prepdojo-draft-" + u + pid + "-" + lang;
}
function saveDraft() {
  if (!currentProblem) return;
  const key = draftKey(currentProblem.id, $("lang-select").value);
  const code = getEditorCode();
  // 空或未改动的模板不存，避免草稿盖住未来的模板切换
  if (!code.trim() || code.trim() === TEMPLATES.python.trim() || code.trim() === TEMPLATES.cpp.trim())
    localStorage.removeItem(key);
  else localStorage.setItem(key, code);
}
$("lang-select").onchange = () => {
  const lang = $("lang-select").value;
  const cur = getEditorCode().trim();
  if (!cur || cur === TEMPLATES.python.trim() || cur === TEMPLATES.cpp.trim()) {
    // 模板未修改：尝试从提交记录恢复该语言的代码
    setEditorCode(TEMPLATES[lang], lang);
    if (currentProblem) {
      api("/api/submissions/last/" + currentProblem.id + "?language=" + lang)
        .then(r => { if (r && r.code && r.code.trim()) setEditorCode(r.code, lang); })
        .catch(() => {});
    }
  } else if (currentProblem) {
    // 已有代码：切换语言时尝试恢复该语言的草稿
    const draft = localStorage.getItem(draftKey(currentProblem.id, lang));
    if (draft && draft.trim()) setEditorCode(draft, lang);
  }
};
// 输入防抖存草稿
let _draftTimer = null;
setInterval(() => { if (_draftTimer === null) _draftTimer = setTimeout(() => { saveDraft(); _draftTimer = null; }, 1500); }, 2000);

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
  // 非 AC 时在教练栏弹修复入口
  if (r.verdict !== "AC" && currentProblem) {
    setTimeout(() => offerFixInCoach(r), 100);
  }
}

function offerFixInCoach(r) {
  const div = document.createElement("div");
  div.className = "coach-msg tool";
  div.innerHTML = `判题结果：<b class="verdict-${r.verdict}">${r.verdict}</b>　
    <button class="btn" style="padding:4px 12px;font-size:13px" id="coach-fix-btn">🔧 AI 修复</button>`;
  $("coach-messages").appendChild(div);
  $("coach-messages").scrollTop = 1e9;
  $("coach-fix-btn").onclick = () => doCoachFix(r);
}

async function doCoachFix(r) {
  if (!currentProblem) return;
  const btn = $("coach-fix-btn");
  if (btn) { btn.disabled = true; btn.textContent = "修复中…"; }
  let reply = "", assistantDiv = coachRender("assistant", "思考中…", "assistant");
  let thinkBox = null;
  try {
    const resp = await fetch("/api/fix/" + currentProblem.id, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: getEditorCode(), language: $("lang-select").value,
        verdict: r.verdict, detail: (r.compile_error || "").slice(0, 500) }),
    });
    if (!resp.ok) {
      let msg = resp.statusText;
      try { msg = (await resp.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.event === "thinking_delta" || ev.event === "reasoning_delta") {
          if (!thinkBox) thinkBox = makeThinkingBox(assistantDiv.parentNode || $("coach-messages"));
          appendThinking(thinkBox, ev.text);
        } else if (ev.event === "content_delta") {
          reply += ev.text;
          assistantDiv.textContent = reply;
        } else if (ev.event === "reply") {
          reply = ev.code || ev.text || "";
        } else if (ev.event === "error") {
          assistantDiv.textContent = "修复失败：" + ev.message;
        }
      }
    }
    if (thinkBox) thinkBox.open = false;
    if (reply) {
      const codeMatch = reply.match(/```(?:\w+)?\s*\n([\s\S]*?)```/);
      const fixedCode = codeMatch ? codeMatch[1].trim() : reply;
      const description = reply.replace(/```[\s\S]*?```/g, "").trim();
      assistantDiv.innerHTML = `<div class="coach-msg assistant" style="white-space:pre-wrap">${esc(description).slice(0, 400)}</div>
        <pre style="background:var(--panel2);border-radius:8px;padding:10px;overflow:auto;max-height:320px;font-size:12.5px;line-height:1.6">${esc(fixedCode)}</pre>
        <button class="btn primary" style="margin-top:8px" id="coach-apply-fix">✅ 应用修复</button>`;
      $("coach-apply-fix").onclick = () => {
        setEditorCode(fixedCode, $("lang-select").value);
        coachRender("tool", "✅ 代码已应用到编辑器，改完再提交试试", "tool");
      };
    }
  } catch (e) {
    assistantDiv.textContent = "修复失败：" + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔧 AI 修复"; }
  }
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
        } else if (ev.event === "thinking_delta") {
          if (!live._think) live._think = makeThinkingBox(live);
          appendThinking(live._think, ev.text);
        } else if (ev.event === "content_delta") {
          // 最终输出是结构化 JSON 报告：不逐 token 展示原文，等 report 事件统一渲染
          if (!live._reporting) {
            live._reporting = true;
            live.insertAdjacentHTML("beforeend",
              '<div class="coach-msg tool">📝 正在汇总判定报告…</div>');
          }
        } else if (ev.event === "report") {
          if (live._think) live._think.open = false;
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

// ---------- AI 讲题教练（SSE 流式 + 沙箱工具轨迹 + thinking 流） ----------
let coachHistory = [];

function coachRender(role, text, cls) {
  const div = document.createElement("div");
  div.className = `coach-msg ${cls}`;
  div.textContent = text;
  $("coach-messages").appendChild(div);
  $("coach-messages").scrollTop = 1e9;
  return div;
}

function makeThinkingBox(parent) {
  const d = document.createElement("details");
  d.className = "thinking";
  d.open = true; // 导入/判题进行中默认展开，看得到 AI 在干活
  d.innerHTML = '<summary>🧠 AI thinking…</summary><div class="th-content"></div>';
  parent.appendChild(d);
  return d;
}
function appendThinking(box, text) {
  const tc = box.querySelector(".th-content");
  tc.textContent += text;
  tc.scrollTop = tc.scrollHeight;
}

// 教练面板已常驻在右侧列

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
        } else if (ev.event === "thinking_delta") {
          if (!assistantDiv._think) {
            const tb = makeThinkingBox(assistantDiv.parentNode || $("coach-messages"));
            assistantDiv._think = tb;
          }
          appendThinking(assistantDiv._think, ev.text);
        } else if (ev.event === "content_delta") {
          reply += ev.text;
          assistantDiv.textContent = reply;
        } else if (ev.event === "reply") {
          reply = ev.text;
          assistantDiv.textContent = reply;
          if (assistantDiv._think) assistantDiv._think.open = false;
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

// ---------- 标签云（学习/测验共用） ----------
const allTagData = { list: [] };
const tagSel = { learn: new Set(), quiz: new Set() };

function renderTagCloud(cloudId, which) {
  const el = $(cloudId);
  const sel = tagSel[which];
  const TOP_N = 24;
  const showAll = !!el._showAll;
  const list = showAll ? allTagData.list : allTagData.list.slice(0, TOP_N);
  el.innerHTML = list.map(([t, n]) =>
    `<button class="tag-chip ${sel.has(t) ? "on" : ""}" data-tag="${esc(t)}">${esc(t)}<i>${n}</i></button>`).join("")
    + (allTagData.list.length > TOP_N
      ? `<button class="tag-chip more">${showAll ? "收起 ▴" : `全部 ${allTagData.list.length} 个 ▾`}</button>` : "");
  el.querySelectorAll(".tag-chip:not(.more)").forEach(ch =>
    ch.onclick = () => {
      const t = ch.dataset.tag;
      sel.has(t) ? sel.delete(t) : sel.add(t);
      renderTagCloud(cloudId, which);
    });
  const more = el.querySelector(".tag-chip.more");
  if (more) more.onclick = () => { el._showAll = !showAll; renderTagCloud(cloudId, which); };
}

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

async function loadLearnTags() { /* 由 loadTags 统一渲染标签云 */ }

$("learn-start-btn").onclick = async () => {
  const tags = [...tagSel.learn].join(",");
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
      ${r.reasoning ? `<details class="thinking"><summary>🧠 讲解员的思考过程</summary><div class="th-content">${esc(r.reasoning)}</div></details>` : ""}
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
    allTagData.list = d.tags;
    renderTagCloud("learn-tag-cloud", "learn");
    renderTagCloud("quiz-tag-cloud", "quiz");
  } catch {}
}

$("quiz-start-btn").onclick = async () => {
  const tags = [...tagSel.quiz].join(",");
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
    ${r.reasoning ? `<details class="thinking"><summary>🧠 面试官的思考过程</summary><div class="th-content">${esc(r.reasoning)}</div></details>` : ""}
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

// ---------- 知识库管理 ----------
let kbThinkingText = "";

// 知识库 tab 切换
function setKbTab(tab) {
  $("kb-tab-knowledge").className = "btn" + (tab === "knowledge" ? " primary" : "");
  $("kb-tab-coding").className = "btn" + (tab === "coding" ? " primary" : "");
  $("kb-knowledge-view").classList.toggle("hidden", tab !== "knowledge");
  $("kb-coding-view").classList.toggle("hidden", tab !== "coding");
}
$("kb-tab-knowledge").onclick = () => setKbTab("knowledge");
$("kb-tab-coding").onclick = () => setKbTab("coding");

// ---------- AI 出题 ----------
$("gen-start-btn").onclick = async () => {
  const brief = $("gen-brief").value.trim();
  if (!brief) return alert("请填写出题需求或题目描述");
  const btn = $("gen-start-btn");
  btn.disabled = true; btn.textContent = "生成中…";
  $("gen-progress-card").classList.remove("hidden");
  const live = $("gen-live");
  live.innerHTML = "";
  let thinkBox = null;
  try {
    const resp = await fetch("/api/problems/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brief }),
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
        const k = ev.event;
        if (k === "thinking_delta" || k === "content_delta") {
          if (!thinkBox) thinkBox = makeThinkingBox(live);
          appendThinking(thinkBox, ev.text);
        } else if (k === "verify_start") {
          const d = document.createElement("div");
          d.className = "file-line";
          d.textContent = `⚙️ 沙箱验证参考解（第 ${ev.attempt + 1} 次尝试，${ev.n_cases} 个用例）…`;
          live.appendChild(d); thinkBox = null;
        } else if (k === "verify_case") {
          const d = document.createElement("div");
          d.className = "card-line";
          d.textContent = `✅ 全部用例跑通：${ev.detail}`;
          live.appendChild(d);
        } else if (k === "verify_fix") {
          const d = document.createElement("div");
          d.className = "fail-line";
          d.textContent = `⚠️ 参考解跑挂，喂回错误让 AI 修复：${(ev.errors || "").slice(0, 120)}`;
          live.appendChild(d);
        } else if (k === "saved") {
          const d = document.createElement("div");
          d.className = "file-line";
          d.innerHTML = `🎉 已入库：<b>${esc(ev.title)}</b>（${esc(ev.difficulty)}，${ev.n_cases} 用例）
           　<button class="btn" style="padding:3px 10px;font-size:12px" onclick="goGenProblem('${esc(ev.problem_id)}')">去刷这道题 →</button>`;
          live.appendChild(d);
          if (thinkBox) thinkBox.open = false;
          $("gen-status").textContent = "生成成功";
        } else if (k === "error") {
          const d = document.createElement("div");
          d.className = "fail-line";
          d.textContent = "❌ " + ev.message;
          live.appendChild(d);
        }
        live.scrollTop = 1e9;
      }
    }
  } catch (e) {
    live.insertAdjacentHTML("beforeend", `<div class="fail-line">❌ ${esc(e.message)}</div>`);
  } finally {
    btn.disabled = false; btn.textContent = "生成题目";
  }
};
function goGenProblem(pid) { showPage("coding"); openProblem(pid); }
window.goGenProblem = goGenProblem;

// ---------- AI 适配导入（批量）与 JSON 导入（共用 SSE 处理器） ----------
async function runImportStream(url, body, handlers) {
  const resp = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
      (handlers[ev.event] || (() => {}))(ev);
    }
  }
}

function kbLine(container, text, cls = "") {
  const d = document.createElement("div");
  if (cls) d.className = cls;
  d.textContent = text;
  container.appendChild(d);
  container.scrollTop = 1e9;
  return d;
}

$("adapt-start-btn").onclick = async () => {
  const path = $("adapt-path").value.trim();
  if (!path) return alert("请填写题目描述目录路径");
  const limit = parseInt($("adapt-limit").value, 10) || 0;
  const btn = $("adapt-start-btn");
  btn.disabled = true; btn.textContent = "适配中…";
  $("gen-progress-card").classList.remove("hidden");
  const live = $("gen-live");
  live.innerHTML = "";
  let thinkBox = null;
  try {
    await runImportStream("/api/problems/adapt", { path, limit }, {
      total: ev => kbLine(live, `共 ${ev.n} 个题目描述文件`, "file-line"),
      file_start: ev => { kbLine(live, `📄 ${ev.file}`, "file-line"); thinkBox = null; },
      thinking_delta: ev => {
        if (!thinkBox) thinkBox = makeThinkingBox(live);
        appendThinking(thinkBox, ev.text);
      },
      content_delta: ev => { if (thinkBox) appendThinking(thinkBox, ev.text); },
      verify_case: ev => kbLine(live, `  ✅ 沙箱验证通过：${(ev.detail || "").slice(0, 80)}`),
      verify_fix: ev => kbLine(live, `  ⚠️ 修复中：${(ev.errors || "").slice(0, 80)}`, "fail-line"),
      saved: ev => {
        const d = kbLine(live, `  🎉 入库：${ev.title}（${ev.n_cases} 用例）`);
        const b = document.createElement("button");
        b.className = "btn"; b.textContent = "去刷 →";
        b.style.cssText = "padding:2px 8px;font-size:11px;margin-left:8px";
        b.onclick = () => goGenProblem(ev.problem_id);
        d.appendChild(b);
      },
      failed: ev => kbLine(live, `  ❌ ${ev.file}: ${ev.error}`, "fail-line"),
      all_done: ev => kbLine(live, `完成：成功 ${ev.ok} / 失败 ${ev.fail}`, "file-line"),
      error: ev => kbLine(live, `❌ ${ev.message}`, "fail-line"),
    });
  } catch (e) { kbLine(live, "❌ " + e.message, "fail-line"); }
  finally { btn.disabled = false; btn.textContent = "开始适配导入"; }
};

$("jsonimp-start-btn").onclick = async () => {
  const path = $("jsonimp-path").value.trim();
  if (!path) return alert("请填写 JSON 目录路径");
  const btn = $("jsonimp-start-btn");
  btn.disabled = true; btn.textContent = "导入中…";
  $("gen-progress-card").classList.remove("hidden");
  const live = $("gen-live");
  live.innerHTML = "";
  try {
    await runImportStream("/api/problems/import_json", { path }, {
      total: ev => kbLine(live, `共 ${ev.n} 个 JSON 文件`, "file-line"),
      imported: ev => kbLine(live, `✅ ${ev.file} → ${ev.id} ${ev.title}`),
      failed: ev => kbLine(live, `❌ ${ev.file}: ${ev.error}`, "fail-line"),
      all_done: ev => kbLine(live, `完成：成功 ${ev.ok} / 失败 ${ev.fail}`, "file-line"),
      error: ev => kbLine(live, `❌ ${ev.message}`, "fail-line"),
    });
  } catch (e) { kbLine(live, "❌ " + e.message, "fail-line"); }
  finally { btn.disabled = false; btn.textContent = "导入"; }
};

async function loadSources() {
  const d = await api("/api/sources");
  const tb = $("kb-sources-table").querySelector("tbody");
  tb.innerHTML = d.sources.map(s => `
    <tr><td>${esc(s.title)}</td><td>${s.n_cards}</td>
    <td class="muted">${esc((s.ingested_at || "").replace("T", " ").slice(0, 16))}</td>
    <td><button class="btn" style="padding:3px 10px;font-size:12px" onclick="delSource(${s.id})">删除</button></td></tr>`
  ).join("") || '<tr><td colspan="4" class="muted">还没有导入任何知识。在上方输入目录路径开始。</td></tr>';
}

async function delSource(id) {
  if (!confirm("删除该来源及其全部题卡？（练习记录保留）")) return;
  await api(`/api/sources/${id}`, { method: "DELETE" });
  loadSources();
}

$("kb-browse-btn").onclick = async () => {
  const path = $("kb-path").value.trim() || "~";
  try {
    const d = await api(`/api/fs/browse?path=${encodeURIComponent(path)}`);
    $("kb-browse-area").innerHTML = `
      <div class="card" style="margin-top:10px;background:var(--panel2)">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn" style="padding:4px 10px;font-size:12px" onclick="kbGo('${esc(d.parent || "")}')">↑ 上级</button>
          <span class="muted" style="font-size:13px">${esc(d.current)}</span>
          <span class="badge tag">${d.importable_count} 个可导入文件</span>
        </div>
        <div style="margin-top:8px;max-height:180px;overflow:auto">
          ${d.dirs.slice(0, 60).map(x =>
            `<div style="padding:2px 0"><a href="javascript:void(0)" onclick="kbGo('${esc(x.path)}')" style="font-size:13.5px">📁 ${esc(x.name)}</a></div>`).join("")}
          ${d.importable_files.slice(0, 40).map(x =>
            `<div class="muted" style="font-size:12.5px;padding:2px 0">📄 ${esc(x.name)}</div>`).join("")}
        </div>
      </div>`;
  } catch (e) { alert("浏览失败：" + e.message); }
};
function kbGo(path) { if (path) { $("kb-path").value = path; $("kb-browse-btn").click(); } }

function kbLog(line, cls = "") {
  const log = $("kb-live");
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = line;
  log.appendChild(div);
  log.scrollTop = 1e9;
}

$("kb-import-btn").onclick = async () => {
  const path = $("kb-path").value.trim();
  if (!path) return alert("请填写目录路径");
  $("kb-progress-card").classList.remove("hidden");
  $("kb-live").innerHTML = "";
  kbThinkingText = "";
  const btn = $("kb-import-btn");
  btn.disabled = true; btn.textContent = "导入中…";
  let filesDone = 0, filesTotal = 0;
  try {
    const resp = await fetch("/api/ingest/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!resp.ok) {
      let msg = resp.statusText;
      try { msg = (await resp.json()).detail || msg; } catch {}
      throw new Error(msg);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "", thinkDiv = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        const k = ev.event;
        if (k === "file_start") {
          kbLog(`📄 ${ev.file}（${ev.blocks} 个知识块）`, "file-line");
          thinkDiv = null;
        } else if (k === "file_skip") {
          kbLog(`⏭ ${ev.file}（已导入过，跳过）`, "fail-line");
        } else if (k === "file_failed") {
          kbLog(`⚠️ ${ev.file}: ${ev.error}`, "fail-line");
        } else if (k === "card_done") {
          kbLog(`  ✅ 题卡：${ev.question}`);
        } else if (k === "delta" && ev.delta_kind === "reasoning_delta") {
          if (!thinkDiv) {
            thinkDiv = document.createElement("details");
            thinkDiv.className = "thinking";
            thinkDiv.open = false;
            thinkDiv.innerHTML = '<summary>🧠 AI thinking…</summary><div class="th-content"></div>';
            $("kb-live").appendChild(thinkDiv);
          }
          const tc = thinkDiv.querySelector(".th-content");
          tc.textContent += ev.text;
          tc.scrollTop = tc.scrollHeight;
        } else if (k === "all_done") {
          filesTotal = ev.files_total; filesDone = ev.files_done + ev.files_skipped + ev.files_failed;
          $("kb-progress-fill").style.width = "100%";
          $("kb-progress-text").textContent =
            `完成：${ev.files_done} 文件 / ${ev.cards_added} 张题卡` +
            (ev.cards_failed ? `（${ev.cards_failed} 失败）` : "");
          kbLog(`🎉 导入完成：${ev.cards_added} 张题卡`, "file-line");
          loadSources();
        } else if (k === "error") {
          kbLog(`❌ ${ev.message}`, "fail-line");
        }
      }
    }
  } catch (e) {
    kbLog("❌ " + e.message, "fail-line");
  } finally {
    btn.disabled = false; btn.textContent = "开始导入";
  }
};

// ---------- 设置 ----------
async function loadSettings() {
  loadMyKey();
  if (!currentUser || !currentUser.is_admin) {
    $("card-serverllm").classList.add("hidden");
    $("card-users").classList.add("hidden");
    return;
  }
  try {
    const d = await api("/api/llm/config");
    $("set-baseurl").value = d.base_url;
    const sel = $("set-model-select");
    if (![...sel.options].some(o => o.value === d.model)) {
      sel.innerHTML = `<option value="${esc(d.model)}">${esc(d.model)}</option>`;
    }
    sel.value = d.model;
    $("set-key-masked").textContent = d.configured ? d.api_key_masked : "未配置";
    $("set-status").textContent = d.configured ? "● 已配置" : "○ 未配置";
  } catch {}
  loadUsers();
}

// 个人 API Key
async function loadMyKey() {
  try {
    const d = await api("/api/me/llm");
    $("mykey-status").innerHTML = d.using_own_key
      ? '当前：<b style="color:var(--green)">使用你自己的 Key</b>'
      : (d.server_configured ? '当前：使用服务器共享 Key'
         : '当前：<b style="color:var(--amber)">服务器未配置 Key</b>（可填自己的）');
  } catch {}
}
$("mykey-save-btn").onclick = async () => {
  try {
    await api("/api/me/llm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: $("mykey-input").value.trim() }),
    });
    $("mykey-input").value = "";
    loadMyKey();
  } catch (e) { alert("保存失败：" + e.message); }
};

// 用户管理（管理员）
async function loadUsers() {
  try {
    const d = await api("/api/admin/users");
    const tb = $("users-table").querySelector("tbody");
    tb.innerHTML = d.users.map(u => `
      <tr><td>${esc(u.username)}</td>
        <td>${u.is_admin ? "管理员" : "成员"}</td>
        <td class="muted">${u.llm_today}</td>
        <td class="muted">${u.api_key ? "✔" : "—"}</td>
        <td><button class="btn" style="padding:3px 10px;font-size:12px"
            onclick="resetUserPass('${esc(u.username)}')">重置密码</button>
          <button class="btn" style="padding:3px 10px;font-size:12px"
            onclick="delUser('${esc(u.username)}')">删除</button></td></tr>`).join("");
  } catch {}
}
$("au-add-btn").onclick = async () => {
  const name = $("au-name").value.trim(), pass = $("au-pass").value;
  if (!name || pass.length < 4) return alert("用户名必填，密码至少 4 位");
  try {
    await api("/api/admin/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name, password: pass,
        is_admin: $("au-admin").checked }),
    });
    $("au-name").value = ""; $("au-pass").value = ""; $("au-admin").checked = false;
    loadUsers();
  } catch (e) { alert("添加失败：" + e.message); }
};
async function resetUserPass(name) {
  const pass = prompt(`为 ${name} 设置新密码（至少 4 位）：`);
  if (!pass) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(name)}/passwd`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pass }),
    });
    alert("已重置");
  } catch (e) { alert("重置失败：" + e.message); }
}
async function delUser(name) {
  if (!confirm(`删除用户 ${name}？（其练习记录保留）`)) return;
  try {
    await api(`/api/admin/users/${encodeURIComponent(name)}`, { method: "DELETE" });
    loadUsers();
  } catch (e) { alert("删除失败：" + e.message); }
}
window.resetUserPass = resetUserPass;
window.delUser = delUser;

$("set-scan-btn").onclick = async () => {
  const btn = $("set-scan-btn");
  btn.disabled = true; btn.textContent = "扫描中…";
  try {
    // 先保存当前填写的 url/key 再扫描
    await api("/api/llm/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: $("set-baseurl").value.trim(),
        model: $("set-model-select").value, api_key: $("set-apikey").value.trim() }),
    });
    const d = await api("/api/llm/models");
    const sel = $("set-model-select");
    sel.innerHTML = d.models.map(m =>
      `<option value="${esc(m)}" ${m === d.current ? "selected" : ""}>${esc(m)}</option>`).join("");
    sel.value = d.current;
    $("set-status").textContent = `● 扫到 ${d.models.length} 个模型`;
    refreshBadge();
  } catch (e) { alert("扫描失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "扫描可用模型"; }
};

$("set-save-btn").onclick = async () => {
  try {
    const d = await api("/api/llm/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: $("set-baseurl").value.trim(),
        model: $("set-model-select").value, api_key: $("set-apikey").value.trim() }),
    });
    $("set-status").textContent = d.llm_ready ? `● 已保存（${d.model}）` : "已保存，但 key 仍为空";
    $("set-apikey").value = "";
    loadSettings(); refreshBadge();
    // 自动测试连通性
    try {
      const tr = await api("/api/llm/test", { method: "POST" });
      $("set-status").textContent = tr.ok ? "● 连接成功 ✅" : "❌ " + tr.detail;
    } catch (e) { $("set-status").textContent = "⚠️ 无法测试: " + e.message; }
  } catch (e) { alert("保存失败：" + e.message); }
};

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

// ---------- 登录 / 登出 ----------
async function tryLogin() {
  const btn = $("login-btn");
  btn.disabled = true; btn.textContent = "登录中…";
  try {
    await api("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-username").value.trim(),
        password: $("login-password").value }),
    });
    location.reload(); // 干净地重新初始化全部状态
  } catch (e) {
    $("login-error").textContent = e.message;
    btn.disabled = false; btn.textContent = "登 录";
  }
}
$("login-btn").onclick = tryLogin;

// ---------- 注册 ----------
let authMode = "login", regCodeNeeded = false;
function setAuthTab(mode) {
  authMode = mode;
  $("auth-tab-login").className = "btn" + (mode === "login" ? " primary" : "");
  $("auth-tab-register").className = "btn" + (mode === "register" ? " primary" : "");
  $("login-btn").classList.toggle("hidden", mode !== "login");
  $("register-btn").classList.toggle("hidden", mode !== "register");
  // 邀请码框只在「注册」tab 且服务端为邀请码模式时出现——登录不需要邀请码
  $("reg-code").classList.toggle("hidden", !(mode === "register" && regCodeNeeded));
  $("login-error").textContent = "";
}
$("auth-tab-login").onclick = () => setAuthTab("login");
$("auth-tab-register").onclick = () => setAuthTab("register");

async function initRegistrationMode() {
  try {
    const d = await api("/api/auth/registration_mode");
    if (d.mode === "off") {
      $("auth-tab-register").disabled = true;
      $("auth-tab-register").title = "未开放自助注册";
    } else if (d.mode === "code") {
      regCodeNeeded = true;
    } // open：不需要邀请码；显隐统一由 setAuthTab 控制
  } catch {}
}
initRegistrationMode();

$("register-btn").onclick = async () => {
  const btn = $("register-btn");
  btn.disabled = true; btn.textContent = "注册中…";
  try {
    const body = { username: $("login-username").value.trim(),
                   password: $("login-password").value };
    if (!$("reg-code").classList.contains("hidden"))
      body.code = $("reg-code").value.trim();
    const r = await api("/api/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.ok) location.reload();
  } catch (e) {
    $("login-error").textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = "注册并登录";
  }
};
$("login-password").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.isComposing) tryLogin();
});
$("logout-btn").onclick = async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  location.reload();
};

// ---------- 启动 ----------
async function boot() {
  try {
    currentUser = await api("/api/me");
  } catch (e) {
    currentUser = undefined; // 未登录（仅多用户模式会走到这）
    showLogin();
    return;
  }
  applyUserUI();
  initEditor();
  setEditorCode(TEMPLATES.python, "python");
  refreshBadge();
  loadProblems().then(loadHeroStats);
  loadTags();
  loadLearnTags();
  setQuizMode("learn");
  refreshLearnProgress();
}
boot();

// inline onclick 导出
window.delSource = delSource;
window.kbGo = kbGo;

async function delProblem(pid) {
  if (!confirm("删除这道 AI 生成题？")) return;
  await api("/api/problems/" + pid, { method: "DELETE" });
  loadProblems();
}
window.delProblem = delProblem;
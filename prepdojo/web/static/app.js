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
  const multiuser = !!(u && u.multiuser);
  $("card-mykey").classList.toggle("hidden", !multiuser);
  const privacy = $("privacy-copy");
  if (privacy) {
    privacy.textContent = multiuser
      ? "练习进度按账号隔离；题目、代码与记录保存在本服务的部署服务器。"
      : "题目、代码与练习记录只保存在这台电脑。";
  }
  if (!multiuser) return; // 单机模式：不显示用户相关 UI
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
// 所有服务端/LLM 字段都按不可信输入处理。动态内容优先通过 textContent 写入；
// CSS 类名仅从下列白名单生成，避免把题目 id、难度、判定等拼进 HTML。
const asObject = (v) => v && typeof v === "object" && !Array.isArray(v) ? v : {};
const asArray = (v) => Array.isArray(v) ? v : [];
const displayText = (v, fallback = "") => {
  if (v === null || v === undefined) return fallback;
  if (["string", "number", "boolean"].includes(typeof v)) return String(v);
  return fallback;
};
const finiteText = (v, fallback = "—") => {
  if ((typeof v !== "number" && typeof v !== "string") || (typeof v === "string" && !v.trim())) return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? String(n) : fallback;
};
const scoreText = (v) => {
  if ((typeof v !== "number" && typeof v !== "string") || (typeof v === "string" && !v.trim())) return "—";
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 && n <= 10 ? String(n) : "—";
};
const DIFFICULTIES = {
  easy: { cls: "easy", label: "简单" },
  medium: { cls: "medium", label: "中等" },
  hard: { cls: "hard", label: "困难" },
};
const VERDICTS = new Set(["AC", "WA", "TLE", "MLE", "RE", "CE"]);
const safeLanguage = (value, fallback = "python") =>
  ["python", "cpp"].includes(displayText(value)) ? displayText(value) : fallback;
function difficultyInfo(value) {
  return DIFFICULTIES[displayText(value)] || { cls: "", label: "未知" };
}
function interviewPriorityInfo(value) {
  const priority = Number(value);
  if (priority === 1) return { cls: "priority-1", label: "必刷" };
  if (priority === 2) return { cls: "priority-2", label: "高频" };
  return { cls: "priority-3", label: "补充" };
}
function verdictInfo(value) {
  const candidate = displayText(value).toUpperCase();
  const text = VERDICTS.has(candidate) ? candidate : "UNKNOWN";
  return { text, cls: VERDICTS.has(text) ? `verdict-${text}` : "muted" };
}
function makeEl(tag, { className = "", text = "", title = "" } = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (title) node.title = title;
  node.textContent = displayText(text);
  return node;
}
function addBadge(parent, value, kind = "tag") {
  const info = kind === "difficulty" ? difficultyInfo(value)
    : kind === "priority" ? interviewPriorityInfo(value) : null;
  const badge = makeEl("span", {
    className: `badge${info && info.cls ? ` ${info.cls}` : kind === "tag" ? " tag" : ""}`,
    text: info ? info.label : value,
  });
  parent.appendChild(badge);
  return badge;
}
function appendMessage(parent, text, cls) {
  const div = makeEl("div", { className: `coach-msg ${cls}`, text });
  parent.appendChild(div);
  return div;
}
function parseSseEvent(chunk) {
  if (!chunk.startsWith("data: ")) return null;
  try {
    const value = JSON.parse(chunk.slice(6));
    return asObject(value);
  } catch {
    return null;
  }
}
function toolStartText(event, maxLength = 50) {
  const ev = asObject(event);
  const args = asObject(ev.args);
  const rawArg = args.code ?? args.problem_id ?? "";
  const preview = displayText(rawArg).slice(0, maxLength).replace(/\n/g, "⏎");
  return `⚙️ ${displayText(ev.name, "工具").slice(0, 40)}(${preview}${preview ? "…" : ""})`;
}

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
  holder.replaceChildren();
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
        // 撤销/重做：显式绑定防止被浏览器截获
        "Cmd-Z": cmr => cmr.undo(),
        "Cmd-Shift-Z": cmr => cmr.redo(),
        "Ctrl-Z": cmr => cmr.undo(),
        "Ctrl-Shift-Z": cmr => cmr.redo(),
        "Ctrl-Y": cmr => cmr.redo(),
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
      [h.llm_ready ? "已就绪" : "未配置", "AI（判题始终可用）"],
    ];
    const holder = $("hero-stats");
    holder.replaceChildren();
    for (const [v, k] of items) {
      const item = makeEl("div", { className: "hs" });
      item.append(makeEl("b", { text: v }), makeEl("span", { text: k }));
      holder.appendChild(item);
    }
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
    if (h.llm_ready) { b.textContent = "● AI 已就绪"; b.className = "on"; }
    else { b.textContent = "○ AI 未配置（判题可用）"; b.className = ""; }
  } catch {}
}

// ---------- 代码题 ----------
let problems = [], currentProblem = null, lastSubmission = null;

async function loadProblems() {
  const d = await api("/api/problems");
  problems = asArray(asObject(d).problems).map(asObject);
  const tb = $("problem-table").querySelector("tbody");
  tb.replaceChildren();
  for (const p of problems) {
    const pid = displayText(p.id);
    const attempts = Number.isFinite(Number(p.attempts)) ? Number(p.attempts) : 0;
    const tr = makeEl("tr", { className: "problem-row" });
    tr.dataset.id = pid;
    const statusCell = makeEl("td");
    statusCell.appendChild(p.ever_ac
      ? makeEl("span", { text: "✅", title: "已攻克" })
      : attempts > 0
        ? makeEl("span", { text: "❌", title: `未通过（${attempts} 次提交）` })
        : makeEl("span", { className: "muted", text: "⬜", title: "没做过" }));
    const priorityCell = makeEl("td");
    addBadge(priorityCell, p.interview_priority, "priority");
    const leetcodeId = Number(p.leetcode_id);
    const leetcodeCell = makeEl("td", {
      className: "muted",
      text: Number.isInteger(leetcodeId) && leetcodeId > 0 ? `LC ${leetcodeId}` : "—",
    });
    const idCell = makeEl("td", { className: "muted", text: pid });
    const titleCell = makeEl("td", { text: p.title });
    const difficultyCell = makeEl("td");
    addBadge(difficultyCell, p.difficulty, "difficulty");
    const tagsCell = makeEl("td");
    asArray(p.tags).forEach(tag => addBadge(tagsCell, tag));
    const countCell = makeEl("td", { className: "muted", text: finiteText(p.n_cases) });
    const actionCell = makeEl("td");
    if (pid.startsWith("cpg-") && currentUser && currentUser.is_admin) {
      const del = makeEl("button", { className: "btn", text: "删除" });
      del.style.cssText = "padding:2px 8px;font-size:11px";
      del.addEventListener("click", event => {
        event.stopPropagation();
        delProblem(pid);
      });
      actionCell.appendChild(del);
    }
    tr.append(statusCell, priorityCell, leetcodeCell, idCell, titleCell,
      difficultyCell, tagsCell, countCell, actionCell);
    tr.addEventListener("click", () => openProblem(pid));
    tb.appendChild(tr);
  }
  const ac = problems.filter(p => p.ever_ac).length;
  const tried = problems.filter(p => p.attempts > 0).length;
  $("problem-stat-brief").textContent =
    `已攻克 ${ac}/${problems.length} · 做过 ${tried} · 错题 ${tried - ac}`;
}

$("wrong-drill-btn").onclick = async () => {
  const d = await api("/api/problems/wrong");
  const wrong = asArray(asObject(d).wrong).map(asObject);
  if (!wrong.length) return alert("错题本是空的——提交过但未 AC 的题才会进错题本。");
  const pick = wrong[Math.floor(Math.random() * wrong.length)];
  openProblem(displayText(pick.id));
};

async function openProblem(pid) {
  currentProblem = asObject(await api("/api/problems/" + encodeURIComponent(displayText(pid))));
  if (cm) setTimeout(() => cm.refresh(), 0);
  $("problem-list-view").classList.add("hidden");
  $("problem-detail-view").classList.remove("hidden");
  $("pd-title").textContent = `${displayText(currentProblem.id)} · ${displayText(currentProblem.title)}`;
  const badges = $("pd-badges");
  badges.replaceChildren();
  addBadge(badges, currentProblem.interview_priority, "priority");
  const leetcodeId = Number(currentProblem.leetcode_id);
  if (Number.isInteger(leetcodeId) && leetcodeId > 0) addBadge(badges, `参考 LC ${leetcodeId}`);
  addBadge(badges, currentProblem.difficulty, "difficulty");
  asArray(currentProblem.tags).forEach(tag => addBadge(badges, tag));
  let st = displayText(currentProblem.statement);
  const samples = asArray(currentProblem.samples).map(asObject);
  if (samples.length) {
    st += "\n\n【样例】\n" +
      samples.map(s =>
        `输入：\n${displayText(s.input)}\n输出：\n${displayText(s.output)}`).join("\n\n");
  }
  st += `\n\n（共 ${finiteText(currentProblem.n_cases)} 组测试用例；时限 ${finiteText(currentProblem.time_limit_ms)}ms）`;
  $("statement").textContent = st;
  const lang = $("lang-select").value;
  // 草稿优先 → 无草稿时从提交记录不限语言恢复 → 都没有才用模板
  const draft = localStorage.getItem(draftKey(currentProblem.id, lang));
  if (draft && draft.trim()) {
    setEditorCode(draft, lang);
  } else {
    setEditorCode(TEMPLATES[lang], lang); // 先设模板，再异步恢复
    api("/api/submissions/last/" + encodeURIComponent(displayText(currentProblem.id)))  // 不限语言，取最近一次提交
      .then(r => {
        const last = asObject(r);
        const code = displayText(last.code);
        if (code.trim()) {
          // 如果上次提交的语言和当前不同，自动切换下拉
          const savedLanguage = safeLanguage(last.language, lang);
          if (savedLanguage !== lang) {
            $("lang-select").value = savedLanguage;
          }
          setEditorCode(code, savedLanguage);
        }
      })
      .catch(() => {});
  }
  saveDraft();
  $("result-area").replaceChildren();
  lastSubmission = null;
  coachHistory = [];
  const coachMsgs = $("coach-messages");
  if (coachMsgs) coachMsgs.replaceChildren();
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
      api("/api/submissions/last/" + encodeURIComponent(displayText(currentProblem.id)) + "?language=" + encodeURIComponent(lang))
        .then(r => {
          const code = displayText(asObject(r).code);
          if (code.trim()) setEditorCode(code, lang);
        })
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
  $("result-area").replaceChildren(makeEl("p", { className: "muted", text: "沙箱运行中…" }));
  try {
    const r = await api("/api/submit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ problem_id: currentProblem.id,
        language: $("lang-select").value, code }),
    });
    lastSubmission = r;
    renderResult(r);
  } catch (e) {
    $("result-area").replaceChildren(
      makeEl("p", { className: "verdict-RE", text: "提交失败：" + displayText(e.message, "未知错误") }));
  } finally {
    btn.disabled = false; btn.textContent = "提交判题";
  }
};

function renderResult(r) {
  r = asObject(r);
  const verdict = verdictInfo(r.verdict);
  const area = $("result-area");
  area.replaceChildren();
  const summary = makeEl("p");
  summary.style.fontSize = "15px";
  summary.append(document.createTextNode("判定："));
  summary.appendChild(makeEl("span", { className: verdict.cls, text: verdict.text }));
  summary.append(document.createTextNode(` · 最慢用例 ${finiteText(r.max_time_ms)}ms`));
  area.appendChild(summary);

  const compileError = displayText(r.compile_error);
  if (compileError) {
    area.append(makeEl("p", { className: "muted", text: "编译错误：" }),
      makeEl("pre", { text: compileError }));
  } else {
    const cases = asArray(r.cases).map(asObject);
    if (!cases.length) {
      area.appendChild(makeEl("p", { className: "muted", text: "后端未返回逐用例详情。" }));
    } else {
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      const header = document.createElement("tr");
      ["用例", "结果", "耗时", "详情"].forEach(text => header.appendChild(makeEl("th", { text })));
      thead.appendChild(header);
      const tbody = document.createElement("tbody");
      cases.forEach((c, index) => {
        const cv = verdictInfo(c.verdict);
        let detail = displayText(c.detail);
        if (!detail && cv.text === "WA") {
          const hasExpected = c.expected !== undefined && c.expected !== null;
          const hasStdout = c.stdout !== undefined && c.stdout !== null;
          detail = hasExpected || hasStdout
            ? `期望 ${displayText(c.expected, "—")} / 实际 ${displayText(c.stdout, "—")}`
            : "隐藏用例未通过（详情未公开）";
        } else if (!detail && cv.text === "RE") {
          detail = displayText(c.stderr).slice(0, 200) || "运行时错误（详情未公开）";
        }
        const row = document.createElement("tr");
        row.append(
          makeEl("td", { text: `#${finiteText(c.idx, String(index + 1))}` }),
          makeEl("td", { className: cv.cls, text: cv.text }),
          makeEl("td", { text: `${finiteText(c.time_ms)}ms` }),
          makeEl("td", { className: "muted", text: detail }),
        );
        tbody.appendChild(row);
      });
      table.append(thead, tbody);
      area.appendChild(table);
    }
  }
  // 非 AC 时在教练栏弹修复入口
  if (verdict.text !== "AC" && currentProblem) {
    setTimeout(() => offerFixInCoach(r), 100);
  }
}

let lastFailedResult = null;  // 最近一次非 AC 的判题结果
let lastFixedCode = null;     // 最近一次 AI 修复的代码
function offerFixInCoach(r) {
  lastFailedResult = r;
  const verdict = verdictInfo(asObject(r).verdict).text;
  const btn = $("coach-fix-btn");
  if (btn) { btn.disabled = false; btn.style.opacity = "1"; btn.textContent = "🔧 修复 " + verdict; _fixBtnStyle(btn, ""); }
  coachRender("tool", `判题结果：${verdict}；可点右上角「🔧 修复」让 AI 改代码`, "tool");
}
function _fixBtnStyle(btn, bg) { btn.style.background = bg; btn.style.borderColor = bg; btn.style.color = bg ? "#fff" : ""; }

async function doCoachFix(r) {
  if (!currentProblem) return;
  const btn = $("coach-fix-btn");
  if (btn) { btn.disabled = true; btn.textContent = "修复中…"; btn.style.opacity = ".6"; _fixBtnStyle(btn, ""); }
  let reply = "", assistantDiv = coachRender("assistant", "思考中…", "assistant");
  let thinkBox = null;
  try {
    const resp = await fetch("/api/fix/" + encodeURIComponent(displayText(currentProblem.id)), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: getEditorCode(), language: $("lang-select").value,
        verdict: verdictInfo(r.verdict).text, detail: displayText(r.compile_error).slice(0, 500) }),
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
        const ev = parseSseEvent(chunk);
        if (!ev) continue;
        if (ev.event === "thinking_delta" || ev.event === "reasoning_delta") {
          if (!thinkBox) thinkBox = makeThinkingBox(assistantDiv.parentNode || $("coach-messages"));
          appendThinking(thinkBox, ev.text);
        } else if (ev.event === "content_delta") {
          reply += displayText(ev.text);
          appendStreamingText(assistantDiv, ev.text);
        } else if (ev.event === "reply") {
          reply = displayText(ev.code) || displayText(ev.text);
        } else if (ev.event === "error") {
          assistantDiv.textContent = "修复失败：" + displayText(ev.message, "未知错误");
        }
      }
    }
    if (thinkBox) thinkBox.open = false;
    if (reply) {
      if (reply.startsWith("⚠️")) {
        assistantDiv.replaceChildren();
        assistantDiv.style.color = "var(--amber)";
        assistantDiv.textContent = reply;
        if (btn) { btn.disabled = false; btn.textContent = "🔧 修复"; btn.style.opacity = "1"; _fixBtnStyle(btn, ""); }
      } else {
        const codeMatch = reply.match(/```(?:\w+)?\s*\n([\s\S]*?)```/);
        lastFixedCode = codeMatch ? codeMatch[1].trim() : reply;
        assistantDiv.replaceChildren();
        const intro = makeEl("div", {
          text: reply.replace(/```[\s\S]*?```/g, "").trim().slice(0, 400),
        });
        intro.style.whiteSpace = "pre-wrap";
        const code = makeEl("pre", { text: lastFixedCode });
        code.style.cssText = "background:var(--panel2);border-radius:8px;padding:10px;overflow:auto;max-height:320px;font-size:12.5px;line-height:1.6";
        assistantDiv.append(intro, code);
        if (btn) { btn.disabled = false; btn.textContent = "✅ 应用修复"; btn.style.opacity = "1"; _fixBtnStyle(btn, "var(--green)"); }
      }
    }
  } catch (e) {
    assistantDiv.textContent = "修复失败：" + e.message;
    if (btn) { btn.disabled = false; btn.textContent = "🔧 修复"; btn.style.opacity = "1"; _fixBtnStyle(btn, ""); }
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
  const oldLive = $("ai-judge-live");
  if (oldLive) oldLive.remove();
  const wrapper = makeEl("div", { className: "card" });
  wrapper.id = "ai-judge-live";
  wrapper.style.marginTop = "14px";
  wrapper.appendChild(makeEl("b", { text: "🤖 AI 判题" }));
  const live = makeEl("div", { className: "tool-log" });
  wrapper.appendChild(live);
  area.appendChild(wrapper);
  try {
    const resp = await fetch(`/api/ai_judge/${encodeURIComponent(displayText(currentProblem.id))}`, {
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
        const ev = parseSseEvent(chunk);
        if (!ev) continue;
        if (ev.event === "tool_start") {
          appendMessage(live, toolStartText(ev), "tool");
        } else if (ev.event === "tool_done") {
          appendMessage(live, "✅ " + displayText(ev.summary), "tool");
        } else if (ev.event === "thinking_delta") {
          if (!live._think) live._think = makeThinkingBox(live);
          appendThinking(live._think, ev.text);
        } else if (ev.event === "content_delta") {
          // 最终输出是结构化 JSON 报告：不逐 token 展示原文，等 report 事件统一渲染
          if (!live._reporting) {
            live._reporting = true;
            appendMessage(live, "📝 正在汇总判定报告…", "tool");
          }
        } else if (ev.event === "report") {
          if (live._think) live._think.open = false;
          renderAiJudgeReport(live, ev.report);
        } else if (ev.event === "report_raw") {
          appendMessage(live, displayText(ev.text), "assistant");
        } else if (ev.event === "error") {
          appendMessage(live, "❌ " + displayText(ev.message, "未知错误"), "tool");
        }
      }
    }
  } catch (e) {
    appendMessage(live, "❌ " + displayText(e.message, "未知错误"), "tool");
  } finally {
    btn.disabled = false; btn.textContent = "🤖 AI 判题";
  }
};

function renderAiJudgeReport(container, r) {
  r = asObject(r);
  const bs = asObject(r.better_solution);
  const complexity = asObject(r.complexity);
  const verdict = verdictInfo(r.sandbox_verdict);
  const card = makeEl("div", { className: "card" });
  card.style.marginTop = "10px";
  const heading = makeEl("p");
  heading.style.fontSize = "16px";
  const strong = makeEl("b", { text: "判定：" });
  strong.appendChild(makeEl("span", { className: verdict.cls, text: verdict.text }));
  heading.append(strong, document.createTextNode("　"),
    makeEl("span", { className: "muted", text: r.summary }));
  card.appendChild(heading);

  const complexityLine = makeEl("p", { text: "复杂度：时间 " });
  complexityLine.append(makeEl("b", { text: displayText(complexity.time, "未知") }),
    document.createTextNode(" · 空间 "), makeEl("b", { text: displayText(complexity.space, "未知") }));
  card.append(complexityLine, makeEl("p", { text: "🔬 边界分析" }),
    makeEl("div", { className: "per-point", text: displayText(r.boundary_analysis, "—") }));

  if (bs.exists === true) {
    card.appendChild(makeEl("p", {
      text: `🚀 更优解法：${displayText(bs.name, "未命名")}（${displayText(bs.complexity, "未知")}）`,
    }));
    const better = makeEl("div", { className: "per-point hit" });
    better.append(makeEl("b", { text: "为什么更优：" }),
      document.createTextNode(displayText(bs.why_better)), document.createElement("br"),
      makeEl("b", { text: "思路提示：" }), document.createTextNode(displayText(bs.hint)));
    card.appendChild(better);
  }
  const related = asArray(r.related_knowledge);
  if (related.length) {
    card.appendChild(makeEl("p", { text: "📚 知识点（更优解法背后）" }));
    related.forEach(item => card.appendChild(makeEl("div", { className: "per-point", text: item })));
  }
  const tips = asArray(r.interview_tips);
  if (tips.length) {
    card.appendChild(makeEl("p", { text: "🎤 面试官视角" }));
    tips.forEach(item => card.appendChild(makeEl("div", { className: "per-point", text: item })));
  }
  container.appendChild(card);
}

// ---------- AI 讲题教练（SSE 流式 + 沙箱工具轨迹 + thinking 流） ----------
let coachHistory = [];

function coachRender(role, text, cls) {
  const div = document.createElement("div");
  div.className = `coach-msg ${["user", "assistant", "tool"].includes(cls) ? cls : "assistant"}`;
  div.textContent = displayText(text);
  $("coach-messages").appendChild(div);
  $("coach-messages").scrollTop = 1e9;
  return div;
}

function makeThinkingBox(parent) {
  const d = document.createElement("details");
  d.className = "thinking";
  d.open = true; // 导入/判题进行中默认展开，看得到 AI 在干活
  d.append(makeEl("summary", { text: "🧠 AI thinking…" }),
    makeEl("div", { className: "th-content" }));
  parent.appendChild(d);
  return d;
}
function appendThinking(box, text) {
  const tc = box.querySelector(".th-content");
  if (!tc) return;
  if (!tc._streamText) {
    tc._streamText = document.createTextNode("");
    tc.appendChild(tc._streamText);
  }
  tc._streamText.appendData(displayText(text));
  tc.scrollTop = tc.scrollHeight;
}

function appendStreamingText(node, text) {
  if (!node._streamText) {
    node.replaceChildren();
    node._streamText = document.createTextNode("");
    node.appendChild(node._streamText);
  }
  node._streamText.appendData(displayText(text));
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
    const resp = await fetch(`/api/chat/problem/${encodeURIComponent(displayText(currentProblem.id))}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: coachHistory,
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
        const ev = parseSseEvent(chunk);
        if (!ev) continue;
        if (ev.event === "tool_start") {
          coachRender("tool", toolStartText(ev, 40), "tool");
          assistantDiv = coachRender("assistant", "", "assistant");
          reply = "";
        } else if (ev.event === "tool_done") {
          coachRender("tool", "✅ " + displayText(ev.summary), "tool");
        } else if (ev.event === "thinking_delta") {
          if (!assistantDiv._think) {
            const tb = makeThinkingBox(assistantDiv.parentNode || $("coach-messages"));
            assistantDiv._think = tb;
          }
          appendThinking(assistantDiv._think, ev.text);
        } else if (ev.event === "content_delta") {
          reply += displayText(ev.text);
          appendStreamingText(assistantDiv, ev.text);
        } else if (ev.event === "reply") {
          reply = displayText(ev.text);
          assistantDiv.textContent = reply;
          if (assistantDiv._think) assistantDiv._think.open = false;
        } else if (ev.event === "error") {
          assistantDiv.textContent = "出错：" + displayText(ev.message, "未知错误");
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
  el.replaceChildren();
  list.forEach(entry => {
    const pair = asArray(entry);
    const t = displayText(pair[0]);
    const chip = makeEl("button", { className: `tag-chip${sel.has(t) ? " on" : ""}`, text: t });
    chip.appendChild(makeEl("i", { text: finiteText(pair[1], "0") }));
    chip.addEventListener("click", () => {
      sel.has(t) ? sel.delete(t) : sel.add(t);
      renderTagCloud(cloudId, which);
    });
    el.appendChild(chip);
  });
  if (allTagData.list.length > TOP_N) {
    const more = makeEl("button", {
      className: "tag-chip more",
      text: showAll ? "收起 ▴" : `全部 ${allTagData.list.length} 个 ▾`,
    });
    more.addEventListener("click", () => {
      el._showAll = !showAll;
      renderTagCloud(cloudId, which);
    });
    el.appendChild(more);
  }
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
  const cards = asArray(asObject(d).cards).map(asObject);
  if (!cards.length) {
    return alert("没有可学的卡：全部学完了（或题库为空，先 ingest）。");
  }
  learnQueue = cards; learnIdx = 0;
  $("learn-session").classList.remove("hidden");
  showLearnCard();
};

function showLearnCard() {
  const c = asObject(learnQueue[learnIdx]);
  $("learn-progress-cnt").textContent = `第 ${learnIdx + 1} / ${learnQueue.length} 卡`;
  const question = $("learn-question");
  question.replaceChildren();
  asArray(c.topic_tags).forEach(tag => addBadge(question, tag));
  if (c.learned) addBadge(question, "已学", "difficulty").className = "badge easy";
  question.append(document.createElement("br"), document.createElement("br"),
    document.createTextNode(displayText(c.question)));
  $("learn-answer").classList.add("hidden");
  $("learn-show-btn").classList.remove("hidden");
  $("learn-done-btn").classList.add("hidden");
  $("learn-later-btn").classList.add("hidden");
  $("learn-explain-area").replaceChildren();
}

$("learn-show-btn").onclick = () => {
  const c = learnQueue[learnIdx];
  $("learn-answer").classList.remove("hidden");
  $("learn-show-btn").classList.add("hidden");
  $("learn-done-btn").classList.remove("hidden");
  $("learn-later-btn").classList.remove("hidden");
  const points = $("learn-points");
  points.replaceChildren(...asArray(c.answer_points).map(point =>
    makeEl("div", { className: "per-point", text: point })));
};

$("learn-explain-btn").onclick = async () => {
  const c = learnQueue[learnIdx];
  const btn = $("learn-explain-btn");
  btn.disabled = true; btn.textContent = "🧠 讲解生成中…";
  try {
    const r = asObject(await api(`/api/cards/${encodeURIComponent(displayText(c.id))}/explain`));
    const e = asObject(r.explanation);
    const area = $("learn-explain-area");
    area.replaceChildren();
    if (displayText(r.reasoning)) {
      const reasoning = makeEl("details", { className: "thinking" });
      reasoning.append(makeEl("summary", { text: "🧠 讲解员的思考过程" }),
        makeEl("div", { className: "th-content", text: r.reasoning }));
      area.appendChild(reasoning);
    }
    const card = makeEl("div", { className: "card" });
    card.style.marginTop = "12px";
    const core = makeEl("p");
    core.append(makeEl("b", { text: "核心：" }), document.createTextNode(displayText(e.core)));
    const expanded = makeEl("p", { text: e.expanded });
    expanded.style.whiteSpace = "pre-wrap";
    card.append(core, expanded);
    [["analogy", "🔗 类比："], ["mnemonic", "📌 记忆锚点："]].forEach(([key, label]) => {
      if (displayText(e[key])) {
        const line = makeEl("p");
        line.append(makeEl("b", { text: label }), document.createTextNode(displayText(e[key])));
        card.appendChild(line);
      }
    });
    const related = asArray(e.related).map(item => displayText(item)).filter(Boolean);
    if (related.length) card.appendChild(makeEl("p", { className: "muted", text: "相关：" + related.join(" · ") }));
    if (r.cached === true) {
      const cached = makeEl("p", { className: "muted", text: "（缓存）" });
      cached.style.fontSize = "11px";
      card.appendChild(cached);
    }
    area.appendChild(card);
  } catch (err) {
    alert("讲解失败：" + err.message);
  } finally {
    btn.disabled = false; btn.textContent = "🧠 AI 讲解";
  }
};

$("learn-done-btn").onclick = async () => {
  const c = learnQueue[learnIdx];
  try {
    await api(`/api/cards/${encodeURIComponent(displayText(c.id))}/learn`, {
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
    allTagData.list = asArray(asObject(d).tags).filter(Array.isArray);
    renderTagCloud("learn-tag-cloud", "learn");
    renderTagCloud("quiz-tag-cloud", "quiz");
  } catch {}
}

$("quiz-start-btn").onclick = async () => {
  const tags = [...tagSel.quiz].join(",");
  const n = parseInt($("quiz-num").value, 10);
  const onlyLearned = $("quiz-only-learned").checked ? 1 : 0;
  const d = asObject(await api(`/api/cards/next?tags=${encodeURIComponent(tags)}&n=${n}&only_learned=${onlyLearned}`));
  const cards = asArray(d.cards).map(asObject);
  if (!cards.length) {
    return alert("题库为空：请先用 `prepdojo ingest <知识目录>` 接入你的八股资料");
  }
  quizQueue = cards; quizIdx = 0;
  $("quiz-test-view").querySelector("#quiz-session").classList.remove("hidden");
  if (d.fallback) {
    alert("已学的卡暂时抽不出题，本次从全部卡里抽（学都没学的题分数低是正常的）。");
  }
  showQuizCard();
};

function showQuizCard() {
  const c = asObject(quizQueue[quizIdx]);
  $("quiz-progress").textContent = `第 ${quizIdx + 1} / ${quizQueue.length} 题`;
  const question = $("quiz-question");
  question.replaceChildren();
  asArray(c.topic_tags).forEach(tag => addBadge(question, tag));
  const difficulty = makeEl("span", {
    className: "muted",
    text: " 难度" + displayText(c.difficulty, "未知"),
  });
  difficulty.style.fontSize = "12px";
  question.append(difficulty, document.createElement("br"), document.createElement("br"),
    document.createTextNode(displayText(c.question)));
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
  r = asObject(r);
  c = asObject(c);
  const fb = $("quiz-feedback");
  fb.classList.remove("hidden");
  fb.replaceChildren();
  if (displayText(r.reasoning)) {
    const reasoning = makeEl("details", { className: "thinking" });
    reasoning.append(makeEl("summary", { text: "🧠 面试官的思考过程" }),
      makeEl("div", { className: "th-content", text: r.reasoning }));
    fb.appendChild(reasoning);
  }
  const card = makeEl("div", { className: "card" });
  card.style.marginTop = "14px";
  const score = makeEl("div", { className: "score-big", text: scoreText(r.score) });
  const suffix = makeEl("span", { className: "muted", text: " / 10" });
  suffix.style.fontSize = "16px";
  score.appendChild(suffix);
  card.append(score, makeEl("p", { text: r.overall }));
  asArray(r.per_point).map(asObject).forEach(point => {
    const covered = point.covered === true;
    const row = makeEl("div", {
      className: `per-point ${covered ? "hit" : "miss"}`,
      text: `${covered ? "✅" : "❌"} ${displayText(point.point)}`,
    });
    row.appendChild(makeEl("div", { className: "muted", text: point.comment }));
    card.appendChild(row);
  });
  const addPointGroup = (title, values, cls) => {
    const items = asArray(values);
    if (!items.length) return;
    card.appendChild(makeEl("p", { text: title }));
    items.forEach(item => card.appendChild(makeEl("div", { className: `per-point ${cls}`, text: item })));
  };
  addPointGroup("📌 遗漏要点", r.missed, "miss");
  addPointGroup("🌟 加分项", r.extra_good, "hit");
  const referenceTitle = makeEl("p", { className: "muted", text: "参考要点（来自你的知识库）：" });
  referenceTitle.style.marginTop = "10px";
  card.appendChild(referenceTitle);
  asArray(r.reference).forEach(item => card.appendChild(makeEl("div", { className: "per-point", text: item })));
  fb.appendChild(card);

  const followUp = displayText(r.follow_up);
  if (followUp) {
    const followBox = makeEl("div", { className: "followup-box" });
    followBox.append(makeEl("b", { text: "💬 追问：" }), document.createTextNode(followUp));
    const answer = makeEl("textarea", { className: "answer" });
    answer.id = "followup-answer";
    answer.placeholder = "回答追问…";
    const toolbar = makeEl("div", { className: "toolbar" });
    const followButton = makeEl("button", { className: "btn primary", text: "提交追问回答" });
    followButton.id = "followup-grade-btn";
    toolbar.appendChild(followButton);
    const result = makeEl("div");
    result.id = "followup-result";
    fb.append(followBox, answer, toolbar, result);
  }
  const nextToolbar = makeEl("div", { className: "toolbar" });
  const inlineNext = makeEl("button", { className: "btn primary", text: "下一题" });
  inlineNext.id = "quiz-next-inline";
  nextToolbar.appendChild(inlineNext);
  fb.appendChild(nextToolbar);
  const fBtn = $("followup-grade-btn");
  if (fBtn) fBtn.onclick = async () => {
    const fa = $("followup-answer").value.trim();
    if (!fa) return alert("先回答追问");
    fBtn.disabled = true; fBtn.textContent = "评分中…";
    try {
      const rr = await api("/api/quiz/followup", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card_id: c.id, question: followUp, answer: fa,
          context_answer: quizLastAnswer, style: $("quiz-style").value }),
      });
      const safeResult = asObject(rr);
      const result = $("followup-result");
      const resultCard = makeEl("div", { className: "card" });
      const followScore = makeEl("b", { className: "score-big", text: `${scoreText(safeResult.score)}/10` });
      followScore.style.fontSize = "24px";
      resultCard.append(followScore, makeEl("p", { text: safeResult.overall }),
        makeEl("p", { className: "muted", text: "追问参考答案：" }),
        makeEl("div", { className: "per-point", text: safeResult.reference_answer }));
      result.replaceChildren(resultCard);
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
  live.replaceChildren();
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
        const ev = parseSseEvent(chunk);
        if (!ev) continue;
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
          d.textContent = `⚠️ 参考解跑挂，喂回错误让 AI 修复：${displayText(ev.errors).slice(0, 120)}`;
          live.appendChild(d);
        } else if (k === "saved") {
          const d = makeEl("div", { className: "file-line", text: "🎉 已入库：" });
          d.append(makeEl("b", { text: ev.title }),
            document.createTextNode(`（${difficultyInfo(ev.difficulty).label}，${finiteText(ev.n_cases)} 用例）　`));
          const go = makeEl("button", { className: "btn", text: "去刷这道题 →" });
          go.style.cssText = "padding:3px 10px;font-size:12px";
          const problemId = displayText(ev.problem_id);
          go.addEventListener("click", () => goGenProblem(problemId));
          d.appendChild(go);
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
    live.appendChild(makeEl("div", { className: "fail-line", text: "❌ " + displayText(e.message, "未知错误") }));
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
      const ev = parseSseEvent(chunk);
      if (!ev) continue;
      const eventName = displayText(ev.event);
      if (Object.prototype.hasOwnProperty.call(handlers, eventName)) handlers[eventName](ev);
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
  live.replaceChildren();
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
      verify_case: ev => kbLine(live, `  ✅ 沙箱验证通过：${displayText(ev.detail).slice(0, 80)}`),
      verify_fix: ev => kbLine(live, `  ⚠️ 修复中：${displayText(ev.errors).slice(0, 80)}`, "fail-line"),
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
  live.replaceChildren();
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
  const sources = asArray(asObject(d).sources).map(asObject);
  tb.replaceChildren();
  if (!sources.length) {
    const row = document.createElement("tr");
    const cell = makeEl("td", { className: "muted", text: "还没有导入任何知识。在上方输入目录路径开始。" });
    cell.colSpan = 4;
    row.appendChild(cell);
    tb.appendChild(row);
    return;
  }
  sources.forEach(source => {
    const row = document.createElement("tr");
    const action = document.createElement("td");
    const del = makeEl("button", { className: "btn", text: "删除" });
    del.style.cssText = "padding:3px 10px;font-size:12px";
    const sourceId = displayText(source.id);
    del.addEventListener("click", () => delSource(sourceId));
    action.appendChild(del);
    row.append(makeEl("td", { text: source.title }),
      makeEl("td", { text: finiteText(source.n_cards) }),
      makeEl("td", {
        className: "muted",
        text: displayText(source.ingested_at).replace("T", " ").slice(0, 16),
      }), action);
    tb.appendChild(row);
  });
}

async function delSource(id) {
  if (!confirm("删除该来源及其全部题卡？（练习记录保留）")) return;
  await api(`/api/sources/${encodeURIComponent(displayText(id))}`, { method: "DELETE" });
  loadSources();
}

$("kb-browse-btn").onclick = async () => {
  const path = $("kb-path").value.trim() || "~";
  try {
    const d = await api(`/api/fs/browse?path=${encodeURIComponent(path)}`);
    const info = asObject(d);
    const area = $("kb-browse-area");
    const card = makeEl("div", { className: "card" });
    card.style.cssText = "margin-top:10px;background:var(--panel2)";
    const toolbar = document.createElement("div");
    toolbar.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap";
    const up = makeEl("button", { className: "btn", text: "↑ 上级" });
    up.style.cssText = "padding:4px 10px;font-size:12px";
    const parentPath = displayText(info.parent);
    up.disabled = !parentPath;
    up.addEventListener("click", () => kbGo(parentPath));
    const current = makeEl("span", { className: "muted", text: info.current });
    current.style.fontSize = "13px";
    const count = makeEl("span", {
      className: "badge tag",
      text: `${finiteText(info.importable_count, "0")} 个可导入文件`,
    });
    toolbar.append(up, current, count);
    const listing = document.createElement("div");
    listing.style.cssText = "margin-top:8px;max-height:180px;overflow:auto";
    asArray(info.dirs).slice(0, 60).map(asObject).forEach(entry => {
      const row = document.createElement("div");
      row.style.padding = "2px 0";
      const link = makeEl("button", { className: "path-link", text: "📁 " + displayText(entry.name) });
      link.type = "button";
      link.style.cssText = "font-size:13.5px;background:none;border:0;padding:0;color:var(--accent);cursor:pointer";
      const entryPath = displayText(entry.path);
      link.addEventListener("click", () => kbGo(entryPath));
      row.appendChild(link);
      listing.appendChild(row);
    });
    asArray(info.importable_files).slice(0, 40).map(asObject).forEach(entry => {
      const row = makeEl("div", { className: "muted", text: "📄 " + displayText(entry.name) });
      row.style.cssText = "font-size:12.5px;padding:2px 0";
      listing.appendChild(row);
    });
    card.append(toolbar, listing);
    area.replaceChildren(card);
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
  $("kb-live").replaceChildren();
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
        const ev = parseSseEvent(chunk);
        if (!ev) continue;
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
            thinkDiv.append(makeEl("summary", { text: "🧠 AI thinking…" }),
              makeEl("div", { className: "th-content" }));
            $("kb-live").appendChild(thinkDiv);
          }
          appendThinking(thinkDiv, ev.text);
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
  $("card-mykey").classList.toggle("hidden", !(currentUser && currentUser.multiuser));
  if (currentUser && currentUser.multiuser) loadMyKey();
  if (!currentUser || !currentUser.is_admin) {
    $("card-serverllm").classList.add("hidden");
    $("card-users").classList.add("hidden");
    return;
  }
  try {
    const d = asObject(await api("/api/llm/config"));
    $("set-baseurl").value = displayText(d.base_url);
    const sel = $("set-model-select");
    if (![...sel.options].some(o => o.value === d.model)) {
      const option = makeEl("option", { text: d.model });
      option.value = displayText(d.model);
      sel.replaceChildren(option);
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
    const d = asObject(await api("/api/me/llm"));
    const status = $("mykey-status");
    status.replaceChildren();
    if (d.using_own_key) {
      status.append(document.createTextNode(" 当前："));
      const own = makeEl("b", { text: "使用你自己的 Key" });
      own.style.color = "var(--green)";
      status.appendChild(own);
    } else if (d.server_configured) {
      status.textContent = " 当前：使用服务器共享 Key";
    } else {
      status.append(document.createTextNode(" 当前："));
      const missing = makeEl("b", { text: "服务器未配置 Key" });
      missing.style.color = "var(--amber)";
      status.append(missing, document.createTextNode("（可填自己的）"));
    }
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
    const d = asObject(await api("/api/admin/users"));
    const tb = $("users-table").querySelector("tbody");
    tb.replaceChildren();
    asArray(asObject(d).users).map(asObject).forEach(user => {
      const username = displayText(user.username);
      const row = document.createElement("tr");
      const action = document.createElement("td");
      const reset = makeEl("button", { className: "btn", text: "重置密码" });
      const del = makeEl("button", { className: "btn", text: "删除" });
      [reset, del].forEach(button => { button.style.cssText = "padding:3px 10px;font-size:12px"; });
      reset.addEventListener("click", () => resetUserPass(username));
      del.addEventListener("click", () => delUser(username));
      action.append(reset, document.createTextNode(" "), del);
      row.append(makeEl("td", { text: username }),
        makeEl("td", { text: user.is_admin ? "管理员" : "成员" }),
        makeEl("td", { className: "muted", text: finiteText(user.llm_today, "0") }),
        makeEl("td", { className: "muted", text: user.has_api_key ? "✔" : "—" }),
        action);
      tb.appendChild(row);
    });
  } catch {}
}
$("au-add-btn").onclick = async () => {
  const name = $("au-name").value.trim(), pass = $("au-pass").value;
  if (!name || pass.length < 8) return alert("用户名必填，密码至少 8 位");
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
  const pass = prompt(`为 ${name} 设置新密码（至少 8 位）：`);
  if (pass === null) return;
  if (pass.length < 8) return alert("密码至少 8 位");
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
    const d = asObject(await api("/api/llm/models"));
    const sel = $("set-model-select");
    const models = asArray(asObject(d).models);
    sel.replaceChildren(...models.map(model => {
      const text = displayText(model);
      const option = makeEl("option", { text });
      option.value = text;
      return option;
    }));
    sel.value = displayText(d.current);
    $("set-status").textContent = `● 扫到 ${models.length} 个模型`;
    refreshBadge();
  } catch (e) { alert("扫描失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "扫描可用模型"; }
};

$("set-save-btn").onclick = async () => {
  try {
    const d = asObject(await api("/api/llm/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: $("set-baseurl").value.trim(),
        model: $("set-model-select").value, api_key: $("set-apikey").value.trim() }),
    }));
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
  const s = asObject(await api("/api/stats"));
  const ac = problems.filter(p => p.ever_ac).length;
  const items = [
    ["八股题卡", s.cards], ["已学习", s.learned_cards ?? "—"], ["代码题", s.problems],
    ["代码已攻克", `${ac}/${problems.length}`], ["提交次数", s.submissions], ["AC 次数", s.ac],
    ["八股练习", s.quiz_attempts], ["八股均分", s.quiz_avg_score ?? "—"],
  ];
  const grid = $("stats-grid");
  grid.replaceChildren(...items.map(([k, v]) => {
    const card = makeEl("div", { className: "card" });
    card.append(makeEl("div", { className: "num", text: v }),
      makeEl("div", { className: "muted", text: k }));
    return card;
  }));
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

async function tryRegister() {
  const btn = $("register-btn");
  btn.disabled = true; btn.textContent = "注册中…";
  try {
    const body = { username: $("login-username").value.trim(),
                   password: $("login-password").value };
    if (!$("reg-code").classList.contains("hidden"))
      body.code = $("reg-code").value.trim();
    const r = asObject(await api("/api/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
    if (r.ok) location.reload();
  } catch (e) {
    $("login-error").textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = "注册并登录";
  }
}
$("register-btn").onclick = tryRegister;
["login-username", "login-password", "reg-code"].forEach(id => {
  $(id).addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      if (authMode === "register") tryRegister();
      else tryLogin();
    }
  });
});
$("logout-btn").onclick = async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  location.reload();
};


// ---------- 拖拽调整列宽 ----------
(function() {
  const grid = document.getElementById('problem-detail-view');
  if (!grid) return;
  let dragging = false, startX = 0, startCols = [], startIdx = 0;

  function getCols() {
    return [document.querySelector('.col-stmt'), document.querySelector('.col-editor'),
            document.querySelector('.col-coach')].map(c => c ? c.getBoundingClientRect() : null);
  }

  grid.addEventListener('mousemove', e => {
    if (dragging) {
      const dx = e.clientX - startX;
      const w = [...startCols];
      w[startIdx] = Math.max(100, startCols[startIdx] + dx);
      w[startIdx+1] = Math.max(100, startCols[startIdx+1] - dx);
      grid.style.gridTemplateColumns = w.map(x => x + 'px').join(' ');
    } else {
      const [c1, c2, c3] = getCols();
      if (!c1 || !c2 || !c3) return;
      const gap = 4;
      if (Math.abs(e.clientX - c1.right) < gap) { grid.style.cursor = 'col-resize'; grid._dragIdx = 0; }
      else if (Math.abs(e.clientX - c2.right) < gap) { grid.style.cursor = 'col-resize'; grid._dragIdx = 1; }
      else if (Math.abs(e.clientX - c3.right) < gap) { grid.style.cursor = 'col-resize'; grid._dragIdx = 1; }
      else { grid.style.cursor = ''; }
    }
  });

  grid.addEventListener('mousedown', e => {
    if (grid.style.cursor === 'col-resize') {
      startCols = getCols().map(c => c.width);
      startX = e.clientX;
      startIdx = grid._dragIdx;
      dragging = true;
      grid.style.userSelect = 'none';
      e.preventDefault();
    }
  });

  document.addEventListener('mouseup', () => {
    if (dragging) {
      grid.style.userSelect = '';
      dragging = false;
      const cols = grid.style.gridTemplateColumns;
      if (cols) localStorage.setItem('prepdojo-col-widths', cols);
    }
  });

  const saved = localStorage.getItem('prepdojo-col-widths');
  if (saved) grid.style.gridTemplateColumns = saved;
})();

// 固定修复按钮（教练栏标题旁）
$("coach-fix-btn").onclick = () => {
  const btn = $("coach-fix-btn");
  // 按钮是「应用修复」状态 → 直接应用，支持 Cmd+Z 撤回
  if (lastFixedCode && btn && btn.textContent.includes("应用")) {
    const lang = $("lang-select").value;
    if (cm) cm.replaceRange(lastFixedCode, {line:0,ch:0}, {line:cm.lastLine(),ch:cm.getLine(cm.lastLine()).length});
    else setEditorCode(lastFixedCode, lang);
    coachRender("tool", "✅ 代码已应用（Cmd+Z 可撤回），改完再提交试试", "tool");
    btn.textContent = "🔧 修复"; btn.style.opacity = "1"; _fixBtnStyle(btn, "");
    lastFixedCode = null;
  } else if (lastFailedResult) {
    doCoachFix(lastFailedResult);
  }
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

async function delProblem(pid) {
  if (!confirm("删除这道 AI 生成题？")) return;
  await api("/api/problems/" + encodeURIComponent(displayText(pid)), { method: "DELETE" });
  loadProblems();
}
